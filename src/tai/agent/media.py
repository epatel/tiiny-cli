"""Agent tools for non-text output: `image` and `video` (generate on the Tiiny), `show`
and `ask` (present things / ask questions in the browser viewer).

`image` and `video` load their model through ModelLoader, which asks (via the TUI
modal) before unloading anything, e.g. the agent's own model. The agent reloads its
model by itself before the next step. Results are saved inside the project and never
overwrite an existing file, so the tools don't need the write permission.
"""

from __future__ import annotations

import random
import re
import threading
from collections.abc import Callable
from contextlib import AbstractContextManager
from dataclasses import dataclass, field
from pathlib import Path
from typing import Protocol

from tai.agent.tools import Tool, ToolContext, ToolError
from tai.agent.viewer import AskCancelled, Viewer, kind_for
from tai.client import TiinyClient
from tai.loader import LoadCancelled, LoaderUI, ModelLoader
from tai.models import Model, resolve
from tai.options import ImageOptions
from tai.video import VideoJob, VideoOptions


class MediaUI(LoaderUI, Protocol):
    def activity(self, label: str) -> AbstractContextManager[Callable[[float, str], None]]:
        """Status-line progress for a long job: yields update(percent, message)."""
        ...


@dataclass
class MediaContext:
    client: TiinyClient
    viewer: Viewer
    agent_model: str | None = None
    ui: MediaUI | None = None  # installed by the TUI (modal + status line)
    cancel: threading.Event = field(default_factory=threading.Event)  # set by Esc (Agent.on_cancel)

    def load(self, task: str, query: str | None) -> Model:
        """Load the model for ``task`` (asking before unloading anything) and return it."""
        if self.ui is None:
            raise ToolError(f"Generating {task} needs the interactive UI")
        loader = ModelLoader(self.client, self.ui, protect=self.agent_model)
        model = resolve(loader.catalog(), query, task)
        try:
            loader.ensure_loaded(model)
        except LoadCancelled:
            raise ToolError(f"The user declined unloading models, so no {task} was generated") from None
        return model


def slug(text: str, limit: int = 40) -> str:
    return re.sub(r"[^a-z0-9]+", "-", text.lower()).strip("-")[:limit].rstrip("-") or "image"


def free_path(p: Path) -> Path:
    """``p``, or ``stem-2``, ``stem-3``… if it exists: never overwrite."""
    n = 2
    candidate = p
    while candidate.exists():
        candidate = p.with_name(f"{p.stem}-{n}{p.suffix}")
        n += 1
    return candidate


def _target(args: dict, ctx: ToolContext, folder: str, suffix: str) -> Path:
    """Where to save: ``path`` or ``folder/<prompt slug><suffix>``, inside the project, never
    an existing file."""
    path = ctx.resolve(args.get("path") or f"{folder}/{slug(args['prompt'])}{suffix}")
    if not ctx.inside(path):
        raise ToolError(f"Results are saved inside the project; give a relative path ({folder}/…)")
    return free_path(path.with_suffix(suffix))


@dataclass(frozen=True)
class Generated:
    """A generated file ready to save and show."""
    target: Path
    data: bytes
    title: str
    seed: int


def _seed(given) -> int:
    """The device doesn't report the seed it used, so always send one (random unless given)."""
    return random.randrange(2**31) if given is None else int(given)


def _deliver(m: MediaContext, ctx: ToolContext, out: Generated) -> str:
    out.target.parent.mkdir(parents=True, exist_ok=True)
    out.target.write_bytes(out.data)
    m.viewer.present(kind_for(out.target), title=out.title, path=out.target)
    return (f"Saved {ctx.show(out.target)} ({len(out.data) // 1024} KB, seed {out.seed}) "
            "and showed it in the viewer.")


def _output_params(noun: str) -> dict:
    """Parameters every generating tool takes besides its own."""
    return {"path": {"type": "string", "description": "Where to save, relative to the project"},
            "seed": {"type": "integer", "description": f"Seed; the same seed and settings give the same {noun} "
                                                       "(default: random, reported in the result)"},
            "model": {"type": "string", "description": f"{noun.capitalize()} model (default: the installed one)"}}


def make_image_tool(m: MediaContext) -> Tool:
    def run(args: dict, ctx: ToolContext) -> str:
        target = _target(args, ctx, "images", ".png")
        model = m.load("image", args.get("model"))
        m.ui.info(f"Generating image with {model.name}")
        opts = ImageOptions(size=args.get("size") or "1024x1024", seed=args.get("seed"),
                            steps=args.get("steps"), guidance=args.get("guidance_scale"),
                            negative=args.get("negative_prompt"))
        seed = _seed(opts.seed)
        data = m.client.image(model.id, args["prompt"], **opts.request(seed))
        return _deliver(m, ctx, Generated(target, data, args["prompt"], seed))

    return Tool("image", "Generate an image from a text prompt on the Tiiny's image model. Saves a PNG in the "
                         "project (default images/<prompt>.png) and shows it to the user in the viewer. "
                         "May need to swap models (the user is asked); takes 10-60 s.",
                {"prompt": {"type": "string", "description": "What to draw, in detail"},
                 "size": {"type": "string", "description": "WIDTHxHEIGHT, default 1024x1024"},
                 "steps": {"type": "integer", "description": "Denoising steps (default: the model's own)"},
                 "guidance_scale": {"type": "number", "description": "Guidance scale (default: the model's own)"},
                 "negative_prompt": {"type": "string", "description": "What the image should not contain"},
                 **_output_params("image")},
                run, required=["prompt"])


def _video_options(args: dict, ctx: ToolContext) -> VideoOptions:
    frame = ctx.resolve(args["image"]) if args.get("image") else None
    if frame and not frame.is_file():
        raise ToolError(f"Start image not found: {args['image']}")
    opts = VideoOptions(size=args.get("size") or "832x480", seconds=int(args.get("seconds") or 4),
                        steps=args.get("steps"), seed=args.get("seed"), image=frame)
    _check(opts, {})
    return opts


def _check(opts: VideoOptions, info: dict) -> None:
    problems = opts.problems(info)
    if problems:
        raise ToolError("; ".join(p.replace("--", "") for p in problems))


def _interruptible(cancel: threading.Event) -> Callable[[float], None]:
    """A sleep for VideoJob's polling that ends the wait when the user presses Esc."""
    def sleep(seconds: float) -> None:
        if cancel.wait(seconds):
            raise ToolError("Interrupted. The device keeps rendering; unloading the video model stops it")
    return sleep


def make_video_tool(m: MediaContext) -> Tool:
    def run(args: dict, ctx: ToolContext) -> str:
        opts = _video_options(args, ctx)
        target = _target(args, ctx, "videos", ".mp4")
        model = m.load("video", args.get("model"))
        _check(opts, m.client.video_info(model.id))
        seed = _seed(opts.seed)
        m.cancel.clear()
        job = VideoJob(m.client, lambda _: m.ui.activity(f"Rendering video ({opts.seconds}s, {opts.size})"),
                       sleep=_interruptible(m.cancel))
        data = job.render(model.id, args["prompt"], opts.request(seed))
        return _deliver(m, ctx, Generated(target, data, args["prompt"], seed))

    return Tool("video", "Generate a short video clip with sound on the Tiiny's video model, from a text prompt "
                         "and optionally a start image. Saves an MP4 in the project (default "
                         "videos/<prompt>.mp4) and shows it in the viewer. Slow: about 7 minutes for 2 s at "
                         "832x480 with 4 steps, and much longer for bigger sizes or more steps (the device gives "
                         "up after 1 hour), so keep clips short unless the user asks otherwise. Needs about half "
                         "the NPU, so it may swap models (the user is asked).",
                {"prompt": {"type": "string", "description": "What happens in the clip, in detail; sounds too"},
                 "seconds": {"type": "integer", "description": "Length, 1-7 (default 4)"},
                 "size": {"type": "string", "description": "One of 832x480 (default), 480x832, 1344x768, "
                                                           "768x1344"},
                 "steps": {"type": "integer", "description": "4-8 (default: the model's own, 7); 4 is fastest"},
                 "image": {"type": "string", "description": "Start frame to animate (an image file)"},
                 **_output_params("video")},
                run, required=["prompt"])


def make_show_tool(m: MediaContext) -> Tool:
    def run(args: dict, ctx: ToolContext) -> str:
        title = args.get("title") or ""
        if args.get("path"):
            p = ctx.resolve(args["path"])
            if not p.is_file():
                raise ToolError(f"File not found: {p}")
            m.viewer.present(kind_for(p), title=title or ctx.show(p), path=p)
        else:
            kind = next((k for k in ("html", "markdown", "text") if args.get(k)), None)
            if kind is None:
                raise ToolError("Give a path, or html / markdown / text content")
            m.viewer.present(kind, title=title, content=args[kind])
        return f"Showing it in the viewer ({m.viewer.url})."

    return Tool("show", "Show something to the user in their browser viewer: a file (image, audio, video, "
                        ".glb 3D model, .html, .md, or any file as a download) or inline html / markdown / text. "
                        "Use it for anything that isn't plain text. Opens the viewer the first time.",
                {"path": {"type": "string", "description": "File to show"},
                 "html": {"type": "string", "description": "HTML to render (sandboxed; scripts allowed)"},
                 "markdown": {"type": "string"}, "text": {"type": "string"},
                 "title": {"type": "string", "description": "Caption"}},
                run)


def make_ask_tool(m: MediaContext) -> Tool:
    def run(args: dict, ctx: ToolContext) -> str:
        try:
            answer = m.viewer.ask(args["question"], args.get("choices"))
        except AskCancelled as e:
            raise ToolError(f"No answer from the user ({e})") from None
        return f"The user answered: {answer}"

    return Tool("ask", "Ask the user a question in the browser viewer and wait for the answer. Give choices "
                       "for buttons, or none for a free-text answer. Use it when the user should pick between "
                       "options you showed (e.g. images).",
                {"question": {"type": "string"},
                 "choices": {"type": "array", "items": {"type": "string"}, "description": "Button labels"}},
                run, required=["question"])


def media_tools(m: MediaContext) -> dict[str, Tool]:
    tools = [make_image_tool(m), make_video_tool(m), make_show_tool(m), make_ask_tool(m)]
    return {t.name: t for t in tools}
