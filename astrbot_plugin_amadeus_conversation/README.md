# Amadeus QQ Bridge

这是一个面向 AstrBot 4.29.4 的 QQ 连续对话插件。它在每个
`unified_msg_origin` 上维护一个逻辑 Activity，让 QQ 私聊和群聊在 Agent
回复后仍保持可继续状态，并把下一条消息重新交给同一个 Activity。

把 `astrbot_plugin_amadeus_conversation` 目录复制到 AstrBot 的
`data/plugins/`，重启实例后在插件配置中启用。群聊默认开启，仍可通过
`allowed_umos` 限制接管范围。

插件支持 QQ 文本、图片、语音、视频、文件、@、回复和 JSON 等消息段：

- 入站富消息会保存在 Activity 记录中，并通过 `amadeus_wait_for_user` 返回
  `content` 与结构化 `segments`。
- `amadeus_send_message` 支持 `text` 与 `segments`，可以由 Agent 发送图片、
  语音、视频、文件、@ 和回复。
- 默认不限制 Agent 步数、Activity 生命周期、连续消息数和出站消息数；把
  对应配置设为大于 0 才启用限制。
- AstrBot 重启后，下一条同一 QQ 会话消息会恢复最近一次被中断的 Activity，
  并把持久化的对话记录注入恢复上下文。

支持 `/amadeus status`、`/amadeus stop`、`/amadeus reset` 和 `/stop`。

## 配置要点

`group_enabled` 默认是 `true`。如需无限等待用户，把
`wait_timeout_seconds` 设为 `0`；如需恢复重启后的会话，保持
`resume_after_restart` 为 `true`。

## 测试

在插件目录执行：

```text
python -m unittest discover -s tests -v
```
