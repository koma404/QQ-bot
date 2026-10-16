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
        # everything seen/sent in private chats, for the UI (messages above feeds the LLM)
        self.db.execute(
            "CREATE TABLE IF NOT EXISTS log ("
            "id INTEGER PRIMARY KEY AUTOINCREMENT, peer INTEGER NOT NULL,"
            "direction TEXT NOT NULL, source TEXT NOT NULL, content TEXT NOT NULL, ts REAL NOT NULL)"
        )
        self.db.execute("CREATE INDEX IF NOT EXISTS idx_log_peer ON log(peer, id)")
        self.db.execute(
            "CREATE TABLE IF NOT EXISTS contacts (peer INTEGER PRIMARY KEY, nickname TEXT NOT NULL)"
        )
        self.db.commit()
        self._backfill_log()

    def _backfill_log(self) -> None:
        """Upgrade path: show chats recorded before the UI existed (runs once, while the log is empty)."""
        if self.db.execute("SELECT 1 FROM log LIMIT 1").fetchone():
            return
        self.db.execute(
            "INSERT INTO log(peer, direction, source, content, ts) "
            "SELECT peer, CASE role WHEN 'user' THEN 'in' ELSE 'out' END, "
            "CASE role WHEN 'user' THEN 'friend' ELSE 'ai' END, content, ts FROM messages ORDER BY id"
        )
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

    # ---------- UI-facing chat log ----------
    def log_message(self, peer: int, direction: str, source: str, content: str) -> dict:
        """direction: in | out.  source: friend | ai | manual | device."""
        ts = time.time()
        cur = self.db.execute(
            "INSERT INTO log(peer, direction, source, content, ts) VALUES (?,?,?,?,?)",
            (peer, direction, source, content, ts),
        )
        self.db.commit()
        return {"id": cur.lastrowid, "peer": peer, "direction": direction,
                "source": source, "content": content, "ts": ts}

    def chat(self, peer: int, limit: int = 200, before_id: int | None = None) -> list[dict]:
        rows = self.db.execute(
            "SELECT id, direction, source, content, ts FROM log "
            "WHERE peer=? AND (? IS NULL OR id<?) ORDER BY id DESC LIMIT ?",
            (peer, before_id, before_id, limit),
        ).fetchall()
        return [{"id": i, "peer": peer, "direction": d, "source": s, "content": c, "ts": t}
                for i, d, s, c, t in reversed(rows)]

    def conversations(self) -> dict[int, dict]:
        rows = self.db.execute(
            "SELECT peer, id, direction, source, content, ts FROM log "
            "WHERE id IN (SELECT MAX(id) FROM log GROUP BY peer)"
        ).fetchall()
        return {p: {"id": i, "direction": d, "source": s, "text": c, "ts": t}
                for p, i, d, s, c, t in rows}

    def set_contact(self, peer: int, nickname: str) -> None:
        self.db.execute(
            "INSERT INTO contacts(peer, nickname) VALUES (?,?) "
            "ON CONFLICT(peer) DO UPDATE SET nickname=excluded.nickname",
            (peer, nickname),
        )
        self.db.commit()

    def contacts(self) -> dict[int, str]:
        return dict(self.db.execute("SELECT peer, nickname FROM contacts").fetchall())
