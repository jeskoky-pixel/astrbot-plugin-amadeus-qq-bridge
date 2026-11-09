from __future__ import annotations

import json
from typing import Any

from ..compat.astrbot_4294 import FunctionTool, ToolSet
from ..runtime.message_codec import serialize_segments


def build_tools(activity: Any, coordinator: Any, wait_timeout: int) -> ToolSet:
    async def send_message(
        event,
        action_id: str,
        text: str = "",
        segments: list[dict[str, Any]] | None = None,
    ) -> str:
        if not action_id or not str(action_id).strip():
            return json.dumps({"status": "failed", "error": "action_id_required"}, ensure_ascii=False)
        return json.dumps(
            await coordinator.send_once(
                activity,
                text,
                "intermediate",
                serialize_segments(segments),
                action_id=str(action_id),
            ),
            ensure_ascii=False,
        )

    async def wait_for_user(event, timeout_seconds: int = 0) -> str:
        if activity.cancel_event.is_set():
            return json.dumps({"status": "stopped"}, ensure_ascii=False)
        messages = await coordinator.wait_for_user(activity, int(timeout_seconds or wait_timeout or 0))
        if activity.cancel_event.is_set():
            return json.dumps({"status": "stopped"}, ensure_ascii=False)
        if messages is None:
            return json.dumps({"status": "timeout"}, ensure_ascii=False)
        for message in messages:
            rich = serialize_segments(message.segments)
            if any(item.get("type") not in {"text", "plain"} for item in rich):
                activity.rich_followups[message.message_id] = rich
        return json.dumps(
            {
                "status": "message_received" if messages else "stopped",
                "count": len(messages),
                "messages": [
                    {
                        "message_id": message.message_id,
                        "sender_id": message.sender_id,
                        "text": message.text,
                        "content": message.agent_text(),
                        "segments": serialize_segments(message.segments),
                    }
                    for message in messages
                ],
            },
            ensure_ascii=False,
            default=str,
        )

    async def end_activity(event, reason: str = "model") -> str:
        await coordinator.stop(activity, reason)
        return json.dumps({"status": "ended", "reason": reason}, ensure_ascii=False)

    return ToolSet(
        [
            FunctionTool(
                name="amadeus_send_message",
                description="向当前 QQ 会话发送消息。action_id 必须是本次动作唯一且可重试的幂等标识；segments 可发送图片、语音、视频、文件、@、回复或 JSON。",
                parameters={
                    "type": "object",
                    "properties": {
                        "action_id": {"type": "string"},
                        "text": {"type": "string"},
                        "segments": {"type": "array", "items": {"type": "object"}},
                    },
                    "required": ["action_id"],
                },
                handler=send_message,
            ),
            FunctionTool(
                name="amadeus_wait_for_user",
                description="等待当前 QQ 会话的新消息。返回值包含一批结构化 segments，并且可被 stop 立即打断。普通回复后 Activity 会自动保持可继续状态。",
                parameters={
                    "type": "object",
                    "properties": {"timeout_seconds": {"type": "integer", "minimum": 0}},
                    "required": [],
                },
                handler=wait_for_user,
            ),
            FunctionTool(
                name="amadeus_end_activity",
                description="明确结束当前连续 Activity。只有用户要求停止或任务彻底结束时调用。",
                parameters={
                    "type": "object",
                    "properties": {"reason": {"type": "string"}},
                    "required": [],
                },
                handler=end_activity,
            ),
        ]
    )
