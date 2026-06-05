from __future__ import annotations

import sqlite3
from pathlib import Path
from typing import Any

from .utils import json_dumps, json_loads, short_id, unix_now, utc_now_iso


class Database:
    def __init__(self, path: str) -> None:
        self.path = path
        Path(path).parent.mkdir(parents=True, exist_ok=True) if Path(path).parent != Path(".") else None

    def connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.path)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA foreign_keys=ON")
        return conn

    def init(self) -> None:
        with self.connect() as conn:
            conn.executescript(
                """
                CREATE TABLE IF NOT EXISTS users (
                    telegram_user_id INTEGER PRIMARY KEY,
                    chat_id INTEGER NOT NULL,
                    username TEXT,
                    first_name TEXT,
                    last_name TEXT,
                    locale TEXT,
                    user_token TEXT,
                    token_expires_at TEXT,
                    user_json TEXT,
                    guest_email TEXT,
                    guest_order_password TEXT,
                    affiliate_code TEXT,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    blocked_at TEXT
                );

                CREATE TABLE IF NOT EXISTS sessions (
                    telegram_user_id INTEGER PRIMARY KEY,
                    state TEXT NOT NULL,
                    data TEXT,
                    updated_at TEXT NOT NULL
                );

                CREATE TABLE IF NOT EXISTS callbacks (
                    id TEXT PRIMARY KEY,
                    telegram_user_id INTEGER NOT NULL,
                    action TEXT NOT NULL,
                    payload TEXT,
                    expires_at INTEGER NOT NULL,
                    created_at TEXT NOT NULL
                );
                CREATE INDEX IF NOT EXISTS idx_callbacks_expires_at ON callbacks(expires_at);

                CREATE TABLE IF NOT EXISTS orders_cache (
                    order_no TEXT PRIMARY KEY,
                    telegram_user_id INTEGER,
                    guest_email TEXT,
                    order_password TEXT,
                    status TEXT,
                    payment_id INTEGER,
                    channel_id INTEGER,
                    payload TEXT,
                    last_notified_status TEXT,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                );
                CREATE INDEX IF NOT EXISTS idx_orders_cache_user ON orders_cache(telegram_user_id, updated_at);

                CREATE TABLE IF NOT EXISTS broadcasts (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    status TEXT NOT NULL,
                    filter_json TEXT,
                    message_html TEXT NOT NULL,
                    attachment_json TEXT,
                    created_by INTEGER,
                    total_count INTEGER DEFAULT 0,
                    success_count INTEGER DEFAULT 0,
                    failure_count INTEGER DEFAULT 0,
                    error_json TEXT,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                );

                CREATE TABLE IF NOT EXISTS events (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    unique_key TEXT UNIQUE,
                    kind TEXT NOT NULL,
                    payload TEXT,
                    created_at TEXT NOT NULL
                );

                CREATE TABLE IF NOT EXISTS stock_subscriptions (
                    telegram_user_id INTEGER NOT NULL,
                    product_slug TEXT NOT NULL,
                    product_title TEXT,
                    created_at TEXT NOT NULL,
                    PRIMARY KEY (telegram_user_id, product_slug)
                );
                CREATE INDEX IF NOT EXISTS idx_stock_subscriptions_product ON stock_subscriptions(product_slug);
                """
            )

    def upsert_user(self, user: dict[str, Any], chat_id: int, locale: str) -> dict[str, Any]:
        now = utc_now_iso()
        telegram_user_id = int(user["id"])
        with self.connect() as conn:
            existing = conn.execute(
                "SELECT * FROM users WHERE telegram_user_id=?",
                (telegram_user_id,),
            ).fetchone()
            if existing:
                conn.execute(
                    """
                    UPDATE users
                    SET chat_id=?, username=?, first_name=?, last_name=?, locale=?, updated_at=?, blocked_at=NULL
                    WHERE telegram_user_id=?
                    """,
                    (
                        chat_id,
                        user.get("username"),
                        user.get("first_name"),
                        user.get("last_name"),
                        user.get("language_code") or existing["locale"] or locale,
                        now,
                        telegram_user_id,
                    ),
                )
            else:
                conn.execute(
                    """
                    INSERT INTO users (
                        telegram_user_id, chat_id, username, first_name, last_name, locale,
                        created_at, updated_at
                    ) VALUES (?,?,?,?,?,?,?,?)
                    """,
                    (
                        telegram_user_id,
                        chat_id,
                        user.get("username"),
                        user.get("first_name"),
                        user.get("last_name"),
                        user.get("language_code") or locale,
                        now,
                        now,
                    ),
                )
        return self.get_user(telegram_user_id) or {}

    def get_user(self, telegram_user_id: int) -> dict[str, Any] | None:
        with self.connect() as conn:
            row = conn.execute("SELECT * FROM users WHERE telegram_user_id=?", (telegram_user_id,)).fetchone()
            return dict(row) if row else None

    def list_users(self, only_active: bool = True) -> list[dict[str, Any]]:
        sql = "SELECT * FROM users"
        if only_active:
            sql += " WHERE blocked_at IS NULL"
        sql += " ORDER BY updated_at DESC"
        with self.connect() as conn:
            return [dict(row) for row in conn.execute(sql).fetchall()]

    def set_user_token(self, telegram_user_id: int, token: str, expires_at: str | None, user_json: Any) -> None:
        with self.connect() as conn:
            conn.execute(
                """
                UPDATE users
                SET user_token=?, token_expires_at=?, user_json=?, updated_at=?
                WHERE telegram_user_id=?
                """,
                (token, expires_at, json_dumps(user_json), utc_now_iso(), telegram_user_id),
            )

    def clear_user_token(self, telegram_user_id: int) -> None:
        with self.connect() as conn:
            conn.execute(
                """
                UPDATE users
                SET user_token=NULL, token_expires_at=NULL, user_json=NULL, updated_at=?
                WHERE telegram_user_id=?
                """,
                (utc_now_iso(), telegram_user_id),
            )

    def set_guest_credentials(self, telegram_user_id: int, email: str, order_password: str) -> None:
        with self.connect() as conn:
            conn.execute(
                """
                UPDATE users
                SET guest_email=?, guest_order_password=?, updated_at=?
                WHERE telegram_user_id=?
                """,
                (email, order_password, utc_now_iso(), telegram_user_id),
            )

    def set_affiliate_code(self, telegram_user_id: int, code: str) -> None:
        with self.connect() as conn:
            conn.execute(
                "UPDATE users SET affiliate_code=?, updated_at=? WHERE telegram_user_id=?",
                (code, utc_now_iso(), telegram_user_id),
            )

    def mark_blocked(self, telegram_user_id: int) -> None:
        with self.connect() as conn:
            conn.execute(
                "UPDATE users SET blocked_at=?, updated_at=? WHERE telegram_user_id=?",
                (utc_now_iso(), utc_now_iso(), telegram_user_id),
            )

    def get_session(self, telegram_user_id: int) -> tuple[str, dict[str, Any]] | None:
        with self.connect() as conn:
            row = conn.execute("SELECT * FROM sessions WHERE telegram_user_id=?", (telegram_user_id,)).fetchone()
        if not row:
            return None
        return row["state"], json_loads(row["data"], {})

    def set_session(self, telegram_user_id: int, state: str, data: dict[str, Any] | None = None) -> None:
        with self.connect() as conn:
            conn.execute(
                """
                INSERT INTO sessions (telegram_user_id, state, data, updated_at)
                VALUES (?,?,?,?)
                ON CONFLICT(telegram_user_id) DO UPDATE SET
                    state=excluded.state,
                    data=excluded.data,
                    updated_at=excluded.updated_at
                """,
                (telegram_user_id, state, json_dumps(data or {}), utc_now_iso()),
            )

    def clear_session(self, telegram_user_id: int) -> None:
        with self.connect() as conn:
            conn.execute("DELETE FROM sessions WHERE telegram_user_id=?", (telegram_user_id,))

    def create_callback(self, telegram_user_id: int, action: str, payload: dict[str, Any] | None = None, ttl: int = 3600) -> str:
        callback_id = short_id(10)
        with self.connect() as conn:
            conn.execute(
                """
                INSERT INTO callbacks (id, telegram_user_id, action, payload, expires_at, created_at)
                VALUES (?,?,?,?,?,?)
                """,
                (callback_id, telegram_user_id, action, json_dumps(payload or {}), unix_now() + ttl, utc_now_iso()),
            )
        return callback_id

    def get_callback(self, callback_id: str) -> dict[str, Any] | None:
        with self.connect() as conn:
            row = conn.execute("SELECT * FROM callbacks WHERE id=?", (callback_id,)).fetchone()
        if not row or row["expires_at"] < unix_now():
            return None
        data = dict(row)
        data["payload"] = json_loads(data.get("payload"), {})
        return data

    def cleanup_callbacks(self) -> None:
        with self.connect() as conn:
            conn.execute("DELETE FROM callbacks WHERE expires_at < ?", (unix_now(),))

    def cache_order(
        self,
        order_no: str,
        *,
        telegram_user_id: int | None,
        status: str | None,
        payload: Any,
        guest_email: str | None = None,
        order_password: str | None = None,
        payment_id: int | None = None,
        channel_id: int | None = None,
    ) -> None:
        now = utc_now_iso()
        with self.connect() as conn:
            conn.execute(
                """
                INSERT INTO orders_cache (
                    order_no, telegram_user_id, guest_email, order_password, status,
                    payment_id, channel_id, payload, created_at, updated_at
                ) VALUES (?,?,?,?,?,?,?,?,?,?)
                ON CONFLICT(order_no) DO UPDATE SET
                    telegram_user_id=COALESCE(excluded.telegram_user_id, orders_cache.telegram_user_id),
                    guest_email=COALESCE(excluded.guest_email, orders_cache.guest_email),
                    order_password=COALESCE(excluded.order_password, orders_cache.order_password),
                    status=COALESCE(excluded.status, orders_cache.status),
                    payment_id=COALESCE(excluded.payment_id, orders_cache.payment_id),
                    channel_id=COALESCE(excluded.channel_id, orders_cache.channel_id),
                    payload=excluded.payload,
                    updated_at=excluded.updated_at
                """,
                (
                    order_no,
                    telegram_user_id,
                    guest_email,
                    order_password,
                    status,
                    payment_id,
                    channel_id,
                    json_dumps(payload),
                    now,
                    now,
                ),
            )

    def get_cached_order(self, order_no: str) -> dict[str, Any] | None:
        with self.connect() as conn:
            row = conn.execute("SELECT * FROM orders_cache WHERE order_no=?", (order_no,)).fetchone()
        if not row:
            return None
        data = dict(row)
        data["payload"] = json_loads(data.get("payload"), {})
        return data

    def recent_orders(self, telegram_user_id: int, limit: int = 10) -> list[dict[str, Any]]:
        with self.connect() as conn:
            rows = conn.execute(
                """
                SELECT * FROM orders_cache
                WHERE telegram_user_id=?
                ORDER BY updated_at DESC
                LIMIT ?
                """,
                (telegram_user_id, limit),
            ).fetchall()
        result = []
        for row in rows:
            data = dict(row)
            data["payload"] = json_loads(data.get("payload"), {})
            result.append(data)
        return result

    def record_event_once(self, unique_key: str, kind: str, payload: Any) -> bool:
        try:
            with self.connect() as conn:
                conn.execute(
                    "INSERT INTO events (unique_key, kind, payload, created_at) VALUES (?,?,?,?)",
                    (unique_key, kind, json_dumps(payload), utc_now_iso()),
                )
            return True
        except sqlite3.IntegrityError:
            return False

    def create_broadcast(
        self,
        created_by: int | None,
        message_html: str,
        filter_json: dict[str, Any] | None = None,
        attachment_json: dict[str, Any] | None = None,
    ) -> int:
        now = utc_now_iso()
        with self.connect() as conn:
            cur = conn.execute(
                """
                INSERT INTO broadcasts (
                    status, filter_json, message_html, attachment_json, created_by, created_at, updated_at
                ) VALUES (?,?,?,?,?,?,?)
                """,
                (
                    "created",
                    json_dumps(filter_json or {}),
                    message_html,
                    json_dumps(attachment_json or {}),
                    created_by,
                    now,
                    now,
                ),
            )
            return int(cur.lastrowid)

    def update_broadcast(self, broadcast_id: int, **fields: Any) -> None:
        if not fields:
            return
        fields["updated_at"] = utc_now_iso()
        assignments = ", ".join(f"{key}=?" for key in fields)
        values = list(fields.values()) + [broadcast_id]
        with self.connect() as conn:
            conn.execute(f"UPDATE broadcasts SET {assignments} WHERE id=?", values)

    def subscribe_stock(self, telegram_user_id: int, product_slug: str, product_title: str) -> None:
        with self.connect() as conn:
            conn.execute(
                """
                INSERT INTO stock_subscriptions (telegram_user_id, product_slug, product_title, created_at)
                VALUES (?,?,?,?)
                ON CONFLICT(telegram_user_id, product_slug) DO UPDATE SET
                    product_title=excluded.product_title
                """,
                (telegram_user_id, product_slug, product_title, utc_now_iso()),
            )

    def unsubscribe_stock(self, telegram_user_id: int, product_slug: str) -> None:
        with self.connect() as conn:
            conn.execute(
                "DELETE FROM stock_subscriptions WHERE telegram_user_id=? AND product_slug=?",
                (telegram_user_id, product_slug),
            )

    def stock_subscribers(self, product_slug: str) -> list[dict[str, Any]]:
        with self.connect() as conn:
            rows = conn.execute(
                """
                SELECT s.*, u.chat_id
                FROM stock_subscriptions s
                JOIN users u ON u.telegram_user_id=s.telegram_user_id
                WHERE s.product_slug=? AND u.blocked_at IS NULL
                """,
                (product_slug,),
            ).fetchall()
        return [dict(row) for row in rows]
