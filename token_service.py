"""
本地 Token 服务（可选）。

机器人不要保存 SecretKey，可从此服务获取短期 Token。
启动: python token_service.py
请求: GET http://127.0.0.1:8765/token
"""

from __future__ import annotations

import json
import logging
from http.server import BaseHTTPRequestHandler, HTTPServer

from config import JoyInsideConfig
from joyinside import JoyInsideAuth

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")

cfg = JoyInsideConfig.from_env()
auth = JoyInsideAuth(cfg.access_key, cfg.secret_key)


class TokenHandler(BaseHTTPRequestHandler):
    def do_GET(self) -> None:
        if self.path not in ("/token", "/health"):
            self.send_error(404)
            return

        if self.path == "/health":
            body = {"status": "ok"}
        else:
            token = auth.get_token(bot_id=cfg.bot_id)
            body = {"access_token": token, "bot_id": cfg.bot_id}

        data = json.dumps(body, ensure_ascii=False).encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def log_message(self, format: str, *args) -> None:
        logging.info("%s - %s", self.address_string(), format % args)


def main() -> None:
    host, port = "127.0.0.1", 8765
    server = HTTPServer((host, port), TokenHandler)
    print(f"Token 服务已启动: http://{host}:{port}/token")
    server.serve_forever()


if __name__ == "__main__":
    main()
