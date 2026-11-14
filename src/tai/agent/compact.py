"""Context compaction for `tai agent`.

The model summarises the whole conversation (same system prompt and tools, so the
device can reuse its prompt cache); the conversation is then replaced by one user
message holding that summary plus the latest messages, kept word for word. The cut
for the kept tail is made only before a user or assistant message, so a tool call is
never separated from its result.
"""

from __future__ import annotations

import json
from collections.abc import Iterator

from tai.agent.loop import Agent, Compacted, ContextUse, Event, Notice, PromptProgress, SummaryProgress, TextDelta, TurnEnd
from tai.client import TiinyError

DEFAULT_AT = 85  # % of the context window at which `tai agent` compacts by itself
KEEP_CHARS = 12_000  # at most this much of the latest conversation is kept word for word
KEEP_SHARE = 0.4  # ... and at most ~10% of the window (4 characters per token)

HEADER = "# Summary of the conversation so far"
INTRO = ("(The earlier conversation was compacted to save context. "
         "The messages after this one are the latest, kept word for word.)")

REQUEST = """Stop working for a moment: summarise this conversation for yourself. The summary will replace
the conversation, and you will continue from it plus the last few messages. Do not call any tools.
Write Markdown with these sections:
## Task
What the user wants, in their own words where the wording matters.
## User's decisions and preferences
## Done so far
Files changed (paths) and why, commands run and what they showed.
## Current state
What works, what fails (exact error text), what is in progress.
## Next steps
## Key facts
Paths, names, values and short snippets you will need again.
Be complete but concise: facts, not narration."""


def is_summary(msg: dict) -> bool:
    return msg.get("role") == "user" and str(msg.get("content") or "").startswith(HEADER)


def keep_chars(window: int) -> int:
    return min(KEEP_CHARS, int(window * KEEP_SHARE)) if window else KEEP_CHARS


def split_point(messages: list[dict], limit: int = KEEP_CHARS) -> int:
    """Index where the kept tail starts: the earliest user/assistant message such that
    everything from it on fits in ``limit`` characters. len(messages) keeps nothing."""
    cut, size = len(messages), 0
    for i in range(len(messages) - 1, -1, -1):
        size += _chars(messages[i])
        if size > limit:
            break
        if messages[i]["role"] in ("user", "assistant"):
            cut = i
    return cut


def _chars(msg: dict) -> int:
    calls = msg.get("tool_calls")
    return len(str(msg.get("content") or "")) + (len(json.dumps(calls)) if calls else 0)


class Compactor:
    """Compacts an agent's conversation: by itself once the context is ``at`` % full (between
    steps and before a turn; not during phased work, which hands off instead), or on `/compact`."""

    def __init__(self, agent: Agent, at: int = DEFAULT_AT):
        self.agent, self.at = agent, at
        self.failed = False  # an automatic try failed in this turn: don't retry every step
        agent.compactor = self

    def new_turn(self) -> None:
        self.failed = False

    def due(self) -> bool:
        return self._enabled() and ContextUse(self.agent.tokens, self.agent.config.context_window).pct >= self.at

    def _enabled(self) -> bool:
        """On, not in phased work (it hands off instead), and no failed try in this turn."""
        return bool(self.at) and self.agent.budget is None and not self.failed

    def turn(self, focus: str = "") -> Iterator[Event]:
        """`/compact`: compaction on its own, ending the turn."""
        self.agent.start_turn()
        yield from self.run(focus)
        yield TurnEnd("interrupted" if self.agent.cancelled else "done")

    def run(self, focus: str = "") -> Iterator[Event]:
        """Replace the conversation by the model's summary of it plus the latest messages.
        On failure or Esc the conversation stays as it was."""
        agent = self.agent
        if len(agent.messages) < 2:
            yield Notice("Nothing to compact yet")
            return
        yield Notice("Compacting the conversation (the model summarises it; Esc keeps it as it is)")
        tokens, self.failed = agent.tokens, True
        try:
            ask = REQUEST + (f"\nPay special attention to: {focus}" if focus else "")
            summary = yield from self._summary([*agent.messages, {"role": "user", "content": ask}])
        except TiinyError as e:
            summary, problem = "", f"failed: {e}"
        else:
            problem = "cancelled" if agent.cancelled else "" if summary else "failed: the model wrote no summary"
        if problem:
            agent.tokens = tokens
            yield Notice(f"Compaction {problem}; the conversation stays as it was")
            return
        yield self._replace(summary)

    def _replace(self, summary: str) -> Compacted:
        agent = self.agent
        before = len(agent.messages)
        kept = agent.messages[split_point(agent.messages, keep_chars(agent.config.context_window)):]
        agent.transcript.replace([{"role": "user", "content": f"{HEADER}\n{INTRO}\n\n{summary}"}, *kept])
        agent.reset_context()
        self.failed = False
        return Compacted(summary, before, len(kept))

    def _summary(self, convo: list[dict]) -> Iterator[Event]:
        """Stream the summary, showing only progress (its text is not a chat message); returns it."""
        stream, chars = self.agent.stream(convo), 0
        while True:
            try:
                ev = next(stream)
            except StopIteration as done:
                return (done.value.get("content") or "").strip()
            if isinstance(ev, TextDelta):
                chars += len(ev.text)
                yield SummaryProgress(chars)
            elif isinstance(ev, PromptProgress):
                yield ev
