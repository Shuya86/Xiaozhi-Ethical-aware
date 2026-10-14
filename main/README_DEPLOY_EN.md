# xiaozhi-esp32-server Complete Deployment and Startup Guide

English | [简体中文](./README_DEPLOY_ZH.md)

This guide covers first-time deployment, daily startup, ESP32 connection,
browser text chat, status checks, and shutdown for the complete system.

> The deployment scripts are in the repository root, one level above this
> directory. Run all commands from `D:\xiaozhi-esp32-server` (or your actual
> clone directory), not from this `main` directory.

## 1. System components

| Component | Default port | Purpose |
| --- | ---: | --- |
| MySQL | 3306 | Users, devices, agents, and model configuration |
| Redis | 6379 | Login and server-configuration cache |
| xiaozhi-server WebSocket | 8000 | Real-time ESP32 and browser chat connection |
| manager-web | 8001 | Management console |
| manager-api | 8002 | Management, configuration, and OTA API |
| xiaozhi-server HTTP | 8003 | Text-chat page, status, and health endpoints |
| MCP endpoint (optional) | 8004 | MCP endpoint service |
| voiceprint-api (optional) | 8005 | Voiceprint service |

The core startup order is:

```text
Docker → MySQL/Redis → manager-api → manager-web
       → Python environment/model/secret sync → xiaozhi-server
       → ESP32 online and agent synced → browser text chat enabled
```

## 2. Before the first deployment

Windows, Linux, and macOS are supported. On Windows, install and start Docker
Desktop first. The deployment tool checks these dependencies and can attempt
to install missing ones on supported platforms:

- Docker;
- JDK 21 and Maven;
- Node.js and npm;
- Miniconda/Conda;
- Python 3.10 environment, FFmpeg, and libopus.

Ensure ports `3306`, `6379`, `8000`, `8001`, `8002`, and `8003` are not used by
another application. Deployment changes only `xiaozhi-esp32-server`; it does
not modify the adjacent `xiaozhi-esp32-main` firmware directory.

## 3. One-click deployment on Windows

Double-click this file in the repository root:

```text
deploy.bat
```

The GUI selects these eight stages by default:

```text
prereqs → db → api → web → pyenv → model → secret → server
```

Click **Start** and wait for every stage to complete. If a system installer
changes `PATH`, close the deployment window and open `deploy.bat` again.

PowerShell users can run:

```powershell
cd D:\xiaozhi-esp32-server
python auto_deploy.py
```

On Linux or macOS:

```bash
cd /path/to/xiaozhi-esp32-server
python3 auto_deploy.py
```

The tool creates the database containers, builds and starts the management
services, creates the Conda environment, installs dependencies, downloads the
ASR model, synchronizes `server.secret`, and writes the current LAN OTA and
WebSocket addresses. A failed stage stops the remaining stages and returns a
non-zero exit code.

See [`../docs/Auto_Deploy_EN.md`](../docs/Auto_Deploy_EN.md) for all deployment
options.

Use `deploy.bat` for first-time deployment or to select deployment stages
again. After the first deployment succeeds, use `run_xiaozhi-server.bat` in
the repository root for daily startup.

## 4. First-time application configuration

After the services start:

1. Open `http://127.0.0.1:8001`.
2. Register and sign in as the first administrator.
3. Configure the actual LLM provider and API key.
4. Select the agent's ASR, LLM, TTS, memory, intent, and voice modules.
5. Add the required persona and ethical constraints to the agent system prompt.
6. Save the settings, then connect or reconnect the ESP32.

Do not place API keys, the manager-api secret, or optional-module credentials
in frontend files. The deployment status output does not display them.

## 5. Bring the ESP32 online

The deployment tool displays the current LAN addresses. Their default formats
are:

```text
OTA:       http://<server-LAN-IP>:8002/xiaozhi/ota/
WebSocket: ws://<server-LAN-IP>:8000/xiaozhi/v1/
```

Start the ESP32 and ensure it can reach the server computer. Allow ports `8000`
and `8002` through the firewall. Only after the device connects does
xiaozhi-server retrieve that device's LLM, system prompt, and module settings
from manager-api.

This is an intentional hardware-first design. Before the ESP32 is online, the
browser page can load, but `/api/config` returns `llm_ready: false` and text
input remains unavailable. Chat is enabled after device configuration sync.

## 6. Browser text chat

After the ESP32 is online, open:

```text
http://127.0.0.1:8003/text_chat
```

From another device on the LAN, use:

```text
http://<server-LAN-IP>:8003/text_chat
```

Text mode temporarily pauses the ESP32 microphone to prevent simultaneous
text and voice requests. Switching back to voice mode or closing the page
restores the microphone. Browser chat reuses the LLM, ethical prompt, memory,
and intent modules already loaded for the ESP32.

See
[`xiaozhi-server/core/web/README_EN.md`](./xiaozhi-server/core/web/README_EN.md)
for the feature guide.

## 7. Daily startup

On a Windows computer where first-time deployment has completed, double-click
this file in the repository root:

```text
run_xiaozhi-server.bat
```

The launcher contains no fixed drive letter or username. It locates Anaconda,
Miniconda, `py`, or `python`, then:

1. checks required tools and starts Docker Desktop when necessary;
2. starts MySQL, Redis, manager-api, manager-web, and xiaozhi-server;
3. starts `voiceprint-api` and `mcp-endpoint-server` when their sibling
   repositories are installed;
4. performs a final status check and prints the console and text-chat URLs.

This is a daily launcher. It does not install missing dependencies or download
the model again. Use `deploy.bat` if first-time deployment is incomplete.

For command-line automation, use `run_xiaozhi-server.bat --no-pause` so the
launcher returns its exit code without waiting for a key. Run
`run_xiaozhi-server.bat --help` for usage information.

The equivalent command-line startup from the repository root is:

```powershell
python auto_deploy.py --only prereqs,db,api,web,server --no-install
```

Ready containers and services are safely skipped instead of being started on
the same ports again. You can also open `deploy.bat` to select stages in the
GUI. To repeat the complete check, run:

```powershell
python auto_deploy.py
```

After changing source code, restart all application services:

```powershell
python auto_deploy.py --only api,web,server --restart
```

After changing an agent or model setting, reconnect the ESP32 so browser chat
receives the latest device configuration.

## 8. Status checks

```powershell
python auto_deploy.py --status
```

A healthy result includes:

- Docker is `ready`;
- MySQL and Redis are `running`;
- manager-api, manager-web, and xiaozhi-server are `UP`;
- `data/.config.yaml secret set` is `True`.

Direct checks:

| Address | Expected result |
| --- | --- |
| `http://127.0.0.1:8001` | Management console |
| `http://127.0.0.1:8002/xiaozhi/doc.html` | manager-api documentation |
| `http://127.0.0.1:8003/health` | `{"status":"ok"}` |
| `http://127.0.0.1:8003/api/config` | Modules and hardware-gate state |
| `http://127.0.0.1:8003/text_chat` | Browser text chat |

## 9. Stop the system

```powershell
python auto_deploy.py --stop
```

The tool stops manager-api, manager-web, and xiaozhi-server processes that it
started, then asks whether to stop MySQL, Redis, and optional modules. To
confirm automatically:

```powershell
python auto_deploy.py --stop --yes
```

Stopping the database containers does not delete their data. Do not manually
delete containers or volumes unless you intentionally want to reset the data
and have a backup.

## 10. Optional modules

Voiceprint and MCP endpoint are separate repositories and are not included in
the default eight stages. If their sibling directories are already present:

```powershell
python auto_deploy.py --only voiceprint,mcp
```

Their credentials remain in local configuration files. Status only reports
that credentials are configured; it never prints their values.

## 11. Troubleshooting

| Problem | Check |
| --- | --- |
| `Docker daemon unavailable` | Start Docker Desktop and run again |
| Port already in use | Run `python auto_deploy.py --status`; stop the conflicting process |
| manager-api fails | Read `manager-api/logs/auto_deploy_manager_api.log` |
| manager-web fails | Read `manager-web/logs/auto_deploy_manager_web.log` |
| xiaozhi-server fails | Read `xiaozhi-server/tmp/auto_deploy_server.log` |
| Browser shows LLM Offline | Connect the ESP32 and verify its agent has a working LLM |
| Configuration change is missing | Reconnect the ESP32, then refresh the chat page |
| LAN client cannot connect | Check the server IP, firewall, and ports 8000–8003 |

To resynchronize the manager-api secret and LAN OTA/WebSocket addresses:

```powershell
python auto_deploy.py --only secret --reconfigure
```
