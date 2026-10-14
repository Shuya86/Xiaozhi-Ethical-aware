#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
xiaozhi-esp32-server one-shot local deployment orchestrator.

Automates docs/Deployment_all.md, "方式二：本地源码运行（全模块）":
  1. prereqs   - check/install Docker, JDK21, Maven, Node.js, Miniconda
  2. db        - start MySQL + Redis containers
  3. api       - build & run manager-api (Spring Boot)
  4. web       - install & run manager-web (Vue)
  5. pyenv     - create conda env + install xiaozhi-server Python deps
  6. model     - download the SenseVoiceSmall ASR model
  7. secret    - read manager-api's generated secret from local MySQL, write
                 data/.config.yaml, and configure websocket/OTA addresses
  8. server    - run xiaozhi-server

The local-source path retrieves `server.secret` directly from the MySQL
container, so registering the first admin account is no longer a deployment
blocker. You still need to register an administrator and configure an LLM API
key in manager-web before holding a real conversation. If MySQL is external,
the "secret" stage falls back to asking for the value manually.

Optional add-on modules (separate repos, not run by default):
  voiceprint - voiceprint-api (docs/voiceprint-integration.md)
  mcp        - mcp-endpoint-server (docs/mcp-endpoint-enable.md)
Run with e.g. --only voiceprint,mcp. Each looks for its project folder as a
sibling of this repo (../voiceprint-api, ../mcp-endpoint-server) unless
VOICEPRINT_API_DIR / MCP_ENDPOINT_DIR env vars say otherwise. Their auth
credentials stay in the local config files and are never printed in status
output. Add the authenticated health URL in manager-api's 参数管理
(server.voice_print / server.mcp_endpoint) after logging in as an admin.

Usage:
  python auto_deploy.py                 run every stage in order
  python auto_deploy.py --only db,api   run just the listed stages
  python auto_deploy.py --skip prereqs  run everything except prereqs
  python auto_deploy.py --status        show what's currently running
  python auto_deploy.py --stop          stop background processes/containers
  python auto_deploy.py --yes           don't ask before installing tools
  python auto_deploy.py --foreground    run xiaozhi-server attached (last stage blocks)

Re-running is safe: every stage checks what's already in place and skips it.
"""
import argparse
import csv
import json
import os
import platform
import queue
import re
import shutil
import socket
import subprocess
import sys
import threading
import time
import urllib.request
import webbrowser
from pathlib import Path
from types import SimpleNamespace

# --------------------------------------------------------------------------
# constants
# --------------------------------------------------------------------------
REPO_ROOT = Path(__file__).resolve().parent
MAIN = REPO_ROOT / "main"
XIAOZHI_SERVER = MAIN / "xiaozhi-server"
MANAGER_API = MAIN / "manager-api"
MANAGER_WEB = MAIN / "manager-web"
STATE_FILE = REPO_ROOT / ".auto_deploy_state.json"

CONDA_ENV = "xiaozhi-esp32-server"
MYSQL_CONTAINER = "xiaozhi-esp32-server-db"
REDIS_CONTAINER = "xiaozhi-esp32-server-redis"
MYSQL_ROOT_PASSWORD = "123456"
MYSQL_DB = "xiaozhi_esp32_server"
MODEL_URL = "https://modelscope.cn/models/iic/SenseVoiceSmall/resolve/master/model.pt"
MODEL_PATH = XIAOZHI_SERVER / "models" / "SenseVoiceSmall" / "model.pt"
MODEL_MIN_BYTES = 200 * 1024 * 1024  # real file is ~900MB; anything smaller is a bad/partial download

API_PORT = 8002
WEB_PORT = 8001
SERVER_PORT = 8000
HTTP_PORT = 8003

# Optional add-on modules (docs/voiceprint-integration.md, docs/mcp-endpoint-enable.md).
# These are separate repos, not part of this monorepo, so they're not part of the
# default STAGES run and their location is configurable rather than assumed.
VOICEPRINT_DIR = Path(os.environ.get("VOICEPRINT_API_DIR", REPO_ROOT.parent / "voiceprint-api"))
MCP_ENDPOINT_DIR = Path(os.environ.get("MCP_ENDPOINT_DIR", REPO_ROOT.parent / "mcp-endpoint-server"))
VOICEPRINT_PORT = 8005
MCP_ENDPOINT_PORT = 8004
VOICEPRINT_CONTAINER = "voiceprint-api"
MCP_ENDPOINT_CONTAINER = "mcp-endpoint-server"

OS_NAME = platform.system()  # 'Windows' | 'Linux' | 'Darwin'
STAGES = ["prereqs", "db", "api", "web", "pyenv", "model", "secret", "server"]
OPTIONAL_STAGES = ["voiceprint", "mcp"]

# Windows consoles default to the system codepage (e.g. GBK), which mangles the
# Chinese status text below; force UTF-8 where the runtime allows it.
for _stream in (sys.stdout, sys.stderr):
    try:
        _stream.reconfigure(encoding="utf-8")
    except (AttributeError, ValueError):
        pass

# --------------------------------------------------------------------------
# pluggable I/O: lets a GUI (or any other frontend) drive the exact same
# stage functions the CLI uses, without those functions knowing or caring
# whether they're talking to a terminal or a window.
# --------------------------------------------------------------------------
class StageAborted(Exception):
    """Raised to unwind out of a stage when a stop is requested mid-flight."""


class StageFailed(RuntimeError):
    """Raised when a deployment stage cannot prove that it completed."""


STOP_EVENT = threading.Event()


class UIHooks:
    """Default (terminal) implementation; a GUI overrides every method."""

    def confirm(self, prompt, auto_yes):
        if auto_yes:
            return True
        return input(f"{prompt} [y/N] ").strip().lower() in ("y", "yes")

    def ask_text(self, prompt, default=""):
        suffix = f" [{default}]" if default else ""
        value = input(f"{prompt}{suffix}: ").strip()
        return value or default

    def emit(self, line):
        print(line, flush=True)

    def emit_progress(self, text):
        print(f"\r{text}", end="", flush=True)

    def stage_status(self, name, status):
        pass


HOOKS = UIHooks()


# --------------------------------------------------------------------------
# small helpers
# --------------------------------------------------------------------------
def log(msg, level="INFO"):
    prefix = {"INFO": "[*]", "OK": "[+]", "WARN": "[!]", "ERR": "[x]"}.get(level, "[*]")
    HOOKS.emit(f"{prefix} {msg}")


def confirm(prompt, auto_yes):
    return HOOKS.confirm(prompt, auto_yes)


def which(cmd):
    return shutil.which(cmd)


def resolve_cmd(name):
    """Resolve a command name to its full path via PATH.

    On Windows, subprocess.Popen given a bare list bypasses the shell's
    PATHEXT search, so "mvn" (which only ships as mvn.cmd, no mvn.exe) or
    "conda" (often only conda.bat) fails with WinError 2 even though it's
    on PATH. shutil.which() already does the PATHEXT-aware search, so
    resolve through it instead of guessing extensions per tool.
    """
    return shutil.which(name) or name


def run(cmd, cwd=None, check=True, env=None):
    """Run cmd, streaming its output through HOOKS.emit line by line.

    Polls STOP_EVENT while the command runs so a GUI Stop button (or Ctrl+C)
    can kill it and unwind the current stage via StageAborted.
    """
    log("$ " + " ".join(cmd))
    proc = subprocess.Popen(
        cmd, cwd=cwd, env=env,
        stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True, bufsize=1,
        encoding="utf-8", errors="replace",
    )
    out_q = queue.Queue()

    def reader():
        try:
            for line in proc.stdout:
                out_q.put(line.rstrip("\n"))
        finally:
            out_q.put(None)

    threading.Thread(target=reader, daemon=True).start()

    lines = []
    try:
        while True:
            try:
                line = out_q.get(timeout=0.3)
            except queue.Empty:
                if STOP_EVENT.is_set():
                    proc.terminate()
                    try:
                        proc.wait(timeout=5)
                    except subprocess.TimeoutExpired:
                        proc.kill()
                    raise StageAborted(f"stopped: {' '.join(cmd)}")
                continue
            if line is None:
                break
            lines.append(line)
            HOOKS.emit(line)
    except KeyboardInterrupt:
        proc.terminate()
        raise
    finally:
        proc.wait()
        if proc.stdout is not None:
            proc.stdout.close()

    if check and proc.returncode != 0:
        raise StageFailed(f"command failed ({proc.returncode}): {' '.join(cmd)}")
    return SimpleNamespace(returncode=proc.returncode, stdout="\n".join(lines))


def run_capture(cmd, cwd=None, env=None, timeout=30):
    """Run a short probe without echoing its output to the UI.

    Health/status probes use this helper so a stopped Docker daemon or an
    expected "not found" result does not flood the deployment log.
    """
    try:
        return subprocess.run(
            cmd, cwd=cwd, env=env, capture_output=True, text=True,
            encoding="utf-8", errors="replace", timeout=timeout,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        return SimpleNamespace(returncode=1, stdout="", stderr=str(exc))


def port_open(host, port, timeout=1.5):
    try:
        with socket.create_connection((host, port), timeout=timeout):
            return True
    except OSError:
        return False


def http_ready(url, timeout=2):
    try:
        with urllib.request.urlopen(url, timeout=timeout) as response:
            return 200 <= response.status < 500
    except Exception:
        return False


def wait_for(predicate, timeout, interval, desc):
    log(f"waiting for {desc} (timeout {timeout}s)...")
    start = time.time()
    while time.time() - start < timeout:
        if STOP_EVENT.is_set():
            raise StageAborted(f"stopped while waiting for {desc}")
        if predicate():
            log(f"{desc} is ready", "OK")
            return True
        time.sleep(interval)
    log(f"timed out waiting for {desc}", "WARN")
    return False


def wait_for_port_release(port, timeout=15):
    """Wait for a stopped service to release its listening port."""
    log(f"waiting for port {port} to be released...")
    start = time.time()
    while time.time() - start < timeout:
        if STOP_EVENT.is_set():
            raise StageAborted(f"stopped while waiting for port {port}")
        if not port_open("127.0.0.1", port):
            return
        time.sleep(0.5)
    raise StageFailed(f"port {port} was not released after stopping the service")


def load_state():
    if STATE_FILE.exists():
        try:
            return json.loads(STATE_FILE.read_text(encoding="utf-8"))
        except (OSError, ValueError) as exc:
            log(f"ignoring invalid state file {STATE_FILE}: {exc}", "WARN")
    return {}


def save_state(state):
    STATE_FILE.write_text(json.dumps(state, indent=2), encoding="utf-8")


def lan_ip():
    route_ip = None
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        s.connect(("8.8.8.8", 80))
        route_ip = s.getsockname()[0]
    except OSError:
        pass
    finally:
        s.close()

    # The address selected by the default route is normally the interface that
    # the ESP32 can reach. Prefer it over VMware/WSL/Docker adapter addresses.
    if route_ip and not route_ip.startswith("127."):
        return route_ip

    candidates = []

    try:
        candidates.extend(
            item[4][0]
            for item in socket.getaddrinfo(socket.gethostname(), None, socket.AF_INET)
        )
    except OSError:
        pass

    def priority(value):
        if value.startswith("192.168."):
            return 0
        if value.startswith("10."):
            return 1
        if value.startswith("172."):
            try:
                second = int(value.split(".")[1])
                if 16 <= second <= 31:
                    return 2
            except (ValueError, IndexError):
                pass
        return 9

    usable = sorted(
        {value for value in candidates if value and not value.startswith("127.")},
        key=priority,
    )
    return usable[0] if usable else "127.0.0.1"


def spawn_background(cmd, cwd, log_path, env=None):
    log_path.parent.mkdir(parents=True, exist_ok=True)
    log_file = open(log_path, "a", encoding="utf-8", errors="replace")
    log_file.write(f"\n\n===== auto_deploy launch {time.ctime()} =====\n")
    log_file.flush()
    creationflags = 0
    if OS_NAME == "Windows":
        creationflags = subprocess.CREATE_NEW_PROCESS_GROUP
    try:
        proc = subprocess.Popen(
            cmd, cwd=cwd, stdout=log_file, stderr=subprocess.STDOUT,
            env=env, creationflags=creationflags,
            start_new_session=(OS_NAME != "Windows"),
        )
    finally:
        log_file.close()
    log(f"started pid={proc.pid}, logging to {log_path}")
    return proc.pid


def pid_alive(pid):
    if pid is None:
        return False
    if OS_NAME == "Windows":
        r = run_capture(["tasklist", "/FI", f"PID eq {pid}", "/FO", "CSV", "/NH"])
        if r.returncode != 0:
            return False
        try:
            return any(row and len(row) > 1 and row[1] == str(pid)
                       for row in csv.reader(r.stdout.splitlines()))
        except csv.Error:
            return False
    try:
        os.kill(pid, 0)
        return True
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    except OSError:
        return False


def kill_pid(pid):
    if not pid_alive(pid):
        return
    log(f"stopping pid={pid}")
    if OS_NAME == "Windows":
        subprocess.run(["taskkill", "/PID", str(pid), "/T", "/F"], capture_output=True)
    else:
        import signal
        try:
            os.kill(pid, signal.SIGTERM)
            time.sleep(2)
            if pid_alive(pid):
                os.kill(pid, signal.SIGKILL)
        except OSError:
            pass


# --------------------------------------------------------------------------
# prerequisite install helpers (per OS package manager)
# --------------------------------------------------------------------------
def winget_install(package_id):
    return run(
        ["winget", "install", "-e", "--id", package_id,
         "--accept-source-agreements", "--accept-package-agreements"],
        check=False,
    ).returncode == 0


def brew_install(pkg, cask=False):
    cmd = ["brew", "install"] + (["--cask"] if cask else []) + [pkg]
    return run(cmd, check=False).returncode == 0


def apt_install(pkg):
    run(["sudo", "apt-get", "update"], check=False)
    return run(["sudo", "apt-get", "install", "-y", pkg], check=False).returncode == 0


def is_debian_like():
    return which("apt-get") is not None


def install_tool(display_name, winget_id=None, brew_pkg=None, brew_cask=False,
                  apt_pkg=None, manual_hint=""):
    """Best-effort install via the native package manager for this OS."""
    log(f"installing {display_name} ...")
    if OS_NAME == "Windows" and winget_id and which("winget"):
        return winget_install(winget_id)
    if OS_NAME == "Darwin" and which("brew"):
        if brew_pkg:
            return brew_install(brew_pkg, cask=brew_cask)
    if OS_NAME == "Linux" and is_debian_like() and apt_pkg:
        return apt_install(apt_pkg)
    log(f"don't know how to auto-install {display_name} on this system.", "WARN")
    if manual_hint:
        log(f"  -> {manual_hint}", "WARN")
    return False


# --------------------------------------------------------------------------
# prereq checks
# --------------------------------------------------------------------------
def check_docker():
    if not which("docker"):
        return False
    r = run_capture([resolve_cmd("docker"), "info"], timeout=15)
    return r.returncode == 0


def check_jdk21():
    java = which("java")
    if not java:
        return False
    r = run_capture([resolve_cmd("java"), "-version"])
    text = r.stdout + r.stderr
    m = re.search(r'version "(\d+)', text)
    return bool(m and int(m.group(1)) >= 21)


def check_maven():
    return which("mvn") is not None


def check_node():
    return which("node") is not None and which("npm") is not None


def check_conda():
    return which("conda") is not None


def start_docker_desktop_windows():
    candidates = [
        r"C:\Program Files\Docker\Docker\Docker Desktop.exe",
        os.path.expandvars(r"%ProgramFiles%\Docker\Docker\Docker Desktop.exe"),
    ]
    for exe in candidates:
        if Path(exe).exists():
            log("launching Docker Desktop, this can take a minute on first start...")
            subprocess.Popen([exe])
            return wait_for(check_docker, timeout=120, interval=3, desc="Docker daemon")
    return False


def stage_prereqs(args):
    log("== stage: prereqs ==")
    checks = [
        ("Docker", lambda: which("docker") is not None,
         dict(winget_id="Docker.DockerDesktop", brew_pkg="docker", brew_cask=True,
              apt_pkg="docker.io",
              manual_hint="https://docs.docker.com/get-docker/")),
        ("JDK 21", check_jdk21,
         dict(winget_id="EclipseAdoptium.Temurin.21.JDK", brew_pkg="openjdk@21",
              apt_pkg="openjdk-21-jdk",
              manual_hint="https://adoptium.net/temurin/releases/?version=21")),
        ("Maven", check_maven,
         dict(winget_id="Apache.Maven", brew_pkg="maven", apt_pkg="maven",
              manual_hint="https://maven.apache.org/download.cgi")),
        ("Node.js", check_node,
         dict(winget_id="OpenJS.NodeJS.LTS", brew_pkg="node", apt_pkg="nodejs",
              manual_hint="https://nodejs.org/")),
        ("Conda", check_conda,
         dict(winget_id="Anaconda.Miniconda3", brew_pkg="miniconda", brew_cask=True,
              apt_pkg=None,
              manual_hint="https://docs.conda.io/projects/miniconda/en/latest/")),
    ]
    all_ok = True
    for name, check_fn, install_kwargs in checks:
        if check_fn():
            log(f"{name}: found", "OK")
            continue
        log(f"{name}: not found", "WARN")
        if args.no_install:
            all_ok = False
            continue
        if not confirm(f"Install {name} now?", args.yes):
            all_ok = False
            continue
        install_tool(name, **install_kwargs)
        if check_fn():
            log(f"{name}: installed", "OK")
        else:
            log(f"{name}: still not detected on PATH. You may need to restart "
                f"your terminal (PATH refresh) and re-run this script.", "WARN")
            all_ok = False

    if which("docker") and not check_docker():
        if OS_NAME == "Windows":
            if not start_docker_desktop_windows():
                all_ok = False
        else:
            log("Docker is installed but the daemon isn't reachable. "
                "Start it (e.g. `sudo systemctl start docker`) and re-run.", "WARN")
            all_ok = False

    if not all_ok:
        log("some prerequisites are still missing; later stages that need them will fail.", "WARN")
    return all_ok


# --------------------------------------------------------------------------
# stage: db (mysql + redis containers)
# --------------------------------------------------------------------------
def container_exists(name):
    if not check_docker():
        return False
    r = run_capture([resolve_cmd("docker"), "inspect", name])
    return r.returncode == 0


def container_running(name):
    if not check_docker():
        return False
    r = run_capture(
        [resolve_cmd("docker"), "inspect", "-f", "{{.State.Running}}", name]
    )
    return r.returncode == 0 and "true" in r.stdout.lower()


def stage_db(args):
    log("== stage: db (MySQL + Redis) ==")
    if not check_docker():
        raise SystemExit("Docker is required for this stage. Run --only prereqs first.")

    if not container_exists(MYSQL_CONTAINER):
        run(["docker", "run", "--name", MYSQL_CONTAINER,
             "-e", f"MYSQL_ROOT_PASSWORD={MYSQL_ROOT_PASSWORD}",
             "-p", "3306:3306",
             "-e", f"MYSQL_DATABASE={MYSQL_DB}",
             "-e", "MYSQL_INITDB_ARGS=--character-set-server=utf8mb4 --collation-server=utf8mb4_unicode_ci",
             "-e", "TZ=Asia/Shanghai",
             "-d", "mysql:latest"])
    elif not container_running(MYSQL_CONTAINER):
        run(["docker", "start", MYSQL_CONTAINER])
    else:
        log(f"{MYSQL_CONTAINER} already running", "OK")

    if not container_exists(REDIS_CONTAINER):
        run(["docker", "run", "--name", REDIS_CONTAINER, "-d", "-p", "6379:6379", "redis"])
    elif not container_running(REDIS_CONTAINER):
        run(["docker", "start", REDIS_CONTAINER])
    else:
        log(f"{REDIS_CONTAINER} already running", "OK")

    if not wait_for(lambda: port_open("127.0.0.1", 3306), timeout=90, interval=2,
                    desc="MySQL port 3306"):
        raise StageFailed("MySQL did not become ready on port 3306")
    if not wait_for(lambda: port_open("127.0.0.1", 6379), timeout=30, interval=1,
                    desc="Redis port 6379"):
        raise StageFailed("Redis did not become ready on port 6379")
    # MySQL accepts TCP connections slightly before it's ready to authenticate on first init.
    time.sleep(5)


# --------------------------------------------------------------------------
# stage: manager-api
# --------------------------------------------------------------------------
def stage_api(args):
    log("== stage: api (manager-api) ==")
    if not check_maven() or not check_jdk21():
        raise SystemExit("Maven + JDK21 are required for this stage. Run --only prereqs first.")

    state = load_state()
    if port_open("127.0.0.1", API_PORT):
        tracked_pid = state.get("manager_api_pid")
        if getattr(args, "restart", False) and pid_alive(tracked_pid):
            log("restarting the tracked manager-api to load current code")
            kill_pid(tracked_pid)
            wait_for_port_release(API_PORT)
        elif getattr(args, "restart", False):
            raise StageFailed(
                f"port {API_PORT} is occupied by an untracked process"
            )
        else:
            log(f"something is already listening on {API_PORT}, assuming manager-api is up", "OK")
            return

    log_path = MANAGER_API / "logs" / "auto_deploy_manager_api.log"
    pid = spawn_background([resolve_cmd("mvn"), "-q", "spring-boot:run"], cwd=MANAGER_API, log_path=log_path)
    state["manager_api_pid"] = pid
    save_state(state)

    def ready():
        if not pid_alive(pid):
            raise StageFailed(f"manager-api exited early; check {log_path}")
        return port_open("127.0.0.1", API_PORT)

    if not wait_for(ready, timeout=240, interval=3, desc="manager-api on port 8002"):
        raise StageFailed(
            f"manager-api did not become ready; check {log_path} "
            "(the first Maven run can take several minutes)"
        )


# --------------------------------------------------------------------------
# stage: manager-web
# --------------------------------------------------------------------------
def stage_web(args):
    log("== stage: web (manager-web) ==")
    if not check_node():
        raise SystemExit("Node.js is required for this stage. Run --only prereqs first.")

    state = load_state()
    if port_open("127.0.0.1", WEB_PORT):
        tracked_pid = state.get("manager_web_pid")
        if getattr(args, "restart", False) and pid_alive(tracked_pid):
            log("restarting the tracked manager-web to load current code")
            kill_pid(tracked_pid)
            wait_for_port_release(WEB_PORT)
        elif getattr(args, "restart", False):
            raise StageFailed(
                f"port {WEB_PORT} is occupied by an untracked process"
            )
        else:
            log(f"something is already listening on {WEB_PORT}, assuming manager-web is up", "OK")
            return

    npm = resolve_cmd("npm")
    if not (MANAGER_WEB / "node_modules").exists():
        run([npm, "install"], cwd=MANAGER_WEB)

    log_path = MANAGER_WEB / "logs" / "auto_deploy_manager_web.log"
    pid = spawn_background([npm, "run", "serve"], cwd=MANAGER_WEB, log_path=log_path)
    state["manager_web_pid"] = pid
    save_state(state)

    def ready():
        if not pid_alive(pid):
            raise StageFailed(f"manager-web exited early; check {log_path}")
        return port_open("127.0.0.1", WEB_PORT)

    if not wait_for(ready, timeout=120, interval=2, desc="manager-web on port 8001"):
        raise StageFailed(f"manager-web did not become ready; check {log_path}")


# --------------------------------------------------------------------------
# stage: python env for xiaozhi-server
# --------------------------------------------------------------------------
def conda_env_exists():
    r = run([resolve_cmd("conda"), "env", "list"], check=False)
    return any(line.split()[0] == CONDA_ENV for line in r.stdout.splitlines() if line and not line.startswith("#"))


def conda_run(args_list, cwd=None, check=True):
    return run([resolve_cmd("conda"), "run", "-n", CONDA_ENV, "--no-capture-output"] + args_list, cwd=cwd, check=check)


def stage_pyenv(args):
    log("== stage: pyenv (conda + xiaozhi-server deps) ==")
    if not check_conda():
        raise SystemExit("Conda is required for this stage. Run --only prereqs first.")

    if not conda_env_exists():
        run([resolve_cmd("conda"), "create", "-n", CONDA_ENV, "python=3.10", "-y"])
    else:
        log(f"conda env '{CONDA_ENV}' already exists", "OK")

    conda = resolve_cmd("conda")
    channels = []
    if not args.no_mirror:
        channels = [
            "-c", "https://mirrors.tuna.tsinghua.edu.cn/anaconda/cloud/conda-forge",
            "-c", "https://mirrors.tuna.tsinghua.edu.cn/anaconda/pkgs/main",
        ]

    run([conda, "install", "-n", CONDA_ENV] + channels + ["libopus", "ffmpeg", "-y"])
    if OS_NAME == "Linux":
        run([conda, "install", "-n", CONDA_ENV] + channels + ["libiconv", "-y"])

    pip_args = ["pip", "install", "-r", "requirements.txt"]
    if not args.no_mirror:
        pip_args += ["--index-url", "https://mirrors.aliyun.com/pypi/simple/"]
    conda_run(pip_args, cwd=XIAOZHI_SERVER)


# --------------------------------------------------------------------------
# stage: model download
# --------------------------------------------------------------------------
def download_with_progress(url, dest):
    dest.parent.mkdir(parents=True, exist_ok=True)
    tmp = dest.with_suffix(dest.suffix + ".part")

    def report(block_num, block_size, total_size):
        if STOP_EVENT.is_set():
            raise StageAborted("model download stopped")
        if total_size <= 0:
            return
        done = block_num * block_size
        pct = min(100, done * 100 // total_size)
        HOOKS.emit_progress(
            f"downloading model... {pct}% ({done // (1024*1024)}MB/{total_size // (1024*1024)}MB)")

    urllib.request.urlretrieve(url, tmp, reporthook=report)
    HOOKS.emit("")
    tmp.replace(dest)


def stage_model(args):
    log("== stage: model (SenseVoiceSmall ASR) ==")
    if MODEL_PATH.exists() and MODEL_PATH.stat().st_size >= MODEL_MIN_BYTES:
        log("model.pt already present", "OK")
        return
    log(f"downloading from {MODEL_URL}")
    try:
        download_with_progress(MODEL_URL, MODEL_PATH)
    except Exception as e:
        raise SystemExit(
            f"model download failed ({e}). Download it manually from {MODEL_URL} "
            f"and place it at {MODEL_PATH}")
    log("model.pt downloaded", "OK")


# --------------------------------------------------------------------------
# optional stages: voiceprint-api / mcp-endpoint-server
#
# Both are separate repos (see docs/voiceprint-integration.md and
# docs/mcp-endpoint-enable.md) that run as their own docker-compose project,
# not part of the core 方式二 pipeline. They're opt-in: --only voiceprint,mcp.
# --------------------------------------------------------------------------
def read_key_value(path, pattern):
    if not path.exists():
        return None
    m = re.search(pattern, path.read_text(encoding="utf-8"), re.MULTILINE)
    return m.group(1) if m else None


def read_voiceprint_token():
    return read_key_value(VOICEPRINT_DIR / "data" / ".voiceprint.yaml", r"authorization:\s*(\S+)")


def read_mcp_key():
    return read_key_value(MCP_ENDPOINT_DIR / "data" / ".mcp-endpoint-server.cfg", r"^key\s*=\s*(\S+)")


def ensure_voiceprint_uses_docker_internal_host():
    """voiceprint-api runs in its own container and talks to our MySQL
    container over the host's published 3306 port. A LAN IP works but goes
    stale the moment the host's IP changes; host.docker.internal is Docker
    Desktop's stable alias for "the host machine" and never needs updating.
    Returns True if the config file was rewritten (caller should restart the
    container for the change to take effect)."""
    cfg = VOICEPRINT_DIR / "data" / ".voiceprint.yaml"
    if not cfg.exists():
        return False
    text = cfg.read_text(encoding="utf-8")
    if re.search(r"host:\s*host\.docker\.internal", text):
        return False
    new_text = re.sub(r"(host:\s*)\S+", r"\1host.docker.internal", text, count=1)
    if new_text == text:
        return False
    cfg.write_text(new_text, encoding="utf-8")
    log("rewrote voiceprint-api's mysql.host to host.docker.internal "
        "(stable regardless of LAN IP changes)", "OK")
    return True


def stage_voiceprint(args):
    log("== stage: voiceprint (voiceprint-api) ==")
    if not VOICEPRINT_DIR.exists():
        raise SystemExit(
            f"voiceprint-api not found at {VOICEPRINT_DIR}. Download it from "
            f"https://github.com/xinnan-tech/voiceprint-api, rename the folder "
            f"to 'voiceprint-api' and place it there (or set VOICEPRINT_API_DIR).")
    if not check_docker():
        raise SystemExit("Docker is required for this stage.")

    docker = resolve_cmd("docker")
    changed = ensure_voiceprint_uses_docker_internal_host()
    already_up = port_open("127.0.0.1", VOICEPRINT_PORT)

    if already_up and changed:
        log("config changed, restarting voiceprint-api container", "INFO")
        run([docker, "compose", "-f", "docker-compose.yml", "restart"], cwd=VOICEPRINT_DIR)
        already_up = False

    if already_up:
        log("voiceprint-api already up", "OK")
    else:
        run([docker, "compose", "-f", "docker-compose.yml", "up", "-d"], cwd=VOICEPRINT_DIR)
        if not wait_for(
                lambda: port_open("127.0.0.1", VOICEPRINT_PORT),
                timeout=180, interval=3,
                desc="voiceprint-api on port 8005 (first run downloads a speaker model)"):
            raise StageFailed("voiceprint-api did not become ready on port 8005")

    token = read_voiceprint_token()
    if token:
        log("voiceprint-api auth token is configured (value hidden)", "OK")
        log("build server.voice_print from the token in data/.voiceprint.yaml", "INFO")
    else:
        log(f"couldn't read the auth token from {VOICEPRINT_DIR / 'data' / '.voiceprint.yaml'}", "WARN")


def stage_mcp(args):
    log("== stage: mcp (mcp-endpoint-server) ==")
    if not MCP_ENDPOINT_DIR.exists():
        raise SystemExit(
            f"mcp-endpoint-server not found at {MCP_ENDPOINT_DIR}. Download it from "
            f"https://github.com/xinnan-tech/mcp-endpoint-server, rename the folder "
            f"to 'mcp-endpoint-server' and place it there (or set MCP_ENDPOINT_DIR).")
    if not check_docker():
        raise SystemExit("Docker is required for this stage.")

    docker = resolve_cmd("docker")
    if port_open("127.0.0.1", MCP_ENDPOINT_PORT):
        log("mcp-endpoint-server already up", "OK")
    else:
        run([docker, "compose", "-f", "docker-compose.yml", "up", "-d"], cwd=MCP_ENDPOINT_DIR)
        if not wait_for(lambda: port_open("127.0.0.1", MCP_ENDPOINT_PORT),
                        timeout=60, interval=2,
                        desc="mcp-endpoint-server on port 8004"):
            raise StageFailed("mcp-endpoint-server did not become ready on port 8004")

    key = read_mcp_key()
    if key:
        log("mcp-endpoint-server key is configured (value hidden)", "OK")
        log("build server.mcp_endpoint from the key in data/.mcp-endpoint-server.cfg", "INFO")
    else:
        log(f"couldn't read the key from {MCP_ENDPOINT_DIR / 'data' / '.mcp-endpoint-server.cfg'}", "WARN")


# --------------------------------------------------------------------------
# stage: secret (the one manual step) + data/.config.yaml
# --------------------------------------------------------------------------
def read_config_secret(config_path):
    if not config_path.exists():
        return None
    text = config_path.read_text(encoding="utf-8", errors="replace")
    m = re.search(r"^\s*secret:\s*([^\s#]+)", text, re.MULTILINE)
    if not m:
        return None
    value = m.group(1).strip("'\"")
    if not value or value.lower() in ("null", "none") or "你的" in value:
        return None
    return value


def config_has_real_secret(config_path):
    return read_config_secret(config_path) is not None


def read_manager_param(param_code):
    """Read one manager parameter directly from the local MySQL container.

    This is intentionally limited to known parameter names and captured
    silently: server.secret must never be printed into the deployment log.
    """
    allowed = {"server.secret", "server.websocket", "server.ota"}
    if param_code not in allowed or not container_running(MYSQL_CONTAINER):
        return None
    sql = (
        "SELECT param_value FROM sys_params "
        f"WHERE param_code='{param_code}' LIMIT 1;"
    )
    result = run_capture([
        resolve_cmd("docker"), "exec", MYSQL_CONTAINER,
        "mysql", "-N", "-B", "-uroot", f"-p{MYSQL_ROOT_PASSWORD}",
        MYSQL_DB, "-e", sql,
    ])
    if result.returncode != 0:
        return None
    value = result.stdout.strip().splitlines()
    if not value:
        return None
    value = value[-1].strip()
    return None if not value or value.lower() == "null" else value


def write_manager_param(param_code, value):
    allowed = {"server.websocket", "server.ota"}
    if param_code not in allowed or not container_running(MYSQL_CONTAINER):
        return False
    escaped = value.replace("\\", "\\\\").replace("'", "''")
    sql = (
        "UPDATE sys_params SET param_value="
        f"'{escaped}' WHERE param_code='{param_code}';"
    )
    result = run_capture([
        resolve_cmd("docker"), "exec", MYSQL_CONTAINER,
        "mysql", "-N", "-B", "-uroot", f"-p{MYSQL_ROOT_PASSWORD}",
        MYSQL_DB, "-e", sql,
    ])
    if result.returncode != 0:
        return False
    # manager-api caches parameters in Redis. Remove only the updated key so
    # its next read is guaranteed to see the database value.
    if container_running(REDIS_CONTAINER):
        run_capture([
            resolve_cmd("docker"), "exec", REDIS_CONTAINER,
            "redis-cli", "HDEL", "sys:params", param_code,
        ])
    return True


def write_manager_config(config_path, template_path, api_url, secret):
    text = template_path.read_text(encoding="utf-8")
    text = re.sub(
        r"(^\s*url:\s*)http://[^\n]+",
        lambda match: match.group(1) + api_url,
        text,
        count=1,
        flags=re.MULTILINE,
    )
    text = re.sub(
        r"(^\s*secret:\s*)\S+",
        lambda match: match.group(1) + secret,
        text,
        count=1,
        flags=re.MULTILINE,
    )
    config_path.parent.mkdir(parents=True, exist_ok=True)
    config_path.write_text(text, encoding="utf-8")


def configure_public_endpoints():
    ip = lan_ip()
    if ip.startswith("127."):
        log("no usable LAN IPv4 address was detected; keeping the existing "
            "WebSocket/OTA addresses", "WARN")
        return {}, False

    log(f"detected current LAN IPv4: {ip}")
    values = {
        "server.websocket": f"ws://{ip}:{SERVER_PORT}/xiaozhi/v1/",
        "server.ota": f"http://{ip}:{API_PORT}/xiaozhi/ota/",
    }
    updated = True
    for key, value in values.items():
        current = read_manager_param(key)
        if current == value:
            log(f"{key} already matches current IP -> {value}", "OK")
        elif write_manager_param(key, value):
            old_value = current or "not set"
            log(f"updated {key}: {old_value} -> {value}", "OK")
        else:
            updated = False
    return values, updated


def stage_secret(args):
    log("== stage: secret (manager-api registration + config) ==")
    config_path = XIAOZHI_SERVER / "data" / ".config.yaml"
    template_path = XIAOZHI_SERVER / "config_from_api.yaml"

    if not template_path.exists():
        raise StageFailed(f"template not found: {template_path}")

    configured_secret = read_config_secret(config_path)

    # manager-api creates server.secret during startup. For the local-source
    # deployment we own the MySQL container, so this formerly manual step can
    # be completed safely without asking the user to expose the secret.
    database_secret = read_manager_param("server.secret")
    if database_secret:
        api_url = f"http://127.0.0.1:{API_PORT}/xiaozhi"
        if (args.reconfigure or configured_secret != database_secret
                or not config_path.exists()):
            write_manager_config(
                config_path, template_path, api_url, database_secret
            )
            log("wrote data/.config.yaml using manager-api's generated secret", "OK")
        else:
            log("data/.config.yaml already matches manager-api's secret", "OK")
        _, endpoints_updated = configure_public_endpoints()
        if not endpoints_updated:
            log("could not update websocket/OTA parameters automatically; "
                "set them later in 参数管理", "WARN")
        return

    if configured_secret and not args.reconfigure:
        log("data/.config.yaml already has a secret configured; the local "
            "database was unavailable, so it could not be cross-checked", "WARN")
        return

    HOOKS.emit("")
    HOOKS.emit("  This step needs a human: manager-api hands out a fresh random")
    HOOKS.emit("  server.secret on every from-scratch deploy, and it's only visible")
    HOOKS.emit("  after you log into the web console.")
    HOOKS.emit("")
    HOOKS.emit(f"  1. Open http://127.0.0.1:{WEB_PORT} (opening it for you now)")
    HOOKS.emit("  2. Register an account - the FIRST account registered becomes admin")
    HOOKS.emit("  3. Go to 参数字典 -> 参数管理, find server.secret, copy its value")
    HOOKS.emit("")
    try:
        webbrowser.open(f"http://127.0.0.1:{WEB_PORT}")
    except Exception:
        pass

    if STOP_EVENT.is_set():
        raise StageAborted("stopped before secret entry")

    api_url = HOOKS.ask_text("manager-api URL", f"http://127.0.0.1:{API_PORT}/xiaozhi")
    secret = HOOKS.ask_text("server.secret value (leave empty to skip)", "")
    if not secret:
        log("no secret entered, leaving data/.config.yaml unwritten", "WARN")
        return

    write_manager_config(config_path, template_path, api_url, secret)
    log(f"wrote {config_path}", "OK")
    configure_public_endpoints()
    HOOKS.emit("")
    HOOKS.emit("  Don't forget to also set these in 参数管理 (manager-web):")
    HOOKS.emit(f"    server.websocket -> ws://{lan_ip()}:{SERVER_PORT}/xiaozhi/v1/")
    HOOKS.emit(f"    server.ota       -> http://{lan_ip()}:{API_PORT}/xiaozhi/ota/")
    HOOKS.emit("  and put a model API key under 模型配置 before talking to a device.")
    HOOKS.emit("")


# --------------------------------------------------------------------------
# stage: xiaozhi-server
# --------------------------------------------------------------------------
def stage_server(args):
    log("== stage: server (xiaozhi-server) ==")
    if not check_conda() or not conda_env_exists():
        raise StageFailed("Run --only pyenv first to create the conda environment.")
    if not config_has_real_secret(XIAOZHI_SERVER / "data" / ".config.yaml"):
        raise StageFailed("Run --only secret first to configure manager-api access.")

    cmd = [resolve_cmd("conda"), "run", "-n", CONDA_ENV, "--no-capture-output", "python", "app.py"]
    if args.foreground:
        log("running in foreground, Ctrl+C to stop")
        run(cmd, cwd=XIAOZHI_SERVER)
        return

    health_url = f"http://127.0.0.1:{HTTP_PORT}/health"
    state = load_state()
    if http_ready(health_url):
        tracked_pid = state.get("xiaozhi_server_pid")
        if getattr(args, "restart", False) and pid_alive(tracked_pid):
            log("restarting the tracked xiaozhi-server to load current code")
            kill_pid(tracked_pid)
            wait_for_port_release(HTTP_PORT)
        elif getattr(args, "restart", False):
            raise StageFailed(
                f"port {HTTP_PORT} is occupied by an untracked healthy service"
            )
        else:
            log("xiaozhi-server health check already passes", "OK")
            return

    old_pid = state.get("xiaozhi_server_pid")
    if port_open("127.0.0.1", HTTP_PORT):
        if pid_alive(old_pid):
            log("restarting the tracked xiaozhi-server to load current code")
            kill_pid(old_pid)
            wait_for_port_release(HTTP_PORT)
        else:
            raise StageFailed(
                f"port {HTTP_PORT} is occupied by an untracked process and "
                "the health endpoint is unavailable"
            )

    log_path = XIAOZHI_SERVER / "tmp" / "auto_deploy_server.log"
    pid = spawn_background(cmd, cwd=XIAOZHI_SERVER, log_path=log_path)
    state["xiaozhi_server_pid"] = pid
    save_state(state)
    def ready():
        if not pid_alive(pid):
            raise StageFailed(f"xiaozhi-server exited early; check {log_path}")
        return http_ready(health_url)

    if not wait_for(ready, timeout=120, interval=2,
                    desc=f"xiaozhi-server health endpoint on port {HTTP_PORT}"):
        raise StageFailed(f"xiaozhi-server did not become ready; check {log_path}")


# --------------------------------------------------------------------------
# status / stop
# --------------------------------------------------------------------------
def collect_status():
    """Structured snapshot of everything this script tracks or can detect,
    reused by both the CLI's --status printout and the GUI's status panel."""
    state = load_state()
    services = {}
    for label, port, pid_key, probe in [
        ("manager-api", API_PORT, "manager_api_pid", None),
        ("manager-web", WEB_PORT, "manager_web_pid", None),
        ("xiaozhi-server", SERVER_PORT, "xiaozhi_server_pid",
         lambda: http_ready(f"http://127.0.0.1:{HTTP_PORT}/health")),
    ]:
        pid = state.get(pid_key)
        up = probe() if probe else port_open("127.0.0.1", port)
        services[label] = dict(port=port, up=up,
                                pid=pid, alive=pid_alive(pid))
    docker_ready = check_docker()
    return dict(
        ip=lan_ip(),
        docker_ready=docker_ready,
        containers={
            MYSQL_CONTAINER: docker_ready and container_running(MYSQL_CONTAINER),
            REDIS_CONTAINER: docker_ready and container_running(REDIS_CONTAINER),
        },
        services=services,
        secret_set=config_has_real_secret(XIAOZHI_SERVER / "data" / ".config.yaml"),
        voiceprint=dict(
            installed=VOICEPRINT_DIR.exists(),
            up=port_open("127.0.0.1", VOICEPRINT_PORT),
            credential_set=bool(read_voiceprint_token()),
        ),
        mcp=dict(
            installed=MCP_ENDPOINT_DIR.exists(),
            up=port_open("127.0.0.1", MCP_ENDPOINT_PORT),
            credential_set=bool(read_mcp_key()),
        ),
    )


def do_status(args):
    s = collect_status()
    print(f"Docker: {'ready' if s['docker_ready'] else 'daemon unavailable'}")
    print(f"  {MYSQL_CONTAINER}: {'running' if s['containers'][MYSQL_CONTAINER] else 'stopped/absent'}")
    print(f"  {REDIS_CONTAINER}: {'running' if s['containers'][REDIS_CONTAINER] else 'stopped/absent'}")
    print("Services:")
    for label, info in s["services"].items():
        print(f"  {label}: {'UP' if info['up'] else 'down'} (port {info['port']}, tracked pid {info['pid']}, "
              f"{'alive' if info['alive'] else 'not tracked/alive'})")
    print("Config:")
    print(f"  data/.config.yaml secret set: {s['secret_set']}")
    print("Optional modules:")
    vp = s["voiceprint"]
    if vp["installed"]:
        print(f"  voiceprint-api      : {'UP' if vp['up'] else 'down'} (port {VOICEPRINT_PORT})"
              + (" (credential configured)" if vp["credential_set"] else ""))
    else:
        print(f"  voiceprint-api      : not installed ({VOICEPRINT_DIR})")
    mcp = s["mcp"]
    if mcp["installed"]:
        print(f"  mcp-endpoint-server : {'UP' if mcp['up'] else 'down'} (port {MCP_ENDPOINT_PORT})"
              + (" (credential configured)" if mcp["credential_set"] else ""))
    else:
        print(f"  mcp-endpoint-server : not installed ({MCP_ENDPOINT_DIR})")
    print("URLs (once everything is up):")
    print(f"  admin console : http://{s['ip']}:{WEB_PORT}")
    print(f"  manager-api   : http://{s['ip']}:{API_PORT}/xiaozhi")
    print(f"  OTA           : http://{s['ip']}:{API_PORT}/xiaozhi/ota/")
    print(f"  websocket     : ws://{s['ip']}:{SERVER_PORT}/xiaozhi/v1/")


def do_stop(args):
    state = load_state()
    for key in ("manager_api_pid", "manager_web_pid", "xiaozhi_server_pid"):
        kill_pid(state.get(key))
        state.pop(key, None)
    save_state(state)
    if confirm("Also stop the MySQL/Redis containers?", args.yes):
        for name in (MYSQL_CONTAINER, REDIS_CONTAINER):
            if container_running(name):
                run([resolve_cmd("docker"), "stop", name], check=False)
    if (VOICEPRINT_DIR.exists() or MCP_ENDPOINT_DIR.exists()) and confirm(
            "Also stop voiceprint-api / mcp-endpoint-server (if installed/running)?", args.yes):
        for name in (VOICEPRINT_CONTAINER, MCP_ENDPOINT_CONTAINER):
            if container_running(name):
                run([resolve_cmd("docker"), "stop", name], check=False)


# --------------------------------------------------------------------------
# main
# --------------------------------------------------------------------------
STAGE_FUNCS = {
    "prereqs": stage_prereqs,
    "db": stage_db,
    "api": stage_api,
    "web": stage_web,
    "pyenv": stage_pyenv,
    "model": stage_model,
    "secret": stage_secret,
    "server": stage_server,
    "voiceprint": stage_voiceprint,
    "mcp": stage_mcp,
}


def run_stage(name, args):
    """Run one stage, translating stop/error into a status instead of a crash.
    Returns one of: 'done', 'aborted', 'failed'."""
    HOOKS.stage_status(name, "running")
    try:
        result = STAGE_FUNCS[name](args)
        if result is False:
            raise StageFailed(f"stage '{name}' did not complete successfully")
    except StageAborted as e:
        log(str(e) or "stopped by user", "WARN")
        HOOKS.stage_status(name, "aborted")
        return "aborted"
    except SystemExit as e:
        log(str(e), "ERR")
        HOOKS.stage_status(name, "failed")
        return "failed"
    except Exception as e:
        log(f"unexpected error in stage '{name}': {e}", "ERR")
        HOOKS.stage_status(name, "failed")
        return "failed"
    HOOKS.stage_status(name, "done")
    return "done"


def run_stages(stages, args):
    """Run stages in order. Stops advancing on the first abort/failure, and
    marks anything after it 'skipped' so a status view stays accurate."""
    STOP_EVENT.clear()
    for i, name in enumerate(stages):
        if STOP_EVENT.is_set():
            HOOKS.stage_status(name, "skipped")
            continue
        result = run_stage(name, args)
        if result in ("aborted", "failed"):
            for remaining in stages[i + 1:]:
                HOOKS.stage_status(remaining, "skipped")
            return False
    return True


def parse_stage_list(value):
    if not value:
        return None
    return [s.strip() for s in value.split(",") if s.strip()]


def main():
    p = argparse.ArgumentParser(
        description="Automates docs/Deployment_all.md 方式二 (local source, all modules).",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__,
    )
    p.add_argument(
        "--only",
        help="comma-separated list of stages to run: "
             + ",".join(STAGES + OPTIONAL_STAGES),
    )
    p.add_argument("--skip", help="comma-separated list of stages to skip")
    p.add_argument("--yes", action="store_true", help="don't prompt before installing tools/stopping containers")
    p.add_argument("--no-install", action="store_true", help="never attempt to install missing prerequisites")
    p.add_argument("--no-mirror", action="store_true", help="skip the China (Tsinghua/Aliyun) mirror configuration")
    p.add_argument("--reconfigure", action="store_true", help="redo the secret/config step even if already set")
    p.add_argument("--restart", action="store_true", help="restart tracked services selected by this run")
    p.add_argument("--foreground", action="store_true", help="run xiaozhi-server attached instead of backgrounded")
    p.add_argument("--status", action="store_true", help="show current status and exit")
    p.add_argument("--stop", action="store_true", help="stop background processes (and optionally containers)")
    args = p.parse_args()

    if args.status:
        return do_status(args)
    if args.stop:
        return do_stop(args)

    only = parse_stage_list(args.only)
    skip = parse_stage_list(args.skip) or []
    stages = only if only else [s for s in STAGES if s not in skip]

    for name in stages:
        if name not in STAGE_FUNCS:
            raise SystemExit(
                f"unknown stage '{name}', valid stages: "
                f"{', '.join(STAGES + OPTIONAL_STAGES)}"
            )

    log(f"platform: {OS_NAME}, repo: {REPO_ROOT}")
    log(f"stages to run: {', '.join(stages)}")

    success = run_stages(stages, args)

    print()
    if success:
        log("deployment completed.", "OK")
    else:
        log("deployment stopped because a stage failed.", "ERR")
    do_status(args)
    return 0 if success else 1


if __name__ == "__main__":
    try:
        sys.exit(main())
    except KeyboardInterrupt:
        log("interrupted", "WARN")
        sys.exit(130)
