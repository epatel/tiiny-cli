"""How a run worked, not only what it scored: measured from its ``requests.jsonl`` and its final repo.

- ``overhead``: the share of written tokens that didn't go into a write or edit of a deliverable
  (the task's ``deliverables`` globs). Writing is the slow part on the device.
- ``rewrites``: ``write`` calls to a file the run had already written (edit would have been cheaper).
- ``strays``: files the run left in the repo that aren't deliverables (scratch scripts, copies).
- ``first_deliverable``: seconds until the first write or edit of a deliverable.
- ``health``: aihealth's score of the finished repo (code tasks only; None without aihealth).
- ``code``: aihealth's score of only the source files the run changed or added (not tests), weighted
  by lines; ``code_ref`` is the same for the task's reference solution. A second grade next to the
  tests: they say whether it works, this says how maintainable the change is. Only meaningful for
  runs that pass (an untouched repo scores well).
- ``delta``: aihealth's verdict on the change itself (``aihealth delta`` of the finished repo against the
  starting one, tests included): improved / unchanged / degraded. ``smells_added`` counts the smells the
  run introduced or made worse, ``smells_fixed`` those it removed or eased, and ``smells`` names the
  added ones. ``code`` scores whole files, so a small change to a file rarely moves it; the delta sees
  each smell the change adds (a leftover debug file, copied logic, a long new function).
"""

from __future__ import annotations

import fnmatch
import functools
import json
import re
import shutil
import subprocess
import sys
import tempfile
from dataclasses import asdict, dataclass, field
from pathlib import Path

IGNORED = (".tai/*", "*/__pycache__/*", "__pycache__/*", ".pytest_cache/*", "node_modules/*", "*.pyc")
HIDDEN_TESTS = ("tests_hidden", "test_hidden")  # where the graders install hidden tests
CODE = ("python", "typescript")
TEST_FILE = re.compile(r"(^|/)(tests?|__tests__)/|(^|/)(test_[^/]*|[^/]*_test\.\w+|[^/]*\.(test|spec)\.\w+|conftest\.py)$")


@dataclass
class Process:
    overhead: float = 0.0
    rewrites: int = 0
    strays: list[str] = field(default_factory=list)
    first_deliverable: float | None = None
    health: float | None = None
    code: float | None = None
    code_ref: float | None = None
    delta: str | None = None
    smells_added: int | None = None
    smells_fixed: int | None = None
    smells: list[str] = field(default_factory=list)

    def to_dict(self) -> dict:
        return asdict(self)


class Deliverables:
    """The task's deliverable globs, matched against paths relative to the repo."""

    def __init__(self, repo: Path, globs: list[str]):
        self.repo, self.globs = repo, globs

    def rel(self, path: str) -> str:
        p = Path(path)
        if p.is_absolute():
            try:
                return str(p.resolve().relative_to(self.repo))
            except ValueError:
                return str(p)
        return str(Path(path))

    def match(self, rel: str) -> bool:
        return any(fnmatch.fnmatch(rel, g) for g in self.globs)


def measure(task, repo: Path, log: Path) -> Process:
    """Process metrics of a finished run of ``task`` (a harness.Task: folder, deliverables, domain)."""
    deliver = Deliverables(repo, task.deliverables)
    found = Process(strays=strays(repo, task.folder / "repo", deliver))
    if log.is_file():
        tally = Tally(deliver)
        for line in log.read_text().splitlines():
            if line.strip():
                tally.add(json.loads(line))
        tally.into(found)
    if task.domain in CODE:
        code_metrics(found, repo, task.folder)
    return found


def code_metrics(found: Process, repo: Path, task_folder: Path) -> None:
    """aihealth on the finished repo: the scan (whole files) and the delta (what the change added)."""
    start = task_folder / "repo"
    report = scan(repo) or {}
    found.health = report.get("score")
    found.code = code_health(report, repo, start)
    found.code_ref = reference(task_folder)
    change = delta(repo, start)
    if change is not None:
        found.delta, found.smells_added, found.smells_fixed, found.smells = summarize_delta(change)


class Tally:
    """Adds up the requests of a run, in order."""

    def __init__(self, deliver: Deliverables):
        self.deliver = deliver
        self.written = self.useful = self.rewrites = 0
        self.elapsed = 0.0
        self.first: float | None = None
        self.seen: set[str] = set()

    def add(self, req: dict) -> None:
        self.elapsed += req.get("seconds") or 0
        tokens = (req.get("usage") or {}).get("completion_tokens") or 0
        self.written += tokens
        paths = _edited(req, self.deliver)
        if any(self.deliver.match(p) for _, p in paths):
            self._deliverable(tokens)
        for name, p in paths:
            self.rewrites += name == "write" and p in self.seen
            self.seen.add(p)

    def _deliverable(self, tokens: int) -> None:
        self.useful += tokens
        if self.first is None:
            self.first = self.elapsed

    def into(self, found: Process) -> None:
        found.overhead = round(1 - self.useful / self.written, 3) if self.written else 0.0
        found.rewrites = self.rewrites
        found.first_deliverable = None if self.first is None else round(self.first, 1)


def _edited(req: dict, deliver: Deliverables) -> list[tuple[str, str]]:
    """(tool, repo-relative path) of every write and edit in the request's reply."""
    calls = (req.get("reply") or {}).get("tool_calls") or []
    out = []
    for call in calls:
        path = _path_arg(call) if call.get("name") in ("write", "edit") else None
        if path:
            out.append((call["name"], deliver.rel(path)))
    return out


def _path_arg(call: dict) -> str | None:
    try:
        path = json.loads(call.get("arguments") or "{}").get("path")
    except json.JSONDecodeError:
        return None
    return path if isinstance(path, str) else None


def strays(repo: Path, start: Path, deliver: Deliverables) -> list[str]:
    """Files added to the repo that aren't deliverables or tool caches."""
    before = {str(p.relative_to(start)) for p in start.rglob("*") if p.is_file()}
    rels = (str(p.relative_to(repo)) for p in sorted(repo.rglob("*")) if p.is_file())
    return [rel for rel in rels if rel not in before and not _expected(rel, deliver)]


def _expected(rel: str, deliver: Deliverables) -> bool:
    """A deliverable, a cache or work-folder file, or a hidden test the grader installed."""
    ignored = any(fnmatch.fnmatch(rel, g) for g in IGNORED)
    return ignored or deliver.match(rel) or rel.split("/")[0] in HIDDEN_TESTS


def health(repo: Path) -> float | None:
    """aihealth's project score for the repo, without the work folder and tool caches."""
    return (scan(repo) or {}).get("score")


def scan(repo: Path) -> dict | None:
    """aihealth's JSON report of the repo (file paths relative to it); None without aihealth."""
    exe = Path(sys.executable).parent / "aihealth"
    if not exe.is_file():
        return None
    with tempfile.NamedTemporaryFile("w", suffix=".toml", delete=False) as cfg:
        cfg.write('[scan]\nexclude = [".tai/*", "node_modules/*", "tests_hidden/*", "test_hidden/*"]\n')
    try:
        proc = subprocess.run([str(exe), "scan", "--format", "json", "--config", cfg.name, str(repo)],
                              capture_output=True, text=True, timeout=300)
        return json.loads(proc.stdout)
    except (subprocess.TimeoutExpired, json.JSONDecodeError):
        return None
    finally:
        Path(cfg.name).unlink(missing_ok=True)


COPY_IGNORE = shutil.ignore_patterns(".tai", "node_modules", "__pycache__", ".pytest_cache", "*.pyc", *HIDDEN_TESTS)
ADDED, EASED = ("new", "worsened"), ("fixed", "improved")
GIT = ["git", "-c", "user.name=bench", "-c", "user.email=bench@localhost", "-c", "commit.gpgsign=false"]


def delta(repo: Path, start: Path) -> dict | None:
    """``aihealth delta`` (JSON) of ``repo`` against ``start``: the starting repo is committed to a
    scratch git repo and the finished one laid over it. None without aihealth or git."""
    exe = Path(sys.executable).parent / "aihealth"
    if not exe.is_file() or not shutil.which("git"):
        return None
    with tempfile.TemporaryDirectory() as tmp:
        work = Path(tmp) / "repo"
        shutil.copytree(start, work, ignore=COPY_IGNORE)
        for args in (["init", "-q"], ["add", "-A"], ["commit", "-q", "--allow-empty", "-m", "start"]):
            subprocess.run([*GIT, *args], cwd=work, check=True, capture_output=True)
        for child in work.iterdir():
            if child.name != ".git":
                shutil.rmtree(child) if child.is_dir() else child.unlink()
        shutil.copytree(repo, work, ignore=COPY_IGNORE, dirs_exist_ok=True)
        try:
            proc = subprocess.run([str(exe), "delta", "-v", "--format", "json", "--repo", str(work)],
                                  capture_output=True, text=True, timeout=300)
            return json.loads(proc.stdout)
        except (subprocess.TimeoutExpired, json.JSONDecodeError):
            return None


def summarize_delta(report: dict) -> tuple[str, int, int, list[str]]:
    """(verdict, smells added or worsened, smells fixed or eased, "path: smell in symbol" per added one)."""
    changes = [c for f in report.get("files", []) for c in f.get("changes", [])]
    added = [c for c in changes if c.get("kind") in ADDED]
    named = [f"{c['path']}: {c['smell']}" + (f" in {c['symbol']}" if c.get("symbol") else "") for c in added]
    return report.get("verdict", "?"), len(added), sum(c.get("kind") in EASED for c in changes), named


def code_health(report: dict, repo: Path, start: Path) -> float | None:
    """Line-weighted score of the non-test files that differ from ``start`` (None if none did)."""
    files = [f for f in report.get("files", []) if not TEST_FILE.search(f["path"]) and _changed(f["path"], repo, start)]
    loc = sum(f["loc"] for f in files)
    return round(sum(f["score"] * f["loc"] for f in files) / loc, 2) if loc else None


def _changed(rel: str, repo: Path, start: Path) -> bool:
    before = start / rel
    return not before.is_file() or before.read_bytes() != (repo / rel).read_bytes()


@functools.lru_cache(maxsize=None)
def reference(folder: Path) -> float | None:
    """``code_health`` of the task's reference solution (its ``solution/`` laid over the repo)."""
    if not (folder / "solution").is_dir():
        return None
    with tempfile.TemporaryDirectory() as tmp:
        repo = Path(tmp) / "repo"
        shutil.copytree(folder / "repo", repo)
        shutil.copytree(folder / "solution", repo, dirs_exist_ok=True)
        report = scan(repo)
        return code_health(report, repo, folder / "repo") if report else None
