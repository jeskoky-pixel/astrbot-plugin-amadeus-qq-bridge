# 测试报告

单元测试覆盖同 UMO 复用 Runner、等待消息不进入 follow-up、消息去重和
Activity 映射；源码使用 `compileall` 检查。AstrBot 实例环境还通过了消息
段序列化和图片消息链构造的导入检查。真实 NapCat 验收应发送文本、图片、
语音或文件，确认 `message_received` 同时包含 `content` 与 `segments`，再
确认 `amadeus_send_message` 的消息链被 NapCat 发送。
