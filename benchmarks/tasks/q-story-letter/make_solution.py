"""Writes a mechanically valid story into the repo given as argument, to check grade.py (not literature)."""
import sys
from pathlib import Path

FIRST = "The letter was addressed to Nils in his own handwriting."
repo = Path(sys.argv[1])
story = repo / "story"
story.mkdir(exist_ok=True)
for c in range(1, 3):
    lines = [f"# Chapter {c}: Part {c}", FIRST]
    lines += [f'Nils held the letter up to lamp {c}-{i} and Ada said "again" while Ole waited at hour {i}.'
              for i in range(22)]
    (story / f"{c:02d}.md").write_text("\n".join(lines) + "\n")
(story / "characters.md").write_text("- Nils Aberg: sorter\n- Ada Holm: postmistress\n- Ole Brandt: courier\n")
