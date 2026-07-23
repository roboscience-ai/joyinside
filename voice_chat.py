"""
语音对话：对齐官方 RK Linux demo 的自由模式。

官方方式（默认）:
  麦克风持续上行 → 云端 VAD 切句 → ASR → 自动触发智能体 → TTS
  不传 needManualCall，不发 CLIENT_AUDIO_FINISH，不手动 send_text

可选 --manual: 旧的端侧 VAD / Enter 分段模式
"""

from __future__ import annotations

import argparse
import logging
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

from config import BYTES_PER_FRAME, FRAME_MS, SAMPLE_RATE, JoyInsideConfig
from joyinside import JoyInsideAuth
from joyinside.brain import ConversationBrain
from joyinside.local_audio import (
    StreamingPcmPlayer,
    list_audio_devices,
    record_manual_turn,
)
from joyinside.speech import JoyInsideSpeech

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
logger = logging.getLogger(__name__)

EXIT_WORDS = ("退出", "再见", "拜拜", "结束对话")
_player: StreamingPcmPlayer | None = None


def parse_device(value: str | None) -> int | str | None:
    if value is None:
        return None
    try:
        return int(value)
    except ValueError:
        return value


def print_audio_devices(input_device: int | str | None, output_device: int | str | None) -> None:
    try:
        if input_device is None:
            in_idx = sd.default.device[0]
            print(f"麦克风: [{in_idx}] {sd.query_devices(in_idx)['name']}", flush=True)
        else:
            print(f"麦克风: 设备 {input_device}", flush=True)
        if output_device is None:
            out_idx = sd.default.device[1]
            print(f"播放设备: [{out_idx}] {sd.query_devices(out_idx)['name']}", flush=True)
        else:
            print(f"播放设备: 设备 {output_device}", flush=True)
    except Exception as exc:
        print(f"无法查询音频设备: {exc}", flush=True)


def _is_mp3(data: bytes) -> bool:
    return bool(
        data
        and (
            data[:3] == b"ID3"
            or data[:2] in (b"\xff\xfb", b"\xff\xf3", b"\xff\xf2")
        )
    )


def _rms_pcm(pcm: bytes) -> float:
    n = len(pcm) // 2
    if not n:
        return 0.0
    samples = struct.unpack(f"<{n}h", pcm)
    return (sum(v * v for v in samples) / n) ** 0.5 / 32768.0


def _prepare_player(output_device: int | str | None) -> StreamingPcmPlayer:
    global _player
    if _player is None:
        _player = StreamingPcmPlayer(device=output_device)
    return _player


def run_free_agent(
    *,
    input_device: int | str | None,
    output_device: int | str | None,
) -> None:
    """官方自由模式：持续上传，云端 VAD + ASR + 智能体。"""
    print("=" * 50, flush=True)
    print("语音对话（官方自由模式：云端 VAD）", flush=True)
    print("直接说话即可；说「退出」结束，Ctrl+C 退出", flush=True)
    print("=" * 50, flush=True)

    cfg = JoyInsideConfig.from_env()
    auth = JoyInsideAuth(cfg.access_key, cfg.secret_key)
    session_id = str(uuid.uuid4())
    connection_lost = threading.Event()
    stop = threading.Event()
    tts_suppressed = threading.Event()
    last_user = {"text": ""}

    def on_error(_err: Exception) -> None:
        connection_lost.set()

    speech = JoyInsideSpeech(
        bot_id=cfg.bot_id,
        get_token=lambda: auth.get_token(bot_id=cfg.bot_id),
        manual_mode=False,
        use_agent=True,
        auto_interrupt_agent=False,
        on_error=on_error,
    )

    print("正在连接…", flush=True)
    speech.connect(session_id=session_id)
    speech.ensure_pcm_output()
    print(f"已连接，会话 {session_id[:8]}…", flush=True)
    print_audio_devices(input_device, output_device)

    player = _prepare_player(output_device)
    player.start()

    def on_partial(text: str) -> None:
        if text:
            print(f"\r  识别中: {text}", end="", flush=True)

    def on_final(text: str) -> None:
        user = text.strip()
        last_user["text"] = user
        tts_suppressed.clear()
        print(flush=True)
        if user:
            print(f"你: {user}", flush=True)
            if any(w in user for w in EXIT_WORDS):
                print("机器人: 好的，再见！", flush=True)
                stop.set()

    def on_agent_start(text: str, _meta: dict) -> None:
        print(f"  智能体处理中: {text}", flush=True)

    def on_agent(chunk: str, _meta: dict) -> None:
        if chunk:
            print(f"\r  机器人: {chunk}", end="", flush=True)

    def on_tts(data: bytes, _meta: dict) -> None:
        if tts_suppressed.is_set():
            return
        if data and not _is_mp3(data):
            if not player.is_playing:
                player.start()
            player.feed(data)

    def on_interrupted() -> None:
        tts_suppressed.set()
        player.clear()
        print("\n  (已打断)", flush=True)

    def on_complete() -> None:
        print(flush=True)
        player.finish()
        player.start()

    speech.on_asr_partial = on_partial
    speech.on_asr_final = on_final
    speech.on_agent_start = on_agent_start
    speech.on_agent = on_agent
    speech.on_tts_audio = on_tts
    speech.on_agent_interrupted = on_interrupted
    speech.on_round_complete = on_complete
    speech.begin_asr()

    frame_samples = int(SAMPLE_RATE * FRAME_MS / 1000)
    audio_q: queue.Queue[bytes] = queue.Queue(maxsize=50)

    def callback(indata, _f, _t, status) -> None:
        if status:
            print(f"\n[音频] {status}", flush=True)
        mono = indata[:, 0] if indata.ndim > 1 else indata
        try:
            audio_q.put_nowait(mono.copy().tobytes())
        except queue.Full:
            pass

    stream = sd.InputStream(
        samplerate=SAMPLE_RATE,
        channels=1,
        dtype="int16",
        device=input_device,
        blocksize=frame_samples,
        callback=callback,
    )

    def reconnect() -> bool:
        nonlocal session_id
        try:
            speech.close()
            time.sleep(0.5)
            connection_lost.clear()
            session_id = str(uuid.uuid4())
            speech.connect(session_id=session_id)
            speech.ensure_pcm_output()
            speech.begin_asr()
            print(f"已重连，会话 {session_id[:8]}…", flush=True)
            return True
        except Exception as exc:
            print(f"重连失败: {exc}", flush=True)
            return False

    print("\n持续监听中…", flush=True)
    frames = 0
    try:
        with stream:
            while not stop.is_set():
                if connection_lost.is_set() or not speech.wait_connected(0):
                    if not reconnect():
                        break
                try:
                    pcm = audio_q.get(timeout=0.3)
                except queue.Empty:
                    continue
                frames += 1
                if frames % 25 == 0:
                    lv = _rms_pcm(pcm)
                    bar = "█" * min(20, int(lv * 400))
                    print(f"\r  听… {lv:.3f} {bar:<20}", end="", flush=True)
                speech.stream_asr_chunk(pcm, is_last=False)
    except KeyboardInterrupt:
        print("\n已退出", flush=True)
    finally:
        stop.set()
        speech.close()
        if _player is not None:
            _player.clear()


def agent_turn_manual(
    speech: JoyInsideSpeech,
    *,
    input_device: int | str | None,
    output_device: int | str | None,
    silence_threshold: float,
    timeout: float = 90.0,
) -> tuple[str, str]:
    """手动模式一轮：端侧 VAD + CLIENT_AUDIO_FINISH，云端自动触发智能体。"""
    user_text = ""
    agent_parts: list[str] = []
    asr_ready = threading.Event()
    round_done = threading.Event()
    player = _prepare_player(output_device)

    if player.is_playing:
        speech.interrupt()
        player.clear()
    player.start()

    def on_partial(text: str) -> None:
        print(f"\r  识别中: {text}", end="", flush=True)

    def on_final(text: str) -> None:
        nonlocal user_text
        user_text = text.strip()
        asr_ready.set()
        if user_text:
            print(f"\n  识别完成: {user_text}", flush=True)

    def on_agent(text: str, _meta: dict) -> None:
        agent_parts.append(text)

    def on_tts(data: bytes, _meta: dict) -> None:
        if data and not _is_mp3(data):
            player.feed(data)

    def on_interrupted() -> None:
        player.clear()

    prev = (
        speech.on_asr_partial,
        speech.on_asr_final,
        speech.on_agent,
        speech.on_tts_audio,
        speech.on_agent_interrupted,
        speech.on_round_complete,
    )
    speech.on_asr_partial = on_partial
    speech.on_asr_final = on_final
    speech.on_agent = on_agent
    speech.on_tts_audio = on_tts
    speech.on_agent_interrupted = on_interrupted
    speech.on_round_complete = round_done.set
    speech.begin_asr()

    def send_frame(pcm: bytes, is_last: bool) -> None:
        speech.stream_asr_chunk(pcm, is_last=is_last)
        if is_last:
            speech.finish_asr()

    try:
        recorded = record_manual_turn(
            send_frame,
            device=input_device,
            silence_threshold=silence_threshold,
            push_to_talk=True,
        )
        if len(recorded.pcm) < BYTES_PER_FRAME:
            return "", ""
        if not asr_ready.wait(8.0):
            raise TimeoutError("ASR 超时")
        print("  已提交音频，等待智能体…", flush=True)
        if not round_done.wait(timeout):
            raise TimeoutError("智能体回复超时")
        player.finish()
        return user_text, "".join(agent_parts).strip()
    finally:
        (
            speech.on_asr_partial,
            speech.on_asr_final,
            speech.on_agent,
            speech.on_tts_audio,
            speech.on_agent_interrupted,
            speech.on_round_complete,
        ) = prev


def local_turn(
    speech: JoyInsideSpeech,
    brain: ConversationBrain,
    *,
    input_device: int | str | None,
    output_device: int | str | None,
    silence_threshold: float,
) -> tuple[str, str]:
    """本地大脑：ASR → 本地回复 → 纯 TTS。"""
    user_text = ""
    asr_done = threading.Event()

    def on_final(text: str) -> None:
        nonlocal user_text
        user_text = text.strip()
        asr_done.set()

    prev_partial, prev_final = speech.on_asr_partial, speech.on_asr_final
    speech.on_asr_partial = lambda t: print(f"\r  识别中: {t}", end="", flush=True) if t else None
    speech.on_asr_final = on_final
    speech.begin_asr()

    def send_frame(pcm: bytes, is_last: bool) -> None:
        speech.stream_asr_chunk(pcm, is_last=is_last)
        if is_last:
            speech.finish_asr()

    try:
        recorded = record_manual_turn(
            send_frame,
            device=input_device,
            silence_threshold=silence_threshold,
            push_to_talk=True,
        )
        print(flush=True)
        if len(recorded.pcm) < BYTES_PER_FRAME:
            return "", ""
        if not asr_done.wait(30.0):
            raise TimeoutError("ASR 超时")

        reply = brain.reply(user_text)
        player = _prepare_player(output_device)
        player.start()

        def on_tts(data: bytes, _meta: dict) -> None:
            if data and not _is_mp3(data):
                player.feed(data)

        done = threading.Event()
        prev_tts, prev_done = speech.on_tts_audio, speech.on_round_complete
        speech.on_tts_audio = on_tts
        speech.on_round_complete = done.set
        try:
            speech.speak(reply)
            if not done.wait(60.0):
                raise TimeoutError("TTS 超时")
            player.finish()
        finally:
            speech.on_tts_audio, speech.on_round_complete = prev_tts, prev_done

        return user_text, reply
    finally:
        speech.on_asr_partial, speech.on_asr_final = prev_partial, prev_final


def run_manual_chat(
    *,
    input_device: int | str | None,
    output_device: int | str | None,
    silence_threshold: float,
    use_agent: bool,
) -> None:
    mode = "JoyInside 智能体" if use_agent else "本地大脑"
    print("=" * 50, flush=True)
    print(f"语音对话（{mode}，手动模式）", flush=True)
    print("按 Enter 说话，停顿约 1 秒自动结束", flush=True)
    print("Ctrl+C 退出", flush=True)
    print("=" * 50, flush=True)

    cfg = JoyInsideConfig.from_env()
    auth = JoyInsideAuth(cfg.access_key, cfg.secret_key)
    session_id = str(uuid.uuid4())
    connection_lost = threading.Event()

    speech = JoyInsideSpeech(
        bot_id=cfg.bot_id,
        get_token=lambda: auth.get_token(bot_id=cfg.bot_id),
        manual_mode=True,
        use_agent=use_agent,
        auto_interrupt_agent=not use_agent,
        on_error=lambda _e: connection_lost.set(),
    )

    print("正在连接…", flush=True)
    speech.connect(session_id=session_id)
    speech.ensure_pcm_output()
    brain = ConversationBrain() if not use_agent else None
    print(f"已连接，会话 {session_id[:8]}…", flush=True)
    print_audio_devices(input_device, output_device)

    def reconnect() -> bool:
        nonlocal session_id
        try:
            speech.close()
            time.sleep(0.5)
            connection_lost.clear()
            session_id = str(uuid.uuid4())
            speech.connect(session_id=session_id)
            speech.ensure_pcm_output()
            print(f"已重连，会话 {session_id[:8]}…", flush=True)
            return True
        except Exception as exc:
            print(f"重连失败: {exc}")
            return False

    try:
        while True:
            if connection_lost.is_set() or not speech.wait_connected(0):
                if not reconnect():
                    break
            try:
                input("\n[Enter 说话] ")
            except EOFError:
                break

            print("正在听…")
            try:
                if use_agent:
                    user_text, reply = agent_turn_manual(
                        speech,
                        input_device=input_device,
                        output_device=output_device,
                        silence_threshold=silence_threshold,
                    )
                else:
                    user_text, reply = local_turn(
                        speech,
                        brain,
                        input_device=input_device,
                        output_device=output_device,
                        silence_threshold=silence_threshold,
                    )
            except TimeoutError as exc:
                print(f"{exc}，请重试")
                continue
            except Exception as exc:
                print(f"失败: {exc}")
                if connection_lost.is_set():
                    reconnect()
                continue

            if not user_text:
                print("未识别到内容，请重试")
                continue

            print(f"你: {user_text}")
            if any(w in user_text for w in EXIT_WORDS):
                print("机器人: 好的，再见！")
                break
            print(f"机器人: {reply or '（已播放语音）'}")
    except KeyboardInterrupt:
        print("\n已退出")
    finally:
        speech.close()


def main() -> None:
    parser = argparse.ArgumentParser(description="JoyInside 语音对话")
    parser.add_argument("--list-devices", action="store_true")
    parser.add_argument("--input", dest="input_device")
    parser.add_argument("--output", dest="output_device")
    parser.add_argument("--silence-threshold", type=float, default=0.015)
    parser.add_argument("--local", action="store_true", help="本地大脑模式（需 --manual）")
    parser.add_argument(
        "--manual",
        action="store_true",
        help="手动模式（Enter + 端侧 VAD）；默认使用官方云端 VAD 自由模式",
    )
    args = parser.parse_args()

    if args.list_devices:
        list_audio_devices()
        return

    input_device = parse_device(args.input_device)
    output_device = parse_device(args.output_device)

    if args.local and not args.manual:
        print("本地大脑请加 --manual", flush=True)
        sys.exit(1)

    if args.manual or args.local:
        run_manual_chat(
            input_device=input_device,
            output_device=output_device,
            silence_threshold=args.silence_threshold,
            use_agent=not args.local,
        )
    else:
        run_free_agent(
            input_device=input_device,
            output_device=output_device,
        )


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        print("\n已退出")
    except Exception as exc:
        print(f"\n启动失败: {exc}", flush=True)
        sys.exit(1)
