"""
语音对话：麦克风 ASR -> 本地逻辑 -> TTS 耳机播放。

用法:
  python voice_chat.py                  # 开始对话
  python voice_chat.py --list-devices   # 列出音频设备
  python voice_chat.py --input 1 --output 4   # 指定麦克风/耳机设备号
"""

from __future__ import annotations

import argparse
import logging
import sys
from pathlib import Path

import sounddevice as sd

ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT))

from config import BYTES_PER_FRAME, SAMPLE_RATE, JoyInsideConfig
from joyinside import JoyInsideAuth
from joyinside.audio import chunk_pcm
from joyinside.local_audio import (
    list_audio_devices,
    play_pcm,
    record_until_silence,
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
    """打印当前使用的输入/输出设备。"""
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


def speak_with_playback(
    speech: JoyInsideSpeech,
    helper: SyncSpeechHelper,
    text: str,
    *,
    output_device: int | str | None,
    timeout: float = 60.0,
) -> None:
    """TTS 合成完整音频后播放到耳机。"""
    speech.ensure_pcm_output()
    audio = helper.speak_and_collect(text, timeout=timeout)
    if not audio:
        print("警告: TTS 未返回音频数据", flush=True)
        return

    if audio[:3] == b"ID3" or audio[:2] in (b"\xff\xfb", b"\xff\xf3", b"\xff\xf2"):
        print(
            "警告: TTS 返回的是 MP3 而非 PCM，请确认已调用 ensure_pcm_output()",
            flush=True,
        )
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
) -> None:
    print("=" * 50, flush=True)
    print("语音对话启动中（JoyInside ASR + TTS）", flush=True)
    print("按 Enter 开始说话，说完停顿约 1 秒会自动结束录音", flush=True)
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

    try:
        while True:
            try:
                input("\n[按 Enter 开始说话] ")
            except EOFError:
                break

            print("正在听…（对着麦克风说话，说完稍等）")
            try:
                recorded = record_until_silence(
                    device=input_device,
                    silence_threshold=silence_threshold,
                )
            except Exception as exc:
                logger.error("录音失败: %s", exc)
                print(f"录音失败: {exc}")
                print("可运行 run_voice_chat.bat --list-devices 查看设备编号")
                continue

            if len(recorded.pcm) < BYTES_PER_FRAME:
                print("没有录到声音，请检查麦克风设备或调低 --silence-threshold")
                continue

            print(f"录音 {recorded.duration_s:.1f}s，正在识别…")
            try:
                user_text = helper.recognize_and_wait(
                    chunk_pcm(recorded.pcm),
                    timeout=30.0,
                ).strip()
            except TimeoutError:
                print("识别超时，请重试")
                continue

            if not user_text:
                print("未识别到内容，请再说一次")
                continue

            print(f"你: {user_text}")

            if any(word in user_text for word in EXIT_WORDS):
                reply = "好的，再见！"
                print(f"机器人: {reply}")
                speak_with_playback(speech, helper, reply, output_device=output_device)
                break

            reply = robot_brain(user_text)
            print(f"机器人: {reply}")
            try:
                speak_with_playback(speech, helper, reply, output_device=output_device)
            except TimeoutError:
                print("TTS 超时，请重试", flush=True)

    except KeyboardInterrupt:
        print("\n已退出对话")
    finally:
        speech.close()


def main() -> None:
    parser = argparse.ArgumentParser(description="JoyInside 语音对话（麦克风 + 耳机）")
    parser.add_argument(
        "--list-devices",
        action="store_true",
        help="列出本机音频输入/输出设备",
    )
    parser.add_argument(
        "--input",
        dest="input_device",
        help="麦克风设备编号或名称（默认系统默认输入）",
    )
    parser.add_argument(
        "--output",
        dest="output_device",
        help="播放设备编号或名称（默认系统默认输出，一般为耳机）",
    )
    parser.add_argument(
        "--silence-threshold",
        type=float,
        default=0.015,
        help="静音检测阈值，环境吵时可调到 0.02~0.04（默认 0.015）",
    )
    args = parser.parse_args()

    if args.list_devices:
        list_audio_devices()
        return

    run_chat(
        input_device=parse_device(args.input_device),
        output_device=parse_device(args.output_device),
        silence_threshold=args.silence_threshold,
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
