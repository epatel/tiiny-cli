"""Grade: the hidden scope tests, plus the project's own tests (kept, extended by three, passing)."""
import re
import subprocess
import sys
from pathlib import Path

from grading import Score, node_score

START = 6  # tests in the starting repo
repo = Path(sys.argv[1])
score = node_score(repo)
own = Score()
tests = list((repo / "tests").glob("*.test.ts"))
count = sum(len(re.findall(r"^\s*test\(", t.read_text(), re.M)) for t in tests)
own.check(count >= START + 3, f"the project has at least three new tests ({count - START})")
run = subprocess.run(["node", "--test", *map(str, tests)], cwd=repo, capture_output=True, text=True, timeout=300)
own.check(bool(tests) and run.returncode == 0, "the project's own tests pass")
score.add(own).report()
