"""Full-screen TUI for `tai agent` (textual).

The agent loop runs in a worker thread and posts its events here as messages.
Permission requests block that thread on a threading.Event until the modal
answers. Esc interrupts the current turn; Ctrl+C quits.
"""

from __future__ import annotations

import json
import threading
from collections.abc import Callable
from dataclasses import dataclass, field

from rich.markdown import Markdown
from rich.syntax import Syntax
from rich.text import Text
from textual import work
from textual.app import App, ComposeResult
from textual.binding import Binding
from textual.containers import Vertical, VerticalScroll
from textual.message import Message
from textual.screen import ModalScreen
from textual.widgets import Collapsible, Footer, Input, Static

from tai.agent.loop import (Agent, Decision, Event, ReasoningDelta, TextDelta, ToolEnd, ToolStart,
                            TurnEnd)
from tai.agent.tools import Preview, Tool

RESULT_PREVIEW = 3000  # characters of a tool result shown when expanded

HELP = """**Keys:** Enter send · Esc interrupt · Ctrl+C quit
**Commands:** `/clear` forget the conversation · `/skills` · `/mcp` · `/<skill> [task]` use a skill · `/help` · `/exit`
Before edits and shell commands you'll be asked: **y** yes · **a** always (this session) · **n** no"""


@dataclass
class AppInfo:
    title: str  # top bar: model, folder, session
    initial: str | None = None  # task to send on start
    on_clear: Callable[[], None] = lambda: None
    skills: list[tuple[str, str]] = field(default_factory=list)  # (name, description)
    servers: list[str] = field(default_factory=list)  # one status line per MCP server


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
    Collapsible { border: none; padding: 0; margin: 0 0 0 2; }
    #status { height: 1; padding: 0 1; color: $text-muted; }
    #prompt { margin: 0 1; }
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
        self._pending: threading.Event | None = None

    # -- layout ----------------------------------------------------------
    def compose(self) -> ComposeResult:
        yield Static(self.info.title, id="title")
        yield VerticalScroll(id="log")
        yield Static("", id="status")
        yield Input(placeholder="Ask tai agent to do something…  (/help)", id="prompt")
        yield Footer()

    def on_mount(self) -> None:
        self._replay(self.agent.messages)
        self._status("ready")
        self.query_one("#prompt", Input).focus()
        if self.info.initial:
            self._submit(self.info.initial)

    @property
    def log_view(self) -> VerticalScroll:
        return self.query_one("#log", VerticalScroll)

    def _add(self, widget) -> None:
        self.log_view.mount(widget)
        self.log_view.scroll_end(animate=False)

    def _status(self, text: str) -> None:
        self.query_one("#status", Static).update(text)

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
    def on_input_submitted(self, event: Input.Submitted) -> None:
        text = event.value.strip()
        event.input.value = ""
        if text:
            self._submit(text)

    def _submit(self, text: str) -> None:
        if text.startswith("/"):
            cmd, _, arg = text[1:].partition(" ")
            self._command(cmd, arg.strip())
            return
        if self.busy:
            self.notify("Still working — press Esc to interrupt first.", severity="warning")
            return
        self._add(Static(Text(f"› {text}"), classes="user"))
        self.busy = True
        self._status("◐ thinking…")
        self.run_turn(text)

    def _command(self, cmd: str, arg: str) -> None:
        actions = {"exit": self.action_quit, "quit": self.action_quit, "q": self.action_quit,
                   "clear": self.action_clear, "help": lambda: self._note(HELP),
                   "skills": self._list_skills, "mcp": self._list_servers}
        if cmd in actions:
            actions[cmd]()
        elif cmd in dict(self.info.skills):
            self._submit(f"Use the `{cmd}` skill." + (f" {arg}" if arg else ""))
        else:
            self.notify(f"Unknown command /{cmd} — try /help", severity="warning")

    def _note(self, markdown: str) -> None:
        self._add(Static(Markdown(markdown), classes="assistant"))

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
        done = threading.Event()
        answer: list[Decision] = [Decision.DENY]

        def answered(decision: Decision | None) -> None:
            answer[0] = decision or Decision.DENY
            done.set()

        self._pending = done
        self.call_from_thread(self._ask, preview, answered)
        done.wait()
        self._pending = None
        return answer[0]

    def _ask(self, preview: Preview, answered: Callable[[Decision | None], None]) -> None:
        self._status(f"◐ waiting for you: {preview.title}")
        self.push_screen(PermissionScreen(preview), answered)

    # -- events ----------------------------------------------------------
    def on_agent_event(self, message: AgentEvent) -> None:
        ev = message.event
        handler = {TextDelta: self._on_text, ReasoningDelta: self._on_reasoning, ToolStart: self._on_tool_start,
                   ToolEnd: self._on_tool_end, TurnEnd: self._on_turn_end}[type(ev)]
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
        self._status(f"◐ {ev.label}")

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
        self._status("◐ thinking…")

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
            self._status("◐ interrupting…")

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
