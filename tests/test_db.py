import sqlite3

import pytest

from tai import db
from tai.db import MIGRATIONS, Store, migrate


def test_fresh_db_runs_all_migrations(tmp_path):
    conn = sqlite3.connect(tmp_path / "t.db")
    assert migrate(conn) == len(MIGRATIONS)
    assert migrate(conn) == len(MIGRATIONS)  # idempotent


def test_upgrade_from_v1_keeps_data(tmp_path):
    path = tmp_path / "t.db"
    conn = sqlite3.connect(path)
    conn.executescript(f"BEGIN; {MIGRATIONS[0]}; PRAGMA user_version = 1; COMMIT;")
    conn.execute("INSERT INTO session(model, created_at, updated_at) VALUES ('m', 0, 0)")
    conn.commit()
    conn.close()
    s = Store(path)
    assert s.conn.execute("PRAGMA user_version").fetchone()[0] == len(MIGRATIONS)
    assert s.find_session("1")["model"] == "m"
    s.set_setting("think", "off")
    assert s.get_setting("think") == "off"


def test_failed_migration_rolls_back(tmp_path, monkeypatch):
    monkeypatch.setattr(db, "MIGRATIONS", MIGRATIONS + ["CREATE TABLE x (a); SELECT nope FROM nowhere"])
    conn = sqlite3.connect(tmp_path / "t.db")
    with pytest.raises(sqlite3.OperationalError):
        db.migrate(conn)
    assert conn.execute("PRAGMA user_version").fetchone()[0] == len(MIGRATIONS)
    assert not conn.execute("SELECT name FROM sqlite_master WHERE name = 'x'").fetchone()


def test_sessions_and_messages(tmp_path):
    s = Store(tmp_path / "t.db")
    sid = s.create_session("Qwen/Qwen3.6-35B-A3B", name="work")
    s.add_message(sid, "user", "hi", ["a.png"])
    s.add_message(sid, "assistant", "hello")
    assert [m["role"] for m in s.messages(sid)] == ["user", "assistant"]
    assert s.find_session("work")["id"] == sid
    assert s.sessions()[0]["n"] == 2 and s.sessions()[0]["first"] == "hi"
    s.delete_session(sid)
    assert s.messages(sid) == []
