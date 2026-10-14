# xiaozhi-esp32-server 完整系统部署与启动指南

[English](./README_DEPLOY_EN.md) | 简体中文

本文说明如何从仓库根目录完成整个系统的首次部署、日常启动、ESP32
硬件接入、网页文本聊天、状态检查和停止服务。

> 本项目的部署脚本位于上一级仓库根目录。文中的命令均应在
> `D:\xiaozhi-esp32-server`（或实际克隆目录）中运行，而不是在本
> `main` 目录中运行。

## 1. 系统组成

| 组件 | 默认端口 | 作用 |
| --- | ---: | --- |
| MySQL | 3306 | 保存用户、设备、智能体和模型配置 |
| Redis | 6379 | 缓存登录状态和服务器配置 |
| xiaozhi-server WebSocket | 8000 | ESP32 与网页聊天的实时连接 |
| manager-web | 8001 | 智控台管理页面 |
| manager-api | 8002 | 管理后台、配置接口和 OTA 接口 |
| xiaozhi-server HTTP | 8003 | 文本聊天页面、状态和健康检查 |
| MCP endpoint（可选） | 8004 | MCP 接入点服务 |
| voiceprint-api（可选） | 8005 | 声纹识别服务 |

核心启动顺序为：

```text
Docker → MySQL/Redis → manager-api → manager-web
       → Python 环境/模型/密钥同步 → xiaozhi-server
       → ESP32 上线并同步智能体 → 网页文本聊天可用
```

## 2. 首次部署前准备

支持 Windows、Linux 和 macOS。Windows 建议预先安装并启动 Docker
Desktop。部署器会检查以下工具，并可在支持的平台上尝试安装缺失项：

- Docker；
- JDK 21 和 Maven；
- Node.js 与 npm；
- Miniconda/Conda；
- Python 3.10 环境、FFmpeg 和 libopus。

确保端口 `3306`、`6379`、`8000`、`8001`、`8002`、`8003` 未被其他
程序占用。部署过程只修改 `xiaozhi-esp32-server`，不会修改相邻的
`xiaozhi-esp32-main` 固件目录。

## 3. Windows 一键部署

在仓库根目录双击：

```text
deploy.bat
```

部署窗口默认选择以下八个阶段：

```text
prereqs → db → api → web → pyenv → model → secret → server
```

点击 **Start** 并等待全部阶段显示完成。首次安装系统工具后，如果界面
提示环境变量尚未刷新，请关闭窗口后重新双击 `deploy.bat`。

也可以在 PowerShell 中执行：

```powershell
cd D:\xiaozhi-esp32-server
python auto_deploy.py
```

Linux 或 macOS 使用：

```bash
cd /path/to/xiaozhi-esp32-server
python3 auto_deploy.py
```

部署器会自动创建数据库容器、构建并启动管理服务、创建 Conda 环境、
安装依赖、下载 ASR 模型、同步 `server.secret`，并写入当前局域网的 OTA
和 WebSocket 地址。任一阶段失败时，后续阶段会停止并返回非零退出码。

更详细的一键部署参数见
[`../docs/Auto_Deploy.md`](../docs/Auto_Deploy.md)。

`deploy.bat` 用于首次部署或重新选择部署阶段。首次部署完成后，日常启动
建议改用仓库根目录的 `run_xiaozhi-server.bat`。

## 4. 首次业务配置

服务启动后按以下顺序配置：

1. 打开 `http://127.0.0.1:8001`；
2. 注册第一个管理员账号并登录；
3. 在模型配置中填写实际使用的 LLM API Key；
4. 配置智能体使用的 ASR、LLM、TTS、记忆、意图模块和音色；
5. 在智能体系统提示词中配置所需的角色和伦理约束；
6. 保存设置后让 ESP32 连接或重新连接服务器。

API Key、manager-api secret 和可选模块认证密钥不应写入前端文件，也
不会显示在部署器状态输出中。

## 5. ESP32 硬件上线

部署器会显示当前局域网地址。默认服务地址格式如下：

```text
OTA:       http://<服务器局域网IP>:8002/xiaozhi/ota/
WebSocket: ws://<服务器局域网IP>:8000/xiaozhi/v1/
```

启动 ESP32 后，确认它能够访问服务器所在电脑，且防火墙允许端口
`8000` 和 `8002`。设备连接后，xiaozhi-server 才会从 manager-api 获取
该设备的 LLM、系统提示词和模块配置。

这是硬件优先的设计：ESP32 尚未上线时，网页文本聊天页面可以打开，
但 `/api/config` 返回 `llm_ready: false`，文本输入不可用；设备完成配置
同步后才会变为可用。

## 6. 网页文本聊天

ESP32 成功上线后打开：

```text
http://127.0.0.1:8003/text_chat
```

局域网其他设备使用：

```text
http://<服务器局域网IP>:8003/text_chat
```

切换到文本模式会暂时暂停 ESP32 麦克风输入，以避免文字和语音同时触发
对话；切回语音模式或关闭页面会恢复麦克风。网页会复用 ESP32 已加载的
LLM、伦理提示词、记忆和意图模块。

功能说明见
[`xiaozhi-server/core/web/README_ZH.md`](./xiaozhi-server/core/web/README_ZH.md)。

## 7. 日常启动

首次部署已经成功的 Windows 电脑，可以直接双击仓库根目录中的：

```text
run_xiaozhi-server.bat
```

这个启动器不包含固定盘符或用户名，会自动寻找 Anaconda、Miniconda、
`py` 或 `python`。它依次执行以下操作：

1. 检查必需工具，并在需要时启动 Docker Desktop；
2. 启动 MySQL、Redis、manager-api、manager-web 和 xiaozhi-server；
3. 自动识别当前局域网 IPv4；如果数据库中的 `server.ota` 或
   `server.websocket` 仍使用旧 IP，则更新参数并清除对应 Redis 缓存；
4. 如果仓库相邻位置存在 `voiceprint-api` 或 `mcp-endpoint-server`，启动
   对应可选服务；
5. 执行最终状态检查并显示管理后台和文本聊天地址。

该文件是日常启动器，不安装缺失依赖，也不重新下载模型。首次部署或
环境缺失时应运行 `deploy.bat`。

从命令行或自动化脚本调用时，可以使用
`run_xiaozhi-server.bat --no-pause`，完成后直接返回退出码而不等待按键；
使用 `run_xiaozhi-server.bat --help` 可查看帮助。

等价的命令行方式是在仓库根目录执行：

```powershell
python auto_deploy.py --only prereqs,db,api,web,secret,server --no-install
```

已经就绪的容器和服务会被安全跳过，不会重复占用端口。也可以再次双击
`deploy.bat`，通过图形界面选择阶段。
如果需要重新运行完整检查，执行：

```powershell
python auto_deploy.py
```

修改源码后重启三个应用服务：

```powershell
python auto_deploy.py --only api,web,server --restart
```

修改智能体或模型配置后，还应让 ESP32 重新连接，以便网页聊天获取最新
设备配置。

## 8. 状态检查

```powershell
python auto_deploy.py --status
```

正常状态应包括：

- Docker 为 `ready`；
- MySQL 和 Redis 为 `running`；
- manager-api、manager-web、xiaozhi-server 为 `UP`；
- `data/.config.yaml secret set` 为 `True`。

还可以直接检查：

| 地址 | 预期结果 |
| --- | --- |
| `http://127.0.0.1:8001` | 智控台页面 |
| `http://127.0.0.1:8002/xiaozhi/doc.html` | manager-api 文档 |
| `http://127.0.0.1:8003/health` | `{"status":"ok"}` |
| `http://127.0.0.1:8003/api/config` | 当前模块及硬件门控状态 |
| `http://127.0.0.1:8003/text_chat` | 网页文本聊天 |

## 9. 停止系统

```powershell
python auto_deploy.py --stop
```

部署器会停止由其启动的 manager-api、manager-web 和 xiaozhi-server，
并询问是否同时停止 MySQL、Redis 及可选模块。需要自动确认时使用：

```powershell
python auto_deploy.py --stop --yes
```

停止数据库容器不会删除数据；不要手动删除容器或数据卷，除非明确需要
清空系统数据并已经做好备份。

## 10. 可选模块

声纹和 MCP endpoint 是独立仓库，不属于默认八个部署阶段。如果对应
兄弟目录已经准备好，可执行：

```powershell
python auto_deploy.py --only voiceprint,mcp
```

它们的认证信息保存在各自本地配置文件中，部署器仅显示“已配置”，不会
输出实际密钥。

## 11. 故障排查

| 问题 | 检查方法 |
| --- | --- |
| `Docker daemon unavailable` | 启动 Docker Desktop 后重新运行 |
| 端口被占用 | 使用 `python auto_deploy.py --status` 检查；关闭占用程序 |
| manager-api 启动失败 | 查看 `manager-api/logs/auto_deploy_manager_api.log` |
| manager-web 启动失败 | 查看 `manager-web/logs/auto_deploy_manager_web.log` |
| xiaozhi-server 启动失败 | 查看 `xiaozhi-server/tmp/auto_deploy_server.log` |
| 网页显示 LLM Offline | 先连接 ESP32，并确认设备智能体已配置可用 LLM |
| 修改配置未生效 | 保存后重新连接 ESP32，再刷新聊天页面 |
| 局域网设备无法访问 | 检查服务器 IP、防火墙和 8000–8003 端口 |

重新同步 manager-api secret、OTA 和 WebSocket 地址：

```powershell
python auto_deploy.py --only secret --reconfigure
```
