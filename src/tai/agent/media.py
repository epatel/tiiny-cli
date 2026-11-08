"""Agent tools for non-text output: `image` (generate on the Tiiny), `show` and `ask`
(present things / ask questions in the browser viewer).

`image` loads the image model through ModelLoader, which asks (via the TUI modal)
before unloading anything, e.g. the agent's own model. The agent reloads its model
by itself before the next step. Images are saved inside the project and never
overwrite an existing file, so the tool doesn't need the write permission.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path

from tai.agent.tools import Tool, ToolContext, ToolError
from tai.agent.viewer import AskCancelled, Viewer, kind_for
from tai.client import TiinyClient
from tai.loader import LoadCancelled, LoaderUI, ModelLoader
from tai.models import resolve


@dataclass
class MediaContext:
    client: TiinyClient
    viewer: Viewer
    agent_model: str | None = None
    ui: LoaderUI | None = None  # installed by the TUI (modal + status line)


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


def _image_target(args: dict, ctx: ToolContext) -> Path:
    path = ctx.resolve(args.get("path") or f"images/{slug(args['prompt'])}.png")
    if not ctx.inside(path):
        raise ToolError("Images are saved inside the project; give a relative path")
    return free_path(path.with_suffix(".png"))


def make_image_tool(m: MediaContext) -> Tool:
    def run(args: dict, ctx: ToolContext) -> str:
        if m.ui is None:
            raise ToolError("Image generation needs the interactive UI")
        target = _image_target(args, ctx)
        loader = ModelLoader(m.client, m.ui, protect=m.agent_model)
        model = resolve(loader.catalog(), args.get("model"), "image")
        try:
            loader.ensure_loaded(model)
        except LoadCancelled:
            raise ToolError("The user declined unloading models, so no image was generated") from None
        m.ui.info(f"Generating image with {model.name}")
        data = m.client.image(model.id, args["prompt"], size=args.get("size") or "1024x1024")
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(data)
        m.viewer.present("image", title=args["prompt"], path=target)
        return f"Saved {ctx.show(target)} ({len(data) // 1024} KB) and showed it in the viewer."

    return Tool("image", "Generate an image from a text prompt on the Tiiny's image model. Saves a PNG in the "
                         "project (default images/<prompt>.png) and shows it to the user in the viewer. "
                         "May need to swap models (the user is asked); takes 10-60 s.",
                {"prompt": {"type": "string", "description": "What to draw, in detail"},
                 "path": {"type": "string", "description": "Where to save, relative to the project"},
                 "size": {"type": "string", "description": "WIDTHxHEIGHT, default 1024x1024"},
                 "model": {"type": "string", "description": "Image model (default: the installed one)"}},
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
    tools = [make_image_tool(m), make_show_tool(m), make_ask_tool(m)]
    return {t.name: t for t in tools}
