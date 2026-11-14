"""Writes a mechanically valid story into the repo given as argument, to check grade.py (not literature)."""
import sys
from pathlib import Path

FIRST = "The light on Vell Point had not failed in forty years."
repo = Path(sys.argv[1])
story = repo / "story"
story.mkdir(exist_ok=True)
for c in range(1, 6):
    lines = [f"# Chapter {c}: Part {c}", FIRST if c in (1, 5) else ""]
    lines += [f'Ilse turned the compass over for the {c}-{i} time and said "again" to the wind at hour {i}.'
              for i in range(60)]
    (story / f"{c:02d}.md").write_text("\n".join(lines) + "\n")
(story / "characters.md").write_text("- Ilse Marrow: keeper\n- Tom Reyes: fisher\n- Ada Venn: doctor\n- Ola Brisk: mayor\n")
for name in ("Tom", "Ada", "Ola"):
    with open(story / "03.md", "a") as f:
        f.write(f"\n{name} came to the tower that evening with nothing to say about it at all.\n")
