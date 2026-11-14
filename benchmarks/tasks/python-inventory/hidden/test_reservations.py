import json
from datetime import datetime, timedelta, timezone

import pytest

from inventory import clock
from inventory.cli import main
from inventory.models import InventoryError, Reservation
from inventory.store import Store

T0 = datetime(2026, 1, 1, 12, 0, tzinfo=timezone.utc)


@pytest.fixture
def store(tmp_path):
    s = Store(tmp_path / "inv.json")
    s.add("A1", "Anchor", 10)
    s.add("B2", "Bolt", 5)
    return s


@pytest.fixture
def at(monkeypatch):
    """Set the clock: at(minutes after T0)."""
    def set_time(minutes=0):
        monkeypatch.setattr("inventory.clock.now", lambda: T0 + timedelta(minutes=minutes))
    set_time(0)
    return set_time


def cli(f, *args):
    return main(["--file", str(f), *args])


def test_clock_is_utc_aware():
    n = clock.now()
    assert n.tzinfo is not None and n.utcoffset() == timedelta(0)


def test_reserve_reduces_availability(store):
    r = store.reserve("A1", 3, "ann", now=T0)
    assert isinstance(r, Reservation) and r.id == "R1" and r.expires_at == T0 + timedelta(minutes=30)
    assert store.available("A1", now=T0) == 7 and store.reserved("A1", now=T0) == 3


def test_expired_reservations_do_not_count(store):
    store.reserve("A1", 4, "ann", ttl_minutes=10, now=T0)
    assert store.available("A1", now=T0 + timedelta(minutes=9)) == 6
    assert store.available("A1", now=T0 + timedelta(minutes=10)) == 10  # active only while expires_at > now


@pytest.mark.parametrize("args", [("ZZ", 1), ("A1", 0), ("A1", -2), ("A1", 11)])
def test_reserve_refuses(store, args):
    with pytest.raises(InventoryError):
        store.reserve(*args, "ann", now=T0)


def test_reserve_refuses_bad_ttl(store):
    with pytest.raises(InventoryError):
        store.reserve("A1", 1, "ann", ttl_minutes=0, now=T0)


def test_cannot_reserve_more_than_available(store):
    store.reserve("A1", 8, "ann", now=T0)
    with pytest.raises(InventoryError):
        store.reserve("A1", 3, "bob", now=T0)
    assert store.reserve("A1", 2, "bob", now=T0).id == "R2"


def test_release_and_ids_are_never_reused(store):
    store.reserve("A1", 1, "ann", now=T0)
    store.reserve("A1", 1, "ann", now=T0)
    assert store.release("R2").id == "R2"
    assert store.reserve("A1", 1, "ann", now=T0).id == "R3"
    with pytest.raises(InventoryError):
        store.release("R2")


def test_expire_returns_ids_sorted_by_number(store):
    for i in range(1, 12):  # R1..R11, one unit each; R2 and R10 expire after 5 minutes
        store.reserve("A1" if i % 2 else "B2", 1, "x", ttl_minutes=5 if i in (2, 10) else 60, now=T0)
    assert store.expire(now=T0 + timedelta(minutes=5)) == ["R2", "R10"]
    assert set(store.reservations) == {f"R{i}" for i in range(1, 12)} - {"R2", "R10"}


def test_default_now_uses_the_clock(store, at):
    store.reserve("A1", 2, "ann", ttl_minutes=10)
    at(5)
    assert store.available("A1") == 8
    at(11)
    assert store.available("A1") == 10 and store.expire() == ["R1"]


def test_persistence_round_trip(store, tmp_path):
    store.reserve("A1", 2, "ann", now=T0)
    store.reserve("A1", 1, "bob", now=T0)
    store.release("R2")
    store.save()
    data = json.loads((tmp_path / "inv.json").read_text())
    assert [r["id"] for r in data["reservations"]] == ["R1"] and data["next_reservation"] == 3
    assert datetime.fromisoformat(data["reservations"][0]["expires_at"]) == T0 + timedelta(minutes=30)
    again = Store(tmp_path / "inv.json")
    assert again.reservations["R1"].holder == "ann" and again.reservations["R1"].expires_at.tzinfo is not None
    assert again.reserve("B2", 1, "cy", now=T0).id == "R3"


def test_old_files_still_load(tmp_path):
    f = tmp_path / "old.json"
    f.write_text(json.dumps({"items": [{"sku": "A1", "name": "Anchor", "stock": 4}]}))
    s = Store(f)
    assert s.available("A1", now=T0) == 4 and s.reservations == {}
    assert s.reserve("A1", 1, "ann", now=T0).id == "R1"


def test_cli_reserve_release_report(tmp_path, capsys, at):
    f = tmp_path / "inv.json"
    assert cli(f, "add", "B2", "Bolt", "5") == 0 and cli(f, "add", "A1", "Anchor", "10") == 0
    capsys.readouterr()
    assert cli(f, "reserve", "A1", "3", "ann") == 0
    assert capsys.readouterr().out.strip() == "R1"
    assert cli(f, "report") == 0
    assert capsys.readouterr().out.splitlines() == ["sku\tstock\treserved\tavailable", "A1\t10\t3\t7", "B2\t5\t0\t5"]
    assert cli(f, "release", "R1") == 0
    assert capsys.readouterr().out.strip() == "released R1"
    assert cli(f, "list") == 0
    assert capsys.readouterr().out.splitlines() == ["A1\tAnchor\t10\t10", "B2\tBolt\t5\t5"]


def test_cli_ttl_and_expire(tmp_path, capsys, at):
    f = tmp_path / "inv.json"
    cli(f, "add", "A1", "Anchor", "10")
    cli(f, "reserve", "A1", "4", "ann", "--ttl", "5")
    cli(f, "reserve", "A1", "1", "bob")
    capsys.readouterr()
    at(6)
    assert cli(f, "list") == 0
    assert capsys.readouterr().out.splitlines() == ["A1\tAnchor\t10\t9"]
    assert "R1" in json.loads(f.read_text()).get("reservations", [{}])[0].get("id", "")  # list doesn't remove
    assert cli(f, "expire") == 0
    assert capsys.readouterr().out.splitlines() == ["R1"]
    assert [r["id"] for r in json.loads(f.read_text())["reservations"]] == ["R2"]


def test_cli_errors(tmp_path, capsys, at):
    f = tmp_path / "inv.json"
    cli(f, "add", "A1", "Anchor", "2")
    capsys.readouterr()
    assert cli(f, "reserve", "A1", "3", "ann") == 2
    assert capsys.readouterr().err.startswith("error: ")
    assert cli(f, "release", "R9") == 2
    assert capsys.readouterr().err.startswith("error: ")
