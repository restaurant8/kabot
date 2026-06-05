from __future__ import annotations

import json
import logging
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any
from urllib.parse import urlparse

from .bot import BotApp
from .config import Settings

LOG = logging.getLogger(__name__)


class KabotRequestHandler(BaseHTTPRequestHandler):
    app: BotApp
    settings: Settings

    server_version = "kabot/0.1"

    def do_GET(self) -> None:  # noqa: N802
        path = urlparse(self.path).path
        if path == "/healthz":
            self.write_json(200, {"ok": True})
            return
        self.write_json(404, {"ok": False, "error": "not found"})

    def do_POST(self) -> None:  # noqa: N802
        path = urlparse(self.path).path
        if path == self.settings.webhook_path:
            self.handle_telegram_webhook()
            return
        if path == self.settings.event_path:
            self.handle_event_webhook()
            return
        self.write_json(404, {"ok": False, "error": "not found"})

    def handle_telegram_webhook(self) -> None:
        if self.settings.webhook_secret_token:
            got = self.headers.get("X-Telegram-Bot-Api-Secret-Token", "")
            if got != self.settings.webhook_secret_token:
                self.write_json(401, {"ok": False, "error": "invalid telegram secret"})
                return
        try:
            update = self.read_json()
            self.app.handle_update(update)
            self.write_json(200, {"ok": True})
        except Exception as exc:  # noqa: BLE001
            LOG.exception("telegram webhook failed")
            self.write_json(500, {"ok": False, "error": str(exc)})

    def handle_event_webhook(self) -> None:
        if self.settings.event_secret:
            got = self.headers.get("X-Kabot-Secret", "")
            if got != self.settings.event_secret:
                self.write_json(401, {"ok": False, "error": "invalid event secret"})
                return
        try:
            event = self.read_json()
            result = self.app.handle_business_event(event)
            self.write_json(200, result)
        except Exception as exc:  # noqa: BLE001
            LOG.exception("business event failed")
            self.write_json(500, {"ok": False, "error": str(exc)})

    def read_json(self) -> dict[str, Any]:
        length = int(self.headers.get("Content-Length", "0") or "0")
        raw = self.rfile.read(length) if length else b"{}"
        data = json.loads(raw.decode("utf-8"))
        if not isinstance(data, dict):
            raise ValueError("JSON body must be object")
        return data

    def write_json(self, status: int, data: dict[str, Any]) -> None:
        raw = json.dumps(data, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(raw)))
        self.end_headers()
        self.wfile.write(raw)

    def log_message(self, format: str, *args: Any) -> None:
        LOG.info("%s - %s", self.address_string(), format % args)


def run_webhook_server(app: BotApp, settings: Settings) -> None:
    app.init()
    settings.validate_runtime()
    KabotRequestHandler.app = app
    KabotRequestHandler.settings = settings
    server = ThreadingHTTPServer((settings.webhook_host, settings.webhook_port), KabotRequestHandler)
    LOG.info("webhook server listening on http://%s:%s", settings.webhook_host, settings.webhook_port)
    try:
        server.serve_forever()
    finally:
        server.server_close()
