"""Build number for `tai --version`, read from git when tai runs from its checkout.

tai is installed editable (`make setup`), so the code that runs is the checkout itself:
asking git at `--version` time is always exact, needs no hook, and adds no file that
changes on every commit. Installed some other way (no checkout), there is no build part.
"""

from __future__ import annotations

import subprocess
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]  # src/tai/build.py -> the checkout


def _git(*args: str) -> str:
    return subprocess.run(["git", "-C", str(REPO), *args], capture_output=True, text=True,
                          timeout=5, check=True).stdout.strip()


def build() -> str | None:
    """``"build 47 (9138ee6)"``: commits up to HEAD, its short hash, ``+ changes`` when the
    tracked files differ from it. None outside a git checkout of tai."""
    try:
        if Path(_git("rev-parse", "--show-toplevel")).resolve() != REPO:
            return None  # git found some other repo around an installed copy
        count, commit = _git("rev-list", "--count", "HEAD"), _git("rev-parse", "--short", "HEAD")
        dirty = _git("status", "--porcelain", "--untracked-files=no")
    except (OSError, subprocess.SubprocessError):
        return None
    return f"build {count} ({commit}{', + changes' if dirty else ''})"


def describe(version: str, build_: str | None) -> str:
    return f"version {version}" + (f", {build_}" if build_ else "")


def version_text(version: str) -> str:
    return f"tai, {describe(version, build())}"
