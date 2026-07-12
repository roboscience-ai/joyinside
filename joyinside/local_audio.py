"""本机麦克风录音与扬声器/耳机播放。"""

from __future__ import annotations

import queue
import threading
import time
from dataclasses import dataclass

import numpy as np
import sounddevice as sd

from config import CHANNELS, SAMPLE_RATE


@dataclass(frozen=True)
class RecordResult:
    pcm: bytes
    duration_s: float


def list_audio_devices() -> None:
    """打印可用音频设备，便于选择耳机麦克风与输出。"""
    print(sd.query_devices())


def _rms(frame: np.ndarray) -> float:
    if frame.size == 0:
        return 0.0
    samples = frame.astype(np.float32) / 32768.0
    return float(np.sqrt(np.mean(samples * samples)))


def record_until_silence(
    *,
    sample_rate: int = SAMPLE_RATE,
    channels: int = CHANNELS,
    device: int | str | None = None,
    frame_ms: int = 30,
    silence_threshold: float = 0.015,
    silence_duration_s: float = 1.2,
    max_duration_s: float = 20.0,
    min_duration_s: float = 0.8,
    pre_roll_ms: int = 200,
) -> RecordResult:
    """
    从麦克风录音，检测到说话后，静音一段时间自动结束。

    返回 16bit 单声道 PCM bytes。
    """
    frame_samples = int(sample_rate * frame_ms / 1000)
    pre_roll_frames = max(1, int(pre_roll_ms / frame_ms))
    audio_q: queue.Queue[np.ndarray] = queue.Queue()
    pre_buffer: list[np.ndarray] = []
    collected: list[np.ndarray] = []

    speech_started = False
    silent_frames = 0
    silence_limit = max(1, int(silence_duration_s * 1000 / frame_ms))
    max_frames = max(1, int(max_duration_s * 1000 / frame_ms))
    min_frames = max(1, int(min_duration_s * 1000 / frame_ms))
    frame_count = 0
    done = threading.Event()

    def callback(indata, _frames, _time_info, status) -> None:
        if status:
            print(f"[音频] {status}")
        mono = indata[:, 0].copy() if indata.ndim > 1 else indata.copy()
        audio_q.put(mono)

    stream = sd.InputStream(
        samplerate=sample_rate,
        channels=channels,
        dtype="int16",
        device=device,
        blocksize=frame_samples,
        callback=callback,
    )

    with stream:
        while not done.is_set():
            try:
                frame = audio_q.get(timeout=0.5)
            except queue.Empty:
                continue

            frame_count += 1
            level = _rms(frame)

            if not speech_started:
                pre_buffer.append(frame)
                if len(pre_buffer) > pre_roll_frames:
                    pre_buffer.pop(0)
                if level >= silence_threshold:
                    speech_started = True
                    collected.extend(pre_buffer)
                    pre_buffer.clear()
                    silent_frames = 0
                if frame_count >= max_frames:
                    done.set()
                continue

            collected.append(frame)
            if level < silence_threshold:
                silent_frames += 1
            else:
                silent_frames = 0

            if (
                frame_count >= min_frames
                and silent_frames >= silence_limit
            ) or frame_count >= max_frames:
                done.set()

    if not collected:
        return RecordResult(pcm=b"", duration_s=0.0)

    audio = np.concatenate(collected)
    duration_s = len(audio) / sample_rate
    return RecordResult(pcm=audio.tobytes(), duration_s=duration_s)


def play_pcm(
    pcm_data: bytes,
    *,
    sample_rate: int = SAMPLE_RATE,
    device: int | str | None = None,
) -> None:
    """播放 16bit 单声道 PCM 到默认或指定输出设备（耳机）。"""
    if not pcm_data:
        return
    audio = np.frombuffer(pcm_data, dtype=np.int16)
    sd.play(audio, samplerate=sample_rate, device=device)
    sd.wait()


class StreamingPcmPlayer:
    """边收 TTS 边播放，降低首包延迟。"""

    def __init__(
        self,
        *,
        sample_rate: int = SAMPLE_RATE,
        device: int | str | None = None,
        block_ms: int = 60,
    ) -> None:
        self.sample_rate = sample_rate
        self.device = device
        self.block_samples = max(1, int(sample_rate * block_ms / 1000))
        self._q: queue.Queue[bytes | None] = queue.Queue()
        self._thread: threading.Thread | None = None
        self._stream: sd.OutputStream | None = None

    def start(self) -> None:
        if self._thread and self._thread.is_alive():
            return
        self._thread = threading.Thread(target=self._run, daemon=True)
        self._thread.start()

    def ensure_started(self) -> None:
        self.start()

    def feed(self, chunk: bytes) -> None:
        if chunk:
            self._q.put(chunk)

    def finish(self) -> None:
        self._q.put(None)
        if self._thread:
            self._thread.join()

    def _run(self) -> None:
        pending = np.array([], dtype=np.int16)
        with sd.OutputStream(
            samplerate=self.sample_rate,
            channels=CHANNELS,
            dtype="int16",
            device=self.device,
        ) as stream:
            while True:
                item = self._q.get()
                if item is None:
                    if pending.size:
                        stream.write(pending.reshape(-1, 1))
                    break
                samples = np.frombuffer(item, dtype=np.int16)
                pending = np.concatenate([pending, samples])
                while pending.size >= self.block_samples:
                    block = pending[: self.block_samples]
                    pending = pending[self.block_samples :]
                    stream.write(block.reshape(-1, 1))
                time.sleep(0)
