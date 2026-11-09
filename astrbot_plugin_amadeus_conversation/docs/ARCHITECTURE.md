# 架构

插件在最高优先级消息监听器中接管 QQ 私聊和满足触发条件的群聊（群聊默认
关闭）。同一 `unified_msg_origin` 只有一个逻辑 `Activity` 和一个 dispatcher
task。入站消息先写入按 UMO 分配 seq 的 SQLite FIFO，再由 dispatcher 串行
调度；Runner 正在运行时进入同一 Runner 的 `follow_up`，等待工具按 durable
seq 批量领取新消息，普通回答后仍由同一个 Activity task 接管下一轮。

Runner 使用 AstrBot 4.29.4 的 `build_main_agent` 和 `ToolLoopAgentRunner.step()`，
移除内置 `send_message_to_user`，加入三个受控工具。入站组件会序列化为
结构化 segments；`amadeus_send_message` 可构造图片、语音、视频、文件、@、
回复和 JSON 消息链。中间消息要求显式 action_id，outbox 记录 receipt 和
`unknown` 状态。实例重启后，下一条同 UMO 消息会恢复最近的 interrupted
Activity，并注入持久化 transcript；stop callback 会唤醒 wait、阻止 final，
插件卸载会等待 dispatcher task 排空。
