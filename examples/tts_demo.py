"""TTS 示例：将文本合成为 PCM 并保存到文件。"""

from __future__ import annotations

import logging
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from config import JoyInsideConfig
from joyinside import JoyInsideAuth
from joyinside.speech import JoyInsideSpeech, SyncSpeechHelper

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")


def main() -> None:
    cfg = JoyInsideConfig.from_env()
    auth = JoyInsideAuth(cfg.access_key, cfg.secret_key)

    def get_token() -> str:
        return auth.get_token(bot_id=cfg.bot_id)

    speech = JoyInsideSpeech(bot_id=cfg.bot_id, get_token=get_token)
    speech.connect()
    speech.ensure_pcm_output()

    helper = SyncSpeechHelper(speech)
    text = sys.argv[1] if len(sys.argv) > 1 else "你好，我是你的机器人助手。"
    print(f"正在合成: {text}")

    pcm = helper.speak_and_collect(text)
    out = ROOT / "output_tts.pcm"
    out.write_bytes(pcm)
    print(f"已保存 PCM 到 {out}（16kHz, 16bit, mono）")
    print("可用 ffplay -f s16le -ar 16000 -ac 1 output_tts.pcm 播放")

    speech.close()


if __name__ == "__main__":
    main()
