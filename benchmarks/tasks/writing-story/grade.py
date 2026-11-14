"""Grade the brief's checkable rules (files, lengths, required sentence, compass, cast, no repeated
sentences). Writing quality is scored separately by the optional model judge (run.py --judge)."""
import re
import sys
from pathlib import Path

from grading import Score, read, words

FIRST = "The light on Vell Point had not failed in forty years."

repo = Path(sys.argv[1])
story = repo / "story"
chapters = [read(story / f"{i:02d}.md") for i in range(1, 6)]
score = Score()


def body(text: str) -> str:
    return text.split("\n", 1)[1] if "\n" in text else ""


def sentences(text: str) -> set[str]:
    return {s.strip().lower() for s in re.split(r"(?<=[.!?])\s+", text) if len(s.split()) >= 6}


for i, text in enumerate(chapters, 1):
    if not text:
        score.add(Score(0, 5, [f"failed: story/{i:02d}.md is missing (5 checks)"]))
        continue
    score.check(re.match(rf"# Chapter {i}: \S", text) is not None, f"chapter {i} heading")
    n = words(body(text))
    score.check(700 <= n <= 1200, f"chapter {i} is 700-1200 words ({n})")
    score.check("compass" in text.lower(), f"the compass appears in chapter {i}")
    score.check("Ilse" in text, f"Ilse appears in chapter {i}")
    score.check(bool(re.search(r"[\"“].+?[\"”]", text)), f"chapter {i} has dialogue")

score.check(body(chapters[0]).strip().startswith(FIRST), "the first sentence is exact")
score.check(FIRST in chapters[4], "the first sentence returns in chapter 5")

cast = re.findall(r"^\s*[-*]\s*\**([A-Z][\w'’-]*(?: [A-Z][\w'’-]*)*)\**\s*[:—-]", read(story / "characters.md"), re.M)
score.check(bool(cast) and cast[0].startswith("Ilse"), "characters.md lists Ilse Marrow first")
score.check(len(cast) >= 4, f"at least three named characters besides Ilse ({max(len(cast) - 1, 0)})")
text_all = "\n".join(chapters)
missing = [c for c in cast if c.split()[0] not in text_all]
score.check(bool(cast) and not missing, f"every listed character appears in the story (missing: {missing})")

seen, repeated = {}, set()
for i, text in enumerate(chapters):
    for s in sentences(body(text)) - {FIRST.lower()}:
        if s in seen and seen[s] != i:
            repeated.add(s[:60])
        seen.setdefault(s, i)
score.check(not repeated, f"no sentences repeated between chapters ({len(repeated)})")
score.report()
