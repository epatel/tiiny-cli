"""Grade answers.md against the key (answer + sources per question) and check report.md."""
import re
import sys
from pathlib import Path

from grading import Score, normalise, read, words

# question: (patterns that must match the normalised answer, patterns that must not, files at least one of which is cited)
KEY = {
    1: ([r"\b16\b"], [], ["2023-annual-report.md"]),
    2: ([r"\b3\.25\b"], [r"\b3\.4\b", r"\b3\.5\b"], ["memo-2024-02-corrections.md"]),
    3: ([r"\b71\.2\b"], [r"\b71\.5\b"], ["memo-2024-02-corrections.md"]),
    4: ([r"\b128\.4\b"], [r"\b214\b"], ["press-2023-09-extension.md"]),
    5: ([r"^no\b"], [r"^yes"], ["engineering-note-2024-03-aldersgate.md", "staff-newsletter-2024-summer.md"]),
    6: ([r"\b25\b"], [], ["board-minutes-2024-05.md"]),
    7: ([r"saltreach harbour"], [], ["board-minutes-2024-05.md"]),
    8: ([r"\b4\.8\b"], [r"\b9\.7\b", r"\b12\.9\b"], ["2019-annual-report.md"]),
    9: ([r"not stated|not given|unknown|not named"], [r"voss", r"amsel", r"lund"], []),
    10: ([r"\b76\b"], [r"\b71\b"], ["timetable-2024.md", "board-minutes-2024-05.md"]),
    11: ([r"\b46\b"], [], ["board-minutes-2024-05.md"]),
    12: ([r"\b23\b"], [], ["staff-newsletter-2024-summer.md"]),
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
q8_sources = answers.get(8, ("", ""))[1]
score.check("memo-2024-02-corrections.md" in q8_sources, "Q8 cites the correction memo too")

report = read(repo / "report.md")
n = words(report)
score.check(300 <= n <= 800, f"report.md is 300-800 words ({n})")
score.check("3.25" in report, "report.md uses the corrected 2023 ridership")
score.check(bool(re.search(r"disagree|discrepan|conflict|correct|inconsisten", report, re.I)), "report.md discusses the disagreements")
score.report()
