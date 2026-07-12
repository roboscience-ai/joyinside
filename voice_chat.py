"""
语音对话：流式 ASR（边录边传）+ 流式 TTS（边收边播）。

用法:
  python voice_chat.py                  # 流式模式（默认）
  python voice_chat.py --batch          # 批处理模式（录完再传、收完再播）
  python voice_chat.py --list-devices   # 列出音频设备
"""

from __future__ import annotations

import argparse
import logging
import sys
import threading
import time
from pathlib import Path

import sounddevice as sd

ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT))

from config import BYTES_PER_FRAME, SAMPLE_RATE, JoyInsideConfig
from joyinside import JoyInsideAuth
from joyinside.audio import chunk_pcm
from joyinside.local_audio import (
    StreamingPcmPlayer,
    list_audio_devices,
    play_pcm,
    record_until_silence,
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


def robot_brain(user_text: str) -> str:
    """简单回复逻辑，可替换成自己的 LLM。"""
    text = user_text.strip().lstrip("，, ")
    if not text:
        return "我没有听清楚，请再说一遍。"
    lower = text.lower()
    if any(word in lower for word in ("你好", "您好", "hello", "hi")):
        return "你好，我是语音助手，有什么可以帮你的？"
    if "天气" in text:
        return "我暂时查不了天气，你可以问我其他问题。"
    if "名字" in text or "叫什么" in text:
        return "我是 JoyInside 语音机器人，很高兴和你聊天。"
    if "谢谢" in text:
        return "不客气，还有什么想说的吗？"
    return f"我听到你说：{text}。这是一个演示回复，你可以把 robot_brain 换成自己的大模型。"


def print_audio_devices(input_device: int | str | None, output_device: int | str | None) -> None:
    try:
        if input_device is None:
            in_idx = sd.default.device[0]
            in_name = sd.query_devices(in_idx)["name"]
            print(f"麦克风: [{in_idx}] {in_name}", flush=True)
        else:
            print(f"麦克风: 设备 {input_device}", flush=True)

        if output_device is None:
            out_idx = sd.default.device[1]
            out_name = sd.query_devices(out_idx)["name"]
            print(f"播放设备: [{out_idx}] {out_name}", flush=True)
            print("若听不到声音，请用 --list-devices 查看编号，并用 --output 指定耳机", flush=True)
        else:
            print(f"播放设备: 设备 {output_device}", flush=True)
    except Exception as exc:
        print(f"无法查询音频设备: {exc}", flush=True)


def recognize_streaming(
    speech: JoyInsideSpeech,
    *,
    input_device: int | str | None,
    silence_threshold: float,
) -> str:
    """流式 ASR：边录边传，显示中间识别结果。"""
    result: dict[str, str] = {}
    asr_done = threading.Event()

    def on_partial(text: str) -> None:
        if text:
            print(f"\r  识别中: {text}", end="", flush=True)

    def on_final(text: str) -> None:
        result["text"] = text
        asr_done.set()

    prev_partial = speech.on_asr_partial
    prev_final = speech.on_asr_final
    speech.on_asr_partial = on_partial
    speech.on_asr_final = on_final
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
            return ""
        if not asr_done.wait(30.0):
            raise TimeoutError("ASR 超时")
        return result.get("text", "").strip()
    finally:
        speech.on_asr_partial = prev_partial
        speech.on_asr_final = prev_final


def recognize_batch(
    helper: SyncSpeechHelper,
    *,
    input_device: int | str | None,
    silence_threshold: float,
) -> str:
    """批处理 ASR：录完再传。"""
    recorded = record_until_silence(
        device=input_device,
        silence_threshold=silence_threshold,
    )
    if len(recorded.pcm) < BYTES_PER_FRAME:
        return ""
    print(f"录音 {recorded.duration_s:.1f}s，正在识别…")
    return helper.recognize_and_wait(chunk_pcm(recorded.pcm), timeout=30.0).strip()


def speak_streaming(
    speech: JoyInsideSpeech,
    text: str,
    *,
    output_device: int | str | None,
    timeout: float = 60.0,
) -> None:
    """流式 TTS：边收边播。"""
    speech.ensure_pcm_output()
    player = StreamingPcmPlayer(device=output_device)
    player.start()
    done = threading.Event()
    first_at = 0.0
    t0 = time.perf_counter()

    def on_audio(data: bytes, meta: dict) -> None:
        nonlocal first_at
        if not data:
            return
        if data[:3] == b"ID3" or data[:2] in (b"\xff\xfb", b"\xff\xf3", b"\xff\xf2"):
            print("警告: TTS 返回 MP3 而非 PCM", flush=True)
            return
        if first_at == 0.0:
            first_at = time.perf_counter()
            print(f"  TTS 首包 {(first_at - t0) * 1000:.0f}ms", flush=True)
        player.feed(data)

    def on_complete() -> None:
        done.set()

    prev_audio = speech.on_tts_audio
    prev_complete = speech.on_tts_complete
    speech.on_tts_audio = on_audio
    speech.on_tts_complete = on_complete

    try:
        speech.speak(text)
        if not done.wait(timeout):
            raise TimeoutError("TTS 超时")
        player.finish()
        total_ms = (time.perf_counter() - t0) * 1000
        print(f"  TTS 完成 {total_ms:.0f}ms", flush=True)
    finally:
        speech.on_tts_audio = prev_audio
        speech.on_tts_complete = prev_complete


def speak_batch(
    speech: JoyInsideSpeech,
    helper: SyncSpeechHelper,
    text: str,
    *,
    output_device: int | str | None,
    timeout: float = 60.0,
) -> None:
    """批处理 TTS：收齐再播。"""
    speech.ensure_pcm_output()
    audio = helper.speak_and_collect(text, timeout=timeout)
    if not audio:
        print("警告: TTS 未返回音频数据", flush=True)
        return
    if audio[:3] == b"ID3" or audio[:2] in (b"\xff\xfb", b"\xff\xf3", b"\xff\xf2"):
        print("警告: TTS 返回 MP3 而非 PCM", flush=True)
        return
    duration_s = len(audio) / (SAMPLE_RATE * 2)
    print(f"收到音频 {len(audio)} 字节（约 {duration_s:.1f}s），正在播放…", flush=True)
    play_pcm(audio, device=output_device)
    print("播放完成", flush=True)


def run_chat(
    *,
    input_device: int | str | None,
    output_device: int | str | None,
    silence_threshold: float,
    streaming: bool,
) -> None:
    mode = "流式" if streaming else "批处理"
    print("=" * 50, flush=True)
    print(f"语音对话启动中（{mode} ASR + TTS）", flush=True)
    print("按 Enter 开始说话，说完停顿约 1 秒会自动结束录音", flush=True)
    if streaming:
        print("流式模式：边录边识别、边合成边播放", flush=True)
    print("说「退出」「再见」结束对话，Ctrl+C 强制退出", flush=True)
    print("=" * 50, flush=True)

    cfg = JoyInsideConfig.from_env()
    auth = JoyInsideAuth(cfg.access_key, cfg.secret_key)

    def get_token() -> str:
        return auth.get_token(bot_id=cfg.bot_id)

    print("正在连接 JoyInside…", flush=True)
    speech = JoyInsideSpeech(bot_id=cfg.bot_id, get_token=get_token)
    speech.connect()
    helper = SyncSpeechHelper(speech)
    print("连接成功，可以开始对话。", flush=True)
    print_audio_devices(input_device, output_device)

    def do_speak(reply_text: str) -> None:
        if streaming:
            speak_streaming(speech, reply_text, output_device=output_device)
        else:
            speak_batch(speech, helper, reply_text, output_device=output_device)

    try:
        while True:
            try:
                input("\n[按 Enter 开始说话] ")
            except EOFError:
                break

            if streaming:
                print("正在听…（边录边传，说完稍等）")
            else:
                print("正在听…（对着麦克风说话，说完稍等）")

            try:
                if streaming:
                    user_text = recognize_streaming(
                        speech,
                        input_device=input_device,
                        silence_threshold=silence_threshold,
                    )
                else:
                    user_text = recognize_batch(
                        helper,
                        input_device=input_device,
                        silence_threshold=silence_threshold,
                    )
            except TimeoutError:
                print("识别超时，请重试")
                continue
            except Exception as exc:
                logger.error("录音/识别失败: %s", exc)
                print(f"录音/识别失败: {exc}")
                continue

            if not user_text:
                print("没有录到声音或未识别到内容，请重试")
                continue

            print(f"你: {user_text}")

            if any(word in user_text for word in EXIT_WORDS):
                reply = "好的，再见！"
                print(f"机器人: {reply}")
                do_speak(reply)
                break

            reply = robot_brain(user_text)
            print(f"机器人: {reply}")
            try:
                do_speak(reply)
            except TimeoutError:
                print("TTS 超时，请重试", flush=True)

    except KeyboardInterrupt:
        print("\n已退出对话")
    finally:
        speech.close()


def main() -> None:
    parser = argparse.ArgumentParser(description="JoyInside 语音对话（麦克风 + 耳机）")
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
        "--batch",
        action="store_true",
        help="使用批处理模式（录完再传、收完再播）",
    )
    args = parser.parse_args()

    if args.list_devices:
        list_audio_devices()
        return

    run_chat(
        input_device=parse_device(args.input_device),
        output_device=parse_device(args.output_device),
        silence_threshold=args.silence_threshold,
        streaming=not args.batch,
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
