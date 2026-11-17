"""Nudges: the loop steps in where the model is known to go wrong, instead of asking it in the prompt.

Seen in the benchmarks (2026-11-17, Qwen3.6-35B-A3B-Turbo):

- **Announced, then stopped.** A reply like "Let me start by exploring the project." with no tool call
  ends the turn (no tool calls = done), so the work never starts. ``after_reply`` returns a short
  "continue" message instead, at most ``MAX_CONTINUES`` times per turn.
- **Fix loops.** The same failing command (usually the tests) run again and again with an edit in
  between: 15–58 min runs with 20–100 test runs. Prompt lines asking the model to stop after a few
  failures did nothing. ``after_tool`` appends a note to the result of the ``FIX_LOOP``-th failing run
  of the same bash command in a row (and every ``FIX_LOOP`` after), quoting the task again.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field

FIX_LOOP = 4  # failing runs of the same command in a row before the note
MAX_CONTINUES = 2  # "continue" messages per turn (a model that keeps announcing is left alone)
TASK_QUOTE = 2000  # characters of the task quoted in the fix-loop note

# The end of a reply that announces work instead of doing it.
_ANNOUNCES = re.compile(r"(?:^|[.!?\n]\s*)(?:let me(?! know)|let's|i'll|i will|i am going to|i'm going to|now i|next,? i|"
                        r"first,? i|i need to)\b[^.!?\n]*[.:!]?\s*$", re.IGNORECASE)
_EXIT = re.compile(r"exit code (\d+)")

CONTINUE = ("You described your next step but didn't call a tool, so nothing happened. "
            "Do it now: call the tool.")
FIX_LOOP_NOTE = """

[tai: this command has now failed {n} times in a row. Another edit-and-rerun is unlikely to help.
Stop and re-read the task below and the failure output above in full. Say in one sentence what the
task requires that the code doesn't do, then make one change for that.]

The task:
{task}"""


def announces(text: str) -> bool:
    """Whether a reply ends by saying what it will do next (e.g. "Let me read the tests.")."""
    return bool(_ANNOUNCES.search(text.strip()[-300:]))


def _failed(result: str) -> bool:
    m = _EXIT.match(result)
    return bool(m) and m.group(1) != "0"


@dataclass
class Nudges:
    task: str = ""
    continues: int = 0
    streak: dict = field(default_factory=lambda: {"command": "", "count": 0})

    def new_turn(self, task: str) -> None:
        self.task, self.continues = task, 0
        self.streak = {"command": "", "count": 0}

    def after_reply(self, msg: dict) -> str | None:
        """A user message to send when the reply announced work without a tool call, else None."""
        if msg.get("tool_calls"):
            self.continues = 0
            return None
        text = msg.get("content") or ""
        if self.continues >= MAX_CONTINUES or not announces(text):
            return None
        self.continues += 1
        return CONTINUE

    def after_tool(self, name: str, args, result: str) -> str:
        """``result``, with the fix-loop note added when this is a fix loop's ``FIX_LOOP``-th failure."""
        if name != "bash" or not isinstance(args, dict):
            return result
        command = " ".join(str(args.get("command", "")).split())
        if not _failed(result):
            self.streak = {"command": "", "count": 0}
            return result
        count = self.streak["count"] + 1 if command == self.streak["command"] else 1
        self.streak = {"command": command, "count": count}
        if count % FIX_LOOP:
            return result
        return result + FIX_LOOP_NOTE.format(n=count, task=self.task[:TASK_QUOTE])
