import sqlite3
from pathlib import Path

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


def test_upgrade_from_v2_marks_old_sessions_as_chat(tmp_path):
    path = tmp_path / "t.db"
    conn = sqlite3.connect(path)
    for i, sql in enumerate(MIGRATIONS[:2]):
        conn.executescript(f"BEGIN; {sql}; PRAGMA user_version = {i + 1}; COMMIT;")
    conn.execute("INSERT INTO session(model, created_at, updated_at) VALUES ('m', 0, 0)")
    conn.commit()
    conn.close()
    s = Store(path)
    row = s.find_session("1")
    assert row["kind"] == "chat" and row["cwd"] is None


def test_agent_sessions_store_full_messages(tmp_path):
    s = Store(tmp_path / "t.db")
    chat = s.create_session("m")
    sid = s.create_agent_session("m", "/proj")
    call = {"role": "assistant", "content": None,
            "tool_calls": [{"id": "c1", "type": "function", "function": {"name": "read", "arguments": "{}"}}]}
    for msg in ({"role": "user", "content": "hi"}, call, {"role": "tool", "tool_call_id": "c1", "content": "x"}):
        s.add_raw_message(sid, msg)
    assert s.raw_messages(sid)[1] == call
    assert [m["content"] for m in s.messages(sid)] == ["hi", "read", "x"]  # text summaries
    assert s.last_session()["id"] == chat  # chat -c never picks agent sessions
    assert s.last_session("agent", "/proj")["id"] == sid
    assert s.last_session("agent", "/other") is None


def test_prompt_history_is_per_folder_oldest_first_and_capped(tmp_path, monkeypatch):
    s = Store(tmp_path / "t.db")
    proj, other = Path("/proj"), Path("/other")
    for text in ("a", "b", "c"):
        s.add_prompt(proj, text)
    s.add_prompt(other, "x")
    assert s.prompts(proj) == ["a", "b", "c"]
    assert s.prompts(other) == ["x"]
    monkeypatch.setattr(db, "PROMPT_HISTORY_LIMIT", 2)
    assert s.prompts(proj) == ["b", "c"]


def test_private_copy_has_the_settings_but_writes_nothing_to_disk(tmp_path):
    s = Store(tmp_path / "t.db")
    s.set_setting("think", "off")
    p = s.private_copy()
    assert p.get_setting("think") == "off"
    p.add_message(p.create_session("m"), "user", "secret")
    p.add_prompt(Path("/proj"), "secret")
    assert s.sessions() == [] and s.prompts(Path("/proj")) == []


def test_clear_history_removes_sessions_messages_and_prompts_but_keeps_settings(tmp_path):
    s = Store(tmp_path / "t.db")
    s.set_setting("think", "off")
    s.add_message(s.create_session("m"), "user", "hi")
    s.create_agent_session("m", "/proj")
    s.add_prompt(Path("/proj"), "do it")
    assert s.clear_history() == 2
    assert s.sessions() == [] and s.prompts(Path("/proj")) == []
    assert s.conn.execute("SELECT COUNT(*) FROM message").fetchone()[0] == 0
    assert s.get_setting("think") == "off"
