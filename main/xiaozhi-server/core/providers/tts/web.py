import json
import queue
import asyncio
import threading
import traceback
from config.logger import setup_logging
from core.providers.tts.base import TTSProviderBase
from core.providers.tts.dto.dto import SentenceType, ContentType

logger = setup_logging()
TAG = __name__


class WebTTS(TTSProviderBase):
    """
    Text-only TTS for web browser clients.

    Sends every LLM text chunk straight to the browser WebSocket in the
    text-priority thread — no audio generation, no audio-play thread,
    no second queue hop.  Each token appears in the browser as soon as
    it arrives from the LLM.
    """

    # ── stubs required by the abstract base ──────────────────────────────
    async def text_to_speak(self, text, output_file):
        return None

    def to_tts_stream(self, text, opus_handler=None):
        pass  # not used — we override the whole thread

    def generate_filename(self):
        return None

    # ── channel setup ─────────────────────────────────────────────────────
    async def open_audio_channels(self, conn):
        """Start only the text thread; no audio-play thread needed."""
        self.conn = conn
        self.tts_priority_thread = threading.Thread(
            target=self.tts_text_priority_thread, daemon=True
        )
        self.tts_priority_thread.start()

    # ── helpers ───────────────────────────────────────────────────────────
    def _sid(self):
        return getattr(self.conn, "session_id", "")

    def _send(self, payload: dict):
        """Synchronously push a JSON frame from the background thread."""
        try:
            future = asyncio.run_coroutine_threadsafe(
                self.conn.websocket.send(
                    json.dumps(payload, ensure_ascii=False)
                ),
                self.conn.loop,
            )
            future.result(timeout=5)
        except Exception as e:
            logger.bind(tag=TAG).debug(f"WebTTS _send failed: {e}")

    def _send_stop(self):
        self.conn.client_is_speaking = False
        self._send({"type": "tts", "state": "stop", "session_id": self._sid()})

    # ── main loop ─────────────────────────────────────────────────────────
    def tts_text_priority_thread(self):
        while not self.conn.stop_event.is_set():
            try:
                msg = self.tts_text_queue.get(timeout=1)

                if msg.sentence_type == SentenceType.FIRST:
                    self.conn.client_abort = False
                    # tts/start is already sent by send_stt_message;
                    # sending it again just re-arms the browser timeout.
                    self._send({"type": "tts", "state": "start",
                                "session_id": self._sid()})
                    continue

                if self.conn.client_abort:
                    if msg.sentence_type == SentenceType.LAST:
                        self._send_stop()
                    continue

                # Stream every text chunk immediately
                if msg.content_type == ContentType.TEXT:
                    text = msg.content_detail or ""
                    if text:  # send as-is; leading spaces are word separators
                        self._send({"type": "tts", "state": "sentence_start",
                                    "text": text, "session_id": self._sid()})

                if msg.sentence_type == SentenceType.LAST:
                    self._send_stop()

            except queue.Empty:
                continue
            except Exception as e:
                logger.bind(tag=TAG).error(
                    f"WebTTS thread error: {e}\n{traceback.format_exc()}"
                )
                try:
                    self._send_stop()
                except Exception:
                    pass
