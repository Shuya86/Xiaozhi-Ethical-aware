import asyncio
import pathlib
import json
from pydoc import text
from urllib import request
from aiohttp import web
from config.logger import setup_logging
from core.api.ota_handler import OTAHandler
from core.api.vision_handler import VisionHandler
from core.connection import ConnectionHandler

TAG = __name__
BASE_DIR = pathlib.Path(__file__).resolve().parent.parent
WEB_DIR = BASE_DIR / "web"

class SimpleHttpServer:
    def __init__(self, config: dict, ws_server):
        self.config = config
        self.ws_server = ws_server
        self.logger = setup_logging()
        self.ota_handler = OTAHandler(config)
        self.vision_handler = VisionHandler(config)

    def _get_websocket_url(self, local_ip: str, port: int) -> str:
        """获取websocket地址

        Args:
            local_ip: 本地IP地址
            port: 端口号

        Returns:
            str: websocket地址
        """
        server_config = self.config["server"]
        websocket_config = server_config.get("websocket")

        if websocket_config and "你" not in websocket_config:
            return websocket_config
        else:
            return f"ws://{local_ip}:{port}/xiaozhi/v1/"

    async def start(self):
        server_config = self.config["server"]
        read_config_from_api = self.config.get("read_config_from_api", False)
        host = server_config.get("ip", "0.0.0.0")
        port = int(server_config.get("http_port", 8003))

        if port:
            app = web.Application()
            conn_handler = ConnectionHandler(self.config)
            app["conn_handler"] = self.conn_handler

            if not read_config_from_api:
                # 如果没有开启智控台，只是单模块运行，就需要再添加简单OTA接口，用于下发websocket接口
                app.add_routes(
                    [
                        web.get("/xiaozhi/ota/", self.ota_handler.handle_get),
                        web.post("/xiaozhi/ota/", self.ota_handler.handle_post),
                        web.options("/xiaozhi/ota/", self.ota_handler.handle_post),
                    ]
                )
            
            async def handle_text_chat_page(self, request: web.Request):
                return web.FileResponse(WEB_DIR / "text_chat.html")
            
            async def handle_text_api(self, request: web.Request):
                """
                Web 文本输入接口：POST /text
                Body: {"text": "...", "depth": 0}
                Return: {"reply_text": "..."}
                """
                try:
                    data = await request.json()
                except Exception:
                    return web.json_response({"error": "Invalid JSON"}, status=400)

                text = (data.get("text") or "").strip()
                if not text:
                    return web.json_response({"error": "text is empty"}, status=400)
                
                ws = self.ws_server

                handler = ConnectionHandler(
                    self.config,
                    ws._vad,
                    ws._asr,
                    ws._llm,
                    ws._memory,
                    ws._intent,
                    ws,  # server = ws_server
                )

                # chat 是同步函数，放到线程池中运行
                loop = asyncio.get_running_loop()
                reply_text = await loop.run_in_executor(None, handler.chat, text, 0)

                return web.json_response({"reply_text": reply_text})


            # 添加路由
            app.add_routes(
                [
                    web.get("/mcp/vision/explain", self.vision_handler.handle_get),
                    web.post("/mcp/vision/explain", self.vision_handler.handle_post),
                    web.options("/mcp/vision/explain", self.vision_handler.handle_post),
                ]
            )
            
            # 添加文本聊天页面和API路由
            app.add_routes(
                [
                    web.get("/text_chat", self.handle_text_chat_page),
                    web.post("/text", self.handle_text_api),
                    web.options("/text", self.handle_text_api),
                ]
            )

            # 运行服务
            runner = web.AppRunner(app)
            await runner.setup()
            site = web.TCPSite(runner, host, port)
            await site.start()

            # 保持服务运行
            while True:
                await asyncio.sleep(3600)  # 每隔 1 小时检查一次
