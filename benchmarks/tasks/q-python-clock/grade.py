"""Grade: the hidden reservation tests, plus the project's own tests (kept, extended by three, passing)."""
import re
import subprocess
import sys
from pathlib import Path

from grading import Score, pytest_score

START = 2  # test functions in the starting repo
repo = Path(sys.argv[1])
score = pytest_score(repo)
own = Score()
tests = list((repo / "tests").glob("test_*.py"))
count = sum(len(re.findall(r"^\s*(?:async\s+)?def test_", t.read_text(), re.M)) for t in tests)
own.check(count >= START + 3, f"the project has at least three new tests ({count - START})")
run = subprocess.run([sys.executable, "-m", "pytest", "-q", "-p", "no:cacheprovider", "tests"], cwd=repo,
                     capture_output=True, text=True, timeout=300)
own.check(run.returncode == 0, "the project's own tests pass")
score.add(own).report()
