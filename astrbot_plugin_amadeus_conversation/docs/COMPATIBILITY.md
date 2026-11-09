# 兼容性

目标版本严格为 AstrBot v4.29.4（公开提交 `61335414`）。私有 API 集中在 `compat/astrbot_4294.py`：`build_main_agent`、`MainAgentBuildConfig`、`FunctionTool`、`ToolSet`、`active_event_registry`。升级 AstrBot 后需重新检查 Runner 的 `step()` 响应类型、`ProviderRequest.func_tool`、follow_up、工具执行器注入 event 的方式以及 stop callback 注册接口。

当前支持 AstrBot 的 QQ 私聊和满足触发条件的群聊事件，并保留图片、语音、视频、文件、@、
回复和 JSON 消息段。连续工具返回结构化 segments，出站工具可以构造对应
的 `MessageChain`；本地媒体需要通过 `media_roots` 白名单。SQLite 会在重启时
标记运行 Activity 为 `interrupted`，下一条同 UMO 消息会恢复逻辑 Activity
并注入持久化 transcript，内存中的旧 Runner 本身不会被伪造为仍在运行。

所有 AstrBot 私有 API 通过 `compat/astrbot_4294.py` 访问，并在插件初始化时
校验版本 4.29.4。
