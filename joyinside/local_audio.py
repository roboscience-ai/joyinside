"""本机麦克风录音与扬声器/耳机播放。"""

from __future__ import annotations

import queue
import threading
from collections.abc import Callable
from dataclasses import dataclass

import numpy as np
import sounddevice as sd

from config import BYTES_PER_FRAME, CHANNELS, FRAME_MS, SAMPLE_RATE


@dataclass(frozen=True)
class RecordResult:
    pcm: bytes
    duration_s: float


def list_audio_devices() -> None:
    print(sd.query_devices())


def _rms(frame: np.ndarray) -> float:
    if frame.size == 0:
        return 0.0
    samples = frame.astype(np.float32) / 32768.0
    return float(np.sqrt(np.mean(samples * samples)))


def record_manual_turn(
    send_frame: Callable[[bytes, bool], None],
    *,
    sample_rate: int = SAMPLE_RATE,
    channels: int = CHANNELS,
    device: int | str | None = None,
    frame_ms: int = FRAME_MS,
    silence_threshold: float = 0.015,
    silence_duration_s: float = 1.0,
    max_duration_s: float = 20.0,
    min_duration_s: float = 0.5,
    push_to_talk: bool = False,
    on_level: Callable[[float], None] | None = None,
) -> RecordResult:
    """
    手动模式录音（官方：端侧 VAD 代替按键）。

    push_to_talk=True: 按 Enter 后立即开始上传（不再等音量阈值触发）。
    """
    frame_samples = int(sample_rate * frame_ms / 1000)
    frame_interval = frame_ms / 1000.0
    silence_limit = max(1, int(silence_duration_s / frame_interval))
    max_frames = max(1, int(max_duration_s / frame_interval))
    min_frames = max(1, int(min_duration_s / frame_interval))

    audio_q: queue.Queue[np.ndarray] = queue.Queue()
    collected: list[np.ndarray] = []
    speech_started = False
    silent_frames = 0
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
            if on_level:
                on_level(level)

            if not speech_started:
                if push_to_talk or level >= silence_threshold:
                    speech_started = True
                    silent_frames = 0
                else:
                    if frame_count >= max_frames:
                        done.set()
                    continue

            collected.append(frame)
            pcm = frame.tobytes()
            is_last = False

            if level < silence_threshold:
                silent_frames += 1
            else:
                silent_frames = 0

            if (frame_count >= min_frames and silent_frames >= silence_limit) or frame_count >= max_frames:
                is_last = True
                done.set()

            send_frame(pcm, is_last)

    if not collected:
        return RecordResult(pcm=b"", duration_s=0.0)

    audio = np.concatenate(collected)
    return RecordResult(pcm=audio.tobytes(), duration_s=len(audio) / sample_rate)


def play_pcm(
    pcm_data: bytes,
    *,
    sample_rate: int = SAMPLE_RATE,
    device: int | str | None = None,
) -> None:
    if not pcm_data:
        return
    audio = np.frombuffer(pcm_data, dtype=np.int16)
    sd.play(audio, samplerate=sample_rate, device=device)
    sd.wait()


class StreamingPcmPlayer:
    """边收 TTS 边播放。"""

    def __init__(
        self,
        *,
        sample_rate: int = SAMPLE_RATE,
        device: int | str | None = None,
        block_ms: int = 20,
    ) -> None:
        self.sample_rate = sample_rate
        self.device = device
        self.block_samples = max(1, int(sample_rate * block_ms / 1000))
        self._q: queue.Queue[bytes | None] = queue.Queue()
        self._thread: threading.Thread | None = None
        self._bytes_played = 0
        self._started = threading.Event()
        self._playing = False

    @property
    def is_playing(self) -> bool:
        return self._playing

    def start(self) -> None:
        if self._thread and self._thread.is_alive():
            return
        self._started.clear()
        self._playing = True
        self._thread = threading.Thread(target=self._run, daemon=True)
        self._thread.start()
        self._started.wait(timeout=2.0)

    def clear(self) -> None:
        """清空播放队列（官方 CALL_AGENT_INTERRUPTED 时调用）。"""
        while not self._q.empty():
            try:
                self._q.get_nowait()
            except queue.Empty:
                break

    def feed(self, chunk: bytes) -> None:
        if chunk:
            self._q.put(chunk)

    def finish(self) -> None:
        self._q.put(None)
        if self._thread:
            self._thread.join()
        self._playing = False

    def _run(self) -> None:
        pending = np.array([], dtype=np.int16)
        with sd.OutputStream(
            samplerate=self.sample_rate,
            channels=CHANNELS,
            dtype="int16",
            device=self.device,
        ) as stream:
            self._started.set()
            while True:
                item = self._q.get()
                if item is None:
                    if pending.size:
                        stream.write(pending.reshape(-1, 1))
                        self._bytes_played += pending.nbytes
                    break
                samples = np.frombuffer(item, dtype=np.int16)
                pending = np.concatenate([pending, samples])
                while pending.size >= self.block_samples:
                    block = pending[: self.block_samples]
                    pending = pending[self.block_samples :]
                    stream.write(block.reshape(-1, 1))
                    self._bytes_played += block.nbytes
