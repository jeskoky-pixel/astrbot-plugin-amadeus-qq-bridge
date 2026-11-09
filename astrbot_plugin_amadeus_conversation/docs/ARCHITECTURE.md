# 架构

插件在最高优先级消息监听器中接管 QQ 私聊和群聊（群聊默认开启）。同一
`unified_msg_origin` 只有一个逻辑 `Activity`。Runner 正在运行时，后续事件
进入同一 Runner 的 `follow_up`；Runner 调用等待工具时进入 `wait_queue`；
Runner 正常给出最终回答后，Activity 会自动保持 `waiting_user`，下一条消息
会用同一个 Activity ID 重建 Runner，因此模型不必自己维持一个永不结束的
工具循环。

Runner 使用 AstrBot 4.29.4 的 `build_main_agent` 和 `ToolLoopAgentRunner.step()`，
移除内置 `send_message_to_user`，加入三个受控工具。入站组件会序列化为
结构化 segments；`amadeus_send_message` 可构造图片、语音、视频、文件、@、
回复和 JSON 消息链。中间消息和最终消息通过包含 segments 的 action_id 写入
SQLite 并去重。实例重启后，下一条同 UMO 消息会恢复最近的 interrupted
Activity，并注入持久化 transcript。
