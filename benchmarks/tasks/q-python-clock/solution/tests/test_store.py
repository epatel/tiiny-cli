from datetime import datetime, timedelta, timezone

import pytest

from inventory.models import InventoryError
from inventory.store import Store

T0 = datetime(2026, 1, 1, tzinfo=timezone.utc)


@pytest.fixture
def store(tmp_path):
    s = Store(tmp_path / "inv.json")
    s.add("A", "Anchor", 5)
    return s


def test_reserve(store):
    assert store.reserve("A", 2, "x", now=T0).id == "R1" and store.available("A", now=T0) == 3


def test_expire(store):
    store.reserve("A", 2, "x", ttl_minutes=1, now=T0)
    assert store.expire(now=T0 + timedelta(minutes=1)) == ["R1"]


def test_too_many(store):
    with pytest.raises(InventoryError):
        store.reserve("A", 6, "x", now=T0)
