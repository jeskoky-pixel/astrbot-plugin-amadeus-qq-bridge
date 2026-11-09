from __future__ import annotations

import json
from typing import Any

from ..compat.astrbot_4294 import FunctionTool, ToolSet
from ..runtime.message_codec import serialize_segments


def build_tools(activity: Any, coordinator: Any, wait_timeout: int) -> ToolSet:
    async def send_message(event, text: str = "", segments: list[dict[str, Any]] | None = None) -> str:
        return json.dumps(
            await coordinator.send_once(activity, text, "intermediate", serialize_segments(segments)),
            ensure_ascii=False,
        )

    async def wait_for_user(event, timeout_seconds: int = 300) -> str:
        if activity.cancel_event.is_set():
            return json.dumps({"status": "stopped"})
        activity.phase = "waiting_user"
        coordinator.db.update_activity(activity)
        try:
            requested = int(timeout_seconds or wait_timeout or 0)
            if wait_timeout <= 0:
                message = await activity.wait_queue.get()
            else:
                timeout = max(5, min(requested or wait_timeout, wait_timeout))
                message = await __import__("asyncio").wait_for(activity.wait_queue.get(), timeout=timeout)
            return json.dumps(
                {
                    "status": "message_received",
                    "message_id": message.message_id,
                    "sender_id": message.sender_id,
                    "text": message.text,
                    "content": message.agent_text(),
                    "segments": serialize_segments(message.segments),
                },
                ensure_ascii=False,
                default=str,
            )
        except __import__("asyncio").TimeoutError:
            return json.dumps({"status": "timeout"})
        finally:
            if activity.phase == "waiting_user":
                activity.phase = "running"
                coordinator.db.update_activity(activity)

    async def end_activity(event, reason: str = "model") -> str:
        await coordinator.stop(activity, reason)
        return json.dumps({"status": "ended", "reason": reason}, ensure_ascii=False)

    return ToolSet([
        FunctionTool(
            name="amadeus_send_message",
            description="立即向当前 QQ 会话发送消息。可只填 text，也可用 segments 发送图片、语音、视频、文件、@、回复等富消息。发送后 Activity 不会结束。",
            parameters={"type": "object", "properties": {"text": {"type": "string"}, "segments": {"type": "array", "items": {"type": "object"}}}, "required": []},
            handler=send_message,
        ),
        FunctionTool(
            name="amadeus_wait_for_user",
            description="等待当前 QQ 会话的下一条消息。无论用户发送文本、图片、语音、视频还是文件，返回值都会包含 content 和完整 segments。正常聊天不需要显式调用此工具：Activity 会在回复后自动保持可继续状态；需要在同一次推理中等待时才调用。",
            parameters={"type": "object", "properties": {"timeout_seconds": {"type": "integer", "minimum": 5}}, "required": []},
            handler=wait_for_user,
        ),
        FunctionTool(name="amadeus_end_activity", description="结束当前连续 Activity。只有确认不需要继续等待用户时才调用。", parameters={"type": "object", "properties": {"reason": {"type": "string"}}, "required": []}, handler=end_activity),
    ])
