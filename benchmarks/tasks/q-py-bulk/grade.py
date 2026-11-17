"""Grade: the hidden bulk-discount tests, plus the project's own tests (kept, extended, passing)."""
import re
import subprocess
import sys
from pathlib import Path

from grading import Score, pytest_score

START = 4  # test functions in the starting repo
repo = Path(sys.argv[1])
score = pytest_score(repo)
own = Score()
count = sum(len(re.findall(r"^\s*def test_", t.read_text(), re.M)) for t in (repo / "tests").glob("test_*.py"))
own.check(count > START, f"the project has new tests ({count - START})")
run = subprocess.run([sys.executable, "-m", "pytest", "-q", "-p", "no:cacheprovider", "tests"], cwd=repo,
                     capture_output=True, text=True, timeout=300)
own.check(run.returncode == 0, "the project's own tests pass")
score.add(own).report()
