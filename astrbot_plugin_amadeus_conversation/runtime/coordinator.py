from __future__ import annotations

import hashlib
import json
import time
import uuid
from typing import Any

from .activity import Activity, IncomingMessage
from .message_codec import (
    build_message_chain,
    message_for_agent,
    serialize_segments,
    validate_segment_specs,
)


class Coordinator:
    """Durable per-UMO inbox and serialized Activity state machine."""

    def __init__(self, db: Any, config: dict[str, Any] | None = None):
        self.db = db
        self.config = config or {}
        self.activities: dict[str, Activity] = {}
        self.runner_service: Any = None

    def attach_runner_service(self, runner_service: Any) -> None:
        self.runner_service = runner_service

    def get(self, umo: str) -> Activity | None:
        activity = self.activities.get(umo)
        if activity and activity.phase in {"finished", "failed"}:
            return None
        return activity

    def _deadline(self, started_at: float) -> float | None:
        seconds = int(self.config.get("max_activity_seconds", 0))
        return started_at + seconds if seconds > 0 else None

    def create(self, event: Any, message: IncomingMessage) -> Activity:
        activity = Activity(
            uuid.uuid4().hex,
            message.umo,
            message.umo,
            message.sender_id,
            event=event,
        )
        activity.deadline_at = self._deadline(activity.started_at)
        self.activities[message.umo] = activity
        self.db.record_activity(activity)
        self._record_message(activity, event, message)
        self._ensure_task(activity)
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
        activity.started_at = float(row["started_at"] or activity.started_at)
        activity.last_activity_at = float(row["last_activity_at"] or activity.last_activity_at)
        activity.deadline_at = self._deadline(activity.started_at)
        activity.resume_context = getattr(self.db, "transcript", lambda *_: "")(
            message.umo,
            activity.activity_id,
            int(self.config.get("resume_history_messages", 120)),
        )
        self.activities[message.umo] = activity
        self._record_message(activity, event, message)
        self.db.update_activity(activity)
        self._ensure_task(activity)
        return activity

    def _record_message(self, activity: Activity, event: Any, message: IncomingMessage) -> bool:
        try:
            inserted = self.db.inbound_once(message, activity.activity_id)
        except TypeError:
            inserted = self.db.inbound_once(message)
        if inserted:
            activity.event_by_message_id[message.message_id] = event
            activity.seen_message_ids.add(message.message_id)
            activity.last_activity_at = time.time()
            self.db.update_activity(activity)
        return inserted

    async def enqueue(self, activity: Activity, event: Any, message: IncomingMessage) -> str:
        if message.umo != activity.umo:
            return "wrong_umo"
        async with activity.lock:
            if message.message_id in activity.seen_message_ids:
                return "duplicate"
            if not self._record_message(activity, event, message):
                return "duplicate"
            if activity.phase == "stopping" or activity.cancel_event.is_set():
                activity.message_event.set()
                return "stopping"
            if activity.phase == "waiting_user" and activity.runner is not None:
                await activity.wait_queue.put(message)
                activity.message_event.set()
                return "queued"
            limit = int(self.config.get("max_follow_up_messages", 0))
            if activity.phase == "running" and activity.runner is not None and (
                limit <= 0 or activity.follow_up_count < limit
            ):
                ticket = activity.runner.follow_up(
                    message_text=message_for_agent(message.text, message.segments)
                )
                if ticket is not None:
                    activity.follow_up_tickets[message.message_id] = ticket
                    activity.follow_up_count += 1
                    if hasattr(self.db, "mark_prompted"):
                        self.db.mark_prompted(message.message_id, activity.activity_id)
                    self.db.update_activity(activity)
                    activity.message_event.set()
                    return "follow_up"
            activity.message_event.set()
            self._ensure_task(activity)
            return "queued"

    async def route_follow_up(self, activity: Activity, message: IncomingMessage) -> str:
        """Compatibility adapter for integrations written against 0.2.x."""
        result = await self.enqueue(activity, message.event or activity.event, message)
        if result == "queued" and activity.phase == "waiting_user":
            return "queued_for_wait"
        return result

    def _ensure_task(self, activity: Activity) -> None:
        if self.runner_service is None:
            return
        if activity.task is None or activity.task.done():
            activity.task = self.runner_service.create_dispatch_task(activity)

    def next_pending(self, activity: Activity) -> IncomingMessage | None:
        row = self.db.claim_next_pending(activity.umo, activity.activity_id)
        if row is None:
            return None
        payload = json.loads(row["payload_json"] or "[]")
        event = activity.event_by_message_id.get(str(row["message_id"]), activity.event)
        return IncomingMessage(
            str(row["message_id"]),
            str(row["umo"]),
            str(row["sender_id"]),
            str(row["text"] or ""),
            payload if isinstance(payload, list) else [],
            event,
        )

    def mark_consumed(self, message: IncomingMessage, activity: Activity) -> None:
        if hasattr(self.db, "mark_consumed"):
            self.db.mark_consumed(message.message_id, activity.activity_id)
        if hasattr(self.db, "message_seq"):
            activity.wait_baseline_seq = max(
                activity.wait_baseline_seq,
                self.db.message_seq(activity.umo, message.message_id),
            )
        activity.seen_message_ids.add(message.message_id)

    def requeue_followups(self, activity: Activity) -> None:
        for message_id, ticket in list(activity.follow_up_tickets.items()):
            if getattr(ticket, "consumed", False):
                if hasattr(self.db, "mark_consumed"):
                    self.db.mark_consumed(message_id, activity.activity_id)
                if hasattr(self.db, "message_seq"):
                    activity.wait_baseline_seq = max(
                        activity.wait_baseline_seq,
                        self.db.message_seq(activity.umo, message_id),
                    )
            else:
                if hasattr(self.db, "unprompt"):
                    self.db.unprompt(message_id, activity.activity_id)
            activity.follow_up_tickets.pop(message_id, None)

    def pending_after(self, activity: Activity, after_seq: int) -> list[IncomingMessage]:
        rows = self.db.pending_after(activity.umo, activity.activity_id, after_seq)
        messages: list[IncomingMessage] = []
        for row in rows:
            payload = json.loads(row["payload_json"] or "[]")
            messages.append(
                IncomingMessage(
                    str(row["message_id"]),
                    str(row["umo"]),
                    str(row["sender_id"]),
                    str(row["text"] or ""),
                    payload if isinstance(payload, list) else [],
                    activity.event,
                )
            )
        return messages

    async def wait_for_user(self, activity: Activity, timeout_seconds: int) -> list[IncomingMessage] | None:
        if activity.waiter_active:
            return []
        activity.waiter_active = True
        activity.phase = "waiting_user"
        activity.wait_baseline_seq = max(0, activity.wait_baseline_seq)
        self.db.update_activity(activity)
        try:
            timeout = int(timeout_seconds or self.config.get("wait_timeout_seconds", 0) or 0)
            if activity.deadline_at is not None:
                remaining = max(0.0, activity.deadline_at - time.time())
                timeout = min(timeout, int(remaining)) if timeout > 0 else int(remaining)
            wait_deadline = time.monotonic() + timeout if timeout > 0 else None
            while not activity.cancel_event.is_set():
                messages = self.pending_after(activity, activity.wait_baseline_seq)
                if messages:
                    for message in messages:
                        self.mark_consumed(message, activity)
                    return messages
                activity.message_event.clear()
                if self.pending_after(activity, activity.wait_baseline_seq):
                    continue
                cancel_task = __import__("asyncio").create_task(activity.cancel_event.wait())
                message_task = __import__("asyncio").create_task(activity.message_event.wait())
                waiters = {cancel_task, message_task}
                try:
                    remaining_timeout = (
                        max(0.0, wait_deadline - time.monotonic())
                        if wait_deadline is not None
                        else None
                    )
                    if remaining_timeout is not None and remaining_timeout <= 0:
                        return None
                    if remaining_timeout is not None:
                        done, _ = await __import__("asyncio").wait(
                            waiters, timeout=remaining_timeout, return_when=__import__("asyncio").FIRST_COMPLETED
                        )
                        if not done:
                            return None
                    else:
                        await __import__("asyncio").wait(
                            waiters, return_when=__import__("asyncio").FIRST_COMPLETED
                        )
                finally:
                    for task in waiters:
                        task.cancel()
                if activity.cancel_event.is_set():
                    return []
            return []
        finally:
            activity.waiter_active = False
            if activity.phase == "waiting_user" and not activity.cancel_event.is_set():
                activity.phase = "running"
            self.db.update_activity(activity)

    def action_id(
        self,
        activity: Activity,
        text: str,
        kind: str,
        segments: list[dict[str, Any]] | None = None,
        explicit: str | None = None,
    ) -> str:
        if explicit:
            return str(explicit)
        payload = json.dumps(segments or [], ensure_ascii=False, sort_keys=True, default=str)
        return f"{activity.activity_id}:{kind}:{activity.outbound_count + 1}:{hashlib.sha256(payload.encode()).hexdigest()[:12]}"

    async def send_once(
        self,
        activity: Activity,
        text: str,
        kind: str,
        segments: list[dict[str, Any]] | None = None,
        action_id: str | None = None,
    ) -> dict[str, Any]:
        text = (text or "").strip()
        serialised = serialize_segments(segments)
        errors = validate_segment_specs(
            serialised,
            media_roots=self.config.get("media_roots") or [],
            allowed_hosts=self.config.get("allowed_media_hosts") or [],
            max_media_bytes=int(self.config.get("max_media_bytes", 20 * 1024 * 1024)),
        )
        resolved_action_id = self.action_id(activity, text, kind, serialised, action_id)
        if errors:
            return {"status": "failed", "action_id": resolved_action_id, "error": "; ".join(errors)}
        async with activity.outbound_lock:
            status = self.db.outbound_status(resolved_action_id)
            if status == "sent":
                return {"status": "duplicate", "action_id": resolved_action_id}
            max_out = int(self.config.get("max_outbound_messages", 0))
            if max_out > 0 and activity.outbound_count >= max_out:
                return {"status": "failed", "action_id": resolved_action_id, "error": "outbound_limit"}
            stored_text = text
            if serialised:
                stored_text += f"\n[segments]{json.dumps(serialised, ensure_ascii=False, default=str)}"
            if not self.db.reserve_outbound(resolved_action_id, activity.activity_id, stored_text):
                if self.db.outbound_status(resolved_action_id) == "sent":
                    return {"status": "duplicate", "action_id": resolved_action_id}
                return {"status": "unknown", "action_id": resolved_action_id}
            try:
                if serialised:
                    receipt = await activity.event.send(
                        activity.event.chain_result(build_message_chain(text, serialised))
                    )
                else:
                    receipt = await activity.event.send(activity.event.plain_result(text))
            except Exception as exc:  # noqa: BLE001
                self.db.mark_outbound(resolved_action_id, "failed", str(exc))
                return {"status": "failed", "action_id": resolved_action_id, "error": str(exc)}
            platform_id = getattr(receipt, "message_id", None) if receipt is not None else None
            try:
                self.db.mark_outbound(resolved_action_id, "sent", platform_id=platform_id)
            except TypeError:
                self.db.mark_outbound(resolved_action_id, "sent")
            activity.outbound_count += 1
            self.db.update_activity(activity)
            return {
                "status": "sent",
                "action_id": resolved_action_id,
                "segments": len(serialised),
                "platform_message_id": platform_id,
            }

    async def stop(self, activity: Activity, reason: str = "user") -> None:
        activity.stop(reason)
        self.db.update_activity(activity, reason=reason)

    async def drain(self) -> None:
        tasks = [a.task for a in self.activities.values() if a.task and not a.task.done()]
        if tasks:
            await __import__("asyncio").gather(*tasks, return_exceptions=True)

