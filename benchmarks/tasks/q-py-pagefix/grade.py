"""Grade: the hidden pagination tests, plus a regression test that fails on the original code."""
import re
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

from grading import Score, pytest_score

START = 4  # test functions in the starting repo
ORIGINAL = Path(__file__).resolve().parent / "buggy" / "notes"  # = repo/notes (the grader is staged without repo/)
repo = Path(sys.argv[1])
score = pytest_score(repo)
own = Score()


def pytest(cwd: Path) -> int:
    return subprocess.run([sys.executable, "-m", "pytest", "-q", "-p", "no:cacheprovider", "tests"], cwd=cwd,
                          capture_output=True, text=True, timeout=300).returncode


count = sum(len(re.findall(r"^\s*def test_", t.read_text(), re.M)) for t in (repo / "tests").glob("test_*.py"))
own.check(count > START, f"the project has new tests ({count - START})")
own.check(pytest(repo) == 0, "the project's own tests pass")
with tempfile.TemporaryDirectory() as tmp:  # the project's tests on the original, buggy code
    shutil.copytree(repo, tmp, dirs_exist_ok=True, ignore=shutil.ignore_patterns("__pycache__", ".pytest_cache"))
    shutil.rmtree(Path(tmp) / "notes")
    shutil.copytree(ORIGINAL, Path(tmp) / "notes")
    own.check(pytest(Path(tmp)) != 0, "a test of the project's fails on the original code (regression test)")
score.add(own).report()
