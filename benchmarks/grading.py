"""Helpers for the tasks' grade.py scripts (stdlib only; run sandboxed, after the agent is done).

A grade.py takes the finished repo as its only argument and ends with ``score.report()``,
which prints one JSON line: {"passed": n, "total": m, "notes": [...]}.
"""

from __future__ import annotations

import json
import re
import shutil
import subprocess
import sys
import tempfile
import xml.etree.ElementTree as ET
from dataclasses import dataclass, field
from pathlib import Path

HERE = Path(__file__).resolve().parent


@dataclass
class Score:
    """Named pass/fail checks, or test results, added up."""
    passed: int = 0
    total: int = 0
    notes: list[str] = field(default_factory=list)

    def check(self, ok: bool, name: str) -> bool:
        self.total += 1
        self.passed += bool(ok)
        if not ok:
            self.notes.append(f"failed: {name}")
        return bool(ok)

    def add(self, other: Score) -> Score:
        self.passed, self.total = self.passed + other.passed, self.total + other.total
        self.notes += other.notes
        return self

    def report(self) -> None:
        print(json.dumps({"passed": self.passed, "total": self.total, "notes": self.notes[:40]}))


@dataclass
class TestRun:
    """Hidden tests to run: copied from this task's hidden/ to ``repo/dest``."""
    repo: Path
    dest: str
    timeout: int = 300

    def install(self) -> Path:
        target = self.repo / self.dest
        if target.exists():
            shutil.rmtree(target)
        shutil.copytree(HERE / "hidden", target)
        return target

    def run(self, cmd: list[str], xml_file: Path) -> Score:
        try:
            proc = subprocess.run(cmd, cwd=self.repo, capture_output=True, text=True, timeout=self.timeout)
        except subprocess.TimeoutExpired:
            return expected(Score(notes=[f"tests timed out after {self.timeout}s"]))
        score = junit_score(xml_file)
        if not xml_file.is_file() or score.total < hidden_tests():
            score.notes.append((proc.stdout + proc.stderr)[-1500:])
        return expected(score)


def hidden_tests() -> int:
    """How many hidden tests the task has (task.json "hidden_tests")."""
    return int(json.loads((HERE / "task.json").read_text()).get("hidden_tests", 0))


def expected(score: Score) -> Score:
    """Count hidden tests that never ran (import or collection errors) as failed."""
    missing = hidden_tests() - score.passed
    if score.total < hidden_tests():
        score.notes.insert(0, f"{missing} hidden tests failed or never ran")
        score.total = hidden_tests()
    return score


def junit_score(xml_file: Path) -> Score:
    """Passed / total test cases (skips not counted) from a JUnit XML report."""
    if not xml_file.is_file():
        return Score(notes=["no test report (did the tests even start?)"])
    score = Score()
    for case in ET.parse(xml_file).getroot().iter("testcase"):
        if case.find("skipped") is None and case.get("name"):
            ok = case.find("failure") is None and case.find("error") is None
            score.check(ok, f"{case.get('classname', '')} {case.get('name')}".strip())
    return score


def pytest_score(repo: Path, dest: str = "tests_hidden") -> Score:
    """Install hidden/ as ``dest`` and run it with pytest."""
    tests = TestRun(repo, dest)
    tests.install()
    xml_file = Path(tempfile.mkdtemp()) / "junit.xml"
    return tests.run([sys.executable, "-m", "pytest", "-q", "-p", "no:cacheprovider", f"--junitxml={xml_file}",
                      dest], xml_file)


def node_score(repo: Path, dest: str = "test_hidden") -> Score:
    """Install hidden/ as ``dest`` and run its *.test.ts / *.test.js files with node --test."""
    tests = TestRun(repo, dest)
    folder = tests.install()
    files = sorted(str(p.relative_to(repo)) for p in folder.iterdir() if re.search(r"\.test\.[jt]s$", p.name))
    xml_file = Path(tempfile.mkdtemp()) / "junit.xml"
    return tests.run(["node", "--test", "--test-reporter=junit", f"--test-reporter-destination={xml_file}", *files],
                     xml_file)


def words(text: str) -> int:
    return len(re.findall(r"[A-Za-z0-9'’-]+", text))


def read(path: Path) -> str:
    return path.read_text(errors="replace") if path.is_file() else ""


def normalise(text: str) -> str:
    return re.sub(r"[^a-z0-9.]+", " ", text.lower()).strip()
