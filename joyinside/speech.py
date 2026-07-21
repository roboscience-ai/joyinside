"""
JoyInside 语音 WebSocket 客户端（对齐官方协议）。

手动模式 (needManualCall=true):
  流式上传 AUDIO → CLIENT_AUDIO_FINISH → 等待 ASR/Agent/TTS/COMPLETE

官方一轮下行事件:
  ASR → CALL_AGENT_START_EVENT → AGENT → TTS → TTS_COMPLETE → COMPLETE
"""

from __future__ import annotations

import base64
import json
import logging
import threading
import time
import uuid
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any

import websocket

from config import WS_VOICE_CHAT
from joyinside.audio import frame_duration_seconds

logger = logging.getLogger(__name__)

OnAsrFinal = Callable[[str], None]
OnAsrPartial = Callable[[str], None]
OnAgentStart = Callable[[str, dict[str, Any]], None]
OnAgent = Callable[[str, dict[str, Any]], None]
OnTtsAudio = Callable[[bytes, dict[str, Any]], None]
OnRoundComplete = Callable[[], None]
OnAgentInterrupted = Callable[[], None]
OnError = Callable[[Exception], None]


@dataclass
class JoyInsideSpeech:
    bot_id: str
    get_token: Callable[[], str]
    manual_mode: bool = True
    ping_interval: float = 30.0
    uid: str = ""
    on_asr_final: OnAsrFinal | None = None
    on_asr_partial: OnAsrPartial | None = None
    on_agent_start: OnAgentStart | None = None
    on_agent: OnAgent | None = None
    on_tts_audio: OnTtsAudio | None = None
    on_round_complete: OnRoundComplete | None = None
    on_agent_interrupted: OnAgentInterrupted | None = None
    on_error: OnError | None = None
    auto_interrupt_agent: bool = True
    use_agent: bool = False

    _ws: websocket.WebSocketApp | None = field(default=None, init=False, repr=False)
    _thread: threading.Thread | None = field(default=None, init=False, repr=False)
    _connected: threading.Event = field(default_factory=threading.Event, init=False)
    _config_ready: threading.Event = field(default_factory=threading.Event, init=False)
    _lock: threading.Lock = field(default_factory=threading.Lock, init=False)
    _asr_mode: bool = field(default=False, init=False)
    _asr_frame_index: int = field(default=0, init=False)
    _audio_configured: bool = field(default=False, init=False)
    _stop_ping: threading.Event = field(default_factory=threading.Event, init=False)

    def connect(
        self,
        timeout: float = 15.0,
        *,
        session_id: str | None = None,
        request_id: str | None = None,
    ) -> None:
        if self._thread and self._thread.is_alive():
            return

        self._connected.clear()
        self._config_ready.clear()
        self._stop_ping.clear()
        self._audio_configured = False

        session_id = session_id or str(uuid.uuid4())
        request_id = request_id or str(uuid.uuid4())
        params = [
            f"botId={self.bot_id}",
            f"sessionId={session_id}",
            f"requestId={request_id}",
        ]
        if self.manual_mode:
            params.append("needManualCall=true")

        url = f"{WS_VOICE_CHAT}?{'&'.join(params)}"
        token = self.get_token()

        self._ws = websocket.WebSocketApp(
            url,
            header=[f"Authorization: Bearer {token}"],
            on_open=self._on_open,
            on_message=self._on_message,
            on_error=self._on_ws_error,
            on_close=self._on_close,
        )
        self._thread = threading.Thread(
            target=self._ws.run_forever,
            kwargs={"ping_interval": 0},
            daemon=True,
            name="joyinside-ws",
        )
        self._thread.start()

        if not self._connected.wait(timeout):
            raise TimeoutError("WebSocket 连接超时")

    def close(self) -> None:
        self._stop_ping.set()
        if self._ws:
            self._ws.close()
        if self._thread and self._thread.is_alive():
            self._thread.join(timeout=5.0)
        self._thread = None
        self._ws = None
        self._connected.clear()
        self._config_ready.clear()
        self._audio_configured = False

    def wait_connected(self, timeout: float = 15.0) -> bool:
        return self._connected.wait(timeout)

    def ensure_pcm_output(self, sample_rate: str = "16000", timeout: float = 10.0) -> None:
        """连接后仅发送一次 CLIENT_VOICE_CHAT_UPDATE（官方要求）。"""
        if self._audio_configured:
            return
        self.update_audio_config(
            output_codec="pcm",
            output_sample_rate=sample_rate,
            output_frame_size_ms="60",
            wait=True,
            timeout=timeout,
        )
        self._audio_configured = True

    def update_audio_config(
        self,
        *,
        input_codec: str = "pcm",
        input_sample_rate: str = "16000",
        output_codec: str = "pcm",
        output_sample_rate: str = "16000",
        output_frame_size_ms: str | None = None,
        binary: bool = False,
        wait: bool = True,
        timeout: float = 10.0,
    ) -> None:
        event_data: dict[str, Any] = {
            "audio": {
                "binary": binary,
                "input": {"codec": input_codec, "sampleRate": input_sample_rate},
                "output": {"codec": output_codec, "sampleRate": output_sample_rate},
            }
        }
        if output_frame_size_ms:
            event_data["audio"]["output"]["frameSizeMs"] = output_frame_size_ms

        self._config_ready.clear()
        self._send_event("CLIENT_VOICE_CHAT_UPDATE", event_data)

        if wait and not self._config_ready.wait(timeout):
            logger.warning("等待 SERVER_VOICE_CHAT_UPDATED 超时")

    def speak(self, text: str) -> None:
        self._asr_mode = False
        self._send_event("CLIENT_INPUT_TEXT_TO_SPEECH", {"text": text})

    def send_text(self, text: str) -> None:
        """发送文本触发智能体（官方 contentType=TEXT，不经过音频链路）。"""
        self._asr_mode = False
        payload: dict[str, Any] = {
            "mid": str(uuid.uuid4()),
            "contentType": "TEXT",
            "content": {"input": text},
        }
        if self.uid:
            payload["uid"] = self.uid
        self._send_json(payload)

    def begin_asr(self) -> None:
        self._asr_mode = True
        self._asr_frame_index = 0

    def stream_asr_chunk(
        self,
        chunk: bytes,
        *,
        is_last: bool = False,
        pace: bool = False,
    ) -> None:
        """上传一帧音频。pace=True 时按帧时长 sleep（仅用于回放 PCM 文件）。"""
        from config import BYTES_PER_FRAME

        if not chunk and not is_last:
            return

        frame_size = BYTES_PER_FRAME
        if chunk:
            index = self._asr_frame_index
            is_partial_last = is_last and len(chunk) < frame_size
            send_index = ~index if is_partial_last else index
            self._send_audio(chunk, index=send_index)
            self._asr_frame_index += 1
            if pace and not is_last:
                time.sleep(frame_duration_seconds(len(chunk)))

    def finish_asr(self) -> None:
        if self.manual_mode:
            self._send_event("CLIENT_AUDIO_FINISH")

    def recognize_pcm(self, pcm_chunks: list[bytes], *, frame_bytes: int | None = None) -> None:
        from config import BYTES_PER_FRAME

        frame_size = frame_bytes or BYTES_PER_FRAME
        self._asr_mode = True

        for index, chunk in enumerate(pcm_chunks):
            is_partial_last = len(chunk) < frame_size
            send_index = ~index if is_partial_last else index
            self._send_audio(chunk, index=send_index)
            time.sleep(frame_duration_seconds(len(chunk)))

        if self.manual_mode:
            self._send_event("CLIENT_AUDIO_FINISH")

    def recognize_pcm_data(self, pcm_data: bytes, frame_bytes: int) -> None:
        from joyinside.audio import chunk_pcm

        self.recognize_pcm(chunk_pcm(pcm_data, frame_bytes))

    def interrupt(self) -> None:
        self._send_event("CLIENT_INTERRUPT")

    def _send_audio(self, chunk: bytes, *, index: int) -> None:
        payload = {
            "mid": str(uuid.uuid4()),
            "contentType": "AUDIO",
            "content": {
                "audioBase64": base64.b64encode(chunk).decode("ascii"),
                "index": index,
            },
        }
        if self.uid:
            payload["uid"] = self.uid
        self._send_json(payload)

    def _send_event(self, event_type: str, event_data: dict[str, Any] | None = None) -> None:
        content: dict[str, Any] = {"eventType": event_type}
        if event_data is not None:
            content["eventData"] = event_data
        payload = {"mid": str(uuid.uuid4()), "contentType": "EVENT", "content": content}
        if self.uid:
            payload["uid"] = self.uid
        self._send_json(payload)

    def _send_json(self, payload: dict[str, Any]) -> None:
        with self._lock:
            if not self._ws:
                raise RuntimeError("WebSocket 未连接")
            self._ws.send(json.dumps(payload, ensure_ascii=False))

    def _on_open(self, ws: websocket.WebSocketApp) -> None:
        logger.info("WebSocket 已连接")
        self._connected.set()
        threading.Thread(target=self._ping_loop, daemon=True, name="joyinside-ping").start()

    def _ping_loop(self) -> None:
        while not self._stop_ping.is_set() and self._connected.is_set():
            try:
                payload = {"mid": str(uuid.uuid4()), "contentType": "PING"}
                if self.uid:
                    payload["uid"] = self.uid
                self._send_json(payload)
            except Exception as exc:
                logger.debug("心跳发送失败: %s", exc)
            self._stop_ping.wait(self.ping_interval)

    def _on_message(self, ws: websocket.WebSocketApp, message: str) -> None:
        try:
            msg = json.loads(message)
        except json.JSONDecodeError:
            logger.warning("无法解析消息: %s", message[:200])
            return

        content_type = msg.get("contentType")
        content = msg.get("content") or {}

        if content_type == "ASR":
            text = content.get("text", "")
            text_type = content.get("textType", "")
            logger.info("ASR [%s]: %s", text_type, text)
            if text_type == "IS_FINAL":
                if self.on_asr_final:
                    self.on_asr_final(text)
            elif text and self.on_asr_partial:
                self.on_asr_partial(text)
            if (
                text_type == "IS_FINAL"
                and self._asr_mode
                and self.auto_interrupt_agent
                and not self.use_agent
            ):
                self.interrupt()

        elif content_type == "TTS" and self.on_tts_audio:
            audio_b64 = content.get("audioBase64", "")
            if audio_b64:
                self.on_tts_audio(base64.b64decode(audio_b64), content)

        elif content_type == "EVENT":
            event_type = content.get("eventType", "")
            if event_type == "SERVER_VOICE_CHAT_UPDATED":
                self._config_ready.set()
                logger.info("音频配置已更新")
            elif event_type == "CALL_AGENT_START_EVENT":
                input_text = (content.get("eventData") or {}).get("input", "")
                logger.info("智能体开始: %s", input_text)
                if self.on_agent_start:
                    self.on_agent_start(input_text, content)
            elif event_type == "TTS_COMPLETE":
                logger.debug("TTS 完成")
                if self.on_round_complete and not self.use_agent:
                    self.on_round_complete()
            elif event_type == "COMPLETE":
                logger.info("当轮对话完成")
                if self.on_round_complete:
                    self.on_round_complete()
            elif event_type == "EMPTY_CONTENT":
                logger.info("未识别到有效内容")
                if self.on_round_complete:
                    self.on_round_complete()
            elif event_type == "CALL_AGENT_INTERRUPTED":
                logger.info("智能体被打断")
                if self.on_agent_interrupted:
                    self.on_agent_interrupted()
            elif event_type == "REPEAT_CLIENT_SESSION":
                err = RuntimeError("同一 botId 存在重复 WebSocket 连接")
                if self.on_error:
                    self.on_error(err)
                logger.error("%s", err)

        elif content_type == "AGENT":
            text = content.get("content", "") or content.get("text", "")
            if text:
                logger.debug("智能体: %s", text[:120])
                if self.on_agent:
                    self.on_agent(text, content)

        code = msg.get("code")
        if code not in (None, 200, "200"):
            logger.warning("服务端返回异常: %s", msg)

    def _on_ws_error(self, ws: websocket.WebSocketApp, error: Exception) -> None:
        logger.error("WebSocket 错误: %s", error)
        if self.on_error:
            self.on_error(error)

    def _on_close(
        self,
        ws: websocket.WebSocketApp,
        close_status_code: int | None,
        close_msg: str | None,
    ) -> None:
        logger.info("WebSocket 已关闭: %s %s", close_status_code, close_msg)
        self._connected.clear()
        self._stop_ping.set()


class SyncSpeechHelper:
    """回调模式下的同步阻塞调用。"""

    def __init__(self, speech: JoyInsideSpeech) -> None:
        self.speech = speech

    def speak_and_collect(self, text: str, timeout: float = 60.0) -> bytes:
        chunks: list[bytes] = []
        done = threading.Event()

        def on_audio(data: bytes, _meta: dict[str, Any]) -> None:
            chunks.append(data)

        def on_complete() -> None:
            done.set()

        prev_audio = self.speech.on_tts_audio
        prev_complete = self.speech.on_round_complete
        self.speech.on_tts_audio = on_audio
        self.speech.on_round_complete = on_complete

        try:
            self.speech.speak(text)
            if not done.wait(timeout):
                raise TimeoutError("TTS 超时")
            return b"".join(chunks)
        finally:
            self.speech.on_tts_audio = prev_audio
            self.speech.on_round_complete = prev_complete

    def recognize_and_wait(self, pcm_chunks: list[bytes], timeout: float = 30.0) -> str:
        result: dict[str, str] = {}
        done = threading.Event()

        def on_asr(text: str) -> None:
            result["text"] = text
            done.set()

        prev = self.speech.on_asr_final
        self.speech.on_asr_final = on_asr
        try:
            self.speech.recognize_pcm(pcm_chunks)
            if not done.wait(timeout):
                raise TimeoutError("ASR 超时")
            return result.get("text", "")
        finally:
            self.speech.on_asr_final = prev
