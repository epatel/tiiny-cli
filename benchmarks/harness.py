"""Run one benchmark task with `tai agent`'s loop, headless, in plain or phased mode, and grade it.

No UI and no device code here: ``run_task`` takes a chat client (the device's, or a fake one in
tests), so the whole flow is unit-tested. Tool calls are approved automatically, with limits:
- bash runs under macOS ``sandbox-exec``: it may write only in the run folder, has no network,
  and cannot read the benchmark tasks (where the hidden tests are);
- read/grep/glob/ls and write/edit refuse paths outside the run's repo.
"""

from __future__ import annotations

import json
import math
import shutil
import subprocess
import sys
import threading
import time
from collections.abc import Iterator
from dataclasses import asdict, dataclass, field, replace
from pathlib import Path

from tai.agent.compact import Compactor
from tai.agent.loop import Agent, AgentConfig, Decision, Event, Notice, TurnEnd
from tai.agent.phases import PhaseRunner
from tai.agent.prompt import system_prompt
from tai.agent.tools import TOOLS, Tool, ToolContext, ToolError
from process import measure
from transcript import MeteredClient
from variants import Variant

BENCH_DIR = Path(__file__).resolve().parent
TASKS_DIR = BENCH_DIR / "tasks"
MODES = ("plain", "phased")  # the default variants

HEADLESS = """
# Benchmark run
No user is available during this run. Don't ask questions or wait for confirmation: make reasonable
choices, note them, and finish the whole task on your own. Network access is off.
"""
NUDGE_PHASE = ("No user is available. Continue the phase on your own, and call phase_done when its "
               "deliverable is written to files.")
NUDGE_STEPS = "Continue."
CHECKLIST = ("\n\nBefore you start, write the task's requirements as a short checklist (at most 20 lines) in "
             "your reply. Then do the task.")
REVIEW = ("Review your work: go through your checklist and the task item by item against the files you wrote "
          "(read them, and run the tests if there are any). Fix anything missing or wrong. Then reply with "
          "the checklist, each item marked done.")

SANDBOX = """(version 1)
(allow default)
(deny network*)
(deny file-read* (subpath (param "TASKS")))
(deny file-write*)
(allow file-write* (subpath (param "ROOT")) (subpath (param "TMP"))
       (literal "/dev/null") (literal "/dev/zero") (regex #"^/dev/tty") (regex #"^/dev/fd/"))
"""
CONFINED = ("read", "grep", "glob", "ls")


@dataclass
class Task:
    name: str
    folder: Path
    prompt: str
    domain: str = ""
    judge: dict | None = None  # {"files": [...globs], "rubric": [...criteria]} for an optional model judge
    suite: str = "full"  # full | quick (small tasks for iterating)
    deliverables: list[str] = field(default_factory=list)  # globs of the files the task asks for

    @classmethod
    def load(cls, folder: Path) -> Task:
        meta = json.loads((folder / "task.json").read_text())
        prompt = (folder / "task.md").read_text()
        return cls(folder.name, folder, prompt, meta.get("domain", ""), meta.get("judge"), meta.get("suite", "full"),
                   meta.get("deliverables", []))


def all_tasks(suite: str | None = None) -> list[Task]:
    """Every task, or those of one suite."""
    tasks = [Task.load(d) for d in sorted(TASKS_DIR.iterdir()) if (d / "task.json").is_file()]
    return [t for t in tasks if suite is None or t.suite == suite]


@dataclass
class Limits:
    max_phases: int = 12  # explore + plan + up to 10 planned phases
    nudges: int = 2  # "continue" messages after a turn stops early (as a user would type)
    timeout: float = 3600.0  # seconds for the whole run; then the turn is cancelled (0 = no limit)
    compact: int = 85  # plain mode compacts like `tai agent` does by default (0 = off)


@dataclass
class RunResult:
    task: str
    mode: str
    run: int
    score: float = 0.0  # share of the grader's checks that passed
    passed: int = 0
    total: int = 0
    notes: list[str] = field(default_factory=list)
    requests: int = 0
    prompt_tokens: int = 0
    completion_tokens: int = 0
    seconds: float = 0.0
    phases: list[str] = field(default_factory=list)
    ends: list[str] = field(default_factory=list)  # TurnEnd reason of every turn
    timed_out: bool = False  # cut off by the time limit: the score is for unfinished work
    model: str = ""
    output_by: dict[str, int] = field(default_factory=dict)  # written tokens per reply kind (tool name or text)
    process: dict = field(default_factory=dict)  # process.Process: overhead, rewrites, strays, health, ...
    write_speed: float = 0.0  # tokens/s while writing, from the device's timings
    device_errors: int = 0  # requests the device broke off or answered with nothing (no usage)
    judge: dict | None = None

    @property
    def errored(self) -> bool:
        """The device failed during the run (not the time limit): the score says nothing about the agent."""
        return bool(self.device_errors or "error" in self.ends) and not self.timed_out

    def to_json(self) -> str:
        return json.dumps(asdict(self), ensure_ascii=False)

    @classmethod
    def from_json(cls, line: str) -> RunResult:
        """Older results have no ``timed_out``; a cancelled last turn there means the time limit."""
        row = json.loads(line)
        row.setdefault("timed_out", row.get("ends", [""])[-1:] == ["interrupted"])
        return cls(**row)


# -- sandbox and confinement ------------------------------------------------------------
def sandbox_prefix(root: Path, tmp: Path, hidden: Path = TASKS_DIR) -> tuple[str, ...]:
    """sandbox-exec + env for a command that may write only under ``root`` and ``tmp``, and
    can't read ``hidden`` (the tasks, with their hidden tests and graders)."""
    if not shutil.which("sandbox-exec"):
        raise SystemExit("The benchmarks need macOS sandbox-exec to run the agent's commands safely.")
    params = {"ROOT": root, "TMP": tmp, "TASKS": hidden}
    defs = [a for k, v in params.items() for a in ("-D", f"{k}={Path(v).resolve()}")]
    venv_bin = str(Path(sys.executable).parent)
    return ("sandbox-exec", *defs, "-p", SANDBOX, "/usr/bin/env", f"TMPDIR={tmp.resolve()}",
            f"PATH={venv_bin}:/usr/local/bin:/opt/homebrew/bin:/usr/bin:/bin", "PYTHONDONTWRITEBYTECODE=1")


def _confined(tool: Tool) -> Tool:
    def run(args: dict, ctx: ToolContext) -> str:
        if not ctx.inside(ctx.resolve(str(args.get("path") or "."))):
            raise ToolError("Outside the project: this benchmark run may only look inside the project root.")
        return tool.run(args, ctx)
    return replace(tool, run=run)


def bench_tools() -> dict[str, Tool]:
    return {name: _confined(t) if name in CONFINED else t for name, t in TOOLS.items()}


def approve(tool: Tool, args: dict, preview) -> Decision:
    """Allow everything the sandbox covers; refuse writes outside the project."""
    return Decision.DENY if getattr(preview, "outside", False) else Decision.ALLOW


# -- running ----------------------------------------------------------------------------
@dataclass
class Setup:
    """What a run needs besides the task: the client and the model."""
    client: object
    model_id: str
    model_name: str
    context_window: int = 0
    extra: dict = field(default_factory=dict)
    limits: Limits = field(default_factory=Limits)
    log: object = print  # progress lines


@dataclass
class Run:
    """One run of a task with a variant (``mode`` is its name), in its own folder."""
    task: Task
    mode: str
    folder: Path
    number: int = 1
    variant: Variant | None = None  # default: the built-in variant called ``mode``

    @property
    def spec(self) -> Variant:
        return self.variant or Variant.load(self.mode)


def prepare(task: Task, folder: Path) -> tuple[Path, Path]:
    """Fresh copy of the task's starting repo in ``folder``; returns (repo, tmp)."""
    repo, tmp = folder / "repo", folder / "tmp"
    if folder.exists():
        shutil.rmtree(folder)
    shutil.copytree(task.folder / "repo", repo)
    tmp.mkdir(parents=True)
    return repo.resolve(), tmp.resolve()


def make_agent(setup: Setup, repo: Path, tmp: Path, wrap: tuple[str, ...]) -> tuple[Agent, MeteredClient]:
    client = MeteredClient(setup.client, repo.parent / "requests.jsonl")
    config = AgentConfig(setup.model_id, system_prompt(repo, setup.model_name) + HEADLESS, ToolContext(repo, wrap=wrap), extra=setup.extra,
                         tools=bench_tools(), context_window=setup.context_window)
    return Agent(client, config, approve), client


class Driver:
    """Runs turns, records how each ended, and stops everything at the time limit."""

    def __init__(self, agent: Agent, limits: Limits, log):
        self.agent, self.limits, self.log = agent, limits, log
        self.ends: list[str] = []
        self.base_extra = dict(agent.config.extra)
        self.deadline = time.monotonic() + limits.timeout if limits.timeout else math.inf
        self.timer = threading.Timer(limits.timeout, agent.cancel) if limits.timeout else None
        if self.timer:
            self.timer.daemon = True
            self.timer.start()

    @property
    def expired(self) -> bool:
        return time.monotonic() >= self.deadline

    def turn(self, events: Iterator[Event]) -> str:
        end = TurnEnd("done")
        for ev in events:
            if isinstance(ev, Notice):
                self.log(f"  • {ev.text}")
            if isinstance(ev, TurnEnd):
                end = ev
        self.ends.append(end.reason)
        if end.reason == "error":
            self.log(f"  error: {end.error}")
        return end.reason

    def nudged(self, first: str, retry: set[str], nudge: str) -> str:
        """Run ``first``; while a turn ends with a reason in ``retry``, send ``nudge`` (limited)."""
        reason = self.turn(self.agent.run(first))
        for _ in range(self.limits.nudges):
            if reason not in retry or self.expired:
                break
            reason = self.turn(self.agent.run(nudge))
        return reason

    def stage(self, variant: Variant, stage: str) -> None:
        """Switch thinking for the next turns as the variant says for ``stage``."""
        self.agent.config.extra = variant.extra(self.base_extra, stage)

    def close(self) -> None:
        if self.timer:
            self.timer.cancel()


def run_plain(agent: Agent, task: Task, driver: Driver, variant: Variant) -> list[str]:
    """Plain mode, or checklist mode: a checklist first, then the work, then a review turn."""
    checklist = variant.mode == "checklist"
    driver.stage(variant, "work")
    driver.nudged(task.prompt + (CHECKLIST if checklist else ""), {"max_steps"}, NUDGE_STEPS)
    if not checklist or driver.expired:
        return []
    driver.stage(variant, "review")
    driver.nudged(REVIEW, {"max_steps"}, NUDGE_STEPS)
    return ["work", "review"]


def run_phased(agent: Agent, task: Task, driver: Driver, variant: Variant) -> list[str]:
    runner = PhaseRunner(agent, agent.config.ctx.root)
    runner.agent.config.tools.pop("start_phases", None)
    runner.auto = True
    work = runner.start(task.prompt)
    done: list[str] = []
    while _more_phases(work, done, driver):
        name = work.current.name
        driver.log(f"  ▶ {runner.label()}")
        driver.stage(variant, name)
        reason = driver.nudged(runner.begin(), {"done", "max_steps"}, NUDGE_PHASE)
        if reason != "phase_done":
            done.append(f"{name} ({reason})")
            break
        done.append(name)
        if name == variant.stop_after:
            break
    return done


def _more_phases(work, done: list[str], driver: Driver) -> bool:
    within = len(done) < driver.limits.max_phases and not driver.expired
    return work.current is not None and within


def run_task(run: Run, setup: Setup) -> RunResult:
    """Run, then grade; returns the result."""
    task, variant = run.task, run.spec
    repo, tmp = prepare(task, run.folder)
    agent, client = make_agent(setup, repo, tmp, sandbox_prefix(repo, tmp, task.folder.parent))
    agent.config.system += variant.prompt
    if variant.mode != "phased" and setup.limits.compact:
        Compactor(agent, setup.limits.compact)
    driver = Driver(agent, setup.limits, setup.log)
    start = time.monotonic()
    try:
        phases = (run_phased if variant.mode == "phased" else run_plain)(agent, task, driver, variant)
    finally:
        driver.close()
    result = RunResult(task.name, run.mode, run.number, requests=client.requests, prompt_tokens=client.prompt_tokens,
                       completion_tokens=client.completion_tokens, seconds=round(time.monotonic() - start, 1),
                       phases=phases, ends=driver.ends, timed_out=driver.expired, model=setup.model_name,
                       output_by=dict(client.output_by.most_common()), write_speed=client.write_speed,
                       device_errors=client.broken)
    result.process = measure(task, repo, client.path).to_dict()  # before grading installs the hidden tests
    grade(task, repo, tmp, result)
    return result


# -- grading ----------------------------------------------------------------------------
def grade(task: Task, repo: Path, tmp: Path, result: RunResult) -> None:
    """Run the task's grade.py on the finished repo, sandboxed (it runs the agent's code).

    The sandbox hides the tasks folder, so grade.py, the hidden tests and grading.py are
    copied next to the run first."""
    staged = tmp / "grader"
    shutil.copytree(task.folder, staged, ignore=shutil.ignore_patterns("repo"), dirs_exist_ok=True)
    shutil.copy(BENCH_DIR / "grading.py", staged / "grading.py")
    cmd = [*sandbox_prefix(repo, tmp, task.folder.parent), sys.executable, str(staged / "grade.py"), str(repo)]
    try:
        proc = subprocess.run(cmd, capture_output=True, text=True, timeout=600, cwd=staged)
    except subprocess.TimeoutExpired:
        result.notes = ["grader timed out"]
        return
    try:
        report = json.loads(proc.stdout.strip().splitlines()[-1])
    except (json.JSONDecodeError, IndexError):
        result.notes = ["grader printed no report", proc.stderr[-1000:]]
        return
    result.passed, result.total = int(report["passed"]), int(report["total"])
    result.score = round(result.passed / result.total, 3) if result.total else 0.0
    result.notes = list(report.get("notes", []))


def check_grader(task: Task, folder: Path) -> dict[str, RunResult]:
    """Grade the untouched repo ("baseline") and the task's solution/ overlay ("solution")."""
    results = {}
    for name in ("baseline", "solution"):
        repo, tmp = prepare(task, folder / name)
        if name == "solution":
            _apply_solution(task, repo)
        results[name] = result = RunResult(task.name, name, 0)
        grade(task, repo, tmp, result)
    return results


def _apply_solution(task: Task, repo: Path) -> None:
    """The task's solution/ folder copied over the repo, or its make_solution.py run on it."""
    if (task.folder / "solution").is_dir():
        shutil.copytree(task.folder / "solution", repo, dirs_exist_ok=True)
    else:
        subprocess.run([sys.executable, str(task.folder / "make_solution.py"), str(repo)], check=True)
