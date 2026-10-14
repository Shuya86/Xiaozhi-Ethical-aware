# Browser Text Chat

English | [简体中文](./README_ZH.md)

This directory contains xiaozhi-server's browser text-chat UI. It reuses the
agent configuration already loaded by an ESP32, so browser text and hardware
voice input share the same LLM, system prompt, memory, and intent modules.

## Hardware-first gate

In manager-api mode, the ESP32 must come online first:

1. The ESP32 connects to `/xiaozhi/v1/`.
2. The server retrieves that device's agent configuration from manager-api.
3. The server caches its LLM, ethical system prompt, and selected modules.
4. The browser connection reuses that configuration and text input becomes
   available.

Before the hardware is online, `/text_chat` can still load, but `/api/config`
returns `llm_ready: false` and the input remains disabled. This is intentional.
The browser cannot enter an API key or bypass the gate with a default LLM.

## Usage

1. Configure the device agent, LLM API key, and system prompt in manager-web.
2. Start the server and connect the ESP32 successfully.
3. Open `http://<server-address>:8003/text_chat`.
4. Switch to text mode and send a message.
5. Switch back to voice mode, or close the page, to restore the ESP32
   microphone.

Text mode temporarily pauses ESP32 microphone input so browser text and
hardware voice cannot trigger concurrent conversations. Disconnecting the web
client automatically restores voice mode.

## Endpoints used by the page

| Path | Purpose |
| --- | --- |
| `GET /text_chat` | Serves the chat page |
| `GET /api/config` | Reports modules and the `llm_ready` gate |
| `GET /ws_info` | Returns the WebSocket address for the browser |
| `GET /health` | Readiness check for deployment and monitoring |
| `ws://<host>:8000/xiaozhi/v1/` | Chat WebSocket |

The browser creates a unique `device-id` beginning with `web-`; the server uses
this prefix to identify browser clients. LLM API keys and the manager-api
secret are never sent to the browser.

## Main files

- `text_chat.html`: UI, localization, reconnect logic, input modes, and latency
  display.
- `../http_server.py`: page, configuration, address, and health endpoints.
- `../websocket_server.py`: shared device configuration and input-mode state.
- `../handle/textHandler/webModeMessageHandler.py`: input-mode message handler.

See
[`../handle/textHandler/WEB_MODE_EN.md`](../handle/textHandler/WEB_MODE_EN.md)
for the WebSocket protocol notes.

## Troubleshooting

- LLM shows Offline: confirm that the ESP32 is connected and its agent has a
  working LLM. Reconnect the device after changing its agent configuration.
- The page cannot connect: check ports `8000` and `8003`, firewall rules, and
  the `/health` endpoint.
- The ESP32 microphone is inactive: switch the page back to voice mode or
  close it so the server resets the mode.
