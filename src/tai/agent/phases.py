"""Phased work for `tai agent`: one focused phase at a time, each in a fresh context.

A task gets a work folder in the project (``.tai/work/<date>-<slug>/``). The model keeps
``plan.md``, notes, helper tools and tests there; ``state.json`` (written only by this
module) holds the goal, the phases and each finished phase's handoff. A phase starts
with an empty message list and gets everything it knows from that folder, so it can
use the whole context window for its own goal. The first two phases are fixed
(explore, plan); the plan phase defines the rest through ``phase_done(next_phases=…)``.

UI-independent like the loop: the TUI calls ``begin`` to get the prompt for a phase,
runs it as a normal turn, and checks ``finished`` when the turn ends.
"""

from __future__ import annotations

import datetime
import json
import re
from dataclasses import asdict, dataclass, field
from pathlib import Path

from tai.agent.loop import Agent, Budget
from tai.agent.tools import Tool, ToolContext, ToolError

WORK_DIR = Path(".tai") / "work"
STATE = "state.json"
MAX_PLAN = 20_000  # characters of plan.md put into a phase prompt
MAX_HANDOFF = 3_000  # characters per earlier handoff

FIRST_PHASES = (
    ("explore", "Understand the task and everything it touches: read the relevant code, docs, tests and "
                "configuration (and the web for outside APIs). Write what you learn to notes/explore.md: "
                "the files involved and how they work, constraints, risks and open questions. "
                "Do not change project files in this phase."),
    ("plan", "Write plan.md: the goal, the approach, the decisions with their reasons, and the remaining "
             "phases. Each phase gets one focused goal with a concrete deliverable and how to check it. "
             "Where it helps, plan early phases that build tools (scripts in tools/) or tests that later "
             "phases run. Ask the user about real choices before settling them. End with phase_done and "
             "next_phases."),
)

SYSTEM = """
# Phased work
You are working on a larger task in phases. Every phase starts with a fresh context: nothing from
earlier phases is remembered except the work folder and the handoffs in the first message.
- Focus on this phase's goal only, and do it thoroughly. Use as much context as the work needs:
  read the real code instead of guessing.
- Everything a later phase needs must be in a file: plan.md, notes/, tools/ (helper scripts),
  tests/. Writing inside the work folder needs no permission. Change project files only when the
  phase's goal says so.
- Tools and tests you build are for later phases: say in the handoff how to run them.
- When the phase's deliverable is done, call phase_done with a handoff (what was done, what the
  next phase must know, how to check it) and the files that matter. Then stop.
- If you need the user's input, ask and stop; the phase continues after their answer.
- If you are told the context is nearly full, write down what remains and call phase_done now.
"""

WRAP_UP = ("The context is {pct}% full. Write anything still in your head to files in the work folder now, "
           "then call phase_done with a handoff that says what is left.")


@dataclass
class Phase:
    name: str
    goal: str
    status: str = "pending"  # pending | active | done
    handoff: str = ""
    files: list[str] = field(default_factory=list)


@dataclass
class Work:
    folder: Path  # absolute
    goal: str
    phases: list[Phase]
    created: str = ""

    @classmethod
    def create(cls, root: Path, goal: str) -> Work:
        base = root / WORK_DIR / f"{datetime.date.today().isoformat()}-{slug(goal)}"
        folder, n = base, 1
        while folder.exists():
            n += 1
            folder = base.with_name(f"{base.name}-{n}")
        for sub in ("notes", "tools", "tests"):
            (folder / sub).mkdir(parents=True)
        work = cls(folder, goal, [Phase(n, g) for n, g in FIRST_PHASES],
                   datetime.datetime.now().isoformat(timespec="seconds"))
        work.save()
        return work

    @classmethod
    def load(cls, folder: Path) -> Work:
        data = json.loads((folder / STATE).read_text())
        return cls(folder, data["goal"], [Phase(**p) for p in data["phases"]], data.get("created", ""))

    @classmethod
    def latest(cls, root: Path) -> Work | None:
        states = sorted((root / WORK_DIR).glob(f"*/{STATE}"), key=lambda p: p.stat().st_mtime)
        return cls.load(states[-1].parent) if states else None

    def save(self) -> None:
        data = {"goal": self.goal, "created": self.created, "phases": [asdict(p) for p in self.phases]}
        (self.folder / STATE).write_text(json.dumps(data, indent=2) + "\n")

    @property
    def index(self) -> int | None:
        """The phase to work on: the first one not done; None when all are done."""
        return next((i for i, p in enumerate(self.phases) if p.status != "done"), None)

    @property
    def current(self) -> Phase | None:
        i = self.index
        return None if i is None else self.phases[i]


def _set_next_phases(work: Work, upcoming: list | None) -> None:
    """Replace the phases after the current one; the plan phase must give them."""
    i = work.index
    if upcoming is None:
        if work.phases[i].name == "plan" and i == len(work.phases) - 1:
            raise ToolError("The plan phase must list the remaining phases in next_phases.")
        return
    if not all(isinstance(p, dict) and p.get("name") and p.get("goal") for p in upcoming):
        raise ToolError("next_phases: every phase needs a name and a goal")
    work.phases[i + 1:] = [Phase(str(p["name"]), str(p["goal"])) for p in upcoming]


def slug(text: str, limit: int = 40) -> str:
    words = re.sub(r"[^a-z0-9]+", "-", text.lower()).strip("-")
    return words[:limit].rstrip("-") or "task"


class PhaseRunner:
    """Drives phased work on an ``Agent``: fresh context per phase, the ``phase_done`` tool,
    writes inside the work folder without asking, and the context budget."""

    def __init__(self, agent: Agent, root: Path):
        self.agent, self.root = agent, root
        self.work: Work | None = None
        self.finished = False  # the current phase called phase_done
        self.auto = False  # start the next phase without asking ("always" at the gate)
        self._base_system = agent.config.system

    @property
    def active(self) -> bool:
        return self.work is not None

    # -- lifecycle ---------------------------------------------------------
    def start(self, goal: str) -> Work:
        self._attach(Work.create(self.root, goal))
        return self.work

    def resume(self, work: Work | None = None) -> Work | None:
        work = work or Work.latest(self.root)
        if work is not None:
            self._attach(work)
        return work

    def stop(self) -> None:
        """Leave phased mode; the work folder stays."""
        self.work = None
        self.agent.config.system = self._base_system
        self.agent.config.tools.pop("phase_done", None)
        self.agent.auto_allow = None
        self.agent.budget = None

    def _attach(self, work: Work) -> None:
        self.work, self.finished = work, False
        rel = self.rel(work.folder)
        self.agent.config.system = self._base_system + SYSTEM + f"- Work folder: {rel}/\n"
        self.agent.config.tools["phase_done"] = self._tool()
        self.agent.auto_allow = self._in_work_folder
        self.agent.budget = Budget(wrap_up=WRAP_UP)

    def begin(self) -> str:
        """Fresh context for the current phase; returns the first message to send."""
        work = self._need_work()
        phase = work.current
        if phase is None:
            raise ToolError("All phases are done.")
        phase.status = "active"
        work.save()
        self.finished = False
        self.agent.clear()
        return self.prompt()

    # -- prompts -----------------------------------------------------------
    def label(self) -> str:
        """e.g. `phase 2/4 · plan`; empty when not in phased mode or all done."""
        if self.work is None or self.work.index is None:
            return ""
        return f"phase {self.work.index + 1}/{len(self.work.phases)} · {self.work.current.name}"

    def prompt(self) -> str:
        work = self._need_work()
        i, phase = work.index, work.current
        rel = self.rel(work.folder)
        parts = [f"# Phase {i + 1}/{len(work.phases)}: {phase.name}",
                 f"Task: {work.goal}", f"This phase: {phase.goal}",
                 f"Work folder: {rel}/ (plan.md, notes/, tools/, tests/)"]
        done = [p for p in work.phases[:i] if p.status == "done"]
        if done:
            parts.append("## Earlier phases")
            for p in done:
                files = f"\nFiles: {', '.join(p.files)}" if p.files else ""
                parts.append(f"### {p.name}\n{p.handoff[:MAX_HANDOFF]}{files}")
        later = work.phases[i + 1:]
        if later:
            parts.append("## Later phases\n" + "\n".join(f"- {p.name}: {p.goal}" for p in later))
        plan = work.folder / "plan.md"
        if plan.is_file():
            parts.append(f"## plan.md\n{plan.read_text(errors='replace')[:MAX_PLAN]}")
        parts.append("Start by reading the files the handoffs point to that this phase needs.")
        return "\n\n".join(parts)

    def status(self) -> str:
        """Markdown overview of the phases."""
        work = self._need_work()
        marks = {"done": "✓", "active": "▶", "pending": "·"}
        rows = [f"- {marks[p.status]} **{p.name}** — {p.goal}" for p in work.phases]
        return f"**{work.goal}**  (`{self.rel(work.folder)}/`)\n\n" + "\n".join(rows)

    def rel(self, path: Path) -> str:
        return str(path.relative_to(self.root)) if self.root in path.parents else str(path)

    # -- the phase_done tool -----------------------------------------------
    def _tool(self) -> Tool:
        return Tool(
            "phase_done",
            "End the current phase. Call it once the phase's deliverable is complete and written to files.",
            {"handoff": {"type": "string", "description": "What was done, what the next phase must know, how "
                                                          "to run the tools/tests made, what is left."},
             "files": {"type": "array", "items": {"type": "string"},
                       "description": "Files a later phase should read (work folder or project)."},
             "next_phases": {"type": "array", "description": "Replaces the phases after this one (the plan "
                                                             "phase must give them).",
                             "items": {"type": "object", "required": ["name", "goal"],
                                       "properties": {"name": {"type": "string"}, "goal": {"type": "string"}}}}},
            self._done, required=["handoff"])

    def _done(self, args: dict, ctx: ToolContext) -> str:
        work = self._need_work()
        phase = work.current
        if phase is None:
            raise ToolError("All phases are already done.")
        _set_next_phases(work, args.get("next_phases"))
        phase.status, phase.handoff = "done", str(args["handoff"])
        phase.files = [str(f) for f in args.get("files") or []]
        work.save()
        self.finished = True
        self.agent.end_turn("phase_done")
        nxt = work.current
        return f"Phase '{phase.name}' recorded. " + (f"Next: {nxt.name}. Stop now." if nxt else "All phases done.")

    # -- helpers -----------------------------------------------------------
    def in_progress(self) -> bool:
        """The current phase has started in this context and hasn't called phase_done yet."""
        phase = self.work.current if self.work else None
        started = phase is not None and phase.status == "active"
        return started and not self.finished and bool(self.agent.messages)

    def _in_work_folder(self, tool: Tool, args: dict) -> bool:
        path = args.get("path")
        if self.work is None or tool.name not in ("write", "edit"):
            return False
        return isinstance(path, str) and self.work.folder in self.agent.config.ctx.resolve(path).parents

    def _need_work(self) -> Work:
        if self.work is None:
            raise ToolError("No phased work is active.")
        return self.work
