from __future__ import annotations

import sys
import time
from pathlib import Path
from typing import Any

from astrbot.api import AstrBotConfig, star
from astrbot.api.event import AstrMessageEvent, filter

from .compat.astrbot_4294 import assert_compatible, get_data_dir
from .runtime.activity import IncomingMessage
from .runtime.coordinator import Coordinator
from .runtime.runner import RunnerService
from .storage.database import ActivityDatabase


class AmadeusConversationPlugin(star.Star):
    name = "astrbot_plugin_amadeus_conversation"
    author = "Amadeus"

    def __init__(self, context: Any, config: AstrBotConfig | None = None):
        assert_compatible()
        super().__init__(context, config)
        self.config = dict(config or {})
        data_dir = get_data_dir(self.name)
        self.db = ActivityDatabase(Path(data_dir) / "activities.sqlite3")
        self.coordinator = Coordinator(self.db, self.config)
        self.runner_service = RunnerService(context, self.coordinator, self.config)
        self.coordinator.attach_runner_service(self.runner_service)
        self._group_last_prompt: dict[str, float] = {}

    def _message(self, event: AstrMessageEvent) -> IncomingMessage:
        obj = getattr(event, "message_obj", None)
        mid = str(
            getattr(obj, "message_id", None)
            or getattr(obj, "id", None)
            or f"{event.unified_msg_origin}:{event.created_at}"
        )
        return IncomingMessage(
            mid,
            event.unified_msg_origin,
            str(event.get_sender_id()),
            event.get_message_str(),
            list(event.get_messages() or []),
            event,
        )

    def _group_triggered(self, event: AstrMessageEvent) -> bool:
        components = list(event.get_messages() or [])
        self_id = str(event.get_self_id() or "")
        for component in components:
            typ = str(getattr(getattr(component, "type", None), "value", getattr(component, "type", ""))).lower()
            if typ == "at" and str(getattr(component, "qq", "")) in {self_id, "all"}:
                return True
            if typ == "reply" and str(getattr(component, "sender_id", "")) == self_id:
                return True
        raw = (event.get_message_str() or "").strip().lower()
        triggers = [str(item).lower() for item in (self.config.get("group_trigger_words") or [])]
        return bool(triggers and any(trigger and trigger in raw for trigger in triggers))

    def _accepts(self, event: AstrMessageEvent) -> bool:
        if not bool(self.config.get("enabled", True)):
            return False
        allowed_platforms = {str(item) for item in (self.config.get("platforms") or ["aiocqhttp"])}
        if event.get_platform_name() not in allowed_platforms:
            return False
        if str(event.get_sender_id()) and str(event.get_sender_id()) == str(event.get_self_id() or ""):
            return False
        allowed = self.config.get("allowed_umos") or []
        if allowed and event.unified_msg_origin not in allowed:
            return False
        is_group = bool(event.get_group_id())
        if not bool(self.config.get("group_enabled" if is_group else "private_enabled", True)):
            return False
        if is_group and not bool(self.config.get("group_autonomous", False)):
            if not self._group_triggered(event):
                return False
            cooldown = int(self.config.get("group_cooldown_seconds", 0))
            if cooldown > 0:
                # Once a group already has an Activity, explicit @/reply/trigger
                # messages must reach its FIFO even during cooldown.
                if self.coordinator.get(event.unified_msg_origin) is not None:
                    return True
                last = self._group_last_prompt.get(event.unified_msg_origin, 0.0)
                now = time.time()
                if now - last < cooldown:
                    return False
                self._group_last_prompt[event.unified_msg_origin] = now
        return True

    @filter.command("amadeus")
    async def command(self, event: AstrMessageEvent):
        args = (event.message_str or "").strip().split(maxsplit=1)
        action = args[1].lower() if len(args) > 1 else "status"
        activity = self.coordinator.get(event.unified_msg_origin)
        if action in {"stop", "reset"} and activity:
            await self.coordinator.stop(activity, action)
            yield event.plain_result("已停止当前连续 Activity。" if action == "stop" else "已重置当前连续 Activity。")
        elif action == "status":
            yield event.plain_result(f"Activity: {activity.phase if activity else 'idle'}")

    @filter.event_message_type(filter.EventMessageType.ALL, priority=sys.maxsize)
    async def on_message(self, event: AstrMessageEvent):
        raw_text = (event.get_message_str() or "").strip()
        if raw_text == "/stop" or raw_text.startswith("/amadeus"):
            return
        if not self._accepts(event):
            return
        message = self._message(event)
        event.stop_event()
        activity = self.coordinator.get(message.umo)
        if activity is None:
            activity = self.coordinator.restore(event, message) or self.coordinator.create(event, message)
            return
        await self.coordinator.enqueue(activity, event, message)

    async def terminate(self):
        for activity in list(self.coordinator.activities.values()):
            await self.coordinator.stop(activity, "plugin_shutdown")
        await self.coordinator.drain()
        self.db.close()

