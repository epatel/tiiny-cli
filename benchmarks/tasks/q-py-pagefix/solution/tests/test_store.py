from datetime import datetime, timedelta

import pytest

from notes.store import NoteStore

T0 = datetime(2026, 1, 1)


@pytest.fixture
def store():
    s = NoteStore()
    for i in range(5):
        s.add(f"note {i}", T0 + timedelta(days=i), {"even"} if i % 2 == 0 else set())
    return s


def test_search_is_newest_first(store):
    assert [n.text for n in store.search("note").items] == ["note 4", "note 3", "note 2", "note 1", "note 0"]


def test_search_by_tag(store):
    page = store.search(tag="even")
    assert page.total == 3 and {n.text for n in page.items} == {"note 0", "note 2", "note 4"}


def test_first_page(store):
    page = store.search("note", per_page=2)
    assert [n.text for n in page.items] == ["note 4", "note 3"] and page.pages == 3


def test_bad_page(store):
    with pytest.raises(ValueError):
        store.search(page=0)


def test_pages_neither_repeat_nor_skip_notes(store):
    pages = [store.search("note", per_page=2, page=p).items for p in (1, 2, 3)]
    assert [n.text for page in pages for n in page] == ["note 4", "note 3", "note 2", "note 1", "note 0"]
