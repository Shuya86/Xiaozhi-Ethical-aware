# 网页文本聊天消息协议

[English](./WEB_MODE_EN.md) | 简体中文

本文说明网页文本聊天新增的 WebSocket 消息。完整界面说明见
[`../../web/README_ZH.md`](../../web/README_ZH.md)。

## 客户端识别

网页使用以 `web-` 开头的 `device-id` 连接：

```text
ws://<host>:8000/xiaozhi/v1/?device-id=web-<随机值>
```

只有这样的网页连接可以修改 `web_mode`，普通 ESP32 连接发送该消息不会
改变服务器模式。

## 握手

连接建立后，网页发送：

```json
{
  "type": "hello",
  "version": 1,
  "transport": "websocket",
  "audio_params": {
    "format": "opus",
    "sample_rate": 16000,
    "channels": 1,
    "frame_duration": 60
  }
}
```

收到服务器的 `hello` 后才可切换输入模式。

## 输入模式

进入文本模式：

```json
{"type": "web_mode", "mode": "text"}
```

服务器将 `text_mode_active` 设为 `true` 并暂停 ESP32 麦克风输入。

恢复语音模式：

```json
{"type": "web_mode", "mode": "voice"}
```

网页断开时，服务器也会自动恢复语音模式。

## 发送文本

```json
{"type": "listen", "state": "detect", "text": "你好"}
```

服务器使用最近一次在线 ESP32 提供的 LLM 和系统提示词处理消息，并通过
原有的 `stt`、`tts` 消息返回状态与分段文本。如果 ESP32 尚未上线，服务
器不会创建默认 LLM，也不会处理聊天请求。

## 保活

服务器和浏览器默认使用 WebSocket 协议级 Ping/Pong。应用层也支持：

```json
{"type": "ping"}
```

返回：

```json
{"type": "pong", "timestamp": "YYYY-MM-DD HH:MM:SS"}
```
