# Browser Text-Chat Message Protocol

English | [简体中文](./WEB_MODE_ZH.md)

This document describes the WebSocket messages added for browser text chat.
See [`../../web/README_EN.md`](../../web/README_EN.md) for the user-facing
feature guide.

## Client identification

The browser connects with a `device-id` beginning with `web-`:

```text
ws://<host>:8000/xiaozhi/v1/?device-id=web-<random-value>
```

Only such browser connections may change `web_mode`. The same message from a
normal ESP32 connection does not alter the server mode.

## Handshake

After opening the connection, the browser sends:

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

Input mode can be changed after the server returns its `hello` response.

## Input modes

Enter text mode:

```json
{"type": "web_mode", "mode": "text"}
```

The server sets `text_mode_active` to `true` and pauses ESP32 microphone input.

Restore voice mode:

```json
{"type": "web_mode", "mode": "voice"}
```

The server also restores voice mode automatically when the web client
disconnects.

## Send text

```json
{"type": "listen", "state": "detect", "text": "Hello"}
```

The server processes the message with the LLM and system prompt supplied by
the most recently connected ESP32. It returns status and streamed text through
the existing `stt` and `tts` messages. If no ESP32 has come online, the server
does not create a default LLM and does not process the chat request.

## Keepalive

The server and browser normally use protocol-level WebSocket Ping/Pong. An
application-level message is also supported:

```json
{"type": "ping"}
```

Response:

```json
{"type": "pong", "timestamp": "YYYY-MM-DD HH:MM:SS"}
```
