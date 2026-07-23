"""
ASR 测试。

默认：端侧 VAD（官方「端侧 VAD 代替按键」）
  自动检测说话 → 上传 → 静音 1s → CLIENT_AUDIO_FINISH → 打印结果 → 下一轮

  python asr_test.py
  python asr_test.py --input 4
  python asr_test.py --free      # 云端 VAD（自由模式）
  python asr_test.py --manual    # Enter 开始/结束
"""

from __future__ import annotations

import argparse
import queue
import struct
import sys
import threading
import time
import uuid
from pathlib import Path

import sounddevice as sd

ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT))

from config import BYTES_PER_FRAME, FRAME_MS, JoyInsideConfig, SAMPLE_RATE
from joyinside import JoyInsideAuth
from joyinside.audio import chunk_pcm, read_pcm_file
from joyinside.local_audio import record_manual_turn
from joyinside.speech import JoyInsideSpeech


def _rms_pcm(pcm: bytes) -> float:
    n = len(pcm) // 2
    if not n:
        return 0.0
    samples = struct.unpack(f"<{n}h", pcm)
    return (sum(v * v for v in samples) / n) ** 0.5 / 32768.0


def run_auto_vad(speech: JoyInsideSpeech, *, device: int | str | None, silence: float) -> None:
    """端侧 VAD 循环：检测到说话→上传→静音结束→ASR→下一轮。"""
    stop = threading.Event()

    print("端侧 VAD 持续监听，直接说话；停顿约 1 秒自动结束一轮", flush=True)
    print("Ctrl+C 退出", flush=True)

    try:
        while not stop.is_set():
            user_text = ""
            asr_done = threading.Event()

            def on_partial(text: str) -> None:
                if text:
                    print(f"\r    识别中: {text}", end="", flush=True)

            def on_final(text: str) -> None:
                nonlocal user_text
                user_text = text.strip()
                asr_done.set()
                print(flush=True)

            speech.on_asr_partial = on_partial
            speech.on_asr_final = on_final
            speech.begin_asr()

            def show_level(level: float) -> None:
                bar = "█" * min(20, int(level * 400))
                print(f"\r  听… 音量 {level:.3f} {bar:<20}", end="", flush=True)

            def send_frame(pcm: bytes, is_last: bool) -> None:
                speech.stream_asr_chunk(pcm, is_last=is_last)
                if is_last:
                    speech.finish_asr()

            print("\n  等待说话…", flush=True)
            recorded = record_manual_turn(
                send_frame,
                device=device,
                silence_threshold=silence,
                silence_duration_s=1.0,
                push_to_talk=False,
                on_level=show_level,
            )
            print(flush=True)

            if len(recorded.pcm) < BYTES_PER_FRAME:
                continue

            if not asr_done.wait(8.0):
                print("  (ASR 超时)", flush=True)
                continue

            speech.interrupt()
            if user_text:
                print(f"  ✓ {user_text}", flush=True)
            else:
                print("  (未识别到内容)", flush=True)

    except KeyboardInterrupt:
        print("\n已退出")


def run_free_mode(speech: JoyInsideSpeech, *, device: int | str | None) -> None:
    """云端 VAD 自由模式（持续上传，无轮次边界）。"""
    stop = threading.Event()
    frame_samples = int(SAMPLE_RATE * FRAME_MS / 1000)
    audio_q: queue.Queue[bytes] = queue.Queue()

    def on_partial(text: str) -> None:
        if text:
            print(f"\r  识别中: {text}", end="", flush=True)

    def on_final(text: str) -> None:
        if text.strip():
            print(f"\n  ✓ {text.strip()}", flush=True)

    def on_agent(_t: str, _m: dict) -> None:
        speech.interrupt()

    speech.on_asr_partial = on_partial
    speech.on_asr_final = on_final
    speech.on_agent = on_agent

    def callback(indata, _f, _t, status) -> None:
        if status:
            print(f"\n[音频] {status}", flush=True)
        mono = indata[:, 0] if indata.ndim > 1 else indata
        audio_q.put(mono.copy().tobytes())

    stream = sd.InputStream(
        samplerate=SAMPLE_RATE,
        channels=1,
        dtype="int16",
        device=device,
        blocksize=frame_samples,
        callback=callback,
    )

    print("云端 VAD 持续上传，Ctrl+C 退出", flush=True)
    frames = 0

    with stream:
        try:
            while not stop.is_set():
                try:
                    pcm = audio_q.get(timeout=0.3)
                except queue.Empty:
                    continue
                frames += 1
                lv = _rms_pcm(pcm)
                bar = "█" * min(20, int(lv * 400))
                print(f"\r  上传 {frames} 帧 | 音量 {lv:.3f} {bar:<20}", end="", flush=True)
                speech.stream_asr_chunk(pcm, is_last=False)
        except KeyboardInterrupt:
            print(flush=True)


def run_manual_enter(speech: JoyInsideSpeech, *, device: int | str | None, silence: float) -> None:
    speech.on_asr_partial = lambda t: print(f"\r  {t}", end="", flush=True) if t else None
    print("Enter 开始，再 Enter 结束；quit 退出")

    while True:
        cmd = input("\n[Enter 开始] ").strip()
        if cmd.lower() in ("q", "quit", "exit", "退出"):
            break

        result = {"text": ""}
        done = threading.Event()

        def on_final(t: str) -> None:
            result["text"] = t.strip()
            done.set()
            print(flush=True)

        speech.on_asr_final = on_final
        speech.begin_asr()

        stop = threading.Event()

        def wait_stop() -> None:
            input()
            stop.set()

        print("  说话中… 再 Enter 结束", flush=True)
        threading.Thread(target=wait_stop, daemon=True).start()

        frame_samples = int(SAMPLE_RATE * FRAME_MS / 1000)
        audio_q: queue.Queue[bytes] = queue.Queue()

        def cb(indata, _f, _t, _s) -> None:
            mono = indata[:, 0] if indata.ndim > 1 else indata
            audio_q.put(mono.copy().tobytes())

        with sd.InputStream(
            samplerate=SAMPLE_RATE,
            channels=1,
            dtype="int16",
            device=device,
            blocksize=frame_samples,
            callback=cb,
        ):
            while not stop.is_set():
                try:
                    pcm = audio_q.get(timeout=0.3)
                except queue.Empty:
                    continue
                speech.stream_asr_chunk(pcm, is_last=False, pace=True)

        speech.finish_asr()
        done.wait(8)
        speech.interrupt()
        print(f"  ✓ {result['text'] or '(空)'}")


def main() -> None:
    parser = argparse.ArgumentParser(description="JoyInside ASR 测试")
    parser.add_argument("--free", action="store_true", help="云端 VAD 自由模式")
    parser.add_argument("--manual", action="store_true", help="Enter 触发")
    parser.add_argument("--pcm", help="PCM 文件")
    parser.add_argument("--input", dest="device")
    parser.add_argument("--silence", type=float, default=0.008, help="静音阈值")
    args = parser.parse_args()

    device = int(args.device) if args.device else None

    cfg = JoyInsideConfig.from_env()
    auth = JoyInsideAuth(cfg.access_key, cfg.secret_key)

    use_free = args.free
    use_manual = args.manual or bool(args.pcm)

    speech = JoyInsideSpeech(
        bot_id=cfg.bot_id,
        get_token=lambda: auth.get_token(bot_id=cfg.bot_id),
        manual_mode=not use_free,
        use_agent=False,
        auto_interrupt_agent=use_manual,
    )

    modes = {
        "auto": "端侧 VAD（默认）",
        "free": "云端 VAD",
        "manual": "Enter 手动",
    }
    mode = "free" if use_free else ("manual" if use_manual else "auto")
    print("=" * 50)
    print(f"ASR — {modes[mode]}")
    print("=" * 50)

    speech.connect(session_id=str(uuid.uuid4()))

    if device is None:
        try:
            i = sd.default.device[0]
            print(f"麦克风: [{i}] {sd.query_devices(i)['name']}")
        except Exception:
            pass

    try:
        if args.pcm:
            pcm = read_pcm_file(Path(args.pcm))
            speech.begin_asr()
            for i, c in enumerate(chunk_pcm(pcm)):
                if c:
                    speech.stream_asr_chunk(c, is_last=(i == len(chunk_pcm(pcm)) - 1), pace=True)
            speech.finish_asr()
            time.sleep(3)
        elif use_free:
            run_free_mode(speech, device=device)
        elif use_manual:
            run_manual_enter(speech, device=device, silence=args.silence)
        else:
            run_auto_vad(speech, device=device, silence=args.silence)
    finally:
        speech.close()


if __name__ == "__main__":
    main()
