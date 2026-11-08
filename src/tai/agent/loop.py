"""The agent loop, independent of any UI.

``Agent.run(text)`` yields events while it streams the model's reply, asks the
``approve`` callback before tools that change things, runs tools, feeds results
back and repeats until the model answers without tool calls. Every message it
appends is also passed to ``on_message`` so the caller can persist it.
"""

from __future__ import annotations

import json
import threading
from collections.abc import Callable, Iterator
from contextlib import closing
from dataclasses import dataclass, field
from enum import Enum

from tai.agent.tools import TOOLS, Preview, Tool, ToolContext, ToolError
from tai.client import TiinyError

MAX_STEPS = 50  # model calls per user turn
DENIED = "The user denied this tool call. Do not retry it; ask what to do instead or take another approach."
INTERRUPTED = "Interrupted by the user before this tool ran."


class Decision(Enum):
    ALLOW = "allow"
    ALWAYS = "always"  # allow this and future calls in the same permission group
    DENY = "deny"


@dataclass
class TextDelta:
    text: str


@dataclass
class ReasoningDelta:
    text: str


@dataclass
class ToolStart:
    call_id: str
    name: str
    label: str


@dataclass
class ToolEnd:
    call_id: str
    name: str
    result: str
    ok: bool = True
    denied: bool = False


@dataclass
class TurnEnd:
    reason: str  # done | max_steps | interrupted | error
    error: str | None = None


@dataclass
class Notice:
    """Something the user should know that isn't model output, e.g. a model reload."""
    text: str


Event = TextDelta | ReasoningDelta | ToolStart | ToolEnd | TurnEnd | Notice
Approver = Callable[[Tool, dict, Preview], Decision]


@dataclass
class AgentConfig:
    model_id: str
    system: str
    ctx: ToolContext
    extra: dict = field(default_factory=dict)  # extra request fields
    max_steps: int = MAX_STEPS
    tools: dict[str, Tool] = field(default_factory=lambda: dict(TOOLS))  # built-ins + skill + MCP


@dataclass
class Transcript:
    """The conversation so far; ``on_message`` sees every appended message (for saving)."""
    messages: list[dict] = field(default_factory=list)
    on_message: Callable[[dict], None] = lambda m: None

    def append(self, msg: dict) -> None:
        self.messages.append(msg)
        self.on_message(msg)


class _Accumulator:
    """Rebuilds one assistant message from streamed chat-completion deltas."""

    def __init__(self) -> None:
        self.text: list[str] = []
        self.calls: dict[int, dict] = {}

    def add(self, delta: dict) -> None:
        if delta.get("content"):
            self.text.append(delta["content"])
        for tc in delta.get("tool_calls") or []:
            call = self.calls.setdefault(tc.get("index", 0),
                                         {"id": "", "type": "function", "function": {"name": "", "arguments": ""}})
            call["id"] = tc.get("id") or call["id"]
            fn = tc.get("function") or {}
            call["function"]["name"] += fn.get("name") or ""
            call["function"]["arguments"] += fn.get("arguments") or ""

    def message(self) -> dict:
        msg: dict = {"role": "assistant", "content": "".join(self.text) or None}
        if self.calls:
            msg["tool_calls"] = [self.calls[i] for i in sorted(self.calls)]
        return msg


def model_missing(e: TiinyError) -> bool:
    """The device's ways of saying a model isn't loaded (404 model_not_found)."""
    text = str(e)
    return "is not loaded" in text or "does not exist or is not running" in text or "model_not_found" in text


def _delta_events(delta: dict) -> Iterator[Event]:
    if delta.get("reasoning_content"):
        yield ReasoningDelta(delta["reasoning_content"])
    if delta.get("content"):
        yield TextDelta(delta["content"])


class Agent:
    def __init__(self, client, config: AgentConfig, approve: Approver, transcript: Transcript | None = None):
        self.client, self.config, self.approve = client, config, approve
        self.transcript = transcript or Transcript()
        self.granted: set[str] = set()
        self._cancel = threading.Event()
        # Called when the model is no longer loaded (e.g. a tool ran `tai image`, which swapped
        # it out). Should load it again or raise TiinyError. None = just fail the turn.
        self.reload_model: Callable[[], None] | None = None
        self.on_cancel: list[Callable[[], None]] = []  # e.g. release a question waiting in the viewer

    @property
    def messages(self) -> list[dict]:
        return self.transcript.messages

    def cancel(self) -> None:
        """Stop the current turn at the next safe point (between chunks or tools)."""
        self._cancel.set()
        for hook in self.on_cancel:
            hook()

    def clear(self) -> None:
        self.messages.clear()

    def _append(self, msg: dict) -> None:
        self.transcript.append(msg)

    # -- turn ------------------------------------------------------------
    def run(self, text: str) -> Iterator[Event]:
        self._cancel.clear()
        self._append({"role": "user", "content": text})
        try:
            yield from self._steps()
        except TiinyError as e:
            yield TurnEnd("error", str(e))

    def _steps(self) -> Iterator[Event]:
        for _ in range(self.config.max_steps):
            msg = yield from self._stream_or_reload()
            self._append(msg)
            calls = msg.get("tool_calls") or []
            for call in calls:
                yield from self._call(call)
            if self._cancel.is_set():
                yield TurnEnd("interrupted")
                return
            if not calls:
                yield TurnEnd("done")
                return
        yield TurnEnd("max_steps")

    def _stream_or_reload(self) -> Iterator[Event]:
        """Stream a reply; if the model has been unloaded meanwhile, reload it and retry once."""
        try:
            return (yield from self._stream())
        except TiinyError as e:
            if self.reload_model is None or not model_missing(e):
                raise
        yield Notice(f"{self.config.model_id} is no longer loaded (a tool probably swapped it out) — reloading it")
        self.reload_model()
        yield Notice("Model reloaded — continuing")
        return (yield from self._stream())

    def _stream(self) -> Iterator[Event]:
        acc = _Accumulator()
        convo = [{"role": "system", "content": self.config.system}, *self.messages]
        tools = [t.spec() for t in self.config.tools.values()]
        stream = self.client.chat_stream(self.config.model_id, convo, tools=tools, **self.config.extra)
        with closing(stream):  # closing the generator ends the HTTP stream on interrupt
            for chunk in stream:
                if self._cancel.is_set():
                    break
                delta = (chunk.get("choices") or [{}])[0].get("delta") or {}
                acc.add(delta)
                yield from _delta_events(delta)
        return acc.message()

    # -- tools -----------------------------------------------------------
    def _call(self, call: dict) -> Iterator[Event]:
        name = call["function"]["name"]
        tool = self.config.tools.get(name)
        try:
            args = json.loads(call["function"]["arguments"] or "{}")
        except json.JSONDecodeError:
            args = None
        label = tool.summary(args) if tool and isinstance(args, dict) else name
        yield ToolStart(call["id"], name, label)
        result, ok, denied = self._execute(tool, name, args)
        self._append({"role": "tool", "tool_call_id": call["id"], "content": result})
        yield ToolEnd(call["id"], name, result, ok, denied)

    def _execute(self, tool: Tool | None, name: str, args) -> tuple[str, bool, bool]:
        """Returns (result text for the model, ok, denied)."""
        if self._cancel.is_set():
            return INTERRUPTED, False, False
        if tool is None:
            return f"Error: unknown tool '{name}'. Available: {', '.join(self.config.tools)}", False, False
        if not isinstance(args, dict):
            return "Error: tool arguments were not valid JSON", False, False
        try:
            if not self._permitted(tool, args):
                return DENIED, False, True
            return tool.run(args, self.config.ctx), True, False
        except ToolError as e:
            return f"Error: {e}", False, False
        except (KeyError, TypeError, ValueError) as e:
            return f"Error: bad arguments for {name}: {e}", False, False

    def _permitted(self, tool: Tool, args: dict) -> bool:
        if tool.grant is None:
            return True
        preview = tool.preview(args, self.config.ctx)  # may raise ToolError (e.g. edit target missing)
        if tool.grant in self.granted and not preview.outside:
            return True
        decision = self.approve(tool, args, preview)
        if decision is Decision.ALWAYS and not preview.outside:
            self.granted.add(tool.grant)
        return decision is not Decision.DENY
