"""JoyInside 设备注册。"""

from __future__ import annotations

from typing import Any, Literal

import requests

from config import API_BASE

DeviceType = Literal["PHYSICAL_ROBOT", "APP_ROBOT"]


def register_device(
    access_token: str,
    *,
    vendor_id: str,
    app_id: str,
    device_id: str,
    name: str = "我的机器人",
    device_type: DeviceType = "APP_ROBOT",
    timbre_id: str | None = None,
    desc: str | None = None,
) -> str:
    """
    注册设备并返回 botId。

    若控制台已手动创建设备，可跳过此步骤，直接使用设备 ID 作为 botId。
    """
    headers = {"Authorization": f"Bearer {access_token}"}
    body: dict[str, Any] = {
        "vendorId": vendor_id,
        "appId": app_id,
        "deviceId": device_id,
        "type": device_type,
        "name": name,
    }
    if timbre_id:
        body["timbreId"] = timbre_id
    if desc:
        body["desc"] = desc

    response = requests.post(
        f"{API_BASE}/device/register",
        headers=headers,
        json=body,
        timeout=30,
    )
    response.raise_for_status()
    data = response.json()

    if data.get("state") != "SUCCESS":
        raise RuntimeError(f"设备注册失败: {data}")

    return str(data["data"])
