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

The server processes the message with the LLM and system prompt supplied by a
currently connected ESP32. It returns status and streamed text through the
existing `stt` and `tts` messages. If the ESP32 is offline, the browser
WebSocket remains connected for status updates, but chat requests are rejected.

## Hardware status message

When an ESP32 connects or disconnects, the server immediately pushes this to
all browser connections:

```json
{"type":"hardware_status","online":true,"connection_count":1,"llm_ready":true,"listener":"listening","ws_port":8000}
```

The payload contains no device ID, IP address, or secret. Broken connections
are detected by server-side WebSocket Ping/Pong; the listening port remains
open while the device is offline.

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
