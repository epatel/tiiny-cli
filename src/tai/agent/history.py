"""Prompt history for the agent's input: Up/Down walk through earlier prompts,
shell style. Pure state (no UI), so it is tested without textual."""

from __future__ import annotations

from collections.abc import Iterable


class PromptHistory:
    """Entries oldest first. ``pos == len(entries)`` means "at the draft" (the
    text being typed, kept while browsing so Down can bring it back)."""

    def __init__(self, entries: Iterable[str] = ()) -> None:
        self.entries = list(entries)
        self.pos = len(self.entries)
        self.draft = ""

    def add(self, text: str) -> bool:
        """Remember a sent prompt (not twice in a row) and go back to an empty draft.
        True when it was added (so the caller saves it too)."""
        new = bool(text) and (not self.entries or self.entries[-1] != text)
        if new:
            self.entries.append(text)
        self.pos, self.draft = len(self.entries), ""
        return new

    def older(self, current: str) -> str | None:
        """The previous entry, or None at the oldest. Leaving the draft saves it."""
        if self.pos == 0:
            return None
        if self.pos == len(self.entries):
            self.draft = current
        self.pos -= 1
        return self.entries[self.pos]

    def newer(self) -> str | None:
        """The next entry, the draft after the newest, or None when already at the draft."""
        if self.pos >= len(self.entries):
            return None
        self.pos += 1
        return self.draft if self.pos == len(self.entries) else self.entries[self.pos]
