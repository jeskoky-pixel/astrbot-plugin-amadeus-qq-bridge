from __future__ import annotations

import hashlib
import json
import time
import uuid
from typing import Any

from .activity import Activity, IncomingMessage
from .message_codec import build_message_chain, message_for_agent, serialize_segments


class Coordinator:
    def __init__(self, db: Any, config: dict[str, Any] | None = None):
        self.db = db
        self.config = config or {}
        self.activities: dict[str, Activity] = {}

    def get(self, umo: str) -> Activity | None:
        activity = self.activities.get(umo)
        if activity and activity.phase in {"finished", "failed", "stopping"}:
            return None
        return activity

    def create(self, event: Any, message: IncomingMessage) -> Activity:
        activity = Activity(uuid.uuid4().hex, message.umo, message.umo, message.sender_id, event=event)
        activity.seen_message_ids.add(message.message_id)
        self.activities[message.umo] = activity
        self.db.record_activity(activity)
        self._inbound_once(message, activity.activity_id)
        return activity

    def restore(self, event: Any, message: IncomingMessage) -> Activity | None:
        if not bool(self.config.get("resume_after_restart", True)):
            return None
        finder = getattr(self.db, "find_resumable", None)
        row = finder(message.umo) if finder else None
        if row is None:
            return None
        activity = Activity(
            str(row["activity_id"]),
            message.umo,
            message.umo,
            message.sender_id,
            phase="starting",
            event=event,
            restored=True,
            follow_up_count=int(row["follow_up_count"] or 0),
            outbound_count=int(row["outbound_count"] or 0),
        )
        ids = getattr(self.db, "message_ids", lambda *_: [])(message.umo, activity.activity_id)
        activity.seen_message_ids.update(ids)
        activity.seen_message_ids.add(message.message_id)
        activity.resume_context = getattr(self.db, "transcript", lambda *_: "")(
            message.umo,
            activity.activity_id,
            int(self.config.get("resume_history_messages", 120)),
        )
        self.activities[message.umo] = activity
        self._inbound_once(message, activity.activity_id)
        self.db.update_activity(activity)
        return activity

    def _inbound_once(self, message: IncomingMessage, activity_id: str | None = None) -> bool:
        try:
            return self.db.inbound_once(message, activity_id)
        except TypeError:  # compatibility with third-party test repositories
            return self.db.inbound_once(message)

    def accept_message(self, activity: Activity, message: IncomingMessage) -> bool:
        if message.umo != activity.umo or message.message_id in activity.seen_message_ids:
            return False
        if not self._inbound_once(message, activity.activity_id):
            return False
        activity.seen_message_ids.add(message.message_id)
        activity.last_activity_at = time.time()
        self.db.update_activity(activity)
        return True

    async def route_follow_up(self, activity: Activity, message: IncomingMessage) -> str:
        if not self.accept_message(activity, message):
            return "duplicate"
        if activity.phase == "waiting_user":
            await activity.wait_queue.put(message)
            return "queued_for_wait"
        if activity.phase != "running" or activity.runner is None:
            return "ignored"
        limit = int(self.config.get("max_follow_up_messages", 0))
        if limit > 0 and activity.follow_up_count >= limit:
            return "limit"
        ticket = activity.runner.follow_up(message_text=message_for_agent(message.text, message.segments))
        if ticket is None:
            return "runner_done"
        activity.follow_up_count += 1
        self.db.update_activity(activity)
        return "follow_up"

    def action_id(self, activity: Activity, text: str, kind: str, segments: list[dict[str, Any]] | None = None) -> str:
        payload = json.dumps(segments or [], ensure_ascii=False, sort_keys=True, default=str)
        return hashlib.sha256(f"{activity.activity_id}|{kind}|{text}|{payload}".encode()).hexdigest()

    async def send_once(
        self,
        activity: Activity,
        text: str,
        kind: str,
        segments: list[dict[str, Any]] | None = None,
    ) -> dict[str, Any]:
        text = (text or "").strip()
        serialised = serialize_segments(segments)
        action_id = self.action_id(activity, text, kind, serialised)
        if self.db.outbound_status(action_id) in {"reserved", "sent"}:
            return {"status": "duplicate", "action_id": action_id}
        max_out = int(self.config.get("max_outbound_messages", 20))
        if max_out > 0 and activity.outbound_count >= max_out:
            return {"status": "failed", "action_id": action_id, "error": "outbound_limit"}
        stored_text = text
        if serialised:
            stored_text = f"{text}\n[segments]{json.dumps(serialised, ensure_ascii=False, default=str)}"
        if not self.db.reserve_outbound(action_id, activity.activity_id, stored_text):
            return {"status": "duplicate", "action_id": action_id}
        try:
            if serialised:
                await activity.event.send(activity.event.chain_result(build_message_chain(text, serialised)))
            else:
                await activity.event.send(activity.event.plain_result(text))
        except Exception as exc:  # noqa: BLE001
            self.db.mark_outbound(action_id, "failed", str(exc))
            return {"status": "failed", "action_id": action_id, "error": str(exc)}
        self.db.mark_outbound(action_id, "sent")
        activity.outbound_count += 1
        self.db.update_activity(activity)
        return {"status": "sent", "action_id": action_id, "segments": len(serialised)}

    async def stop(self, activity: Activity, reason: str = "user") -> None:
        activity.explicit_end = True
        activity.stop(reason)
        self.db.update_activity(activity, reason=reason)

