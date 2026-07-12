"""
机器人侧集成示例：ASR -> 你自己的逻辑 -> TTS
"""

from __future__ import annotations

import logging
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT))

from config import JoyInsideConfig
from joyinside import JoyInsideAuth
from joyinside.audio import chunk_pcm, read_pcm_file
from joyinside.speech import JoyInsideSpeech, SyncSpeechHelper

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")


def my_robot_brain(user_text: str) -> str:
    """替换成你自己的 LLM / 规则引擎。"""
    user_text = user_text.strip()
    if not user_text:
        return "我没有听清楚，请再说一遍。"
    if "你好" in user_text:
        return "你好，很高兴见到你。"
    return f"你说的是：{user_text}"


def main() -> None:
    cfg = JoyInsideConfig.from_env()
    auth = JoyInsideAuth(cfg.access_key, cfg.secret_key)

    def get_token() -> str:
        return auth.get_token(bot_id=cfg.bot_id)

    speech = JoyInsideSpeech(bot_id=cfg.bot_id, get_token=get_token)
    speech.connect()
    speech.ensure_pcm_output()
    helper = SyncSpeechHelper(speech)

    pcm_path = ROOT / "test_input.pcm"
    if not pcm_path.exists():
        print("未找到 test_input.pcm，将直接做 TTS 演示")
        reply = my_robot_brain("你好")
        pcm = helper.speak_and_collect(reply)
        (ROOT / "robot_reply.pcm").write_bytes(pcm)
        print(f"回复已保存到 robot_reply.pcm: {reply}")
    else:
        user_text = helper.recognize_and_wait(chunk_pcm(read_pcm_file(pcm_path)))
        print(f"用户: {user_text}")
        reply = my_robot_brain(user_text)
        print(f"机器人: {reply}")
        pcm = helper.speak_and_collect(reply)
        (ROOT / "robot_reply.pcm").write_bytes(pcm)

    speech.close()


if __name__ == "__main__":
    main()
