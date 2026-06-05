from __future__ import annotations

import html
import json
import secrets
import string
import time
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation
from typing import Any


def utc_now_iso() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


def unix_now() -> int:
    return int(time.time())


def html_escape(value: Any) -> str:
    return html.escape("" if value is None else str(value), quote=False)


def json_dumps(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"))


def json_loads(value: str | None, default: Any = None) -> Any:
    if not value:
        return default
    try:
        return json.loads(value)
    except json.JSONDecodeError:
        return default


def parse_bool(value: str | None, default: bool = False) -> bool:
    if value is None or value == "":
        return default
    return value.strip().lower() in {"1", "true", "yes", "y", "on"}


def parse_csv_ints(value: str | None) -> set[int]:
    result: set[int] = set()
    if not value:
        return result
    for part in value.split(","):
        part = part.strip()
        if not part:
            continue
        try:
            result.add(int(part))
        except ValueError:
            continue
    return result


def short_id(length: int = 12) -> str:
    alphabet = string.ascii_letters + string.digits
    return "".join(secrets.choice(alphabet) for _ in range(length))


def pick_i18n(value: Any, locale: str = "zh-CN") -> str:
    if isinstance(value, dict):
        for key in (locale, "zh-CN", "zh-TW", "en-US", "en"):
            item = value.get(key)
            if item:
                return str(item)
        for item in value.values():
            if item:
                return str(item)
        return ""
    if value is None:
        return ""
    return str(value)


def format_money(amount: Any, currency: str | None = None) -> str:
    if amount is None or amount == "":
        text = "0.00"
    else:
        try:
            text = f"{Decimal(str(amount)):.2f}"
        except (InvalidOperation, ValueError):
            text = str(amount)
    return f"{text} {currency}" if currency else text


ORDER_STATUS_LABELS = {
    "pending_payment": "待支付",
    "paid": "已支付",
    "processing": "处理中",
    "fulfilling": "履约中",
    "partially_delivered": "部分交付",
    "delivered": "已交付",
    "completed": "已完成",
    "canceled": "已取消",
    "expired": "已过期",
    "failed": "失败",
}


PAYMENT_STATUS_LABELS = {
    "initiated": "已创建",
    "pending": "待确认",
    "success": "支付成功",
    "failed": "支付失败",
    "expired": "已过期",
}


def order_status_label(status: str | None) -> str:
    return ORDER_STATUS_LABELS.get(status or "", status or "未知")


def payment_status_label(status: str | None) -> str:
    return PAYMENT_STATUS_LABELS.get(status or "", status or "未知")


def split_telegram_text(text: str, limit: int = 3800) -> list[str]:
    if len(text) <= limit:
        return [text]
    parts: list[str] = []
    buf: list[str] = []
    size = 0
    for line in text.splitlines(True):
        if size + len(line) > limit and buf:
            parts.append("".join(buf))
            buf = []
            size = 0
        if len(line) > limit:
            while line:
                parts.append(line[:limit])
                line = line[limit:]
            continue
        buf.append(line)
        size += len(line)
    if buf:
        parts.append("".join(buf))
    return parts


def ensure_api_prefix(path: str) -> str:
    if not path.startswith("/"):
        return "/" + path
    return path


def compact_order_items(items: list[dict[str, Any]] | None, locale: str) -> str:
    if not items:
        return "无商品明细"
    lines = []
    for item in items:
        title = pick_i18n(item.get("title") or item.get("sku_snapshot") or item.get("name"), locale)
        qty = item.get("quantity", 1)
        total = item.get("total_price") or item.get("total_amount") or item.get("unit_price")
        lines.append(f"- {html_escape(title)} x{qty} / {html_escape(total)}")
    return "\n".join(lines)
