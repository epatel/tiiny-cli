"""Grade: the hidden language and error tests, plus the project's own tests (kept, extended, passing)."""
import re
import subprocess
import sys
from pathlib import Path

from grading import Score, node_score

repo = Path(sys.argv[1])
score = node_score(repo)
own = Score()
tests = list((repo / "tests").glob("*.test.ts"))
count = sum(len(re.findall(r"^\s*test\(", t.read_text(), re.M)) for t in tests)
own.check(count >= 6, f"the project has tests of its own for the new features ({count} tests)")
run = subprocess.run(["node", "--test", *map(str, tests)], cwd=repo, capture_output=True, text=True, timeout=300)
own.check(bool(tests) and run.returncode == 0, "the project's own tests pass")
score.add(own).report()
