"""Per-friend chat history in SQLite."""
from __future__ import annotations

import os
import sqlite3
import time


class Memory:
    def __init__(self, path: str):
        if path != ":memory:":
            os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
        self.db = sqlite3.connect(path)
        self.db.execute(
            "CREATE TABLE IF NOT EXISTS messages ("
            "id INTEGER PRIMARY KEY AUTOINCREMENT, peer INTEGER NOT NULL,"
            "role TEXT NOT NULL, content TEXT NOT NULL, ts REAL NOT NULL)"
        )
        self.db.execute("CREATE INDEX IF NOT EXISTS idx_peer ON messages(peer, id)")
        self.db.commit()

    def add(self, peer: int, role: str, content: str) -> None:
        self.db.execute(
            "INSERT INTO messages(peer, role, content, ts) VALUES (?,?,?,?)",
            (peer, role, content, time.time()),
        )
        self.db.commit()

    def recent(self, peer: int, n: int) -> list[dict]:
        rows = self.db.execute(
            "SELECT role, content FROM messages WHERE peer=? ORDER BY id DESC LIMIT ?",
            (peer, n),
        ).fetchall()
        return [{"role": r, "content": c} for r, c in reversed(rows)]

    def last_role(self, peer: int) -> str | None:
        row = self.db.execute(
            "SELECT role FROM messages WHERE peer=? ORDER BY id DESC LIMIT 1", (peer,)
        ).fetchone()
        return row[0] if row else None

    def clear(self, peer: int) -> None:
        self.db.execute("DELETE FROM messages WHERE peer=?", (peer,))
        self.db.commit()
