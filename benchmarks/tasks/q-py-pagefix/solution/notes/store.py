"""Notes kept in memory, searchable a page at a time."""

from dataclasses import dataclass, field
from datetime import datetime


@dataclass
class Note:
    id: int
    text: str
    created: datetime
    tags: set[str] = field(default_factory=set)


@dataclass
class Page:
    items: list[Note]
    page: int
    pages: int  # total number of pages (at least 1)
    total: int  # matching notes on all pages


def paginate(items: list, page: int, per_page: int) -> tuple[list, int]:
    """The items on ``page`` (1-based) and the number of pages."""
    if page < 1 or per_page < 1:
        raise ValueError("page and per_page start at 1")
    pages = max(1, -(-len(items) // per_page))
    start = (page - 1) * per_page
    end = start + per_page
    return items[start:end], pages


class NoteStore:
    def __init__(self) -> None:
        self.notes: dict[int, Note] = {}
        self._next = 1

    def add(self, text: str, created: datetime, tags: set[str] | None = None) -> Note:
        note = Note(self._next, text, created, set(tags or ()))
        self.notes[note.id] = note
        self._next += 1
        return note

    def search(self, query: str = "", tag: str | None = None, page: int = 1, per_page: int = 10) -> Page:
        """Notes whose text contains ``query`` (any case) and that have ``tag``, newest first."""
        q = query.lower()
        found = [n for n in self.notes.values() if q in n.text.lower() and (tag is None or tag in n.tags)]
        found.sort(key=lambda n: (n.created, n.id), reverse=True)
        items, pages = paginate(found, page, per_page)
        return Page(items, page, pages, len(found))
