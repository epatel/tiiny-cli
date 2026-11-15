"""Grade answers.md against the key (answer + sources per question) and check summary.md."""
import re
import sys
from pathlib import Path

from grading import Score, normalise, read, words

# question: (patterns that must match the normalised answer, patterns that must not, files at least one of which is cited)
KEY = {
    1: ([r"\b1\.38\b"], [r"\b1\.42\b", r"\b1\.5\b"], ["memo-2024-02-corrections.md"]),
    2: ([r"\b3\b|three"], [r"\b4\b|\b6\b"], ["board-minutes-2024-04.md"]),
    3: ([r"\b5\b|five"], [r"\b6\b"], ["board-minutes-2024-04.md"]),
    4: ([r"\b35\b"], [r"\b20\b"], ["annual-report-2023.md", "board-minutes-2024-04.md"]),
    5: ([r"not stated|not given|unknown|not listed"], [r"\b\d+\b"], []),
}


def parse(text: str) -> dict[int, tuple[str, str]]:
    found = {}
    for m in re.finditer(r"^\s*\**Q(\d+)\**\s*[:.]\s*(.*?)\s*$(?:\s*^\s*\**Sources?\**\s*:\s*(.*?)\s*$)?", text, re.M | re.I):
        found[int(m.group(1))] = (m.group(2), m.group(3) or "")
    return found


repo = Path(sys.argv[1])
answers = parse(read(repo / "answers.md"))
score = Score()
for q, (must, must_not, cited) in KEY.items():
    answer, sources = answers.get(q, ("", ""))
    norm = normalise(answer)
    ok = bool(answer) and all(re.search(p, norm) for p in must) and not any(re.search(p, norm) for p in must_not)
    score.check(ok, f"Q{q} answer ({answer or 'missing'})")
    if cited:
        score.check(any(c in sources for c in cited), f"Q{q} cites {' or '.join(cited)}")
score.check("annual-report-2023.md" in answers.get(2, ("", ""))[1], "Q2 cites the annual report too")

summary = read(repo / "summary.md")
n = words(summary)
score.check(100 <= n <= 250, f"summary.md is 100-250 words ({n})")
score.check("1.38" in summary, "summary.md uses the corrected 2023 passengers")
score.check(bool(re.search(r"disagree|discrepan|conflict|correct|inconsisten|unreliable", summary, re.I)), "summary.md discusses the disagreements")
score.report()
