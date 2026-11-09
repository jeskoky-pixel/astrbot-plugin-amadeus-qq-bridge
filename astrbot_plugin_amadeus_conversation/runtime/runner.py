from __future__ import annotations

import asyncio
import time
from typing import Any

from ..compat.astrbot_4294 import (
    MainAgentBuildConfig,
    ToolSet,
    active_event_registry,
    build_main_agent,
    agent_message_classes,
)
from ..tools.definitions import build_tools


class RunnerService:
    def __init__(self, context: Any, coordinator: Any, config: dict[str, Any]):
        self.context, self.coordinator, self.config = context, coordinator, config

    def create_dispatch_task(self, activity: Any) -> asyncio.Task:
        return asyncio.create_task(self._dispatch(activity), name=f"amadeus:{activity.umo}")

    async def _dispatch(self, activity: Any) -> None:
        try:
            while not activity.cancel_event.is_set():
                message = self.coordinator.next_pending(activity)
                if message is not None:
                    self.coordinator.mark_consumed(message, activity)
                    activity.event = message.event or activity.event
                    await self._run_one_turn(activity, message)
                    self.coordinator.requeue_followups(activity)
                    if activity.cancel_event.is_set():
                        break
                    continue

                activity.message_event.clear()
                cancel_task = asyncio.create_task(activity.cancel_event.wait())
                message_task = asyncio.create_task(activity.message_event.wait())
                waiters = {cancel_task, message_task}
                try:
                    timeout = self._remaining(activity)
                    if timeout is None:
                        await asyncio.wait(waiters, return_when=asyncio.FIRST_COMPLETED)
                    else:
                        done, _ = await asyncio.wait(
                            waiters,
                            timeout=timeout,
                            return_when=asyncio.FIRST_COMPLETED,
                        )
                        if not done:
                            await self.coordinator.stop(activity, "activity_timeout")
                finally:
                    for task in waiters:
                        task.cancel()
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # noqa: BLE001
            activity.phase = "failed"
            self.coordinator.db.update_activity(activity, error=str(exc))
        finally:
            if activity.phase not in {"finished", "failed"} and activity.cancel_event.is_set():
                activity.phase = "stopping"
                self.coordinator.db.update_activity(activity)

    def _remaining(self, activity: Any) -> float | None:
        if activity.deadline_at is None:
            return None
        return max(0.0, activity.deadline_at - time.time())

    async def _run_one_turn(self, activity: Any, message: Any) -> None:
        activity.phase = "starting"
        activity.final_sent = False
        instructions = (
            "\n\n[Amadeus QQ Continuous Activity]\n"
            "你正在一个可持续 QQ Activity 中运行。必须遵守：\n"
            "1. 中间进度或富消息使用 amadeus_send_message，并为每次发送提供新的 action_id。\n"
            "2. 需要在同一次推理中等待用户时使用 amadeus_wait_for_user；它会返回一批结构化 QQ 消息。\n"
            "3. 普通回复后 Activity 会自动保持可继续状态；不要创建第二个 Agent。\n"
            "4. 只有用户明确要求停止或任务彻底结束时才调用 amadeus_end_activity。\n"
        )
        if activity.resume_context:
            instructions += (
                "\n这是一次重启后的 Activity 恢复，请保持以下历史上下文连续；当前事件是新消息，不要重复引用它：\n"
                f"<amadeus_resume_transcript>\n{activity.resume_context}\n</amadeus_resume_transcript>\n"
            )
        cfg = MainAgentBuildConfig(
            tool_call_timeout=int(self.config.get("tool_call_timeout", 86400)),
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
            result.provider_request.func_tool = ToolSet()
        result.provider_request.func_tool.remove_tool("send_message_to_user")
        result.provider_request.func_tool.merge(
            build_tools(
                activity,
                self.coordinator,
                int(self.config.get("wait_timeout_seconds", 0)),
            )
        )
        result.provider_request.system_prompt = (
            f"{result.provider_request.system_prompt or ''}{instructions}"
        )
        if result.reset_coro is not None:
            await result.reset_coro
        activity.resume_context = ""
        activity.phase = "running"
        max_steps = int(self.config.get("max_agent_steps", 0))
        stop_callback = self._make_stop_callback(activity)
        active_event_registry.register_agent_stop_callback(activity.event, stop_callback)
        try:
            if max_steps > 0:
                self._inject_rich_followups(activity)
                async for response in activity.runner.step_until_done(max_steps):
                    self._handle_response(activity, response)
            else:
                while not activity.runner.done() and not activity.cancel_event.is_set():
                    remaining = self._remaining(activity)
                    if remaining is not None and remaining <= 0:
                        await self.coordinator.stop(activity, "activity_timeout")
                        break
                    self._inject_rich_followups(activity)
                    async for response in activity.runner.step():
                        self._handle_response(activity, response)
                    if activity.runner.done():
                        break
            aborted = activity.aborted or bool(
                getattr(activity.runner, "was_aborted", lambda: False)()
            )
            if not activity.cancel_event.is_set() and not aborted:
                final = activity.runner.get_final_llm_resp()
                text = getattr(final, "completion_text", None) if final else None
                if text:
                    turn_id = f"{activity.activity_id}:turn:{activity.outbound_count + 1}"
                    await self.coordinator.send_once(
                        activity, text, "final", action_id=f"final:{turn_id}"
                    )
                activity.phase = "waiting_user"
            else:
                activity.phase = "stopping" if activity.cancel_event.is_set() else "finished"
        except Exception as exc:  # noqa: BLE001
            activity.phase = "failed"
            self.coordinator.db.update_activity(activity, error=str(exc))
            raise
        finally:
            active_event_registry.unregister_agent_stop_callback(activity.event)
            activity.runner = None
            if activity.phase not in {"waiting_user", "failed", "stopping", "finished"}:
                activity.phase = "finished"
            self.coordinator.db.update_activity(activity)

    def _make_stop_callback(self, activity: Any):
        def stop_callback() -> None:
            activity.stop("builtin_stop")
            self.coordinator.db.update_activity(activity, reason="builtin_stop")

        return stop_callback

    def _handle_response(self, activity: Any, response: Any) -> None:
        response_type = getattr(response, "type", None)
        activity.record_response(response_type)
        value = getattr(response_type, "value", response_type)
        if value == "aborted":
            activity.aborted = True
        if value in {"err", "agent_runner_error"}:
            data = getattr(response, "data", None)
            activity.last_error = str(data) if data is not None else "agent runner error"

    def _inject_rich_followups(self, activity: Any) -> None:
        if not activity.rich_followups or not getattr(activity.runner, "run_context", None):
            return
        classes = agent_message_classes()
        parts: list[Any] = []
        for segments in activity.rich_followups.values():
            for segment in segments:
                if not isinstance(segment, dict):
                    continue
                data = segment.get("data") if isinstance(segment.get("data"), dict) else segment
                typ = str(segment.get("type", "")).lower()
                source = str(data.get("url") or data.get("file") or "")
                if typ == "image" and source.startswith(("http://", "https://")):
                    parts.append(classes["ImageURLPart"](image_url={"url": source}))
                elif typ in {"record", "audio", "voice"} and source.startswith(("http://", "https://")):
                    parts.append(classes["AudioURLPart"](audio_url={"url": source}))
        if parts:
            parts.insert(0, classes["TextPart"](text="QQ 富消息已作为多模态输入附加在本条消息中。"))
            activity.runner.run_context.messages.append(classes["Message"](role="user", content=parts))
        activity.rich_followups.clear()

