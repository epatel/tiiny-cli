"""tai command-line interface."""

from __future__ import annotations

import json
import mimetypes
import os
import platform
import subprocess
import sys
import time
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
from tai.models import Model, ResolveError, build_catalog, plan_swap, resolve

out = Console()
err = Console(stderr=True)

SETTING_KEYS = {
    "default.chat", "default.image", "default.tts", "default.asr", "default.embed", "default.3d",
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

    def model_for(self, task: str, query: str | None) -> Model:
        models = self.catalog()
        return resolve(models, query, task, default=self.store.get_setting(f"default.{task}"))

    # -- loading ---------------------------------------------------------
    def ensure_loaded(self, target: Model) -> None:
        """Load ``target`` if needed, asking before unloading anything (see cards/npu-swapping.md)."""
        if target.loaded:
            return
        models = self.catalog()
        target = next(m for m in models if m.id == target.id)  # fresh status
        if target.loaded:
            return
        self._make_room(models, target)
        try:
            self.load(target)
        except LoadFailed as e:
            if not e.memory:
                raise
            self._retry_after_fragmentation(target, e)

    def _make_room(self, models: list[Model], target: Model) -> None:
        plan = plan_swap(models, target, total=int(self.client.npu().get("npu_total") or 100))
        if not plan.unload:
            return
        names = ", ".join(f"{m.name} ({m.npu}%)" for m in plan.unload)
        err.print(f"[yellow]{target.name} needs {target.npu}% NPU, {plan.free_before}% free.[/] "
                  f"Unload: {names}")
        self._confirm("Proceed?")
        self._unload(plan.unload)

    def _retry_after_fragmentation(self, target: Model, failure: LoadFailed) -> None:
        """NPU memory fragments: a big model may not fit after small ones even when
        the percentages add up. The device's own advice is to unload all and retry."""
        others = [m for m in self.catalog() if m.loaded and m.id != target.id]
        if not others:
            raise failure
        err.print(f"[yellow]{failure}[/]\nNPU memory is fragmented. Unload all other models "
                  f"({', '.join(m.name for m in others)}) and retry?")
        self._confirm("Retry?")
        self._unload(others)
        self.load(target)

    def _confirm(self, question: str) -> None:
        if not self.yes and not click.confirm(question, default=True, err=True):
            raise click.Abort()

    def _unload(self, models: list[Model]) -> None:
        for m in models:
            err.print(f"Unloading {m.name}…")
            self.client.stop(m.id)

    def load(self, target: Model) -> None:
        with Progress(
            TextColumn("[bold]Loading {task.fields[name]}"), BarColumn(),
            TextColumn("{task.percentage:>3.0f}%"), TextColumn("[dim]{task.fields[msg]}"),
            TimeElapsedColumn(), console=err, transient=True,
        ) as prog:
            tid = prog.add_task("load", total=100, name=target.name, msg="")
            last: dict = {}
            for ev in self.client.launch(target.id):
                last = ev
                prog.update(tid, completed=float(ev.get("progress") or 0),
                            msg=(ev.get("startup_message") or ev.get("message") or "")[:50])
                if ev.get("error"):
                    raise LoadFailed(target, ev)
        if last.get("_event") == "done" and last.get("success") is False:
            raise LoadFailed(target, last)
        err.print(f"[green]Loaded {target.name}[/]")


class LoadFailed(TiinyError):
    """A model failed to start. ``memory`` is True for NPU allocation failures."""

    def __init__(self, target: Model, ev: dict):
        detail = ev.get("error") or ev.get("startup_error") or ev.get("message") or ""
        self.memory = "allocate memory" in detail or "ALLOC" in detail
        toast = (ev.get("toast") or {}).get("message")
        short = detail.split(";")[0].strip() or "unknown error"
        super().__init__(f"Loading {target.name} failed: {short}"
                         + (" (NPU out of memory)" if self.memory else "")
                         + (f" — device says: {toast}" if toast else ""))


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
    if key.startswith("default."):
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
def _think_extra(app: App, model: Model, think: bool | None) -> dict:
    if think is None:
        s = app.store.get_setting("think")
        think = None if s is None else s == "on"
    if think is None or not model.thinking.get("toggleable"):
        return {}
    return {"chat_template_kwargs": {"enable_thinking": think}}


def _stream_reply(app: App, model: Model, messages: list[dict], extra: dict,
                  show_thinking: bool) -> str:
    text, in_think = [], False
    for chunk in app.client.chat_stream(model.id, messages, **extra):
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


@cli.command()
@click.argument("prompt", nargs=-1)
@click.option("-m", "--model", help="Chat model (default: a loaded one, or your default).")
@click.option("-s", "--system", help="System prompt.")
@click.option("-f", "--file", "files", multiple=True, type=click.Path(exists=True, path_type=Path),
              help="Attach an image (vision models) or a text file (inlined). Repeatable.")
@click.option("--think/--no-think", default=None, help="Toggle reasoning on models that support it.")
@click.option("--show-thinking", is_flag=True, help="Print reasoning to stderr.")
@click.option("--json", "as_json", is_flag=True, help="Print the raw API response.")
@pass_app
def ask(app: App, prompt, model, system, files, think, show_thinking, as_json):
    """One-shot question. Reads stdin too: `git diff | tai ask "review this"`."""
    text = " ".join(prompt)
    if not sys.stdin.isatty():
        piped = sys.stdin.read()
        text = f"{text}\n\n{piped}" if text else piped
    if not text.strip():
        raise click.UsageError("Nothing to ask.")
    m = app.model_for("chat", model)
    images, text = _split_files(m, files, text)
    app.ensure_loaded(m)
    messages = ([{"role": "system", "content": system}] if system else []) + [user_message(text, images)]
    extra = _think_extra(app, m, think)
    if as_json:
        out.print_json(json.dumps(app.client.chat(m.id, messages, **extra)))
    else:
        _stream_reply(app, m, messages, extra, show_thinking)


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
@click.option("-m", "--model", help="Chat model.")
@click.option("-s", "--system", help="System prompt.")
@click.option("-r", "--resume", "resume", help="Resume a session by id or name.")
@click.option("-c", "--continue", "cont", is_flag=True, help="Resume the most recent session.")
@click.option("-n", "--name", help="Name for a new session.")
@click.option("--think/--no-think", default=None)
@pass_app
def chat(app: App, model, system, resume, cont, name, think):
    """Interactive multi-turn chat. Sessions are saved; resume with -c or -r."""
    try:
        import readline  # noqa: F401  (line editing + history for input())
    except ImportError:
        pass
    if resume or cont:
        state = _resume_session(app, resume, model, system)
    else:
        state = _new_session(app, model, system, name)
    state.think = think
    app.ensure_loaded(state.model)
    err.print(f"[bold]{state.model.name}[/] [dim]session {state.sid} — /help for commands, Ctrl-D to exit[/]")

    def reply(m: Model, convo: list[dict], think: bool | None) -> str:
        return _stream_reply(app, m, convo, _think_extra(app, m, think), show_thinking=True)

    ChatSession(app, state, err, reply).run()


def _new_session(app: App, model: str | None, system: str | None, name: str | None) -> SessionState:
    m = app.model_for("chat", model)
    return SessionState(app.store.create_session(m.id, name=name, system=system), m, system)


def _resume_session(app: App, ref: str | None, model: str | None, system: str | None) -> SessionState:
    """Resume session ``ref`` (id or name), or the most recent one when ref is None."""
    store = app.store
    row = store.find_session(ref) if ref else store.last_session()
    if not row:
        raise click.UsageError("No such session.")
    messages = [{"role": r["role"], "content": r["content"]} for r in store.messages(row["id"])]
    label = f" {row['name']}" if row["name"] else ""
    err.print(f"[dim]Resumed session {row['id']}{label} ({len(messages)} messages)[/]")
    return SessionState(row["id"], app.model_for("chat", model or row["model"]),
                        system or row["system"], messages=messages)


@cli.group(invoke_without_command=True)
@click.pass_context
def history(ctx):
    """List saved chat sessions."""
    if ctx.invoked_subcommand:
        return
    app: App = ctx.obj
    t = Table(box=None, header_style="bold")
    for col in ("ID", "Name", "Model", "Msgs", "Updated", "First message"):
        t.add_column(col)
    for s in app.store.sessions():
        first = (s["first"] or "").replace("\n", " ")
        t.add_row(str(s["id"]), s["name"] or "", s["model"].split("/")[-1], str(s["n"]),
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
@click.option("--open", "open_", is_flag=True, help="Open the image when done.")
@pass_app
def image(app: App, prompt, model, output, size, open_):
    """Generate an image from PROMPT."""
    m = app.model_for("image", model)
    app.ensure_loaded(m)
    with err.status(f"Generating with {m.name}…"):
        data = app.client.image(m.id, " ".join(prompt), size=size)
    output.write_bytes(data)
    click.echo(f"Saved {output}")
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
    c = app.client
    controls = {k: v for k, v in {"inference_quality": quality, "seed": seed}.items() if v is not None}

    def ref(task_id: str, artifact: dict) -> dict:
        return {"task_id": task_id, "artifact_id": artifact["artifact_id"]}

    def run(stage: str, **kw) -> tuple[str, list[dict]]:
        task_id = c.task3d_submit(stage, **kw)
        with Progress(TextColumn(f"[bold]{stage:<9}"), BarColumn(), TextColumn("{task.percentage:>3.0f}%"),
                      TextColumn("[dim]{task.fields[msg]}"), TimeElapsedColumn(), console=err) as prog:
            tid = prog.add_task(stage, total=100, msg="")
            while True:
                try:
                    t = c.task3d(task_id)
                except TiinyError as e:  # gateway 504s while the device is busy
                    if "HTTP 5" not in str(e):
                        raise
                    time.sleep(5)
                    continue
                prog.update(tid, completed=t.get("progress") or 0, msg=(t.get("message") or "")[:45])
                if t["status"] == "completed":
                    break
                if t["status"] in ("failed", "cancelled", "error"):
                    raise TiinyError(f"3D {stage} {t['status']}: {t.get('error')}")
                time.sleep(3)
        return task_id, t["result"]["output"]["artifacts"]

    def pick(arts: list[dict], kind: str) -> dict:
        return next(a for a in arts if a["artifact_type"] == kind)

    shape_t, arts = run("shape", image=image_path, params=controls or None)
    shape = ref(shape_t, pick(arts, "shape"))
    tex_t, arts = run("texture", input_refs={"shape_artifact": shape}, params=controls or None)
    refs = {"shape_artifact": shape, "material_artifact": ref(tex_t, pick(arts, "material"))}
    if faces:
        simp_t, arts = run("simplify", input_refs={"shape_artifact": shape},
                           params={"simplify": True, "target_faces": faces})
        geo = next((a for a in arts if a["artifact_type"].startswith("geometry")), None)
        if geo:
            refs["geometry_artifact"] = ref(simp_t, geo)
    exp_t, arts = run("export", input_refs=refs, params={"output_name": output.name})
    glb = next(a for a in arts if a["filename"].endswith(".glb"))
    output.write_bytes(c.task3d_artifact(exp_t, glb["artifact_id"]))
    click.echo(f"Saved {output} ({output.stat().st_size / 1e6:.1f} MB)")
