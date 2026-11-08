"""Local sqlite store: chat sessions/messages and per-task default models.

Migration path (standing default): PRAGMA user_version + an ordered,
forward-only MIGRATIONS list. Never edit a migration that has shipped —
append a new one. See cards/standing-defaults.md.
"""

from __future__ import annotations

import json
import sqlite3
import time
from pathlib import Path

MIGRATIONS = [
    # 1: chat history
    """
    CREATE TABLE session (
        id INTEGER PRIMARY KEY,
        name TEXT UNIQUE,
        model TEXT NOT NULL,
        system TEXT,
        created_at REAL NOT NULL,
        updated_at REAL NOT NULL
    );
    CREATE TABLE message (
        id INTEGER PRIMARY KEY,
        session_id INTEGER NOT NULL REFERENCES session(id) ON DELETE CASCADE,
        role TEXT NOT NULL,
        content TEXT NOT NULL,
        attachments TEXT,
        created_at REAL NOT NULL
    );
    CREATE INDEX message_session ON message(session_id, id)
    """,
    # 2: user preferences (e.g. default.chat = Qwen/Qwen3-Coder-Next)
    "CREATE TABLE setting (key TEXT PRIMARY KEY, value TEXT NOT NULL)",
]


def migrate(conn: sqlite3.Connection) -> int:
    version = conn.execute("PRAGMA user_version").fetchone()[0]
    for i, sql in enumerate(MIGRATIONS[version:], start=version):
        try:
            conn.executescript(f"BEGIN; {sql}; PRAGMA user_version = {i + 1}; COMMIT;")
        except Exception:
            if conn.in_transaction:
                conn.rollback()
            raise
    return conn.execute("PRAGMA user_version").fetchone()[0]


class Store:
    def __init__(self, path: Path | str):
        self.conn = sqlite3.connect(str(path))
        self.conn.row_factory = sqlite3.Row
        self.conn.execute("PRAGMA foreign_keys = ON")
        migrate(self.conn)

    # -- settings --------------------------------------------------------
    def get_setting(self, key: str) -> str | None:
        row = self.conn.execute("SELECT value FROM setting WHERE key = ?", (key,)).fetchone()
        return row["value"] if row else None

    def set_setting(self, key: str, value: str | None) -> None:
        with self.conn:
            if value is None:
                self.conn.execute("DELETE FROM setting WHERE key = ?", (key,))
            else:
                self.conn.execute(
                    "INSERT INTO setting(key, value) VALUES (?, ?) "
                    "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
                    (key, value),
                )

    def settings(self) -> dict[str, str]:
        return {r["key"]: r["value"] for r in self.conn.execute("SELECT key, value FROM setting")}

    # -- sessions --------------------------------------------------------
    def create_session(self, model: str, name: str | None = None, system: str | None = None) -> int:
        now = time.time()
        with self.conn:
            cur = self.conn.execute(
                "INSERT INTO session(name, model, system, created_at, updated_at) VALUES (?,?,?,?,?)",
                (name, model, system, now, now),
            )
        return cur.lastrowid

    def find_session(self, ref: str) -> sqlite3.Row | None:
        if ref.isdigit():
            row = self.conn.execute("SELECT * FROM session WHERE id = ?", (int(ref),)).fetchone()
            if row:
                return row
        return self.conn.execute("SELECT * FROM session WHERE name = ?", (ref,)).fetchone()

    def last_session(self) -> sqlite3.Row | None:
        return self.conn.execute("SELECT * FROM session ORDER BY updated_at DESC LIMIT 1").fetchone()

    def sessions(self, limit: int = 20) -> list[sqlite3.Row]:
        return self.conn.execute(
            "SELECT s.*, (SELECT COUNT(*) FROM message m WHERE m.session_id = s.id) AS n, "
            "(SELECT content FROM message m WHERE m.session_id = s.id AND role = 'user' "
            " ORDER BY id LIMIT 1) AS first "
            "FROM session s ORDER BY updated_at DESC LIMIT ?",
            (limit,),
        ).fetchall()

    def update_session(self, session_id: int, **fields) -> None:
        fields["updated_at"] = time.time()
        cols = ", ".join(f"{k} = ?" for k in fields)
        with self.conn:
            self.conn.execute(f"UPDATE session SET {cols} WHERE id = ?", (*fields.values(), session_id))

    def delete_session(self, session_id: int) -> None:
        with self.conn:
            self.conn.execute("DELETE FROM session WHERE id = ?", (session_id,))

    def add_message(self, session_id: int, role: str, content: str,
                    attachments: list[str] | None = None) -> None:
        now = time.time()
        with self.conn:
            self.conn.execute(
                "INSERT INTO message(session_id, role, content, attachments, created_at) "
                "VALUES (?,?,?,?,?)",
                (session_id, role, content, json.dumps(attachments) if attachments else None, now),
            )
            self.conn.execute("UPDATE session SET updated_at = ? WHERE id = ?", (now, session_id))

    def messages(self, session_id: int) -> list[sqlite3.Row]:
        return self.conn.execute(
            "SELECT * FROM message WHERE session_id = ? ORDER BY id", (session_id,)
        ).fetchall()

    def clear_messages(self, session_id: int) -> None:
        with self.conn:
            self.conn.execute("DELETE FROM message WHERE session_id = ?", (session_id,))
