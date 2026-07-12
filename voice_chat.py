"""
语音对话：默认使用 JoyInside 平台智能体（ASR → 智能体 → TTS）。

用法:
  python voice_chat.py                  # JoyInside 智能体（默认）
  python voice_chat.py --local          # 本地大脑 + 纯 TTS（旧模式）
  python voice_chat.py --list-devices
"""

from __future__ import annotations

import argparse
import logging
import sys
import threading
import time
import uuid
from pathlib import Path

import sounddevice as sd

ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT))

from config import BYTES_PER_FRAME, JoyInsideConfig
from joyinside import JoyInsideAuth
from joyinside.brain import ConversationBrain
from joyinside.local_audio import (
    StreamingPcmPlayer,
    list_audio_devices,
    stream_record_until_silence,
)
from joyinside.speech import JoyInsideSpeech, SyncSpeechHelper

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
logger = logging.getLogger(__name__)

EXIT_WORDS = ("退出", "再见", "拜拜", "结束对话")


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
            print("若听不到声音，请用 --list-devices 查看编号，并用 --output 指定耳机", flush=True)
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


def agent_turn(
    speech: JoyInsideSpeech,
    *,
    input_device: int | str | None,
    output_device: int | str | None,
    silence_threshold: float,
    timeout: float = 90.0,
) -> tuple[str, str]:
    """
    一轮 JoyInside 智能体对话：边录边传 → 平台 ASR+智能体+TTS → 边收边播。

    返回 (用户文本, 智能体回复文本)。
    """
    user_text = ""
    agent_chunks: list[str] = []
    asr_done = threading.Event()
    reply_done = threading.Event()
    player = StreamingPcmPlayer(device=output_device)
    player.start()
    first_audio_at = 0.0
    t0 = time.perf_counter()

    def on_partial(text: str) -> None:
        if text:
            print(f"\r  识别中: {text}", end="", flush=True)

    def on_final(text: str) -> None:
        nonlocal user_text
        user_text = text.strip()
        asr_done.set()

    def on_agent(text: str, _meta: dict) -> None:
        agent_chunks.append(text)

    def on_tts_audio(data: bytes, _meta: dict) -> None:
        nonlocal first_audio_at
        if not data or _is_mp3(data):
            return
        if first_audio_at == 0.0:
            first_audio_at = time.perf_counter()
            print(f"\n  智能体首包 {(first_audio_at - t0) * 1000:.0f}ms", flush=True)
        player.feed(data)

    def on_tts_complete() -> None:
        reply_done.set()

    prev = (
        speech.on_asr_partial,
        speech.on_asr_final,
        speech.on_agent,
        speech.on_tts_audio,
        speech.on_tts_complete,
    )
    speech.on_asr_partial = on_partial
    speech.on_asr_final = on_final
    speech.on_agent = on_agent
    speech.on_tts_audio = on_tts_audio
    speech.on_tts_complete = on_tts_complete
    speech.begin_asr()

    def on_chunk(chunk: bytes, is_last: bool) -> None:
        if chunk:
            speech.stream_asr_chunk(chunk, is_last=is_last)
        if is_last:
            speech.finish_asr()

    try:
        recorded = stream_record_until_silence(
            on_chunk,
            device=input_device,
            silence_threshold=silence_threshold,
        )
        if len(recorded.pcm) < BYTES_PER_FRAME:
            return "", ""

        if not reply_done.wait(timeout):
            raise TimeoutError("智能体回复超时")

        player.finish()
        agent_reply = "".join(agent_chunks).strip()
        return user_text, agent_reply
    finally:
        (
            speech.on_asr_partial,
            speech.on_asr_final,
            speech.on_agent,
            speech.on_tts_audio,
            speech.on_tts_complete,
        ) = prev


def local_turn_streaming(
    speech: JoyInsideSpeech,
    helper: SyncSpeechHelper,
    brain: ConversationBrain,
    *,
    input_device: int | str | None,
    output_device: int | str | None,
    silence_threshold: float,
) -> tuple[str, str]:
    """本地大脑模式（旧）。"""
    result: dict[str, str] = {}
    asr_done = threading.Event()

    def on_partial(text: str) -> None:
        if text:
            print(f"\r  识别中: {text}", end="", flush=True)

    def on_final(text: str) -> None:
        result["user"] = text
        asr_done.set()

    prev_partial, prev_final = speech.on_asr_partial, speech.on_asr_final
    speech.on_asr_partial, speech.on_asr_final = on_partial, on_final
    speech.begin_asr()

    def on_chunk(chunk: bytes, is_last: bool) -> None:
        if chunk:
            speech.stream_asr_chunk(chunk, is_last=is_last)
        if is_last:
            speech.finish_asr()

    try:
        recorded = stream_record_until_silence(
            on_chunk,
            device=input_device,
            silence_threshold=silence_threshold,
        )
        print(flush=True)
        if len(recorded.pcm) < BYTES_PER_FRAME:
            return "", ""
        if not asr_done.wait(30.0):
            raise TimeoutError("ASR 超时")
        user_text = result.get("user", "").strip()
        reply = brain.reply(user_text)

        speech.ensure_pcm_output()
        player = StreamingPcmPlayer(device=output_device)
        player.start()
        done = threading.Event()

        def on_audio(data: bytes, _meta: dict) -> None:
            if data and not _is_mp3(data):
                player.feed(data)

        def on_complete() -> None:
            done.set()

        prev_a, prev_c = speech.on_tts_audio, speech.on_tts_complete
        speech.on_tts_audio, speech.on_tts_complete = on_audio, on_complete
        try:
            speech.speak(reply)
            if not done.wait(60.0):
                raise TimeoutError("TTS 超时")
            player.finish()
        finally:
            speech.on_tts_audio, speech.on_tts_complete = prev_a, prev_c

        return user_text, reply
    finally:
        speech.on_asr_partial, speech.on_asr_final = prev_partial, prev_final


def run_chat(
    *,
    input_device: int | str | None,
    output_device: int | str | None,
    silence_threshold: float,
    use_agent: bool,
) -> None:
    mode_name = "JoyInside 智能体" if use_agent else "本地大脑"
    print("=" * 50, flush=True)
    print(f"语音对话启动中（{mode_name}）", flush=True)
    print("按 Enter 开始说话，说完停顿约 1 秒会自动结束录音", flush=True)
    if use_agent:
        print("对话由 JoyInside 控制台人设/技能驱动，请在平台配置智能体", flush=True)
    print("说「退出」「再见」结束对话，Ctrl+C 强制退出", flush=True)
    print("=" * 50, flush=True)

    cfg = JoyInsideConfig.from_env()
    auth = JoyInsideAuth(cfg.access_key, cfg.secret_key)
    session_id = str(uuid.uuid4())

    def get_token() -> str:
        return auth.get_token(bot_id=cfg.bot_id)

    print("正在连接 JoyInside…", flush=True)
    speech = JoyInsideSpeech(
        bot_id=cfg.bot_id,
        get_token=get_token,
        use_agent=use_agent,
        auto_interrupt_agent=not use_agent,
    )
    speech.connect(session_id=session_id)
    speech.ensure_pcm_output()

    helper = SyncSpeechHelper(speech)
    brain = ConversationBrain() if not use_agent else None

    print("连接成功，可以开始对话。", flush=True)
    print(f"会话 ID: {session_id[:8]}…", flush=True)
    print_audio_devices(input_device, output_device)

    try:
        while True:
            try:
                input("\n[按 Enter 开始说话] ")
            except EOFError:
                break

            print("正在听…（边录边传，说完稍等）")
            try:
                if use_agent:
                    user_text, reply = agent_turn(
                        speech,
                        input_device=input_device,
                        output_device=output_device,
                        silence_threshold=silence_threshold,
                    )
                else:
                    user_text, reply = local_turn_streaming(
                        speech,
                        helper,
                        brain,
                        input_device=input_device,
                        output_device=output_device,
                        silence_threshold=silence_threshold,
                    )
            except TimeoutError as exc:
                print(f"{exc}，请重试")
                continue
            except Exception as exc:
                logger.error("对话失败: %s", exc)
                print(f"对话失败: {exc}")
                continue

            if not user_text:
                print("没有录到声音或未识别到内容，请重试")
                continue

            print(f"你: {user_text}")

            if any(word in user_text for word in EXIT_WORDS):
                print("机器人: 好的，再见！")
                break

            if reply:
                print(f"机器人: {reply}")
            else:
                print("机器人: （未收到文本回复，但可能已播放语音）")

    except KeyboardInterrupt:
        print("\n已退出对话")
    finally:
        speech.close()


def main() -> None:
    parser = argparse.ArgumentParser(description="JoyInside 语音对话")
    parser.add_argument("--list-devices", action="store_true", help="列出音频设备")
    parser.add_argument("--input", dest="input_device", help="麦克风设备编号")
    parser.add_argument("--output", dest="output_device", help="播放设备编号")
    parser.add_argument(
        "--silence-threshold",
        type=float,
        default=0.015,
        help="静音检测阈值（默认 0.015）",
    )
    parser.add_argument(
        "--local",
        action="store_true",
        help="使用本地大脑+纯TTS（不用 JoyInside 智能体）",
    )
    args = parser.parse_args()

    if args.list_devices:
        list_audio_devices()
        return

    run_chat(
        input_device=parse_device(args.input_device),
        output_device=parse_device(args.output_device),
        silence_threshold=args.silence_threshold,
        use_agent=not args.local,
    )


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        print("\n已退出")
    except Exception as exc:
        print(f"\n启动失败: {exc}", flush=True)
        import traceback

        traceback.print_exc()
        input("\n按 Enter 关闭窗口…")
        sys.exit(1)
