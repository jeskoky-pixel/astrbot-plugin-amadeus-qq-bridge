from __future__ import annotations

import asyncio
import time
from dataclasses import replace
from typing import Any

from ..compat.astrbot_4294 import MainAgentBuildConfig, active_event_registry, build_main_agent
from ..tools.definitions import build_tools


class RunnerService:
    def __init__(self, context: Any, coordinator: Any, config: dict[str, Any]):
        self.context, self.coordinator, self.config = context, coordinator, config

    async def run_activity(self, activity: Any, first_text: str) -> None:
        activity.phase = "running"
        activity.final_sent = False
        activity_instructions = (
            "\n\n[Amadeus QQ Continuous Activity]\n"
            "你正在一个可持续的 QQ Activity 中运行。必须遵守：\n"
            "1. 需要用户看到进度、提问或中间结果时，使用 amadeus_send_message；不要用默认发送工具。\n"
            "2. 需要在同一次推理中等待用户时，使用 amadeus_wait_for_user；它会保留文本和富消息段。\n"
            "3. 普通回复后 Activity 会自动保持可继续状态，下一条 QQ 消息会回到同一个 Activity，不要创建第二个 Agent。\n"
            "4. 只有用户明确要求停止，或确认任务彻底结束时，才调用 amadeus_end_activity。\n"
        )
        if activity.resume_context:
            activity_instructions += (
                "\n这是一次重启后的 Activity 恢复。以下是之前的会话记录，请保持人物、目标和上下文连续：\n"
                f"<amadeus_resume_transcript>\n{activity.resume_context}\n</amadeus_resume_transcript>\n"
            )
        cfg = MainAgentBuildConfig(
            tool_call_timeout=int(self.config.get("tool_call_timeout", 120)),
            streaming_response=False,
            add_cron_tools=False,
            max_context_length=int(self.config.get("max_context_length", -1)),
        )
        result = await build_main_agent(
            event=activity.event,
            plugin_context=self.context,
            config=cfg,
            apply_reset=False,
        )
        activity.runner = result.agent_runner
        if result.provider_request.func_tool is None:
            from ..compat.astrbot_4294 import ToolSet
            result.provider_request.func_tool = ToolSet()
        result.provider_request.func_tool.remove_tool("send_message_to_user")
        result.provider_request.func_tool.merge(build_tools(activity, self.coordinator, int(self.config.get("wait_timeout_seconds", 300))))
        result.provider_request.system_prompt = (
            f"{result.provider_request.system_prompt or ''}{activity_instructions}"
        )
        if result.reset_coro is not None:
            await result.reset_coro
        max_steps = int(self.config.get("max_agent_steps", 0))
        activity.runner._step_budget_max = max_steps if max_steps > 0 else None
        active_event_registry.register_agent_stop_callback(activity.event, activity.runner.request_stop)
        max_seconds = int(self.config.get("max_activity_seconds", 0))
        deadline = time.monotonic() + max_seconds if max_seconds > 0 else None
        try:
            while not activity.runner.done() and not activity.cancel_event.is_set():
                if deadline is not None and time.monotonic() >= deadline:
                    await self.coordinator.stop(activity, "activity_timeout")
                    break
                async for response in activity.runner.step():
                    if activity.cancel_event.is_set():
                        activity.runner.request_stop()
                    # Tool calls are intentionally not sent to QQ. Final output is sent once below.
                if activity.runner.done():
                    break
            if not activity.cancel_event.is_set():
                final = activity.runner.get_final_llm_resp()
                text = getattr(final, "completion_text", None) if final else None
                if text and not activity.final_sent:
                    activity.final_sent = True
                    await self.coordinator.send_once(activity, text, "final")
                # Keep the logical Activity alive after a normal answer.  The
                # next event will rearm a new Runner using the same transcript.
                if not activity.explicit_end:
                    activity.phase = "waiting_user"
                else:
                    activity.phase = "finished"
        except Exception as exc:  # noqa: BLE001
            activity.phase = "failed"
            self.coordinator.db.update_activity(activity, error=str(exc))
            raise
        finally:
            active_event_registry.unregister_agent_stop_callback(activity.event)
            if activity.phase not in {"finished", "failed", "stopping", "waiting_user"}:
                activity.phase = "finished"
            self.coordinator.db.update_activity(activity)
            activity.runner = None

