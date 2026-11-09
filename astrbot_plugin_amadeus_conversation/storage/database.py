from __future__ import annotations

import json
import sqlite3
import time
from pathlib import Path
from typing import Any


class ActivityDatabase:
    """Durable Activity, inbox and outbox repository."""

    def __init__(self, path: str | Path):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.conn = sqlite3.connect(self.path, check_same_thread=False)
        self.conn.row_factory = sqlite3.Row
        self._create_schema()
        self._migrate_inbox()
        self._migrate_columns()
        self.conn.execute("UPDATE outbound_messages SET status='unknown' WHERE status='reserved'")
        self.conn.execute(
            "UPDATE activities SET phase='interrupted', stop_reason=COALESCE(stop_reason,'plugin_shutdown') "
            "WHERE phase IN ('starting','running','waiting_user','stopping')"
        )
        self.conn.commit()

    def _create_schema(self) -> None:
        self.conn.executescript(
            """
            CREATE TABLE IF NOT EXISTS activities(
              activity_id TEXT PRIMARY KEY, umo TEXT NOT NULL, phase TEXT NOT NULL,
              started_at REAL NOT NULL, last_activity_at REAL NOT NULL,
              follow_up_count INTEGER NOT NULL DEFAULT 0, outbound_count INTEGER NOT NULL DEFAULT 0,
              last_error TEXT, stop_reason TEXT, deadline_at REAL, response_types TEXT);
            CREATE TABLE IF NOT EXISTS inbound_messages(
              inbound_id INTEGER PRIMARY KEY AUTOINCREMENT,
              umo TEXT NOT NULL, message_id TEXT NOT NULL, seq INTEGER NOT NULL,
              activity_id TEXT, sender_id TEXT NOT NULL, received_at REAL NOT NULL,
              text TEXT, payload_json TEXT NOT NULL,
              needs_prompt INTEGER NOT NULL DEFAULT 1,
              prompted INTEGER NOT NULL DEFAULT 0,
              consumed INTEGER NOT NULL DEFAULT 0,
              consumed_by TEXT, consumed_at REAL,
              UNIQUE(umo, message_id));
            CREATE TABLE IF NOT EXISTS outbound_messages(
              action_id TEXT PRIMARY KEY, activity_id TEXT NOT NULL, status TEXT NOT NULL,
              text TEXT, created_at REAL NOT NULL, updated_at REAL NOT NULL,
              error TEXT, platform_message_id TEXT);
            """
        )
        self.conn.commit()

    def _migrate_inbox(self) -> None:
        columns = {row[1] for row in self.conn.execute("PRAGMA table_info(inbound_messages)")}
        if "inbound_id" in columns:
            return
        activity_expr = "activity_id" if "activity_id" in columns else "NULL AS activity_id"
        legacy = list(self.conn.execute(f"SELECT message_id,umo,sender_id,received_at,text,payload_json,{activity_expr} FROM inbound_messages"))
        self.conn.execute("ALTER TABLE inbound_messages RENAME TO inbound_messages_legacy")
        self.conn.execute(
            """
            CREATE TABLE inbound_messages(
              inbound_id INTEGER PRIMARY KEY AUTOINCREMENT,
              umo TEXT NOT NULL, message_id TEXT NOT NULL, seq INTEGER NOT NULL,
              activity_id TEXT, sender_id TEXT NOT NULL, received_at REAL NOT NULL,
              text TEXT, payload_json TEXT NOT NULL,
              needs_prompt INTEGER NOT NULL DEFAULT 1,
              prompted INTEGER NOT NULL DEFAULT 0,
              consumed INTEGER NOT NULL DEFAULT 0,
              consumed_by TEXT, consumed_at REAL,
              UNIQUE(umo, message_id))
            """
        )
        counters: dict[str, int] = {}
        for row in legacy:
            umo = str(row[1])
            counters[umo] = counters.get(umo, 0) + 1
            self.conn.execute(
                """
                INSERT OR IGNORE INTO inbound_messages
                (umo,message_id,seq,activity_id,sender_id,received_at,text,payload_json,needs_prompt,prompted,consumed,consumed_at)
                VALUES(?,?,?,?,?,?,?,?,0,1,1,?)
                """,
                (umo, str(row[0]), counters[umo], row[6], str(row[2]), float(row[3]), row[4], row[5] or "[]", float(row[3])),
            )
        self.conn.execute("DROP TABLE inbound_messages_legacy")
        self.conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_inbound_pending ON inbound_messages(umo,activity_id,consumed,prompted,seq)"
        )
        self.conn.commit()

    def _migrate_columns(self) -> None:
        self.conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_inbound_pending ON inbound_messages(umo,activity_id,consumed,prompted,seq)"
        )
        activity_columns = {row[1] for row in self.conn.execute("PRAGMA table_info(activities)")}
        if "deadline_at" not in activity_columns:
            self.conn.execute("ALTER TABLE activities ADD COLUMN deadline_at REAL")
        if "response_types" not in activity_columns:
            self.conn.execute("ALTER TABLE activities ADD COLUMN response_types TEXT")
        outbound_columns = {row[1] for row in self.conn.execute("PRAGMA table_info(outbound_messages)")}
        if "platform_message_id" not in outbound_columns:
            self.conn.execute("ALTER TABLE outbound_messages ADD COLUMN platform_message_id TEXT")
        self.conn.commit()

    def record_activity(self, activity: Any) -> None:
        self.conn.execute(
            """
            INSERT OR REPLACE INTO activities
            (activity_id,umo,phase,started_at,last_activity_at,follow_up_count,outbound_count,last_error,stop_reason,deadline_at,response_types)
            VALUES(?,?,?,?,?,?,?,?,?,?,?)
            """,
            (
                activity.activity_id,
                activity.umo,
                activity.phase,
                activity.started_at,
                activity.last_activity_at,
                activity.follow_up_count,
                activity.outbound_count,
                None,
                None,
                activity.deadline_at,
                json.dumps(activity.response_types, ensure_ascii=False),
            ),
        )
        self.conn.commit()

    def update_activity(self, activity: Any, *, error: str | None = None, reason: str | None = None) -> None:
        self.conn.execute(
            """
            UPDATE activities SET phase=?,last_activity_at=?,follow_up_count=?,outbound_count=?,
              last_error=COALESCE(?,last_error),stop_reason=COALESCE(?,stop_reason),
              deadline_at=?,response_types=? WHERE activity_id=?
            """,
            (
                activity.phase,
                activity.last_activity_at,
                activity.follow_up_count,
                activity.outbound_count,
                error,
                reason,
                activity.deadline_at,
                json.dumps(activity.response_types, ensure_ascii=False),
                activity.activity_id,
            ),
        )
        self.conn.commit()

    def inbound_once(self, message: Any, activity_id: str | None = None) -> bool:
        from ..runtime.message_codec import serialize_segments

        umo = str(message.umo)
        self.conn.execute("BEGIN IMMEDIATE")
        try:
            seq = int(
                self.conn.execute(
                    "SELECT COALESCE(MAX(seq),0)+1 FROM inbound_messages WHERE umo=?", (umo,)
                ).fetchone()[0]
            )
            cur = self.conn.execute(
                """
                INSERT OR IGNORE INTO inbound_messages
                (umo,message_id,seq,activity_id,sender_id,received_at,text,payload_json)
                VALUES(?,?,?,?,?,?,?,?)
                """,
                (
                    umo,
                    str(message.message_id),
                    seq,
                    activity_id,
                    str(message.sender_id),
                    time.time(),
                    message.text,
                    json.dumps(serialize_segments(message.segments), ensure_ascii=False, default=str),
                ),
            )
            self.conn.commit()
            return cur.rowcount == 1
        except Exception:
            self.conn.rollback()
            raise

    def latest_seq(self, umo: str) -> int:
        return int(self.conn.execute("SELECT COALESCE(MAX(seq),0) FROM inbound_messages WHERE umo=?", (umo,)).fetchone()[0])

    def message_seq(self, umo: str, message_id: str) -> int:
        row = self.conn.execute(
            "SELECT seq FROM inbound_messages WHERE umo=? AND message_id=?",
            (umo, message_id),
        ).fetchone()
        return int(row[0]) if row else 0

    def claim_next_pending(self, umo: str, activity_id: str) -> sqlite3.Row | None:
        row = self.conn.execute(
            """
            SELECT * FROM inbound_messages
            WHERE umo=? AND consumed=0 AND prompted=0 AND needs_prompt=1
              AND (activity_id=? OR activity_id IS NULL)
            ORDER BY seq LIMIT 1
            """,
            (umo, activity_id),
        ).fetchone()
        if row is not None:
            self.mark_prompted(str(row["message_id"]), activity_id)
        return row

    def pending_after(self, umo: str, activity_id: str, after_seq: int) -> list[sqlite3.Row]:
        return list(
            self.conn.execute(
                """
                SELECT * FROM inbound_messages
                WHERE umo=? AND seq>? AND consumed=0 AND needs_prompt=1
                  AND (activity_id=? OR activity_id IS NULL)
                ORDER BY seq
                """,
                (umo, after_seq, activity_id),
            )
        )

    def mark_prompted(self, message_id: str, activity_id: str) -> None:
        self.conn.execute(
            "UPDATE inbound_messages SET prompted=1 WHERE message_id=? AND (activity_id=? OR activity_id IS NULL)",
            (message_id, activity_id),
        )
        self.conn.commit()

    def unprompt(self, message_id: str, activity_id: str) -> None:
        self.conn.execute(
            "UPDATE inbound_messages SET prompted=0 WHERE message_id=? AND (activity_id=? OR activity_id IS NULL)",
            (message_id, activity_id),
        )
        self.conn.commit()

    def mark_consumed(self, message_id: str, activity_id: str, consumed_by: str | None = None) -> None:
        self.conn.execute(
            """
            UPDATE inbound_messages
            SET consumed=1,prompted=1,consumed_by=?,consumed_at=?
            WHERE message_id=? AND (activity_id=? OR activity_id IS NULL)
            """,
            (consumed_by or activity_id, time.time(), message_id, activity_id),
        )
        self.conn.commit()

    def requeue_message(self, message_id: str, activity_id: str) -> None:
        self.conn.execute(
            "UPDATE inbound_messages SET consumed=0,prompted=0,consumed_by=NULL,consumed_at=NULL WHERE message_id=? AND (activity_id=? OR activity_id IS NULL)",
            (message_id, activity_id),
        )
        self.conn.commit()

    def outbound_status(self, action_id: str) -> str | None:
        row = self.conn.execute("SELECT status FROM outbound_messages WHERE action_id=?", (action_id,)).fetchone()
        return row[0] if row else None

    def reserve_outbound(self, action_id: str, activity_id: str, text: str) -> bool:
        now = time.time()
        cur = self.conn.execute(
            """
            INSERT OR IGNORE INTO outbound_messages
            (action_id,activity_id,status,text,created_at,updated_at,error,platform_message_id)
            VALUES(?,?,?,?,?,?,?,?)
            """,
            (action_id, activity_id, "reserved", text, now, now, None, None),
        )
        self.conn.commit()
        return cur.rowcount == 1

    def mark_outbound(
        self,
        action_id: str,
        status: str,
        error: str | None = None,
        platform_id: str | None = None,
    ) -> None:
        self.conn.execute(
            "UPDATE outbound_messages SET status=?,updated_at=?,error=?,platform_message_id=? WHERE action_id=?",
            (status, time.time(), error, platform_id, action_id),
        )
        self.conn.commit()

    def close(self) -> None:
        self.conn.close()

    def find_resumable(self, umo: str) -> sqlite3.Row | None:
        return self.conn.execute(
            "SELECT * FROM activities WHERE umo=? AND phase='interrupted' AND stop_reason='plugin_shutdown' ORDER BY last_activity_at DESC LIMIT 1",
            (umo,),
        ).fetchone()

    def message_ids(self, umo: str, activity_id: str | None = None) -> list[str]:
        if activity_id:
            rows = self.conn.execute(
                "SELECT message_id FROM inbound_messages WHERE umo=? AND (activity_id=? OR activity_id IS NULL) ORDER BY seq",
                (umo, activity_id),
            )
        else:
            rows = self.conn.execute("SELECT message_id FROM inbound_messages WHERE umo=? ORDER BY seq", (umo,))
        return [str(row[0]) for row in rows]

    def transcript(self, umo: str, activity_id: str | None = None, limit: int = 120) -> str:
        params: list[Any] = [umo]
        where = "i.umo=?"
        if activity_id:
            where += " AND (i.activity_id=? OR i.activity_id IS NULL)"
            params.append(activity_id)
        rows = self.conn.execute(
            f"SELECT i.seq,'user' role,i.text text,i.payload_json payload FROM inbound_messages i WHERE {where} "
            "UNION ALL SELECT o.created_at,'assistant',o.text,NULL FROM outbound_messages o "
            "JOIN activities a2 ON a2.activity_id=o.activity_id WHERE a2.umo=? "
            "ORDER BY 1 DESC LIMIT ?",
            (*params, umo, limit),
        ).fetchall()
        lines = []
        for row in reversed(rows):
            payload = row["payload"] if "payload" in row.keys() else None
            suffix = f" segments={payload}" if payload and payload != "[]" else ""
            lines.append(f"{row['role']}: {row['text'] or ''}{suffix}")
        return "\n".join(lines)[-24000:]
