"""Interactive chat REPL: session state plus one small handler per /command."""

from __future__ import annotations

import base64
import mimetypes
import sys
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING

from tai.client import TiinyError
from tai.models import Model, ResolveError

if TYPE_CHECKING:
    from rich.console import Console

CHAT_HELP = """[bold]/model NAME[/] switch model   [bold]/image PATH[/] attach image to next message
[bold]/think on|off[/] reasoning     [bold]/think show|hide[/] print it or just a spinner
[bold]/system TEXT[/] set system prompt
[bold]/clear[/] forget history      [bold]/name NAME[/] name this session   [bold]/exit[/]"""

EXIT = object()  # returned by a handler to end the loop


def data_url(path: Path) -> str:
    mime = mimetypes.guess_type(path.name)[0] or "image/png"
    return f"data:{mime};base64,{base64.b64encode(path.read_bytes()).decode()}"


def image_part(path: Path) -> dict:
    return {"type": "image_url", "image_url": {"url": data_url(path)}}


def user_message(text: str, images: list[Path]) -> dict:
    if not images:
        return {"role": "user", "content": text}
    return {"role": "user", "content": [{"type": "text", "text": text}, *map(image_part, images)]}


@dataclass
class SessionState:
    sid: int
    model: Model
    system: str | None = None
    think: bool | None = None
    show_thinking: bool = True  # print reasoning (False: a spinner); display only, the model still thinks
    messages: list[dict] = field(default_factory=list)
    pending_images: list[Path] = field(default_factory=list)


Reply = Callable[[Model, list[dict], "bool | None"], str]


class ChatSession:
    """One chat session. ``reply`` streams a model answer for a conversation and
    returns its text; it is injected so the REPL can be tested without a device."""

    def __init__(self, app, state: SessionState, err: Console, reply: Reply):
        self.app, self.state, self.err, self.reply = app, state, err, reply
        self.commands: dict[str, Callable[[str], object]] = {
            "exit": self.cmd_exit, "quit": self.cmd_exit, "q": self.cmd_exit,
            "help": self.cmd_help, "clear": self.cmd_clear, "model": self.cmd_model,
            "image": self.cmd_image, "think": self.cmd_think,
            "system": self.cmd_system, "name": self.cmd_name,
        }

    # -- loop ------------------------------------------------------------
    def run(self) -> None:
        prompt = "\001\033[1;36m\002› \001\033[0m\002" if sys.stdin.isatty() else "› "
        while True:
            try:
                line = input(prompt)
            except EOFError:
                self.err.print()
                return
            if self.handle(line) is EXIT:
                return

    def handle(self, line: str) -> object:
        if not line.strip():
            return None
        if line.startswith("/"):
            cmd, _, arg = line[1:].partition(" ")
            handler = self.commands.get(cmd)
            if handler is None:
                self.err.print(f"[red]unknown command /{cmd}[/]")
                return None
            return handler(arg.strip())
        return self.send(line)

    def send(self, line: str) -> None:
        st = self.state
        user = user_message(line, st.pending_images)
        system = [{"role": "system", "content": st.system}] if st.system else []
        try:
            text = self.reply(st.model, system + st.messages + [user], st.think)
        except KeyboardInterrupt:
            self.err.print("[dim]…interrupted[/]")
            return
        except TiinyError as e:
            self.err.print(f"[red]{e}[/]")
            return
        st.messages += [user, {"role": "assistant", "content": text}]
        store = self.app.store
        store.add_message(st.sid, "user", line, [str(p) for p in st.pending_images] or None)
        store.add_message(st.sid, "assistant", text)
        st.pending_images = []

    # -- /commands ---------------------------------------------------------
    def cmd_exit(self, _arg: str) -> object:
        return EXIT

    def cmd_help(self, _arg: str) -> None:
        self.err.print(CHAT_HELP)

    def cmd_clear(self, _arg: str) -> None:
        self.state.messages.clear()
        self.app.store.clear_messages(self.state.sid)
        self.err.print("[dim]history cleared[/]")

    def cmd_model(self, arg: str) -> None:
        try:
            model = self.app.model_for("chat", arg or None)
            self.app.ensure_loaded(model)
        except (ResolveError, TiinyError) as e:
            self.err.print(f"[red]{e}[/]")
            return
        self.state.model = model
        self.app.store.update_session(self.state.sid, model=model.id)
        self.err.print(f"[dim]now using {model.name}[/]")

    def cmd_image(self, arg: str) -> None:
        path = Path(arg).expanduser()
        if not path.exists():
            self.err.print(f"[red]no such file: {path}[/]")
        elif not self.state.model.vision:
            self.err.print(f"[red]{self.state.model.name} can't see images; /model a vision model first[/]")
        else:
            self.state.pending_images.append(path)
            self.err.print(f"[dim]attached {path.name}[/]")

    def cmd_think(self, arg: str) -> None:
        """on|off: the model's reasoning. show|hide: print it, or only a spinner
        (`tai config set show_thinking off` sets the default)."""
        if arg in ("show", "hide"):
            self.state.show_thinking = arg == "show"
            self.err.print(f"[dim]thinking {'shown' if self.state.show_thinking else 'hidden (spinner)'}[/]")
            return
        self.state.think = arg != "off"
        self.err.print(f"[dim]thinking {'on' if self.state.think else 'off'}[/]")

    def cmd_system(self, arg: str) -> None:
        self.state.system = arg or None
        self.app.store.update_session(self.state.sid, system=self.state.system)

    def cmd_name(self, arg: str) -> None:
        self.app.store.update_session(self.state.sid, name=arg or None)
