import asyncio
import json

import websockets
from config.logger import setup_logging
from core.connection import ConnectionHandler
from config.config_loader import get_config_from_api
from core.auth import AuthManager, AuthenticationError
from core.utils.modules_initialize import initialize_modules
from core.utils.util import check_vad_update, check_asr_update

TAG = __name__


class WebSocketServer:
    def __init__(self, config: dict):
        self.config = config
        self.logger = setup_logging()
        self.config_lock = asyncio.Lock()
        modules = initialize_modules(
            self.logger,
            self.config,
            "VAD" in self.config["selected_module"],
            "ASR" in self.config["selected_module"],
            "LLM" in self.config["selected_module"],
            False,
            "Memory" in self.config["selected_module"],
            "Intent" in self.config["selected_module"],
        )
        self._vad = modules["vad"] if "vad" in modules else None
        self._asr = modules["asr"] if "asr" in modules else None
        self._llm = modules["llm"] if "llm" in modules else None
        self._intent = modules["intent"] if "intent" in modules else None
        self._memory = modules["memory"] if "memory" in modules else None

        # Latest per-device config — refreshed each time an ESP32 connects.
        # Web connections read LLM + prompt from here (safe to share: stateless).
        # Memory/intent instances are NOT shared: they have mutable per-connection
        # state and are not thread-safe to use across simultaneous connections.
        self.cached_device_prompt = None
        self.cached_device_selected_modules = {}

        # Half-duplex mode gate.
        # True  → text mode active: web text input is live, ESP32 mic is paused.
        # False → voice mode active (default): ESP32 mic is live, web text is standby.
        # Controlled by {"type":"web_mode","mode":"text"|"voice"} WebSocket messages.
        # Always reset to False when the web client disconnects.
        self.text_mode_active = False

        # Track every live ConnectionHandler so update_config() can push
        # a new LLM to all active connections immediately after admin changes.
        self._active_handlers = set()
        # Physical ESP32/MQTT connections are tracked separately from browser
        # text-chat connections.  This is the source of truth for the web UI's
        # online/offline indicator; cached models must never imply that a
        # disconnected device is still online.
        self._hardware_handlers = set()
        self.listener_ready = False

        auth_config = self.config["server"].get("auth", {})
        self.auth_enable = auth_config.get("enabled", False)
        # 设备白名单
        self.allowed_devices = set(auth_config.get("allowed_devices", []))
        secret_key = self.config["server"]["auth_key"]
        expire_seconds = auth_config.get("expire_seconds", None)
        self.auth = AuthManager(secret_key=secret_key, expire_seconds=expire_seconds)

    async def start(self):
        server_config = self.config["server"]
        host = server_config.get("ip", "0.0.0.0")
        port = int(server_config.get("port", 8000))

        try:
            async with websockets.serve(
                self._handle_connection, host, port,
                process_request=self._http_response,
                ping_interval=20,   # server sends a WS-protocol ping every 20 s
                ping_timeout=10,    # close if no pong within 10 s (stale connection)
            ):
                # The listening socket remains open whether ESP32 is online or
                # offline.  Only application shutdown cancels this future.
                self.listener_ready = True
                await asyncio.Future()
        finally:
            self.listener_ready = False

    @property
    def hardware_online(self):
        """Whether at least one authenticated physical device is connected."""
        return bool(self._hardware_handlers)

    def hardware_status(self):
        """Return a privacy-safe status payload for HTTP and web clients."""
        model_ready = self._llm is not None
        return {
            "type": "hardware_status",
            "online": self.hardware_online,
            "connection_count": len(self._hardware_handlers),
            # Browser chat is deliberately hardware-first.
            "llm_ready": self.hardware_online and model_ready,
            "model_ready": model_ready,
            "listener": "listening" if self.listener_ready else "starting",
            "ws_port": int(self.config["server"].get("port", 8000)),
        }

    async def _send_hardware_status(self, websocket):
        try:
            await websocket.send(json.dumps(self.hardware_status()))
        except Exception:
            # The normal connection cleanup path removes stale clients.
            pass

    async def _broadcast_hardware_status(self):
        """Push a device transition to every connected browser immediately."""
        targets = [
            handler.websocket
            for handler in list(self._active_handlers)
            if handler.is_web_connection and handler.websocket is not None
        ]
        if targets:
            await asyncio.gather(
                *(self._send_hardware_status(ws) for ws in targets),
                return_exceptions=True,
            )

    async def _handle_connection(self, websocket):
        headers = dict(websocket.request.headers)
        if headers.get("device-id", None) is None:
            # 尝试从 URL 的查询参数中获取 device-id
            from urllib.parse import parse_qs, urlparse

            # 从 WebSocket 请求中获取路径
            request_path = websocket.request.path
            if not request_path:
                self.logger.bind(tag=TAG).error("无法获取请求路径")
                await websocket.close()
                return
            parsed_url = urlparse(request_path)
            query_params = parse_qs(parsed_url.query)
            if "device-id" not in query_params:
                await websocket.send("端口正常，如需测试连接，请使用test_page.html")
                await websocket.close()
                return
            else:
                websocket.request.headers["device-id"] = query_params["device-id"][0]
            if "client-id" in query_params:
                websocket.request.headers["client-id"] = query_params["client-id"][0]
            if "authorization" in query_params:
                websocket.request.headers["authorization"] = query_params[
                    "authorization"
                ][0]

        """处理新连接，每次创建独立的ConnectionHandler"""
        # 先认证，后建立连接
        try:
            await self._handle_auth(websocket)
        except AuthenticationError:
            await websocket.send("认证失败")
            await websocket.close()
            return
        # Query-string credentials may have been copied into request headers
        # above (the browser text client uses this path), so refresh the view.
        headers = dict(websocket.request.headers)
        # 创建ConnectionHandler时传入当前server实例
        handler = ConnectionHandler(
            self.config,
            self._vad,
            self._asr,
            self._llm,
            self._memory,
            self._intent,
            self,  # 传入server实例
        )
        device_id = headers.get("device-id", "")
        handler.device_id = device_id
        handler.is_web_connection = device_id.startswith("web-")
        handler.websocket = websocket
        self._active_handlers.add(handler)
        if handler.is_web_connection:
            # The browser can stay connected while hardware is offline and
            # receive the next online transition without reconnecting.
            await self._send_hardware_status(websocket)
        else:
            self._hardware_handlers.add(handler)
            await self._broadcast_hardware_status()
        try:
            await handler.handle_connection(websocket)
        except Exception as e:
            self.logger.bind(tag=TAG).error(f"处理连接时出错: {e}")
        finally:
            self._active_handlers.discard(handler)
            was_hardware = handler in self._hardware_handlers
            self._hardware_handlers.discard(handler)
            if was_hardware:
                if not self.hardware_online:
                    self.text_mode_active = False
                await self._broadcast_hardware_status()
            # 强制关闭连接（如果还没有关闭的话）
            try:
                # 安全地检查WebSocket状态并关闭
                if hasattr(websocket, "closed") and not websocket.closed:
                    await websocket.close()
                elif hasattr(websocket, "state") and websocket.state.name != "CLOSED":
                    await websocket.close()
                else:
                    # 如果没有closed属性，直接尝试关闭
                    await websocket.close()
            except Exception as close_error:
                self.logger.bind(tag=TAG).error(
                    f"服务器端强制关闭连接时出错: {close_error}"
                )

    async def _http_response(self, websocket, request_headers):
        # 检查是否为 WebSocket 升级请求
        if request_headers.headers.get("connection", "").lower() == "upgrade":
            # 如果是 WebSocket 请求，返回 None 允许握手继续
            return None
        else:
            # 如果是普通 HTTP 请求，返回 "server is running"
            return websocket.respond(200, "Server is running\n")

    async def update_config(self) -> bool:
        """更新服务器配置并重新初始化组件

        Returns:
            bool: 更新是否成功
        """
        try:
            async with self.config_lock:
                # 重新获取配置
                new_config = get_config_from_api(self.config)
                if new_config is None:
                    self.logger.bind(tag=TAG).error("获取新配置失败")
                    return False
                self.logger.bind(tag=TAG).info(f"获取新配置成功")
                # 检查 VAD 和 ASR 类型是否需要更新
                update_vad = check_vad_update(self.config, new_config)
                update_asr = check_asr_update(self.config, new_config)
                self.logger.bind(tag=TAG).info(
                    f"检查VAD和ASR类型是否需要更新: {update_vad} {update_asr}"
                )
                # 更新配置
                self.config = new_config
                # 重新初始化组件
                modules = initialize_modules(
                    self.logger,
                    new_config,
                    update_vad,
                    update_asr,
                    "LLM" in new_config["selected_module"],
                    False,
                    "Memory" in new_config["selected_module"],
                    "Intent" in new_config["selected_module"],
                )

                # ── Update server-level module references ────────────────
                if "vad"    in modules: self._vad    = modules["vad"]
                if "asr"    in modules: self._asr    = modules["asr"]
                if "llm"    in modules: self._llm    = modules["llm"]
                if "intent" in modules: self._intent = modules["intent"]
                if "memory" in modules: self._memory = modules["memory"]

                # ── Push every updated module to all live connections ────
                # Derive the new intent pipeline type from the new config so
                # active connections use the right intent handler immediately.
                sel_intent = new_config.get("selected_module", {}).get("Intent", "")
                new_intent_type = (
                    new_config.get("Intent", {})
                    .get(sel_intent, {})
                    .get("type", "nointent")
                ) if sel_intent else "nointent"

                pushed = []
                for h in list(self._active_handlers):
                    try:
                        if "llm"    in modules and self._llm    is not None:
                            h.llm    = self._llm;    pushed.append("llm")
                        if "intent" in modules and self._intent is not None:
                            h.intent = self._intent; h.intent_type = new_intent_type
                            pushed.append("intent")
                        if "memory" in modules and self._memory is not None:
                            h.memory = self._memory; pushed.append("memory")
                        if "vad"    in modules and self._vad    is not None:
                            h._vad   = self._vad
                            if h.vad is not None: h.vad = self._vad
                            pushed.append("vad")
                        if "asr"    in modules and self._asr    is not None:
                            # Update the shared reference; the live per-connection
                            # ASR keeps running — new type takes effect on reconnect.
                            h._asr = self._asr;  pushed.append("asr")
                    except Exception as push_err:
                        self.logger.bind(tag=TAG).warning(
                            f"Config push to handler failed: {push_err}"
                        )

                if pushed:
                    self.logger.bind(tag=TAG).info(
                        f"Config pushed {list(set(pushed))} to "
                        f"{len(self._active_handlers)} active connection(s)"
                    )
                self.logger.bind(tag=TAG).info(f"更新配置任务执行完毕")
                return True
        except Exception as e:
            self.logger.bind(tag=TAG).error(f"更新服务器配置失败: {str(e)}")
            return False

    async def _handle_auth(self, websocket):
        # 先认证，后建立连接
        if self.auth_enable:
            headers = dict(websocket.request.headers)
            device_id = headers.get("device-id", None)
            client_id = headers.get("client-id", None)
            if self.allowed_devices and device_id in self.allowed_devices:
                # 如果属于白名单内的设备，不校验token，直接放行
                return
            else:
                # 否则校验token
                token = headers.get("authorization", "")
                if token.startswith("Bearer "):
                    token = token[7:]  # 移除'Bearer '前缀
                else:
                    raise AuthenticationError("Missing or invalid Authorization header")
                # 进行认证
                auth_success = self.auth.verify_token(
                    token, client_id=client_id, username=device_id
                )
                if not auth_success:
                    raise AuthenticationError("Invalid token")
