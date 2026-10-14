# 网页文本聊天

[English](./README_EN.md) | 简体中文

本目录包含 xiaozhi-server 自带的网页文本聊天界面。页面通过 WebSocket
复用 ESP32 已加载的智能体配置，使网页输入与硬件语音输入使用同一个
LLM、系统提示词、记忆和意图模块。

## 硬件优先门控

在 manager-api 模式下，必须先让 ESP32 上线：

1. ESP32 连接 `/xiaozhi/v1/`；
2. 服务器从 manager-api 获取该设备的智能体配置；
3. 服务器缓存设备的 LLM、伦理系统提示词和所选模块；
4. 网页连接复用这些配置，文本输入才可用。

硬件尚未上线时，`/text_chat` 页面可以正常打开，`/api/config` 会返回
`llm_ready: false`，输入区保持不可用。这是预期行为，不能通过网页端
填写 API Key 或使用默认 LLM 绕过。

## 使用方法

1. 在智控台配置设备智能体、LLM API Key 和系统提示词；
2. 启动服务器并让 ESP32 成功连接；
3. 打开 `http://<服务器地址>:8003/text_chat`；
4. 切换到文本模式后发送消息；
5. 返回语音模式或关闭网页后，ESP32 麦克风恢复工作。

文本模式会暂时暂停 ESP32 的麦克风输入，避免网页文字和硬件语音同时
触发对话。网页断开时服务器会自动恢复语音模式。

## 页面使用的接口

| 路径 | 用途 |
| --- | --- |
| `GET /text_chat` | 返回聊天页面 |
| `GET /api/config` | 返回模块状态和 `llm_ready` 门控状态 |
| `GET /ws_info` | 返回浏览器应连接的 WebSocket 地址 |
| `GET /health` | 部署器和监控使用的就绪检查 |
| `ws://<host>:8000/xiaozhi/v1/` | 聊天 WebSocket |

浏览器生成以 `web-` 开头的独立 `device-id`，服务器据此识别网页连接。
API Key 和 manager-api secret 不会发送到浏览器。

## 主要文件

- `text_chat.html`：界面、语言切换、自动重连、输入模式和延迟显示；
- `../http_server.py`：页面、配置、地址和健康检查接口；
- `../websocket_server.py`：共享设备配置与文本/语音模式状态；
- `../handle/textHandler/webModeMessageHandler.py`：模式切换消息处理。

WebSocket 消息协议的开发说明见
[`../handle/textHandler/WEB_MODE_ZH.md`](../handle/textHandler/WEB_MODE_ZH.md)。

## 常见问题

- 页面显示 LLM Offline：确认 ESP32 已连接，并在智控台为其智能体配置了
  可用的 LLM；修改智能体配置后让设备重新连接。
- 页面无法连接：检查端口 `8000`、`8003` 和防火墙设置，并访问
  `/health` 确认 HTTP 服务就绪。
- ESP32 没有语音输入：确认网页已切回语音模式，或关闭网页使服务器
  自动复位。
