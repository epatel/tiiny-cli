"""Full-screen TUI for `tai agent` (textual).

The agent loop runs in a worker thread and posts its events here as messages.
Permission requests block that thread on a threading.Event until the modal
answers. Esc interrupts the current turn; Ctrl+C quits.
"""

from __future__ import annotations

import json
import threading
import time
from collections.abc import Callable
from contextlib import contextmanager
from dataclasses import dataclass, field

from rich.markdown import Markdown
from rich.syntax import Syntax
from rich.text import Text
from textual import events, work
from textual.app import App, ComposeResult
from textual.binding import Binding
from textual.containers import Vertical, VerticalScroll
from textual.message import Message
from textual.screen import ModalScreen
from textual.widgets import Collapsible, Footer, Static, TextArea

from tai.agent.loop import (Agent, Decision, Event, Notice, ReasoningDelta, TextDelta, ToolEnd,
                            ToolStart, TurnEnd)
from tai.agent.device import MUTATING, DeviceCommands
from tai.agent.tools import Preview, Tool
from tai.client import TiinyError
from tai.loader import LoadCancelled, ModelLoader
from tai.models import ResolveError

RESULT_PREVIEW = 3000  # characters of a tool result shown when expanded

HELP = """**Keys:** Enter send · Ctrl+J, Alt+Enter or `\\` then Enter for a new line · Esc interrupt · Ctrl+C quit
**Commands:** `/tiiny` manage the device (`/tiiny help`) · `/clear` forget the conversation · `/view` open the browser viewer · `/skills` · `/mcp` · `/<skill> [task]` use a skill · `/help` · `/exit`
Before edits and shell commands you'll be asked: **y** yes · **a** always (this session) · **n** no"""


@dataclass
class Activity:
    """What the agent is doing right now, shown with elapsed time in the status line."""
    label: str
    detail: str = ""
    started: float = field(default_factory=time.monotonic)


@dataclass
class AppInfo:
    title: str  # top bar: model, folder, session
    initial: str | None = None  # task to send on start
    on_clear: Callable[[], None] = lambda: None
    skills: list[tuple[str, str]] = field(default_factory=list)  # (name, description)
    servers: list[str] = field(default_factory=list)  # one status line per MCP server
    open_viewer: Callable[[], str] | None = None  # starts the browser viewer, opens it, returns its URL


class ConfirmScreen(ModalScreen[bool]):
    BINDINGS = [Binding("y", "answer(True)", "Yes"), Binding("n", "answer(False)", "No"),
                Binding("escape", "answer(False)", "No", show=False)]

    def __init__(self, question: str) -> None:
        super().__init__()
        self.question = question

    def compose(self) -> ComposeResult:
        with Vertical(id="permission"):
            yield Static(Text(self.question), id="perm-title")
            yield Static("[bold]y[/] yes   [bold]n[/] no", id="perm-keys")

    def action_answer(self, yes: bool) -> None:
        self.dismiss(yes)


class TuiLoaderUI:
    """LoaderUI for worker threads: questions as a modal, progress in the status line."""

    def __init__(self, app: AgentApp) -> None:
        self.app = app

    def confirm(self, question: str) -> bool:
        return bool(self.app.wait_for_modal(ConfirmScreen(question), default=False))

    def info(self, message: str) -> None:
        self.app.call_from_thread(self.app._busy, message)

    @contextmanager
    def progress(self, name: str):
        def update(pct: float, msg: str) -> None:
            self.app.call_from_thread(self.app._busy, f"Loading {name}", f"{pct:.0f}% {msg[:40]}".strip())
        yield update


class PromptArea(TextArea):
    """Multi-line prompt: Enter sends; Ctrl+J, Alt+Enter, Shift+Enter (where the terminal
    reports it) or a trailing backslash + Enter insert a newline. Pasted text keeps its
    newlines and is not sent until Enter."""

    NEWLINE_KEYS = {"ctrl+j", "alt+enter", "shift+enter"}

    class Submitted(Message):
        def __init__(self, area: PromptArea, value: str) -> None:
            self.area, self.value = area, value
            super().__init__()

    def __init__(self, **kwargs) -> None:
        super().__init__(soft_wrap=True, show_line_numbers=False, highlight_cursor_line=False,
                         placeholder="Ask tai agent to do something…  (Enter send · Ctrl+J new line · /help)",
                         **kwargs)

    async def _on_key(self, event: events.Key) -> None:
        if event.key in self.NEWLINE_KEYS:
            self._take(event)
            self.insert("\n")
        elif event.key == "enter":
            self._take(event)
            self._enter()
        else:
            await super()._on_key(event)

    @staticmethod
    def _take(event: events.Key) -> None:
        event.stop()
        event.prevent_default()

    def _enter(self) -> None:
        row, col = self.cursor_location
        if col and self.document.get_line(row)[col - 1] == "\\":
            self.replace("\n", (row, col - 1), (row, col))  # "\" + Enter: line continuation
        else:
            self.post_message(self.Submitted(self, self.text))


class AgentEvent(Message):
    def __init__(self, event: Event) -> None:
        self.event = event
        super().__init__()


class PermissionScreen(ModalScreen[Decision]):
    BINDINGS = [Binding("y", "decide('allow')", "Yes"), Binding("a", "decide('always')", "Always"),
                Binding("n", "decide('deny')", "No"), Binding("escape", "decide('deny')", "No", show=False)]

    def __init__(self, preview: Preview) -> None:
        super().__init__()
        self.preview = preview

    def compose(self) -> ComposeResult:
        p = self.preview
        lexer = {"diff": "diff", "command": "bash", "json": "json"}.get(p.kind, "text")
        warn = "  [bold red](outside the project)[/]" if p.outside else ""
        with Vertical(id="permission"):
            yield Static(f"[bold]{p.title}[/]{warn}", id="perm-title")
            with VerticalScroll(id="perm-body"):
                yield Static(Syntax(p.body, lexer, theme="ansi_dark", word_wrap=True))
            always = "" if p.outside else "   [bold]a[/] always"
            yield Static(f"[bold]y[/] yes{always}   [bold]n[/] no", id="perm-keys")

    def action_decide(self, choice: str) -> None:
        decision = Decision(choice)
        if decision is Decision.ALWAYS and self.preview.outside:
            decision = Decision.ALLOW
        self.dismiss(decision)


class AgentApp(App):
    CSS = """
    #title { background: $boost; color: $text-muted; padding: 0 1; height: 1; }
    #log { padding: 0 1; }
    .user { color: $accent; margin-top: 1; text-style: bold; }
    .assistant { margin-top: 1; }
    .thinking { color: $text-muted; text-style: italic; margin-top: 1; }
    .tool { margin-top: 1; color: $warning; }
    .tool-ok { color: $success; }
    .tool-fail { color: $error; }
    .notice { color: $text-muted; margin-top: 1; }
    Collapsible { border: none; padding: 0; margin: 0 0 0 2; }
    #status { height: 1; padding: 0 1; color: $text-muted; }
    #prompt { height: auto; max-height: 12; margin: 0 1; }
    PermissionScreen { align: center middle; }
    #permission { width: 90%; height: auto; max-height: 85%; border: thick $warning; background: $surface;
                  padding: 1 2; }
    #perm-body { height: auto; max-height: 30; margin: 1 0; }
    """
    BINDINGS = [Binding("escape", "interrupt", "Interrupt"),
                Binding("ctrl+c", "quit", "Quit", priority=True),
                Binding("ctrl+l", "clear", "Clear")]

    def __init__(self, agent: Agent, info: AppInfo) -> None:
        super().__init__()
        self.agent, self.info = agent, info
        agent.approve = self.approve
        self.busy = False
        self._text: Static | None = None  # assistant message being streamed
        self._text_buf = ""
        self._think: Static | None = None
        self._think_buf = ""
        self._tools: dict[str, tuple[Static, str]] = {}  # call id -> (line, label)
        self._pending: threading.Event | None = None  # a worker waiting on a modal
        self.device_busy = False  # a /tiiny command is running
        self._activity: Activity | None = None
        agent.reload_model = self._reload_agent_model

    # -- layout ----------------------------------------------------------
    def compose(self) -> ComposeResult:
        yield Static(self.info.title, id="title")
        yield VerticalScroll(id="log")
        yield Static("", id="status")
        yield PromptArea(id="prompt")
        yield Footer()

    def on_mount(self) -> None:
        self._replay(self.agent.messages)
        self._status("ready")
        self.set_interval(1.0, self._render_activity)
        self.query_one("#prompt", PromptArea).focus()
        if self.info.initial:
            self._submit(self.info.initial)

    @property
    def log_view(self) -> VerticalScroll:
        return self.query_one("#log", VerticalScroll)

    def _add(self, widget) -> None:
        self.log_view.mount(widget)
        self.log_view.scroll_end(animate=False)

    def _status(self, text: str) -> None:
        """A plain status (idle states); ends the current activity."""
        self._activity = None
        self.query_one("#status", Static).update(text)

    def _busy(self, label: str, detail: str = "") -> None:
        """Show an ongoing activity; the elapsed time keeps counting while the label stays the same."""
        if self._activity is None or self._activity.label != label:
            self._activity = Activity(label, detail)
        else:
            self._activity.detail = detail
        self._render_activity()

    def _render_activity(self) -> None:
        a = self._activity
        if a is None:
            return
        secs = int(time.monotonic() - a.started)
        parts = [f"◐ {a.label}", a.detail, f"{secs}s" if secs else ""]
        self.query_one("#status", Static).update(" · ".join(p for p in parts if p))

    def _replay(self, messages: list[dict]) -> None:
        """Show a resumed conversation: user text, assistant text, tool-call labels."""
        for msg in messages:
            if msg["role"] == "user":
                self._add(Static(Text(f"› {msg['content']}"), classes="user"))
            elif msg["role"] == "assistant":
                if msg.get("content"):
                    self._add(Static(Markdown(msg["content"]), classes="assistant"))
                for call in msg.get("tool_calls") or []:
                    self._add(Static(Text(f"⏺ {_label(call, self.agent.config.tools)}"), classes="tool tool-ok"))

    # -- input -----------------------------------------------------------
    def on_prompt_area_submitted(self, event: PromptArea.Submitted) -> None:
        text = event.value.strip()
        event.area.clear()
        if text:
            self._submit(text)

    def _submit(self, text: str) -> None:
        if text.startswith("/"):
            cmd, _, arg = text[1:].partition(" ")
            self._command(cmd, arg.strip())
            return
        if self.busy or self.device_busy:
            msg = "Still working — press Esc to interrupt first." if self.busy else "Wait for /tiiny to finish."
            self.notify(msg, severity="warning")
            return
        self._add(Static(Text(f"› {text}"), classes="user"))
        self.busy = True
        self._busy("thinking…")
        self.run_turn(text)

    def _command(self, cmd: str, arg: str) -> None:
        actions = {"exit": self.action_quit, "quit": self.action_quit, "q": self.action_quit,
                   "clear": self.action_clear, "help": lambda: self._note(HELP),
                   "skills": self._list_skills, "mcp": self._list_servers, "view": self._view}
        if cmd in actions:
            actions[cmd]()
        elif cmd == "tiiny":
            self._tiiny(arg)
        elif cmd in dict(self.info.skills):
            self._submit(f"Use the `{cmd}` skill." + (f" {arg}" if arg else ""))
        else:
            self.notify(f"Unknown command /{cmd} — try /help", severity="warning")

    def _tiiny(self, arg: str) -> None:
        sub, _ = DeviceCommands.split(arg)
        if self.device_busy:
            self.notify("A /tiiny command is still running.", severity="warning")
        elif sub in MUTATING and self.busy:
            self.notify(f"/tiiny {sub} waits until the agent is idle (Esc to interrupt).", severity="warning")
        else:
            self.device_busy = True
            self._add(Static(Text(f"› /tiiny {arg}".rstrip()), classes="user"))
            self._busy(f"/tiiny {sub or 'status'}")
            self.run_device(arg)

    @work(thread=True, group="device", exclusive=True, exit_on_error=False)
    def run_device(self, arg: str) -> None:
        commands = DeviceCommands(self.agent.client, TuiLoaderUI(self), self.agent.config.model_id)
        try:
            result = commands.run(arg)
        except LoadCancelled:
            result = "Cancelled."
        except (TiinyError, ResolveError) as e:
            result = f"**error:** {e}"
        except Exception as e:  # never let a bug kill the UI silently
            result = f"**error:** {type(e).__name__}: {e}"
        self.call_from_thread(self._device_done, result)

    def _device_done(self, markdown: str) -> None:
        self.device_busy = False
        self._note(markdown)
        if self.busy:
            self._busy("thinking…")
        else:
            self._status("ready")

    def _note(self, markdown: str) -> None:
        self._add(Static(Markdown(markdown), classes="assistant"))

    def _view(self) -> None:
        if self.info.open_viewer is None:
            self.notify("No viewer in this session.", severity="warning")
            return
        self._note(f"Viewer: {self.info.open_viewer()}  (images, audio, 3D and questions from the agent show up there)")

    def _list_skills(self) -> None:
        rows = [f"- **{name}** — {desc}" for name, desc in self.info.skills]
        self._note("**Skills** (`/<name> [task]` to use one)\n\n" + "\n".join(rows) if rows else
                   "No skills found in `.claude/skills/`, `~/.claude/skills/` or `~/.config/tai/skills/`.")

    def _list_servers(self) -> None:
        rows = [f"- {line}" for line in self.info.servers]
        self._note("**MCP servers**\n\n" + "\n".join(rows) if rows else
                   "No MCP servers configured (`.mcp.json` or `~/.config/tai/mcp.json`).")

    # -- agent thread ------------------------------------------------------
    @work(thread=True, exclusive=True, exit_on_error=False)
    def run_turn(self, text: str) -> None:
        try:
            for ev in self.agent.run(text):
                self.post_message(AgentEvent(ev))
        except Exception as e:  # never let a bug kill the UI silently
            self.post_message(AgentEvent(TurnEnd("error", f"{type(e).__name__}: {e}")))

    def approve(self, tool: Tool, args: dict, preview: Preview) -> Decision:
        """Called from the agent thread; blocks until the user answers the modal."""
        self.call_from_thread(self._busy, f"waiting for you: {preview.title}")
        return self.wait_for_modal(PermissionScreen(preview), default=Decision.DENY)

    def _reload_agent_model(self) -> None:
        """Agent thread: load the agent's model again after something unloaded it."""
        loader = ModelLoader(self.agent.client, TuiLoaderUI(self))
        model = next((m for m in loader.catalog() if m.id == self.agent.config.model_id), None)
        if model is None:
            raise TiinyError(f"{self.agent.config.model_id} is not installed on the device")
        try:
            loader.ensure_loaded(model)
        except LoadCancelled:
            raise TiinyError(f"Reload cancelled: {model.name} is not loaded. "
                             f"Use `/tiiny load {model.name}` when you want to continue.") from None

    def wait_for_modal(self, screen: ModalScreen, default):
        """From a worker thread: show ``screen`` and block until it is dismissed.
        Quitting releases the wait, and the answer is then ``default``."""
        done = threading.Event()
        answer = [default]

        def answered(result) -> None:
            answer[0] = default if result is None else result
            done.set()

        self._pending = done
        self.call_from_thread(self.push_screen, screen, answered)
        done.wait()
        self._pending = None
        return answer[0]

    # -- events ----------------------------------------------------------
    def on_agent_event(self, message: AgentEvent) -> None:
        ev = message.event
        handler = {TextDelta: self._on_text, ReasoningDelta: self._on_reasoning, ToolStart: self._on_tool_start,
                   ToolEnd: self._on_tool_end, TurnEnd: self._on_turn_end, Notice: self._on_notice}[type(ev)]
        handler(ev)
        self.log_view.scroll_end(animate=False)

    def _on_text(self, ev: TextDelta) -> None:
        if self._text is None:
            self._text_buf = ""
            self._text = Static("", classes="assistant")
            self._add(self._text)
        self._text_buf += ev.text
        self._text.update(Markdown(self._text_buf))

    def _on_reasoning(self, ev: ReasoningDelta) -> None:
        if self._think is None:
            self._think_buf = ""
            self._think = Static("", classes="thinking")
            self._add(self._think)
        self._think_buf += ev.text
        self._think.update(Text(self._think_buf))

    def _end_text(self) -> None:
        self._text = self._think = None

    def _on_tool_start(self, ev: ToolStart) -> None:
        self._end_text()
        line = Static(Text(f"⏺ {ev.label}"), classes="tool")
        self._tools[ev.call_id] = (line, ev.label)
        self._add(line)
        self._busy(ev.label)

    def _on_tool_end(self, ev: ToolEnd) -> None:
        entry = self._tools.pop(ev.call_id, None)
        if entry is not None:
            line, label = entry
            mark = " ✗ denied" if ev.denied else ("" if ev.ok else " ✗")
            line.update(Text(f"⏺ {label}{mark}"))
            line.add_class("tool-ok" if ev.ok else "tool-fail")
        body = ev.result if len(ev.result) <= RESULT_PREVIEW else ev.result[:RESULT_PREVIEW] + "\n…"
        title = _result_title(ev.result)
        self._add(Collapsible(Static(Text(body)), title=title, collapsed=True))
        self._busy("thinking…")

    def _on_notice(self, ev: Notice) -> None:
        self._end_text()
        self._add(Static(Text(f"• {ev.text}"), classes="notice"))
        self._busy("thinking…")

    def _on_turn_end(self, ev: TurnEnd) -> None:
        self._end_text()
        self.busy = False
        if ev.reason == "error":
            self._add(Static(Text(f"error: {ev.error}"), classes="tool-fail"))
        notes = {"done": "ready", "interrupted": "■ interrupted — ready",
                 "max_steps": "■ stopped: step limit reached — ready", "error": "■ error — ready"}
        self._status(notes.get(ev.reason, "ready"))

    # -- actions ---------------------------------------------------------
    def action_interrupt(self) -> None:
        if self.busy:
            self.agent.cancel()
            self._busy("interrupting…")

    def action_clear(self) -> None:
        if self.busy:
            self.notify("Interrupt first (Esc).", severity="warning")
            return
        self.agent.clear()
        self.info.on_clear()
        self.log_view.remove_children()
        self._status("conversation cleared — ready")

    async def action_quit(self) -> None:
        self.agent.cancel()
        if self._pending is not None:
            self._pending.set()  # release a thread waiting on a permission answer (answer stays DENY)
        self.exit()


def _label(call: dict, tools: dict[str, Tool]) -> str:
    name = call["function"]["name"]
    tool = tools.get(name)
    try:
        args = json.loads(call["function"]["arguments"] or "{}")
    except json.JSONDecodeError:
        return name
    return tool.summary(args) if tool and isinstance(args, dict) else name


def _result_title(result: str) -> str:
    lines = result.count("\n") + 1
    first = result.splitlines()[0] if result else "(no output)"
    return f"{first[:70]}" + (f"  (+{lines - 1} lines)" if lines > 1 else "")
