"""
终端文本对话：输入文字 → JoyInside 智能体(LLM) → TTS 播放。

用法:
  python text_chat.py
  python text_chat.py --output 7
"""

from __future__ import annotations

import argparse
import logging
import sys
import threading
import uuid
from pathlib import Path

import sounddevice as sd

ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT))

from config import JoyInsideConfig
from joyinside import JoyInsideAuth
from joyinside.local_audio import StreamingPcmPlayer
from joyinside.speech import JoyInsideSpeech

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
# 避免 logger 与 print 重复打印智能体文本
logging.getLogger("joyinside.speech").setLevel(logging.WARNING)
logger = logging.getLogger(__name__)

EXIT_WORDS = ("退出", "quit", "exit", "再见")


def parse_device(value: str | None) -> int | str | None:
    if value is None:
        return None
    try:
        return int(value)
    except ValueError:
        return value


def _is_mp3(data: bytes) -> bool:
    return bool(
        data
        and (
            data[:3] == b"ID3"
            or data[:2] in (b"\xff\xfb", b"\xff\xf3", b"\xff\xf2")
        )
    )


def chat_turn(
    speech: JoyInsideSpeech,
    player: StreamingPcmPlayer,
    text: str,
    *,
    timeout: float = 90.0,
) -> str:
    """发送 TEXT，等待 AGENT + TTS，返回完整回复文本。"""
    reply_so_far = ""
    done = threading.Event()
    started_reply = False

    def on_agent(chunk: str, _meta: dict) -> None:
        nonlocal reply_so_far, started_reply
        if not chunk:
            return

        # 平台可能推送累积文本或重复片段，只打印新增部分
        if chunk.startswith(reply_so_far):
            delta = chunk[len(reply_so_far) :]
            reply_so_far = chunk
        elif chunk == reply_so_far:
            return
        else:
            delta = chunk
            reply_so_far += chunk

        if not delta:
            return

        if not started_reply:
            started_reply = True
            print("机器人: ", end="", flush=True)
        print(delta, end="", flush=True)

    def on_tts(data: bytes, _meta: dict) -> None:
        if data and not _is_mp3(data):
            player.feed(data)

    def on_complete() -> None:
        done.set()

    prev = speech.on_agent, speech.on_tts_audio, speech.on_round_complete
    speech.on_agent = on_agent
    speech.on_tts_audio = on_tts
    speech.on_round_complete = on_complete

    try:
        if player.is_playing:
            speech.interrupt()
            player.clear()

        player.start()
        speech.send_text(text)

        if not done.wait(timeout):
            raise TimeoutError("回复超时")

        player.finish()
        if started_reply:
            print(flush=True)
        return reply_so_far.strip()
    finally:
        speech.on_agent, speech.on_tts_audio, speech.on_round_complete = prev


def main() -> None:
    parser = argparse.ArgumentParser(description="终端文本 → LLM → TTS")
    parser.add_argument("--output", dest="output_device", help="播放设备编号")
    parser.add_argument("--timeout", type=float, default=90.0, help="单轮超时(秒)")
    args = parser.parse_args()
    output_device = parse_device(args.output_device)

    print("=" * 50)
    print("终端文本对话（JoyInside 智能体 + TTS）")
    print("直接输入文字回车；输入 quit / 退出 结束")
    print("=" * 50)

    cfg = JoyInsideConfig.from_env()
    auth = JoyInsideAuth(cfg.access_key, cfg.secret_key)
    session_id = str(uuid.uuid4())

    speech = JoyInsideSpeech(
        bot_id=cfg.bot_id,
        get_token=lambda: auth.get_token(bot_id=cfg.bot_id),
        use_agent=True,
    )

    print("正在连接…")
    speech.connect(session_id=session_id)
    speech.ensure_pcm_output()
    print(f"已连接，会话 {session_id[:8]}…")

    if output_device is None:
        try:
            out_idx = sd.default.device[1]
            print(f"播放设备: [{out_idx}] {sd.query_devices(out_idx)['name']}")
        except Exception:
            pass
    else:
        print(f"播放设备: {output_device}")

    player = StreamingPcmPlayer(device=output_device)

    try:
        while True:
            try:
                user = input("\n你: ").strip()
            except EOFError:
                break

            if not user:
                continue
            if user.lower() in EXIT_WORDS or user in EXIT_WORDS:
                print("再见！")
                break

            try:
                chat_turn(speech, player, user, timeout=args.timeout)
            except TimeoutError as exc:
                print(f"{exc}，请重试")
            except Exception as exc:
                logger.error("对话失败: %s", exc)
                print(f"失败: {exc}")

    except KeyboardInterrupt:
        print("\n已退出")
    finally:
        speech.close()


if __name__ == "__main__":
    main()
