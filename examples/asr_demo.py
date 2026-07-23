"""ASR 示例：读取 PCM 文件并识别文本。"""

from __future__ import annotations

import logging
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from config import JoyInsideConfig
from joyinside import JoyInsideAuth
from joyinside.audio import chunk_pcm, read_pcm_file
from joyinside.speech import JoyInsideSpeech, SyncSpeechHelper

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")


def main() -> None:
    cfg = JoyInsideConfig.from_env()
    auth = JoyInsideAuth(cfg.access_key, cfg.secret_key)

    pcm_path = Path(sys.argv[1]) if len(sys.argv) > 1 else ROOT / "test_input.pcm"
    if not pcm_path.exists():
        print(f"请提供 PCM 文件路径，或放置测试文件: {pcm_path}")
        print("用法: python examples/asr_demo.py path/to/audio.pcm")
        sys.exit(1)

    def get_token() -> str:
        return auth.get_token(bot_id=cfg.bot_id)

    speech = JoyInsideSpeech(bot_id=cfg.bot_id, get_token=get_token, manual_mode=True)
    speech.connect()

    pcm_data = read_pcm_file(pcm_path)
    chunks = chunk_pcm(pcm_data)
    print(f"读取 {pcm_path.name}，共 {len(chunks)} 帧")

    helper = SyncSpeechHelper(speech)
    text = helper.recognize_and_wait(chunks)
    print(f"识别结果: {text}")

    speech.close()


if __name__ == "__main__":
    main()
