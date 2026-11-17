from datetime import datetime, timedelta

import pytest

from notes.store import NoteStore, paginate

T0 = datetime(2026, 1, 1)


@pytest.fixture
def store():
    s = NoteStore()
    for i in range(7):
        s.add(f"note {i}", T0 + timedelta(days=i))
    return s


def test_pages_do_not_overlap_and_cover_everything(store):
    seen = [n.id for p in range(1, 4) for n in store.search(per_page=3, page=p).items]
    assert len(seen) == 7 and len(set(seen)) == 7


def test_second_page(store):
    assert [n.text for n in store.search(per_page=3, page=2).items] == ["note 3", "note 2", "note 1"]


def test_last_page_is_partial(store):
    page = store.search(per_page=3, page=3)
    assert [n.text for n in page.items] == ["note 0"] and page.pages == 3 and page.total == 7


def test_page_past_the_end_is_empty(store):
    assert store.search(per_page=3, page=4).items == []


def test_paginate_directly():
    assert paginate(list(range(10)), 2, 4) == ([4, 5, 6, 7], 3)


def test_bad_arguments_still_raise():
    with pytest.raises(ValueError):
        paginate([1], 1, 0)
