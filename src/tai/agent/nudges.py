"""Nudges: the loop steps in where the model is known to go wrong, instead of asking it in the prompt.

Seen in the benchmarks (2026-11-17, Qwen3.6-35B-A3B-Turbo):

- **Announced, then stopped.** A reply like "Let me start by exploring the project." with no tool call
  ends the turn (no tool calls = done), so the work never starts. ``after_reply`` returns a short
  "continue" message instead, at most ``MAX_CONTINUES`` times per turn.
- **Fix loops.** The same failing command (usually the tests) run again and again with an edit in
  between: 15–58 min runs with 20–100 test runs. Prompt lines asking the model to stop after a few
  failures did nothing. ``after_tool`` appends a note to the result of the ``FIX_LOOP``-th failing run
  of the same bash command in a row (and every ``FIX_LOOP`` after), quoting the task again.
- **Explicit requirements lost to habit** (optional, ``AgentConfig.recheck``). The task says "round per
  line" or "call ``clock.now()`` through the module", the model writes the common idiom instead, and a
  prompt line asking it to re-check did nothing. When a turn that changed files is about to end,
  ``before_done`` returns the task split into numbered requirements (``requirements``) and asks for
  each one: done where, or missing and then fixed. Once per turn.
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


RECHECK = """Before you finish, check your work against the task's requirements below, one at a time. Read the code \
you changed again; don't answer from memory. For each requirement, write its number and either "ok: <file and \
function where it's done>" or "missing: <what>". Then fix every missing one and run the tests again. If all are \
ok, say so in one line.

{items}"""
MAX_ITEMS = 30
_BULLET = re.compile(r"^\s*(?:[-*+]|\d+[.)])\s+")
_SENTENCE_END = re.compile(r"(?<=[.!?])\s+(?=[A-Z`\"(])")


def requirements(task: str) -> list[str]:
    """The task as a list of requirements: each bullet, and each sentence of the other paragraphs.
    Headings and very short fragments are left out."""
    items: list[str] = []
    for block in re.split(r"\n\s*\n", task):
        for line in _lines(block):
            parts = [_BULLET.sub("", line)] if _BULLET.match(line) else _SENTENCE_END.split(line)
            items += [p.strip() for p in parts if len(p.split()) >= 4]
    return items[:MAX_ITEMS]


def _lines(block: str) -> list[str]:
    """A paragraph joined into one line, or its lines when it is a list (bullets stay apart)."""
    lines = [ln.strip() for ln in block.strip().splitlines() if ln.strip() and not ln.lstrip().startswith("#")]
    if any(_BULLET.match(ln) for ln in lines):
        return lines
    return [" ".join(lines)] if lines else []


def recheck_message(task: str) -> str | None:
    """The requirements check for ``task``, or None when it has fewer than two requirements."""
    items = requirements(task)
    if len(items) < 2:
        return None
    return RECHECK.format(items="\n".join(f"{n}. {item}" for n, item in enumerate(items, 1)))


def _failed(result: str) -> bool:
    m = _EXIT.match(result)
    return bool(m) and m.group(1) != "0"


@dataclass
class Nudges:
    task: str = ""
    continues: int = 0
    streak: dict = field(default_factory=lambda: {"command": "", "count": 0})
    edited: bool = False  # a write or edit ran in this turn
    rechecked: bool = False

    def new_turn(self, task: str) -> None:
        self.task, self.continues = task, 0
        self.streak = {"command": "", "count": 0}
        self.edited = self.rechecked = False

    def before_done(self) -> str | None:
        """The requirements check, once per turn and only after files were changed; else None."""
        if self.rechecked or not self.edited:
            return None
        self.rechecked = True
        return recheck_message(self.task)

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
        self.edited = self.edited or name in ("write", "edit")
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
