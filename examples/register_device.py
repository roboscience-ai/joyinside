"""注册设备并输出 botId。"""

from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from config import JoyInsideConfig
from joyinside import JoyInsideAuth, register_device


def main() -> None:
    cfg = JoyInsideConfig.from_env()
    auth = JoyInsideAuth(cfg.access_key, cfg.secret_key)

    print("正在获取 Token（使用 vendorId）...")
    token = auth.get_token(vendor_id=cfg.vendor_id)
    print("Token 获取成功")

    print(f"正在注册设备 deviceId={cfg.device_id} ...")
    bot_id = register_device(
        token,
        vendor_id=cfg.vendor_id,
        app_id=cfg.app_id,
        device_id=cfg.device_id,
        name="测试机器人",
        device_type="APP_ROBOT",
    )
    print(f"注册成功，botId = {bot_id}")
    print("请把 botId 写入 .env 的 JOYINSIDE_BOT_ID")


if __name__ == "__main__":
    main()
