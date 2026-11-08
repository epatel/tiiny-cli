"""System prompt for `tai agent`."""

from __future__ import annotations

import datetime
import platform
from pathlib import Path

INSTRUCTION_FILES = ("AGENTS.md", "CLAUDE.md")
MAX_INSTRUCTIONS = 20_000  # characters of project instructions included

BASE = """You are tai agent, a coding assistant running on a local model in the user's terminal.
You work in the project directory below using tools. Be concise: short answers, no filler.

How to work:
- Explore before changing: use ls, glob, grep and read to find the relevant code. Never guess file contents.
- Change files with edit (exact string replacement; read the file first and copy old_string exactly,
  including indentation). Use write only for new files or full rewrites.
- Run tests, linters and scripts with bash. Commands run non-interactively in the project root.
- Keep changes minimal and focused on the request. Match the existing code style.
- If a tool call is denied, do not retry it. Ask the user what they want instead.
- When the task is done, say briefly what you changed. Do not paste whole files back.
- Paths are relative to the project root unless absolute.
"""


def project_instructions(root: Path) -> str:
    """The first AGENTS.md / CLAUDE.md found in the project root, truncated."""
    for name in INSTRUCTION_FILES:
        p = root / name
        if p.is_file():
            text = p.read_text(errors="replace")[:MAX_INSTRUCTIONS]
            return f"\n# Project instructions ({name})\n\n{text}\n"
    return ""


def system_prompt(root: Path, model_name: str, extra: str = "") -> str:
    """``extra``: further sections, e.g. the skill list and MCP server instructions."""
    env = (f"\n# Environment\n- Project root: {root}\n- Date: {datetime.date.today().isoformat()}\n"
           f"- OS: {platform.system()} {platform.release()}\n- Model: {model_name}\n")
    return BASE + env + project_instructions(root) + extra
