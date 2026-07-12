"""从环境变量或 .env 文件加载 JoyInside 配置。"""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

from dotenv import load_dotenv

_ENV_PATH = Path(__file__).resolve().parent / ".env"
load_dotenv(_ENV_PATH)


@dataclass(frozen=True)
class JoyInsideConfig:
    access_key: str
    secret_key: str
    vendor_id: str
    app_id: str
    bot_id: str
    device_id: str = "robot-sn-001"

    @classmethod
    def from_env(cls) -> "JoyInsideConfig":
        def require(name: str) -> str:
            value = os.getenv(name, "").strip()
            if not value:
                raise ValueError(
                    f"缺少环境变量 {name}，请复制 .env.example 为 .env 并填写"
                )
            return value

        return cls(
            access_key=require("JOYINSIDE_ACCESS_KEY"),
            secret_key=require("JOYINSIDE_SECRET_KEY"),
            vendor_id=require("JOYINSIDE_VENDOR_ID"),
            app_id=require("JOYINSIDE_APP_ID"),
            bot_id=require("JOYINSIDE_BOT_ID"),
            device_id=os.getenv("JOYINSIDE_DEVICE_ID", "robot-sn-001").strip(),
        )


API_BASE = "https://api.joyinside.com"
WS_VOICE_CHAT = "wss://ws.joyinside.com/soulmate/voiceChat/v1"

# 音频参数（与平台默认一致）
SAMPLE_RATE = 16000
SAMPLE_WIDTH = 2  # 16bit
CHANNELS = 1
FRAME_MS = 120
BYTES_PER_MS = SAMPLE_RATE * SAMPLE_WIDTH / 1000
BYTES_PER_FRAME = int(BYTES_PER_MS * FRAME_MS)
