# One-Click Deployment (All Local-Source Modules)

English | [简体中文](./Auto_Deploy.md)

This tool deploys MySQL, Redis, manager-api, manager-web, and xiaozhi-server
without replacing the modified source tree. It neither downloads over the
current source code nor changes the adjacent `xiaozhi-esp32-main` firmware
directory.

## Windows GUI

Double-click `deploy.bat` in the repository root. Keep the eight default
stages selected and click **Start**.

The first run may install or download Docker, JDK 21, Maven, Node.js,
Miniconda, Python dependencies, and the SenseVoiceSmall model. If an installer
changes `PATH`, close the deployment window and open `deploy.bat` again.

The deployment tool performs these operations:

1. Starts the local MySQL and Redis containers.
2. Starts the Java management API and Vue management UI.
3. Creates a Python 3.10 Conda environment and installs the audio/server
   dependencies.
4. Downloads and validates the ASR model.
5. Reads the manager-api-generated `server.secret` from local MySQL and writes
   it to `main/xiaozhi-server/data/.config.yaml`.
6. Writes the current LAN addresses to `server.websocket` and `server.ota`.
7. Starts xiaozhi-server and validates every stage with process, port, and
   health checks.

Authentication tokens for optional modules are never printed in status output.

## Required application setup

After deployment:

1. Open `http://127.0.0.1:8001` and register the first administrator.
2. Configure the actual LLM provider and API key in the management UI.
3. Connect the ESP32 hardware first. The device must download its agent, LLM,
   ethical system prompt, and module configuration before browser chat is
   enabled.
4. Open `http://127.0.0.1:8003/text_chat`.

The page can load before the ESP32 is online, but chat intentionally remains
unavailable (`llm_ready=false`). This is the expected hardware-first gate, not
a deployment failure.

## Command line

```powershell
# Full deployment
python auto_deploy.py

# Check or install prerequisites only
python auto_deploy.py --only prereqs

# Show status
python auto_deploy.py --status

# Refresh secret, WebSocket, and OTA settings
python auto_deploy.py --only secret --reconfigure

# Restart the three application services after source changes
python auto_deploy.py --only api,web,server --restart

# Stop services started by this tool
python auto_deploy.py --stop
```

## Daily one-click startup

After the first complete deployment succeeds, Windows users can double-click
`run_xiaozhi-server.bat` in the repository root. It uses the same deployment
engine to start Docker, the databases, all three core applications, and any
installed optional modules. On every start it detects the current LAN IPv4. It
updates the database and invalidates the corresponding Redis cache only when
`server.ota` or `server.websocket` contains a stale IP, then runs a final
status check. This launcher does not install missing tools; use `deploy.bat`
when the environment is incomplete.

Voiceprint and MCP endpoint services are optional separate repositories. If
their sibling directories already exist, start them with:

```powershell
python auto_deploy.py --only voiceprint,mcp
```

## Privacy and Git

The detected LAN IP, manager-api secret, model API keys, and access tokens must
remain in runtime configuration or the database and must not be committed.
The root `.gitignore` excludes `main/xiaozhi-server/data/`, deployment state,
logs, `*.local.*` overrides, private keys, and certificates. Commit only
sanitized `*.example` files when a configuration sample is needed.

`.gitignore` cannot remove secrets that were already committed. If a real key
ever entered Git history, revoke and rotate it at the provider first, then
clean the repository history separately.

## Troubleshooting

The deployment stops at the first failed stage and returns a non-zero exit
code. Relevant logs are:

- `main/manager-api/logs/auto_deploy_manager_api.log`
- `main/manager-web/logs/auto_deploy_manager_web.log`
- `main/xiaozhi-server/tmp/auto_deploy_server.log`

If status reports `daemon unavailable`, start Docker Desktop and run the tool
again. Re-running is safe: ready services are skipped unless `--restart` is
specified.
