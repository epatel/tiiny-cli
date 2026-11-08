"""tai command-line interface."""

from __future__ import annotations

import json
import mimetypes
import os
import platform
import random
import subprocess
import sys
import time
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path

import click
from rich.console import Console
from rich.progress import BarColumn, Progress, TextColumn, TimeElapsedColumn
from rich.table import Table

from tai import __version__
from tai.chat import ChatSession, SessionState, user_message
from tai.client import TiinyClient, TiinyError
from tai.config import ConfigError, data_dir, load_settings
from tai.db import Store
from tai.loader import LoadCancelled, ModelLoader
from tai.mesh import Mesh3D, MeshOptions
from tai.models import Model, ResolveError, build_catalog, resolve
from tai.options import AgentRequest, AskOutput, ChatOptions, ImageOptions, Question, SessionRef, bundle, chat_options

out = Console()
err = Console(stderr=True)

SETTING_KEYS = {
    "default.chat", "default.agent", "default.image", "default.tts", "default.asr", "default.embed", "default.3d",
    "think",  # on|off — default thinking mode for models that can toggle it
    "tts.instruct",  # default voice-design instruct, e.g. "female, british accent"
}


class App:
    def __init__(self, device: str | None, yes: bool):
        self._device = device
        self.yes = yes
        self._client: TiinyClient | None = None
        self._store: Store | None = None

    @property
    def client(self) -> TiinyClient:
        if self._client is None:
            self._client = TiinyClient(load_settings(device=self._device))
        return self._client

    @property
    def store(self) -> Store:
        if self._store is None:
            self._store = Store(data_dir() / "tai.db")
        return self._store

    def catalog(self) -> list[Model]:
        return build_catalog(self.client.models(), self.client.running())

    def agent_model(self, query: str | None) -> Model:
        """A chat model that supports tool use (for `tai agent`)."""
        tool_models = [m for m in self.catalog() if m.task == "chat" and m.tools]
        return resolve(tool_models, query, "chat", default=self.store.get_setting("default.agent"))

    def model_for(self, task: str, query: str | None) -> Model:
        models = self.catalog()
        return resolve(models, query, task, default=self.store.get_setting(f"default.{task}"))

    # -- loading ---------------------------------------------------------
    def ensure_loaded(self, target: Model) -> None:
        """Load ``target`` if needed, asking before unloading anything (see cards/npu-swapping.md)."""
        try:
            ModelLoader(self.client, CliLoaderUI(self.yes)).ensure_loaded(target)
        except LoadCancelled:
            raise click.Abort() from None


class CliLoaderUI:
    """Terminal prompts and a transient progress bar on stderr."""

    def __init__(self, yes: bool):
        self.yes = yes

    def confirm(self, question: str) -> bool:
        err.print(f"[yellow]{question}[/]", highlight=False)
        return self.yes or click.confirm("Proceed?", default=True, err=True)

    def info(self, message: str) -> None:
        err.print(f"[green]{message}[/]" if message.startswith("Loaded") else message, highlight=False)

    @contextmanager
    def progress(self, name: str):
        with Progress(TextColumn("[bold]Loading {task.fields[name]}"), BarColumn(),
                      TextColumn("{task.percentage:>3.0f}%"), TextColumn("[dim]{task.fields[msg]}"),
                      TimeElapsedColumn(), console=err, transient=True) as prog:
            tid = prog.add_task("load", total=100, name=name, msg="")
            yield lambda pct, msg: prog.update(tid, completed=pct, msg=msg[:50])


pass_app = click.make_pass_decorator(App)


class TaiGroup(click.Group):
    def invoke(self, ctx):
        try:
            return super().invoke(ctx)
        except (ConfigError, TiinyError, ResolveError) as e:
            err.print(f"[red]error:[/] {e}")
            sys.exit(1)
        except KeyboardInterrupt:
            err.print("[dim]interrupted[/]")
            sys.exit(130)


@click.group(cls=TaiGroup)
@click.option("--device", envvar="TAI_DEVICE", help="Device address (default: from tiiny config).")
@click.option("-y", "--yes", is_flag=True, envvar="TAI_YES", help="Don't ask before unloading models.")
@click.version_option(__version__, prog_name="tai")
@click.pass_context
def cli(ctx, device, yes):
    """Use every model on your Tiiny device. Models are loaded on demand;
    when the NPU is full you'll be asked which models to unload."""
    ctx.obj = App(device, yes)


def main():
    cli()


# ---------------------------------------------------------------- models
@cli.command("models")
@click.option("--json", "as_json", is_flag=True)
@pass_app
def models_cmd(app: App, as_json):
    """List installed models, what they do, and whether they're loaded."""
    models = app.catalog()
    defaults = app.store.settings()
    if as_json:
        out.print_json(json.dumps([m.__dict__ | {"task": m.task} for m in models]))
        return
    t = Table(box=None, header_style="bold")
    for col in ("", "Name", "Task", "Type", "NPU"):
        t.add_column(col, no_wrap=True)
    t.add_column("Model ID", overflow="fold")
    for m in sorted(models, key=lambda m: (m.task, m.name)):
        mark = "[green]●[/]" if m.loaded else ("[yellow]◐[/]" if m.status not in ("downloaded", "") else "[dim]○[/]")
        star = " *" if defaults.get(f"default.{m.task}") == m.id else ""
        t.add_row(mark, m.name + star, m.task, m.type, f"{m.npu}%", f"[dim]{m.id}[/]")
    out.print(t)
    used = sum(m.npu for m in models if m.loaded)
    out.print(f"[dim]● loaded  ○ not loaded  * your default — NPU in use: {used}%[/]")


@cli.command()
@pass_app
def status(app: App):
    """Show NPU usage and loaded models."""
    npu = app.client.npu()
    out.print(f"NPU: {npu.get('npu_used')}/{npu.get('npu_total')} used, {npu.get('npu_available')} free")
    for m in npu.get("models", []):
        out.print(f"  [green]●[/] {m['model_id']}  {m['npu_usage']}%  [dim]{m['status']}[/]")


@cli.command()
@click.argument("model")
@pass_app
def load(app: App, model):
    """Load MODEL (unloading others if the NPU is full)."""
    m = resolve(app.catalog(), model)
    if m.loaded:
        out.print(f"{m.name} is already loaded.")
    app.ensure_loaded(m)


@cli.command()
@click.argument("model", required=False)
@click.option("--all", "all_", is_flag=True, help="Unload every loaded model.")
@pass_app
def unload(app: App, model, all_):
    """Unload MODEL (or --all) from the NPU."""
    models = app.catalog()
    if all_:
        targets = [m for m in models if m.loaded]
    elif model:
        targets = [resolve(models, model)]
    else:
        raise click.UsageError("Give a MODEL or --all.")
    for m in targets:
        app.client.stop(m.id)
        out.print(f"Unloaded {m.name}")


@cli.group("config", invoke_without_command=True)
@click.pass_context
def config_cmd(ctx):
    """Show or change tai preferences (default models, thinking)."""
    if ctx.invoked_subcommand is None:
        app: App = ctx.obj
        for k, v in sorted(app.store.settings().items()):
            out.print(f"{k} = {v}")
        out.print(f"[dim]keys: {', '.join(sorted(SETTING_KEYS))}[/]")


@config_cmd.command("set")
@click.argument("key", type=click.Choice(sorted(SETTING_KEYS)))
@click.argument("value")
@pass_app
def config_set(app: App, key, value):
    """Set KEY to VALUE. Model defaults accept any unique model name."""
    if key == "default.agent":
        value = app.agent_model(value).id
    elif key.startswith("default."):
        value = resolve(app.catalog(), value, task=key.split(".", 1)[1]).id
    elif key == "think" and value not in ("on", "off"):
        raise click.BadParameter("think must be on or off")
    app.store.set_setting(key, value)
    out.print(f"{key} = {value}")


@config_cmd.command("unset")
@click.argument("key", type=click.Choice(sorted(SETTING_KEYS)))
@pass_app
def config_unset(app: App, key):
    app.store.set_setting(key, None)


# ---------------------------------------------------------------- chat
@dataclass(frozen=True)
class ChatRequest:
    model: Model
    messages: list[dict]
    extra: dict  # extra request fields, e.g. chat_template_kwargs


def _chat_request(app: App, model: Model, messages: list[dict], think: bool | None) -> ChatRequest:
    return ChatRequest(model, messages, _think_extra(app, model, think))


def _think_extra(app: App, model: Model, think: bool | None) -> dict:
    if think is None:
        s = app.store.get_setting("think")
        think = None if s is None else s == "on"
    if think is None or not model.thinking.get("toggleable"):
        return {}
    return {"chat_template_kwargs": {"enable_thinking": think}}


def _stream_reply(app: App, req: ChatRequest, show_thinking: bool) -> str:
    text, in_think = [], False
    for chunk in app.client.chat_stream(req.model.id, req.messages, **req.extra):
        if not chunk.get("choices"):
            continue
        delta = chunk["choices"][0].get("delta") or {}
        r = delta.get("reasoning_content")
        if r and show_thinking:
            err.print(r, style="dim italic", end="", soft_wrap=True, highlight=False)
            in_think = True
        c = delta.get("content")
        if c:
            if in_think:
                err.print()
                in_think = False
            text.append(c)
            sys.stdout.write(c)
            sys.stdout.flush()
    sys.stdout.write("\n")
    return "".join(text)


def _system(opts: ChatOptions) -> list[dict]:
    return [{"role": "system", "content": opts.system}] if opts.system else []


@cli.command()
@click.argument("prompt", nargs=-1)
@click.option("-f", "--file", "files", multiple=True, type=click.Path(exists=True, path_type=Path),
              help="Attach an image (vision models) or a text file (inlined). Repeatable.")
@chat_options
@click.option("--show-thinking", is_flag=True, help="Print reasoning to stderr.")
@click.option("--json", "as_json", is_flag=True, help="Print the raw API response.")
@bundle("question", Question, "prompt", "files")
@bundle("output", AskOutput, "show_thinking", "as_json")
@pass_app
def ask(app: App, question: Question, opts: ChatOptions, output: AskOutput):
    """One-shot question. Reads stdin too: `git diff | tai ask "review this"`."""
    m = app.model_for("chat", opts.model)
    images, text = _split_files(m, question.files, _read_prompt(question.prompt))
    app.ensure_loaded(m)
    req = _chat_request(app, m, _system(opts) + [user_message(text, images)], opts.think)
    if output.as_json:
        out.print_json(json.dumps(app.client.chat(m.id, req.messages, **req.extra)))
    else:
        _stream_reply(app, req, output.show_thinking)


def _read_prompt(words: tuple[str, ...]) -> str:
    """Prompt words, followed by piped stdin when there is any."""
    text = " ".join(words)
    piped = "" if sys.stdin.isatty() else sys.stdin.read()
    if piped.strip():
        text = f"{text}\n\n{piped}" if text else piped
    if not text.strip():
        raise click.UsageError("Nothing to ask.")
    return text


def _split_files(m: Model, files, text: str) -> tuple[list[Path], str]:
    images = []
    for f in files:
        if (mimetypes.guess_type(f.name)[0] or "").startswith("image/"):
            if not m.vision:
                raise click.UsageError(f"{m.name} can't see images; use a vision model (-m).")
            images.append(f)
        else:
            text += f"\n\n--- {f.name} ---\n{f.read_text(errors='replace')}"
    return images, text


@cli.command()
@chat_options
@click.option("-r", "--resume", help="Resume a session by id or name.")
@click.option("-c", "--continue", "cont", is_flag=True, help="Resume the most recent session.")
@click.option("-n", "--name", help="Name for a new session.")
@bundle("session", SessionRef, "resume", "cont", "name")
@pass_app
def chat(app: App, opts: ChatOptions, session: SessionRef):
    """Interactive multi-turn chat. Sessions are saved; resume with -c or -r."""
    try:
        import readline  # noqa: F401  (line editing + history for input())
    except ImportError:
        pass
    state = _resume_session(app, session.resume, opts) if session.resuming else _new_session(app, opts, session.name)
    state.think = opts.think
    app.ensure_loaded(state.model)
    err.print(f"[bold]{state.model.name}[/] [dim]session {state.sid} — /help for commands, Ctrl-D to exit[/]")

    def reply(m: Model, convo: list[dict], think: bool | None) -> str:
        return _stream_reply(app, _chat_request(app, m, convo, think), show_thinking=True)

    ChatSession(app, state, err, reply).run()


def _new_session(app: App, opts: ChatOptions, name: str | None) -> SessionState:
    m = app.model_for("chat", opts.model)
    return SessionState(app.store.create_session(m.id, name=name, system=opts.system), m, opts.system)


def _resume_session(app: App, ref: str | None, opts: ChatOptions) -> SessionState:
    """Resume session ``ref`` (id or name), or the most recent one when ref is None.
    -m and -s, when given, override what the session had."""
    store = app.store
    row = store.find_session(ref) if ref else store.last_session()
    if not row:
        raise click.UsageError("No such session.")
    if row["kind"] != "chat":
        raise click.UsageError(f"Session {row['id']} is an {row['kind']} session; use `tai {row['kind']} -r`.")
    messages = [{"role": r["role"], "content": r["content"]} for r in store.messages(row["id"])]
    label = f" {row['name']}" if row["name"] else ""
    err.print(f"[dim]Resumed session {row['id']}{label} ({len(messages)} messages)[/]")
    return SessionState(row["id"], app.model_for("chat", opts.model or row["model"]),
                        opts.system or row["system"], messages=messages)


@cli.command()
@click.argument("task", nargs=-1)
@click.option("-m", "--model", help="Tool-capable chat model (default: default.agent, else a loaded one).")
@click.option("-r", "--resume", help="Resume an agent session by id.")
@click.option("-c", "--continue", "cont", is_flag=True, help="Continue the latest agent session in this folder.")
@click.option("--no-mcp", is_flag=True, help="Don't connect to MCP servers.")
@bundle("req", AgentRequest, "task", "model", "resume", "cont", "no_mcp")
@pass_app
def agent(app: App, req: AgentRequest):
    """Coding agent in a full-screen UI: reads, edits and runs commands in this folder.

    Asks before every edit (shows the diff), shell command and non-read-only MCP tool.
    Uses MCP servers from .mcp.json / ~/.config/tai/mcp.json and skills from .claude/skills,
    ~/.claude/skills and ~/.config/tai/skills. TASK, if given, is sent first."""
    from tai.agent.command import run_agent  # textual loads only for this command

    run_agent(app, req)


@cli.command("mcp")
def mcp_cmd():
    """Connect to the MCP servers `tai agent` would use and list their tools."""
    from tai.agent import mcp_client

    root = Path.cwd().resolve()
    servers, problems = mcp_client.load_config(root)
    for p in problems:
        err.print(f"[yellow]config problem:[/] {p}")
    if not servers:
        out.print("No MCP servers configured. Add them to .mcp.json (project) or ~/.config/tai/mcp.json.")
        return
    hub = mcp_client.McpHub(servers, root, data_dir() / "logs")
    with err.status("Connecting…"):
        hub.start()
    try:
        for state in hub.states.values():
            _print_server(state)
    finally:
        hub.close()


def _print_server(s) -> None:
    where = s.config.url or " ".join([s.config.command or "", *s.config.args])
    if s.error:
        out.print(f"[red]✗[/] [bold]{s.config.name}[/] [dim]{where}[/]\n    {s.error}")
        return
    out.print(f"[green]●[/] [bold]{s.config.name}[/] [dim]{where}[/]")
    for t in s.tools:
        ro = " [dim](read-only)[/]" if t.read_only else ""
        first = t.description.splitlines()[0][:70] if t.description else ""
        out.print(f"    {t.name}{ro}  [dim]{first}[/]")


@cli.command("skills")
def skills_cmd():
    """List the skills `tai agent` can use."""
    from tai.agent import skills

    found = skills.discover(Path.cwd().resolve())
    if not found:
        dirs = ", ".join(str(d) for d in skills.skill_dirs(Path.cwd()))
        out.print(f"No skills found. Looked in: {dirs}")
        return
    for s in found:
        out.print(f"[bold]{s.name}[/]  [dim]{s.folder}[/]\n    {s.description[:160]}")


@cli.group(invoke_without_command=True)
@click.pass_context
def history(ctx):
    """List saved chat sessions."""
    if ctx.invoked_subcommand:
        return
    app: App = ctx.obj
    t = Table(box=None, header_style="bold")
    for col in ("ID", "Kind", "Name", "Model", "Msgs", "Updated", "First message"):
        t.add_column(col)
    for s in app.store.sessions():
        first = (s["first"] or "").replace("\n", " ")
        t.add_row(str(s["id"]), s["kind"], s["name"] or "", s["model"].split("/")[-1], str(s["n"]),
                  time.strftime("%Y-%m-%d %H:%M", time.localtime(s["updated_at"])),
                  first[:50] + ("…" if len(first) > 50 else ""))
    out.print(t)


@history.command("show")
@click.argument("session")
@pass_app
def history_show(app: App, session):
    row = app.store.find_session(session)
    if not row:
        raise click.UsageError("No such session.")
    for msg in app.store.messages(row["id"]):
        style = "bold cyan" if msg["role"] == "user" else ""
        out.print(f"[{style or 'bold'}]{msg['role']}:[/] ", end="")
        out.print(msg["content"], style=style or None, highlight=False, markup=False)


@history.command("rm")
@click.argument("session")
@pass_app
def history_rm(app: App, session):
    row = app.store.find_session(session)
    if not row:
        raise click.UsageError("No such session.")
    app.store.delete_session(row["id"])
    out.print(f"Deleted session {row['id']}")


# ---------------------------------------------------------------- media
def _play(path: Path) -> None:
    player = {"Darwin": ["afplay"], "Linux": ["aplay", "-q"]}.get(platform.system())
    if player:
        subprocess.run([*player, str(path)], check=False)


def _open(path: Path) -> None:
    opener = {"Darwin": "open", "Linux": "xdg-open"}.get(platform.system())
    if opener:
        subprocess.run([opener, str(path)], check=False)


@cli.command()
@click.argument("prompt", nargs=-1, required=True)
@click.option("-m", "--model")
@click.option("-o", "--output", type=click.Path(path_type=Path), default=Path("image.png"), show_default=True)
@click.option("--size", default="1024x1024", show_default=True, help="WIDTHxHEIGHT")
@click.option("--seed", type=click.IntRange(0), help="Reuse a seed to reproduce an image (default: random).")
@click.option("--steps", type=click.IntRange(1), help="Denoising steps (default: the model's own).")
@click.option("--guidance", type=float, help="Guidance scale (default: the model's own).")
@click.option("--negative", help="What the image should not contain.")
@click.option("--open", "open_", is_flag=True, help="Open the image when done.")
@bundle("opts", ImageOptions, "size", "seed", "steps", "guidance", "negative")
@pass_app
def image(app: App, prompt, model, output, opts: ImageOptions, open_):
    """Generate an image from PROMPT."""
    m = app.model_for("image", model)
    app.ensure_loaded(m)
    seed = random.randrange(2**31) if opts.seed is None else opts.seed
    with err.status(f"Generating with {m.name}…"):
        data = app.client.image(m.id, " ".join(prompt), **opts.request(seed))
    output.write_bytes(data)
    click.echo(f"Saved {output} (seed {seed})")
    if open_:
        _open(output)


@cli.command()
@click.argument("text", nargs=-1)
@click.option("-m", "--model")
@click.option("-o", "--output", type=click.Path(path_type=Path), default=Path("speech.wav"), show_default=True)
@click.option("-i", "--instruct", help='Voice design, e.g. "female, british accent, low pitch".')
@click.option("--play", is_flag=True, help="Play the audio when done.")
@pass_app
def tts(app: App, text, model, output, instruct, play):
    """Speak TEXT (or stdin) into a WAV file."""
    s = " ".join(text)
    if not s and not sys.stdin.isatty():
        s = sys.stdin.read().strip()
    if not s:
        raise click.UsageError("Nothing to say.")
    m = app.model_for("tts", model)
    app.ensure_loaded(m)
    instruct = instruct or app.store.get_setting("tts.instruct")
    with err.status(f"Synthesizing with {m.name}…"):
        audio, meta = app.client.tts(m.id, s, {"instruct": instruct} if instruct else None)
    output.write_bytes(audio)
    click.echo(f"Saved {output} ({meta.get('duration', '?')}s)")
    if play:
        _play(output)


@cli.command()
@click.argument("audio", type=click.Path(exists=True, path_type=Path))
@click.option("-m", "--model")
@click.option("-l", "--language", help="Language hint, e.g. en, sv.")
@pass_app
def asr(app: App, audio, model, language):
    """Transcribe an AUDIO file to text."""
    m = app.model_for("asr", model)
    app.ensure_loaded(m)
    with err.status(f"Transcribing with {m.name}…"):
        d = app.client.asr(m.id, audio, language)
    click.echo(d.get("text", ""))


@cli.command()
@click.argument("texts", nargs=-1)
@click.option("-m", "--model")
@pass_app
def embed(app: App, texts, model):
    """Embed TEXTS (or stdin lines); prints one JSON object per input."""
    items = list(texts)
    if not items and not sys.stdin.isatty():
        items = [ln for ln in sys.stdin.read().splitlines() if ln.strip()]
    if not items:
        raise click.UsageError("Nothing to embed.")
    m = app.model_for("embed", model)
    app.ensure_loaded(m)
    d = app.client.embed(m.id, items)
    for text, row in zip(items, sorted(d["data"], key=lambda r: r["index"])):
        click.echo(json.dumps({"input": text, "embedding": row["embedding"]}))


@cli.command("3d")
@click.argument("image_path", metavar="IMAGE", type=click.Path(exists=True, path_type=Path))
@click.option("-m", "--model")
@click.option("-o", "--output", type=click.Path(path_type=Path), default=None,
              help="Output .glb (default: IMAGE name with .glb).")
@click.option("--faces", type=int, default=None,
              help="Simplify the mesh to about this many faces (default output can be >100 MB).")
@click.option("--quality", type=click.Choice(["fast", "balance", "quality"]), default=None)
@click.option("--seed", type=int, default=None)
@pass_app
def three_d(app: App, image_path, model, output, faces, quality, seed):
    """Turn an IMAGE into a textured 3D model (.glb). Takes several minutes."""
    m = app.model_for("3d", model)
    app.ensure_loaded(m)
    output = output or image_path.with_suffix(".glb")
    pipeline = Mesh3D(app.client, _stage_progress)
    output.write_bytes(pipeline.generate(image_path, output.name, MeshOptions(faces, quality, seed)))
    click.echo(f"Saved {output} ({output.stat().st_size / 1e6:.1f} MB)")


@contextmanager
def _stage_progress(stage: str):
    with Progress(TextColumn(f"[bold]{stage:<9}"), BarColumn(), TextColumn("{task.percentage:>3.0f}%"),
                  TextColumn("[dim]{task.fields[msg]}"), TimeElapsedColumn(), console=err) as prog:
        tid = prog.add_task(stage, total=100, msg="")
        yield lambda pct, msg: prog.update(tid, completed=pct, msg=msg[:45])
