from __future__ import annotations

import asyncio
import json
import sys
from pathlib import Path
from typing import Any

from astrbot.api import AstrBotConfig, star
from astrbot.api.event import AstrMessageEvent, filter
from astrbot.core.star.star_tools import StarTools

from .runtime.activity import IncomingMessage
from .runtime.coordinator import Coordinator
from .runtime.runner import RunnerService
from .storage.database import ActivityDatabase


class AmadeusConversationPlugin(star.Star):
    name = "astrbot_plugin_amadeus_conversation"
    author = "Amadeus"

    def __init__(self, context, config: AstrBotConfig | None = None):
        super().__init__(context, config)
        self.config = dict(config or {})
        data_dir = StarTools.get_data_dir(self.name)
        self.db = ActivityDatabase(Path(data_dir) / "activities.sqlite3")
        self.coordinator = Coordinator(self.db, self.config)
        self.runner_service = RunnerService(context, self.coordinator, self.config)

    def _message(self, event: AstrMessageEvent) -> IncomingMessage:
        obj = getattr(event, "message_obj", None)
        mid = str(getattr(obj, "message_id", None) or getattr(obj, "id", None) or f"{event.unified_msg_origin}:{event.created_at}")
        return IncomingMessage(mid, event.unified_msg_origin, str(event.get_sender_id()), event.get_message_str(), list(getattr(obj, "message", []) or []), event)

    def _accepts(self, event: AstrMessageEvent) -> bool:
        if not bool(self.config.get("enabled", True)):
            return False
        allowed = self.config.get("allowed_umos") or []
        if allowed and event.unified_msg_origin not in allowed:
            return False
        is_group = bool(event.get_group_id())
        return bool(self.config.get("group_enabled" if is_group else "private_enabled", True))

    @filter.command("amadeus")
    async def command(self, event: AstrMessageEvent):
        args = (event.message_str or "").strip().split(maxsplit=1)
        action = args[1].lower() if len(args) > 1 else "status"
        activity = self.coordinator.get(event.unified_msg_origin)
        if action == "stop" and activity:
            await self.coordinator.stop(activity, "command")
            yield event.plain_result("已停止当前连续 Activity。")
        elif action == "reset" and activity:
            await self.coordinator.stop(activity, "reset")
            yield event.plain_result("已重置当前连续 Activity。")
        elif action == "status":
            yield event.plain_result(f"Activity: {activity.phase if activity else 'idle'}")

    @filter.command("stop")
    async def builtin_stop(self, event: AstrMessageEvent):
        activity = self.coordinator.get(event.unified_msg_origin)
        if activity:
            await self.coordinator.stop(activity, "builtin_stop")
            yield event.plain_result("已停止当前连续 Activity。")

    @filter.event_message_type(filter.EventMessageType.ALL, priority=sys.maxsize)
    async def on_message(self, event: AstrMessageEvent):
        raw_text = (event.get_message_str() or "").strip()
        if raw_text == "/stop" or raw_text.startswith("/amadeus"):
            return
        if not self._accepts(event):
            return
        message = self._message(event)
        activity = self.coordinator.get(message.umo)
        event.stop_event()
        if activity is None:
            activity = self.coordinator.restore(event, message) or self.coordinator.create(event, message)
            await self.runner_service.run_activity(activity, message.text)
            return

        # A normal final answer leaves the Activity armed instead of closing it.
        # The next QQ event starts a fresh Runner with the same Activity ID and
        # persisted transcript, so the model does not have to remember to call
        # a special wait tool just to keep a chat alive.
        if activity.phase == "waiting_user" and activity.runner is None:
            if self.coordinator.accept_message(activity, message):
                activity.event = event
                activity.resume_context = self.db.transcript(
                    message.umo,
                    activity.activity_id,
                    int(self.config.get("resume_history_messages", 120)),
                )
                await self.runner_service.run_activity(activity, message.text)
            return
        result = await self.coordinator.route_follow_up(activity, message)
        if result == "limit":
            await self.coordinator.send_once(activity, "连续消息数量已达到上限。", "limit")
        elif result == "runner_done":
            activity.event = event
            activity.resume_context = self.db.transcript(
                message.umo,
                activity.activity_id,
                int(self.config.get("resume_history_messages", 120)),
            )
            await self.runner_service.run_activity(activity, message.text)

    async def terminate(self):
        for activity in list(self.coordinator.activities.values()):
            await self.coordinator.stop(activity, "plugin_shutdown")
        self.db.close()

