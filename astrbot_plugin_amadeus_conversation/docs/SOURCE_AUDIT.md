# AstrBot 4.29.4 源码审查记录

- `Context.tool_loop_agent()` 内部创建并消费 Runner，不适合长期持有；实现使用 `build_main_agent()` 返回的 Runner。
- `ToolLoopAgentRunner.follow_up(message_text=...)` 会把消息排入 pending follow-up；协调器在 `running` 阶段只调用这一接口。
- ProcessStage 会优先处理全局 `SessionWaiter`；本插件等待使用 Activity 自己的 `asyncio.Queue`，避免同一消息同时进入 wait 结果和 follow-up。
- `event.stop_event()` 会影响 AstrBot 默认 `run_agent()` 的停止判断；实现只调用 `runner.step()`，以 Activity 的 cancel event 作为插件停止条件。
- AstrBot EventBus 为消息建立独立 pipeline task，因此第一条 handler 可以保持 Activity 生命周期，后续同 UMO 消息仍能并发进入路由器。
- `active_event_registry` 提供 `register_agent_stop_callback` / `unregister_agent_stop_callback`，用于 `/stop` 和内置停止回调。

参考源码：`astrbot/core/context.py`、`astrbot/core/astr_main_agent.py`、`astrbot/core/agent/runners/tool_loop_agent_runner.py`、`astrbot/core/astr_agent_run_util.py`、`astrbot/core/pipeline/process_stage/follow_up.py`、`astrbot/core/utils/active_event_registry.py`、`astrbot/core/platform/astr_message_event.py`、`astrbot/core/event_bus.py`、`astrbot/core/pipeline/scheduler.py`、`astrbot/core/star/star_handler.py`。
