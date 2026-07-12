"""JoyInside Token 鉴权。"""

from __future__ import annotations

import binascii
import hashlib
import hmac
import time
import uuid
from dataclasses import dataclass
from typing import Any

import requests

from config import API_BASE


@dataclass
class TokenInfo:
    access_token: str
    expire_in: int
    refresh_token: str
    refresh_expire_in: int
    obtained_at: float

    @property
    def is_expired(self) -> bool:
        # 提前 5 分钟刷新
        return time.time() >= self.obtained_at + self.expire_in - 300


class JoyInsideAuth:
    def __init__(self, access_key: str, secret_key: str) -> None:
        self.access_key = access_key
        self.secret_key = secret_key
        self._token: TokenInfo | None = None

    @staticmethod
    def _sign(
        access_version: str,
        access_timestamp: str,
        access_nonce: str,
        access_key_id: str,
        secret_key: str,
    ) -> str:
        params = {
            "accessVersion": access_version,
            "accessTimestamp": access_timestamp,
            "accessNonce": access_nonce,
            "accessKeyId": access_key_id,
        }
        lower_key_params = {k.lower(): v for k, v in params.items()}
        sorted_params = sorted(lower_key_params.items(), key=lambda item: item[0])
        joint_params = "&".join(f"{k}={v}" for k, v in sorted_params)
        digest = hmac.new(
            secret_key.encode("utf-8"),
            joint_params.encode("utf-8"),
            digestmod=hashlib.md5,
        )
        return binascii.hexlify(digest.digest()).decode("utf-8")

    def _build_auth_params(
        self,
        *,
        bot_id: str | None = None,
        vendor_id: str | None = None,
    ) -> dict[str, str]:
        timestamp = str(int(round(time.time() * 1000)))
        nonce = str(uuid.uuid4())
        params = {
            "accessVersion": "V2",
            "accessTimestamp": timestamp,
            "accessNonce": nonce,
            "accessKeyId": self.access_key,
            "accessSign": self._sign("V2", timestamp, nonce, self.access_key, self.secret_key),
        }
        if bot_id:
            params["botId"] = bot_id
        elif vendor_id:
            params["vendorId"] = vendor_id
        else:
            raise ValueError("bot_id 与 vendor_id 至少提供一个")
        return params

    def get_token(
        self,
        *,
        bot_id: str | None = None,
        vendor_id: str | None = None,
        force_refresh: bool = False,
    ) -> str:
        if (
            not force_refresh
            and self._token is not None
            and not self._token.is_expired
        ):
            return self._token.access_token

        body = self._build_auth_params(bot_id=bot_id, vendor_id=vendor_id)
        response = requests.post(f"{API_BASE}/auth/getToken", json=body, timeout=30)
        response.raise_for_status()
        data: dict[str, Any] = response.json()

        if data.get("code") not in (None, 200, "200", 0):
            raise RuntimeError(f"获取 Token 失败: {data}")

        self._token = TokenInfo(
            access_token=data["accessToken"],
            expire_in=int(data["expireIn"]),
            refresh_token=data["refreshToken"],
            refresh_expire_in=int(data["refreshExpireIn"]),
            obtained_at=time.time(),
        )
        return self._token.access_token
