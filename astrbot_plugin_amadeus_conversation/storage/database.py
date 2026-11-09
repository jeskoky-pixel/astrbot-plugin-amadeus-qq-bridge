from __future__ import annotations

import json
import sqlite3
import time
import json
from pathlib import Path
from typing import Any


class ActivityDatabase:
    """Small synchronous SQLite repository; calls are short and serialized."""

    def __init__(self, path: str | Path):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.conn = sqlite3.connect(self.path, check_same_thread=False)
        self.conn.row_factory = sqlite3.Row
        self.conn.executescript(
            """
            CREATE TABLE IF NOT EXISTS activities(
              activity_id TEXT PRIMARY KEY, umo TEXT NOT NULL, phase TEXT NOT NULL,
              started_at REAL NOT NULL, last_activity_at REAL NOT NULL,
              follow_up_count INTEGER NOT NULL DEFAULT 0, outbound_count INTEGER NOT NULL DEFAULT 0,
              last_error TEXT, stop_reason TEXT);
            CREATE TABLE IF NOT EXISTS inbound_messages(
              message_id TEXT PRIMARY KEY, umo TEXT NOT NULL, sender_id TEXT NOT NULL,
              received_at REAL NOT NULL, text TEXT, payload_json TEXT NOT NULL,
              activity_id TEXT);
            CREATE TABLE IF NOT EXISTS outbound_messages(
              action_id TEXT PRIMARY KEY, activity_id TEXT NOT NULL, status TEXT NOT NULL,
              text TEXT, created_at REAL NOT NULL, updated_at REAL NOT NULL, error TEXT);
            """
        )
        columns = {row[1] for row in self.conn.execute("PRAGMA table_info(inbound_messages)")}
        if "activity_id" not in columns:
            self.conn.execute("ALTER TABLE inbound_messages ADD COLUMN activity_id TEXT")
        self.conn.commit()
        self.conn.execute(
            "UPDATE activities SET phase='interrupted', stop_reason=COALESCE(stop_reason,'plugin_shutdown') "
            "WHERE phase IN ('starting','running','waiting_user','stopping')"
        )
        self.conn.commit()

    def record_activity(self, activity: Any) -> None:
        self.conn.execute(
            "INSERT OR REPLACE INTO activities VALUES(?,?,?,?,?,?,?,?,?)",
            (activity.activity_id, activity.umo, activity.phase, activity.started_at,
             activity.last_activity_at, activity.follow_up_count, activity.outbound_count, None, None),
        )
        self.conn.commit()

    def update_activity(self, activity: Any, *, error: str | None = None, reason: str | None = None) -> None:
        self.conn.execute(
            "UPDATE activities SET phase=?, last_activity_at=?, follow_up_count=?, outbound_count=?, last_error=COALESCE(?,last_error), stop_reason=COALESCE(?,stop_reason) WHERE activity_id=?",
            (activity.phase, activity.last_activity_at, activity.follow_up_count, activity.outbound_count, error, reason, activity.activity_id),
        )
        self.conn.commit()

    def inbound_once(self, message: Any, activity_id: str | None = None) -> bool:
        from ..runtime.message_codec import serialize_segments

        cur = self.conn.execute(
            "INSERT OR IGNORE INTO inbound_messages(message_id,umo,sender_id,received_at,text,payload_json,activity_id) VALUES(?,?,?,?,?,?,?)",
            (message.message_id, message.umo, message.sender_id, time.time(), message.text,
             json.dumps(serialize_segments(message.segments), ensure_ascii=False, default=str), activity_id),
        )
        self.conn.commit()
        return cur.rowcount == 1

    def outbound_status(self, action_id: str) -> str | None:
        row = self.conn.execute("SELECT status FROM outbound_messages WHERE action_id=?", (action_id,)).fetchone()
        return row[0] if row else None

    def reserve_outbound(self, action_id: str, activity_id: str, text: str) -> bool:
        now = time.time()
        cur = self.conn.execute(
            "INSERT OR IGNORE INTO outbound_messages VALUES(?,?,?,?,?,?,?)",
            (action_id, activity_id, "reserved", text, now, now, None),
        )
        self.conn.commit()
        return cur.rowcount == 1

    def mark_outbound(self, action_id: str, status: str, error: str | None = None) -> None:
        self.conn.execute("UPDATE outbound_messages SET status=?,updated_at=?,error=? WHERE action_id=?", (status, time.time(), error, action_id))
        self.conn.commit()

    def close(self) -> None:
        self.conn.close()

    def find_resumable(self, umo: str) -> sqlite3.Row | None:
        return self.conn.execute(
            "SELECT * FROM activities WHERE umo=? AND phase='interrupted' "
            "AND stop_reason='plugin_shutdown' ORDER BY last_activity_at DESC LIMIT 1",
            (umo,),
        ).fetchone()

    def message_ids(self, umo: str, activity_id: str | None = None) -> list[str]:
        if activity_id:
            rows = self.conn.execute(
                "SELECT message_id FROM inbound_messages WHERE umo=? AND (activity_id=? OR activity_id IS NULL) ORDER BY received_at",
                (umo, activity_id),
            )
        else:
            rows = self.conn.execute(
                "SELECT message_id FROM inbound_messages WHERE umo=? ORDER BY received_at", (umo,)
            )
        return [str(row[0]) for row in rows]

    def transcript(self, umo: str, activity_id: str | None = None, limit: int = 120) -> str:
        """Return a bounded, human-readable transcript for restart recovery."""
        params: list[Any] = [umo]
        where = "i.umo=?"
        if activity_id:
            where += " AND (i.activity_id=? OR i.activity_id IS NULL)"
            params.append(activity_id)
        rows = self.conn.execute(
            f"SELECT i.received_at,'user' role,i.text text FROM inbound_messages i "
            f"WHERE {where} "
            "UNION ALL SELECT o.created_at,'assistant',o.text FROM outbound_messages o "
            "JOIN activities a2 ON a2.activity_id=o.activity_id WHERE a2.umo=? "
            "ORDER BY 1 DESC LIMIT ?",
            (*params, umo, limit),
        ).fetchall()
        lines = [f"{row['role']}: {row['text'] or ''}" for row in reversed(rows)]
        return "\n".join(lines)[-24000:]
