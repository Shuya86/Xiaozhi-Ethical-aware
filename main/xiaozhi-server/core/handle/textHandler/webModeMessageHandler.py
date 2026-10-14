from typing import Dict, Any

from core.handle.textMessageHandler import TextMessageHandler
from core.handle.textMessageType import TextMessageType

TAG = __name__


class WebModeMessageHandler(TextMessageHandler):
    """
    Handles {"type": "web_mode", "mode": "text"|"voice"} from browser clients.

    text  → sets server.text_mode_active = True  (ESP32 mic paused)
    voice → sets server.text_mode_active = False (ESP32 mic active, default)
    """

    @property
    def message_type(self) -> TextMessageType:
        return TextMessageType.WEB_MODE

    async def handle(self, conn, msg_json: Dict[str, Any]) -> None:
        if not conn.is_web_connection or conn.server is None:
            return
        mode = msg_json.get("mode", "voice")
        if mode == "text" and not conn.server.hardware_status()["llm_ready"]:
            conn.server.text_mode_active = False
            await conn.server._send_hardware_status(conn.websocket)
            return
        conn.server.text_mode_active = (mode == "text")
        conn.logger.bind(tag=TAG).info(
            f"Input mode → {'text (ESP32 standby)' if mode == 'text' else 'voice (ESP32 active)'}"
        )
