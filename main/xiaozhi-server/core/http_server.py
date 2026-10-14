import uuid
import asyncio
import pathlib
from aiohttp import web
from config.logger import setup_logging
from core.api.ota_handler import OTAHandler
from core.api.vision_handler import VisionHandler
from core.utils.dialogue import Dialogue, Message
from core.utils.modules_initialize import initialize_modules

TAG = __name__
BASE_DIR = pathlib.Path(__file__).resolve().parent   # core/
WEB_DIR = BASE_DIR / "web"                           # core/web/

# Max concurrent text-chat sessions kept in memory
_MAX_SESSIONS = 200


class SimpleHttpServer:
    def __init__(self, config: dict, ws_server):
        self.config = config
        self.ws_server = ws_server
        self.logger = setup_logging()
        self.ota_handler = OTAHandler(config)
        self.vision_handler = VisionHandler(config)
        # per-session dialogue history for the text-chat API
        self._sessions: dict[str, Dialogue] = {}
        # dedicated LLM for text-chat (used when ws_server._llm is None)
        self._text_llm = None

    def _get_websocket_url(self, local_ip: str, port: int) -> str:
        server_config = self.config["server"]
        websocket_config = server_config.get("websocket")
        if websocket_config and "你" not in websocket_config:
            return websocket_config
        return f"ws://{local_ip}:{port}/xiaozhi/v1/"

    def _get_llm(self):
        """Return an LLM instance, lazy-initialising one when needed.

        Priority:
          1. Shared LLM already on the WebSocket server (standalone mode).
          2. Initialise from the running config (standalone mode, LLM key present).
          3. Fall back to config.yaml defaults (manager-api mode where the
             server-level config has no per-device LLM section).
        """
        if self.ws_server._llm is not None:
            return self.ws_server._llm

        if self._text_llm is not None:
            return self._text_llm

        # --- attempt 1: current runtime config ---
        cfg = self.config
        if cfg.get("selected_module", {}).get("LLM") and cfg.get("LLM"):
            try:
                modules = initialize_modules(self.logger, cfg, init_llm=True)
                self._text_llm = modules.get("llm")
                if self._text_llm:
                    self.logger.bind(tag=TAG).info("Text-chat LLM initialised from runtime config")
                    return self._text_llm
            except Exception as e:
                self.logger.bind(tag=TAG).warning(f"Runtime config LLM init failed: {e}")

        # --- attempt 2: fall back to config.yaml defaults ---
        # Only use this if the api_key is not a placeholder (contains Chinese).
        try:
            from config.config_loader import get_project_dir, read_config
            default_cfg = read_config(get_project_dir() + "config.yaml")
            sel = default_cfg.get("selected_module", {}).get("LLM")
            llm_cfg = default_cfg.get("LLM", {}).get(sel) if sel else None
            api_key = str(llm_cfg.get("api_key", "") if llm_cfg else "")
            if llm_cfg and api_key and not any(ord(c) > 127 for c in api_key):
                modules = initialize_modules(self.logger, default_cfg, init_llm=True)
                self._text_llm = modules.get("llm")
                if self._text_llm:
                    self.logger.bind(tag=TAG).info("Text-chat LLM initialised from config.yaml defaults")
                    return self._text_llm
            else:
                self.logger.bind(tag=TAG).info(
                    "config.yaml LLM has placeholder values — "
                    "text-chat will become available once an ESP32 device connects"
                )
        except Exception as e:
            self.logger.bind(tag=TAG).error(f"Failed to init text-chat LLM from config.yaml: {e}")

        return None

    def _get_or_create_session(self, session_id: str) -> Dialogue:
        """Return existing dialogue for session_id, or start a fresh one."""
        if session_id not in self._sessions:
            if len(self._sessions) >= _MAX_SESSIONS:
                # evict the oldest entry
                oldest = next(iter(self._sessions))
                del self._sessions[oldest]
            d = Dialogue()
            # self.config["prompt"] may be None in manager-api mode;
            # fall back to config.yaml, then to a hard-coded default.
            system_prompt = self.config.get("prompt")
            if not system_prompt:
                try:
                    from config.config_loader import get_project_dir, read_config
                    system_prompt = read_config(
                        get_project_dir() + "config.yaml"
                    ).get("prompt")
                except Exception:
                    pass
            system_prompt = system_prompt or "You are a helpful assistant."
            d.put(Message(role="system", content=system_prompt))
            self._sessions[session_id] = d
        return self._sessions[session_id]

    async def handle_text_chat_page(self, request: web.Request):
        return web.FileResponse(WEB_DIR / "text_chat.html")

    async def handle_health(self, request: web.Request):
        """Lightweight readiness endpoint used by deployment and monitoring."""
        return web.json_response({"status": "ok"})

    async def handle_text_api(self, request: web.Request):
        """
        POST /text
        Body:  {"text": "...", "session_id": "<optional uuid>"}
        Reply: {"reply_text": "...", "session_id": "<uuid>"}
        """
        try:
            data = await request.json()
        except Exception:
            return web.json_response({"error": "Invalid JSON"}, status=400)

        text = (data.get("text") or "").strip()
        if not text:
            return web.json_response({"error": "text is empty"}, status=400)

        llm = self._get_llm()
        if llm is None:
            return web.json_response(
                {"error": "LLM not available — check your LLM configuration in config.yaml"},
                status=503,
            )

        session_id = (data.get("session_id") or "").strip() or uuid.uuid4().hex
        dialogue = self._get_or_create_session(session_id)
        dialogue.put(Message(role="user", content=text))

        def _call_llm():
            chunks = []
            for chunk in llm.response(session_id, dialogue.get_llm_dialogue()):
                if chunk:
                    chunks.append(chunk)
            return "".join(chunks)

        loop = asyncio.get_running_loop()
        try:
            reply_text = await loop.run_in_executor(None, _call_llm)
        except Exception as e:
            self.logger.bind(tag=TAG).error(f"LLM error: {e}")
            return web.json_response({"error": str(e)}, status=500)

        dialogue.put(Message(role="assistant", content=reply_text))

        return web.json_response({"reply_text": reply_text, "session_id": session_id})

    async def handle_api_config(self, request: web.Request):
        """GET /api/config — return current model selections and server mode."""
        mode = "manager-api" if self.config.get("read_config_from_api") else "standalone"
        manager_url = ""
        if mode == "manager-api":
            manager_url = self.config.get("manager-api", {}).get("url", "")

        # Prefer the per-device module names cached from the latest ESP32
        # connection; fall back to the server-level selected_module dict.
        cached = getattr(self.ws_server, "cached_device_selected_modules", {})
        fallback = self.config.get("selected_module", {})
        def sel(key):
            return cached.get(key) or fallback.get(key) or "—"

        llm_ready = (
            self.ws_server._llm is not None or self._text_llm is not None
        )
        return web.json_response({
            "mode": mode,
            "manager_api_url": manager_url,
            "selected_modules": {
                "LLM":    sel("LLM"),
                "TTS":    sel("TTS"),
                "ASR":    sel("ASR"),
                "Memory": sel("Memory"),
                "Intent": sel("Intent"),
            },
            "llm_ready": llm_ready,
            "synced": llm_ready,   # true once an ESP32 has connected
        })

    async def handle_ws_info(self, request: web.Request):
        server_config = self.config["server"]
        host = request.host.split(":")[0]
        ws_port = int(server_config.get("port", 8000))
        return web.json_response({
            "ws_url": self._get_websocket_url(host, ws_port),
            "ws_port": ws_port,
        })

    async def start(self):
        server_config = self.config["server"]
        read_config_from_api = self.config.get("read_config_from_api", False)
        host = server_config.get("ip", "0.0.0.0")
        port = int(server_config.get("http_port", 8003))

        if port:
            app = web.Application()

            if not read_config_from_api:
                app.add_routes([
                    web.get("/xiaozhi/ota/", self.ota_handler.handle_get),
                    web.post("/xiaozhi/ota/", self.ota_handler.handle_post),
                    web.options("/xiaozhi/ota/", self.ota_handler.handle_post),
                ])

            app.add_routes([
                web.get("/mcp/vision/explain", self.vision_handler.handle_get),
                web.post("/mcp/vision/explain", self.vision_handler.handle_post),
                web.options("/mcp/vision/explain", self.vision_handler.handle_post),
            ])

            app.add_routes([
                web.get("/health", self.handle_health),
                web.get("/text_chat", self.handle_text_chat_page),
                web.post("/text", self.handle_text_api),
                web.options("/text", self.handle_text_api),
                web.get("/ws_info", self.handle_ws_info),
                web.get("/api/config", self.handle_api_config),
            ])

            runner = web.AppRunner(app)
            await runner.setup()
            site = web.TCPSite(runner, host, port)
            await site.start()

            while True:
                await asyncio.sleep(3600)
