from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from .utils import json_loads, parse_bool, parse_csv_ints


def load_dotenv(path: str | Path = ".env") -> None:
    env_path = Path(path)
    if not env_path.exists():
        return
    for raw in env_path.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        key = key.strip()
        value = value.strip().strip('"').strip("'")
        os.environ.setdefault(key, value)


def _json_env(name: str, default: Any) -> Any:
    return json_loads(os.environ.get(name), default)


@dataclass(frozen=True)
class Settings:
    telegram_bot_token: str
    dujiao_base_url: str
    dujiao_api_prefix: str = "/api/v1"
    shop_base_url: str = ""
    support_url: str = ""
    default_locale: str = "zh-CN"
    db_path: str = "kabot.sqlite3"
    run_mode: str = "polling"
    log_level: str = "INFO"
    poll_timeout_seconds: int = 30
    poll_interval_seconds: float = 1.0
    admin_chat_ids: set[int] = field(default_factory=set)
    payment_channels: list[dict[str, Any]] = field(default_factory=list)
    endpoints: dict[str, str] = field(default_factory=dict)
    telegram_login_bot_token: str = ""
    enable_synthetic_telegram_login: bool = True
    order_sku_field: str = ""
    webhook_public_url: str = ""
    webhook_host: str = "0.0.0.0"
    webhook_port: int = 8080
    webhook_path: str = "/telegram/webhook"
    webhook_secret_token: str = ""
    event_path: str = "/kabot/events"
    event_secret: str = ""

    @classmethod
    def load(cls, env_path: str | Path = ".env") -> "Settings":
        load_dotenv(env_path)
        bot_token = os.environ.get("TELEGRAM_BOT_TOKEN", "").strip()
        dujiao_base_url = os.environ.get("DUJIAO_BASE_URL", "").strip().rstrip("/")
        default_channels = [
            {"id": 10, "name": "Alipay"},
            {"id": 20, "name": "WeChat"},
            {"id": 30, "name": "USDT"},
            {"id": 40, "name": "PayPal"},
            {"id": 50, "name": "Stripe"},
        ]
        endpoints = {
            "site_config": "/public/config",
            "categories": "/public/categories",
            "products": "/public/products",
            "product_detail": "/public/products/{slug}",
            "posts": "/public/posts",
            "wallet_profile": "/wallet",
            "wallet_transactions": "/wallet/transactions",
            "wallet_redeem_gift_card": "/wallet/gift-cards/redeem",
        }
        endpoints.update(_json_env("DUJIAO_ENDPOINTS", {}))
        telegram_login_bot_token = os.environ.get("TELEGRAM_LOGIN_BOT_TOKEN", "").strip() or bot_token
        return cls(
            telegram_bot_token=bot_token,
            dujiao_base_url=dujiao_base_url,
            dujiao_api_prefix=os.environ.get("DUJIAO_API_PREFIX", "/api/v1").strip() or "/api/v1",
            shop_base_url=os.environ.get("SHOP_BASE_URL", "").strip().rstrip("/"),
            support_url=os.environ.get("SUPPORT_URL", "").strip(),
            default_locale=os.environ.get("DEFAULT_LOCALE", "zh-CN").strip() or "zh-CN",
            db_path=os.environ.get("DB_PATH", "kabot.sqlite3").strip() or "kabot.sqlite3",
            run_mode=os.environ.get("RUN_MODE", "polling").strip().lower() or "polling",
            log_level=os.environ.get("LOG_LEVEL", "INFO").strip().upper() or "INFO",
            poll_timeout_seconds=int(os.environ.get("POLL_TIMEOUT_SECONDS", "30")),
            poll_interval_seconds=float(os.environ.get("POLL_INTERVAL_SECONDS", "1")),
            admin_chat_ids=parse_csv_ints(os.environ.get("ADMIN_CHAT_IDS")),
            payment_channels=_json_env("PAYMENT_CHANNELS", default_channels),
            endpoints=endpoints,
            telegram_login_bot_token=telegram_login_bot_token,
            enable_synthetic_telegram_login=parse_bool(os.environ.get("ENABLE_SYNTHETIC_TELEGRAM_LOGIN"), True),
            order_sku_field=os.environ.get("ORDER_SKU_FIELD", "").strip(),
            webhook_public_url=os.environ.get("WEBHOOK_PUBLIC_URL", "").strip(),
            webhook_host=os.environ.get("WEBHOOK_HOST", "0.0.0.0").strip() or "0.0.0.0",
            webhook_port=int(os.environ.get("WEBHOOK_PORT", "8080")),
            webhook_path=os.environ.get("WEBHOOK_PATH", "/telegram/webhook").strip() or "/telegram/webhook",
            webhook_secret_token=os.environ.get("WEBHOOK_SECRET_TOKEN", "").strip(),
            event_path=os.environ.get("EVENT_PATH", "/kabot/events").strip() or "/kabot/events",
            event_secret=os.environ.get("KABOT_EVENT_SECRET", "").strip(),
        )

    def validate_runtime(self) -> None:
        missing = []
        if not self.telegram_bot_token:
            missing.append("TELEGRAM_BOT_TOKEN")
        if not self.dujiao_base_url:
            missing.append("DUJIAO_BASE_URL")
        if missing:
            joined = ", ".join(missing)
            raise RuntimeError(f"Missing required config: {joined}")
