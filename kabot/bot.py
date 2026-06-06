from __future__ import annotations

import logging
import re
import time
from typing import Any

from .config import Settings
from .database import Database
from .dujiao import DujiaoClient, DujiaoError, build_telegram_login_payload
from .telegram import TelegramAPI, TelegramError, inline_keyboard
from .utils import (
    compact_order_items,
    format_money,
    html_escape,
    json_loads,
    order_status_label,
    payment_status_label,
    pick_i18n,
    split_telegram_text,
    utc_now_iso,
)

LOG = logging.getLogger(__name__)


def _items(value: Any) -> list[dict[str, Any]]:
    if value is None:
        return []
    if isinstance(value, list):
        return [x for x in value if isinstance(x, dict)]
    if isinstance(value, dict):
        for key in ("data", "items", "list", "records"):
            item = value.get(key)
            if isinstance(item, list):
                return [x for x in item if isinstance(x, dict)]
        if isinstance(value.get("data"), dict):
            return _items(value["data"])
    return []


def _pagination(value: Any) -> dict[str, Any]:
    if isinstance(value, dict):
        if isinstance(value.get("pagination"), dict):
            return value["pagination"]
        if isinstance(value.get("meta"), dict):
            return value["meta"]
    return {}


def _first(*values: Any, default: Any = "") -> Any:
    for value in values:
        if value is not None and value != "":
            return value
    return default


def _int(value: Any, default: int = 0) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


class BotApp:
    def __init__(self, settings: Settings) -> None:
        self.settings = settings
        self.db = Database(settings.db_path)
        self.telegram = TelegramAPI(settings.telegram_bot_token)
        self.dujiao = DujiaoClient(settings.dujiao_base_url, settings.dujiao_api_prefix, settings.endpoints)
        self.bot_username: str | None = None

    def init(self) -> None:
        self.db.init()

    def start_polling(self) -> None:
        self.init()
        self.settings.validate_runtime()
        self.telegram.delete_webhook(drop_pending_updates=False)
        self.bot_username = self.telegram.get_me().get("username")
        LOG.info("polling started as @%s", self.bot_username)
        offset: int | None = None
        while True:
            try:
                updates = self.telegram.get_updates(offset, self.settings.poll_timeout_seconds)
                for update in updates:
                    offset = int(update["update_id"]) + 1
                    self.handle_update(update)
                self.db.cleanup_callbacks()
            except KeyboardInterrupt:
                raise
            except Exception:
                LOG.exception("polling loop failed")
                time.sleep(max(1.0, self.settings.poll_interval_seconds))

    def handle_update(self, update: dict[str, Any]) -> None:
        try:
            if "message" in update:
                self.handle_message(update["message"])
            elif "callback_query" in update:
                self.handle_callback(update["callback_query"])
            elif "my_chat_member" in update:
                self.handle_chat_member(update["my_chat_member"])
        except Exception:
            LOG.exception("failed to handle update: %s", update)

    def handle_chat_member(self, event: dict[str, Any]) -> None:
        user = event.get("from") or {}
        new_status = ((event.get("new_chat_member") or {}).get("status") or "").lower()
        if new_status in {"kicked", "left"} and user.get("id"):
            self.db.mark_blocked(int(user["id"]))

    def handle_message(self, message: dict[str, Any]) -> None:
        chat = message.get("chat") or {}
        tg_user = message.get("from") or {}
        if not chat.get("id") or not tg_user.get("id"):
            return
        chat_id = int(chat["id"])
        telegram_user_id = int(tg_user["id"])
        user = self.db.upsert_user(tg_user, chat_id, self.settings.default_locale)
        text = (message.get("text") or "").strip()
        if not text:
            self.send(chat_id, "目前只处理文本消息。", self.main_keyboard(telegram_user_id))
            return

        if text.startswith("/"):
            self.db.clear_session(telegram_user_id)
            command, _, arg = text.partition(" ")
            command = command.split("@", 1)[0].lower()
            if command == "/start":
                self.handle_start(user, tg_user, arg.strip())
            elif command in {"/menu", "/help"}:
                self.show_main(chat_id, telegram_user_id)
            elif command == "/products":
                self.show_categories(chat_id, telegram_user_id)
            elif command == "/orders":
                self.show_orders(chat_id, telegram_user_id)
            elif command == "/wallet":
                self.show_wallet(chat_id, telegram_user_id)
            elif command == "/affiliate":
                self.show_affiliate(chat_id, telegram_user_id)
            elif command == "/login":
                self.handle_telegram_login(chat_id, telegram_user_id, tg_user)
            elif command == "/guest":
                self.handle_telegram_login(chat_id, telegram_user_id, tg_user)
            elif command == "/admin":
                self.show_admin(chat_id, telegram_user_id)
            else:
                self.send(chat_id, "未知命令。", self.main_keyboard(telegram_user_id))
            return

        session = self.db.get_session(telegram_user_id)
        if session:
            self.handle_session_message(chat_id, telegram_user_id, text, session, message)
            return

        normalized = text.replace(" ", "")
        if normalized in {"商品", "购买", "商城"}:
            self.show_categories(chat_id, telegram_user_id)
        elif normalized in {"订单", "我的订单"}:
            self.show_orders(chat_id, telegram_user_id)
        elif normalized in {"钱包", "余额"}:
            self.show_wallet(chat_id, telegram_user_id)
        elif normalized in {"分销", "推广"}:
            self.show_affiliate(chat_id, telegram_user_id)
        elif normalized in {"客服", "帮助"}:
            self.show_help(chat_id, telegram_user_id)
        else:
            self.db.set_session(telegram_user_id, "search", {})
            self.handle_search(chat_id, telegram_user_id, text)

    def handle_session_message(
        self,
        chat_id: int,
        telegram_user_id: int,
        text: str,
        session: tuple[str, dict[str, Any]],
        message: dict[str, Any],
    ) -> None:
        state, data = session
        if text in {"取消", "/cancel"}:
            self.db.clear_session(telegram_user_id)
            self.send(chat_id, "已取消当前操作。", self.main_keyboard(telegram_user_id))
            return
        try:
            if state == "login_email":
                self.db.set_session(telegram_user_id, "login_password", {"email": text})
                self.send(chat_id, "请输入登录密码。发送“取消”可退出。")
            elif state == "login_password":
                self.try_delete_password(chat_id, message)
                self.handle_login_password(chat_id, telegram_user_id, data.get("email", ""), text)
            elif state == "register_email":
                try:
                    self.dujiao.send_verify_code(text, "register")
                    suffix = "验证码已发送，请输入密码。"
                except DujiaoError:
                    suffix = "请输入密码。若站点开启邮箱验证，请先在网页端获取验证码，下一步可填写。"
                self.db.set_session(telegram_user_id, "register_password", {"email": text})
                self.send(chat_id, suffix)
            elif state == "register_password":
                self.try_delete_password(chat_id, message)
                data["password"] = text
                self.db.set_session(telegram_user_id, "register_code", data)
                self.send(chat_id, "请输入邮箱验证码。")
            elif state == "register_code":
                self.handle_register_code(chat_id, telegram_user_id, data, text)
            elif state == "guest_email":
                self.db.set_session(telegram_user_id, "guest_password", {"email": text})
                self.send(chat_id, "请输入订单查询密码。后续游客订单会用它查询和交付。")
            elif state == "guest_password":
                self.db.set_guest_credentials(telegram_user_id, data.get("email", ""), text)
                self.db.clear_session(telegram_user_id)
                self.send(chat_id, "游客凭据已保存。", self.main_keyboard(telegram_user_id))
            elif state == "search":
                self.handle_search(chat_id, telegram_user_id, text)
            elif state == "quantity":
                self.handle_quantity_input(chat_id, telegram_user_id, data, text)
            elif state == "coupon":
                cart = data.get("cart") or {}
                cart["coupon_code"] = text
                self.db.clear_session(telegram_user_id)
                self.show_order_preview(chat_id, telegram_user_id, cart)
            elif state == "gift_card":
                self.handle_gift_card(chat_id, telegram_user_id, text)
            elif state == "manual_form":
                self.handle_manual_form_input(chat_id, telegram_user_id, data, text)
            elif state == "order_lookup_no":
                self.db.set_session(telegram_user_id, "order_lookup_email", {"order_no": text})
                self.send(chat_id, "请输入下单邮箱。")
            elif state == "order_lookup_email":
                data["email"] = text
                self.db.set_session(telegram_user_id, "order_lookup_password", data)
                self.send(chat_id, "请输入订单查询密码。")
            elif state == "order_lookup_password":
                self.db.clear_session(telegram_user_id)
                self.show_order_detail(chat_id, telegram_user_id, data.get("order_no", ""), guest_email=data.get("email"), guest_password=text)
            elif state == "withdraw_amount":
                self.db.set_session(telegram_user_id, "withdraw_channel", {"amount": text})
                self.send(chat_id, "请输入提现渠道，例如 Alipay / USDT。")
            elif state == "withdraw_channel":
                data["channel"] = text
                self.db.set_session(telegram_user_id, "withdraw_account", data)
                self.send(chat_id, "请输入收款账号。")
            elif state == "withdraw_account":
                self.handle_withdraw_account(chat_id, telegram_user_id, data, text)
            elif state == "broadcast_text":
                self.db.clear_session(telegram_user_id)
                self.show_broadcast_confirm(chat_id, telegram_user_id, text)
            else:
                self.db.clear_session(telegram_user_id)
                self.send(chat_id, "状态已重置。", self.main_keyboard(telegram_user_id))
        except DujiaoError as exc:
            self.send(chat_id, f"站点接口返回错误：{html_escape(exc)}", self.main_keyboard(telegram_user_id))
        except TelegramError as exc:
            LOG.warning("telegram error: %s", exc)

    def handle_callback(self, callback: dict[str, Any]) -> None:
        data = callback.get("data") or ""
        from_user = callback.get("from") or {}
        message = callback.get("message") or {}
        chat = message.get("chat") or {}
        callback_id = callback.get("id") or ""
        if not data.startswith("cb:") or not from_user.get("id") or not chat.get("id"):
            if callback_id:
                self.telegram.answer_callback_query(callback_id, "按钮已失效")
            return
        callback_row = self.db.get_callback(data[3:])
        telegram_user_id = int(from_user["id"])
        chat_id = int(chat["id"])
        self.db.upsert_user(from_user, chat_id, self.settings.default_locale)
        if not callback_row or callback_row["telegram_user_id"] != telegram_user_id:
            self.telegram.answer_callback_query(callback_id, "按钮已过期，请重新打开菜单。")
            return
        self.telegram.answer_callback_query(callback_id)
        action = callback_row["action"]
        payload = callback_row["payload"]
        try:
            self.dispatch_action(action, payload, chat_id, telegram_user_id, message)
        except DujiaoError as exc:
            self.send_or_edit(chat_id, message, f"站点接口返回错误：{html_escape(exc)}", self.main_keyboard(telegram_user_id))
        except TelegramError as exc:
            LOG.warning("telegram callback error: %s", exc)

    def dispatch_action(
        self,
        action: str,
        payload: dict[str, Any],
        chat_id: int,
        telegram_user_id: int,
        message: dict[str, Any],
    ) -> None:
        if action == "main":
            self.show_main(chat_id, telegram_user_id, message)
        elif action == "categories":
            self.show_categories(chat_id, telegram_user_id, message)
        elif action == "products":
            self.show_products(chat_id, telegram_user_id, category_id=payload.get("category_id"), page=_int(payload.get("page"), 1), keyword=payload.get("keyword"), message=message)
        elif action == "search":
            self.db.set_session(telegram_user_id, "search", {})
            self.send_or_edit(chat_id, message, "请输入商品关键词。发送“取消”可退出。")
        elif action == "product":
            self.show_product_detail(chat_id, telegram_user_id, str(payload.get("slug") or ""), message)
        elif action == "posts":
            self.show_posts(chat_id, telegram_user_id, page=_int(payload.get("page"), 1), message=message)
        elif action == "buy":
            self.start_purchase(chat_id, telegram_user_id, str(payload.get("slug") or ""), payload.get("sku_id"), message)
        elif action == "subscribe_stock":
            self.subscribe_stock(chat_id, telegram_user_id, str(payload.get("slug") or ""), str(payload.get("title") or ""), message)
        elif action == "unsubscribe_stock":
            self.unsubscribe_stock(chat_id, telegram_user_id, str(payload.get("slug") or ""), message)
        elif action == "sku":
            self.start_purchase(chat_id, telegram_user_id, str(payload.get("slug") or ""), payload.get("sku_id"), message)
        elif action == "qty":
            self.adjust_quantity(chat_id, telegram_user_id, payload, message)
        elif action == "quantity_input":
            self.db.set_session(telegram_user_id, "quantity", {"cart": payload.get("cart") or {}})
            self.send_or_edit(chat_id, message, "请输入购买数量。发送“取消”可退出。")
        elif action == "coupon":
            self.db.set_session(telegram_user_id, "coupon", {"cart": payload.get("cart") or {}})
            self.send_or_edit(chat_id, message, "请输入优惠码。发送“取消”可退出。")
        elif action == "checkout":
            self.before_checkout(chat_id, telegram_user_id, payload.get("cart") or {}, message)
        elif action == "create_order":
            self.create_order(chat_id, telegram_user_id, payload.get("cart") or {}, message)
        elif action == "login":
            self.ask_login_email(chat_id, telegram_user_id, message)
        elif action == "register":
            self.ask_register_email(chat_id, telegram_user_id, message)
        elif action == "guest":
            self.ask_guest_email(chat_id, telegram_user_id, message)
        elif action == "telegram_login":
            self.handle_telegram_login(chat_id, telegram_user_id, payload.get("telegram_user") or {}, message)
        elif action == "orders":
            self.show_orders(chat_id, telegram_user_id, page=_int(payload.get("page"), 1), message=message)
        elif action == "order":
            self.show_order_detail(chat_id, telegram_user_id, str(payload.get("order_no") or ""), message=message)
        elif action == "order_lookup":
            self.db.set_session(telegram_user_id, "order_lookup_no", {})
            self.send_or_edit(chat_id, message, "请输入订单号。发送“取消”可退出。")
        elif action == "cancel_order":
            self.cancel_order(chat_id, telegram_user_id, str(payload.get("order_no") or ""), message)
        elif action == "pay_channels":
            self.show_payment_channels(chat_id, telegram_user_id, str(payload.get("order_no") or ""), message)
        elif action == "pay":
            self.create_payment(chat_id, telegram_user_id, str(payload.get("order_no") or ""), _int(payload.get("channel_id")), message)
        elif action == "refresh_payment":
            self.refresh_payment(chat_id, telegram_user_id, payload, message)
        elif action == "wallet":
            self.show_wallet(chat_id, telegram_user_id, message)
        elif action == "wallet_tx":
            self.show_wallet_transactions(chat_id, telegram_user_id, page=_int(payload.get("page"), 1), message=message)
        elif action == "gift_card":
            self.db.set_session(telegram_user_id, "gift_card", {})
            self.send_or_edit(chat_id, message, "请输入礼品卡/充值卡代码。发送“取消”可退出。")
        elif action == "member":
            self.show_member(chat_id, telegram_user_id, message)
        elif action == "affiliate":
            self.show_affiliate(chat_id, telegram_user_id, message)
        elif action == "affiliate_open":
            self.open_affiliate(chat_id, telegram_user_id, message)
        elif action == "affiliate_commissions":
            self.show_affiliate_commissions(chat_id, telegram_user_id, message=message)
        elif action == "affiliate_withdraws":
            self.show_affiliate_withdraws(chat_id, telegram_user_id, message=message)
        elif action == "affiliate_withdraw":
            self.db.set_session(telegram_user_id, "withdraw_amount", {})
            self.send_or_edit(chat_id, message, "请输入提现金额。发送“取消”可退出。")
        elif action == "admin":
            self.show_admin(chat_id, telegram_user_id, message)
        elif action == "admin_status":
            self.show_admin_status(chat_id, telegram_user_id, message)
        elif action == "admin_notify_test":
            self.notify_admins("测试通知：kabot 管理员通知通道正常。")
            self.send_or_edit(chat_id, message, "测试通知已发送。", self.admin_keyboard(telegram_user_id))
        elif action == "admin_broadcast":
            self.db.set_session(telegram_user_id, "broadcast_text", {})
            self.send_or_edit(chat_id, message, "请输入要群发的 HTML 文本。发送“取消”可退出。")
        elif action == "admin_broadcast_confirm":
            self.run_broadcast(chat_id, telegram_user_id, str(payload.get("text") or ""), message)
        else:
            self.send_or_edit(chat_id, message, "未知操作。", self.main_keyboard(telegram_user_id))

    def send(self, chat_id: int, text: str, reply_markup: dict[str, Any] | None = None) -> None:
        parts = split_telegram_text(text)
        for part in parts[:-1]:
            self.telegram.send_message(chat_id, part)
        self.telegram.send_message(chat_id, parts[-1], reply_markup=reply_markup)

    def send_or_edit(self, chat_id: int, message: dict[str, Any], text: str, reply_markup: dict[str, Any] | None = None) -> None:
        message_id = message.get("message_id")
        if message_id and len(text) < 3900:
            try:
                self.telegram.edit_message_text(chat_id, int(message_id), text, reply_markup=reply_markup)
                return
            except TelegramError as exc:
                if "message is not modified" not in str(exc):
                    LOG.debug("edit failed, falling back to send: %s", exc)
        self.send(chat_id, text, reply_markup)

    def try_delete_password(self, chat_id: int, message: dict[str, Any]) -> None:
        message_id = message.get("message_id")
        if not message_id:
            return
        try:
            self.telegram.delete_message(chat_id, int(message_id))
        except TelegramError:
            pass

    def cb(self, user_id: int, label: str, action: str, payload: dict[str, Any] | None = None) -> dict[str, Any]:
        return {"text": label, "callback_data": "cb:" + self.db.create_callback(user_id, action, payload)}

    def url_button(self, label: str, url: str) -> dict[str, Any]:
        return {"text": label, "url": url}

    def main_keyboard(self, user_id: int) -> dict[str, Any]:
        rows = [
            [self.cb(user_id, "商品", "categories"), self.cb(user_id, "搜索", "search")],
            [self.cb(user_id, "订单", "orders"), self.cb(user_id, "钱包", "wallet")],
            [self.cb(user_id, "会员", "member"), self.cb(user_id, "分销", "affiliate")],
            [self.cb(user_id, "公告", "posts")],
        ]
        support = self.settings.support_url
        if support:
            rows.append([self.url_button("客服", support)])
        if self.is_admin(user_id):
            rows.append([self.cb(user_id, "管理", "admin")])
        return inline_keyboard(rows)

    def admin_keyboard(self, user_id: int) -> dict[str, Any]:
        return inline_keyboard([
            [self.cb(user_id, "状态", "admin_status"), self.cb(user_id, "群发", "admin_broadcast")],
            [self.cb(user_id, "测试通知", "admin_notify_test"), self.cb(user_id, "返回", "main")],
        ])

    def auth_keyboard(self, user_id: int, tg_user: dict[str, Any] | None = None) -> dict[str, Any]:
        rows: list[list[dict[str, Any]]] = []
        if self.settings.enable_synthetic_telegram_login:
            rows.insert(0, [self.cb(user_id, "Telegram 登录", "telegram_login", {"telegram_user": tg_user})])
        rows.append([self.cb(user_id, "返回", "main")])
        return inline_keyboard(rows)

    def is_admin(self, telegram_user_id: int) -> bool:
        return telegram_user_id in self.settings.admin_chat_ids

    def get_user_token(self, telegram_user_id: int) -> str | None:
        user = self.db.get_user(telegram_user_id)
        return (user or {}).get("user_token")

    def require_auth(self, chat_id: int, telegram_user_id: int, message: dict[str, Any] | None = None) -> str | None:
        token = self.get_user_token(telegram_user_id)
        if token:
            return token
        token, error = self.auto_telegram_login(telegram_user_id)
        if token:
            return token
        text = "这个功能需要 Telegram 登录。"
        if error:
            text += f"\n\n登录失败：{html_escape(error)}"
        if message:
            self.send_or_edit(chat_id, message, text, self.auth_keyboard(telegram_user_id))
        else:
            self.send(chat_id, text, self.auth_keyboard(telegram_user_id))
        return None

    def handle_start(self, user: dict[str, Any], tg_user: dict[str, Any], arg: str) -> None:
        chat_id = int(user["chat_id"])
        telegram_user_id = int(user["telegram_user_id"])
        if arg:
            code = arg
            if arg.startswith("aff_"):
                code = arg[4:]
            if code:
                self.db.set_affiliate_code(telegram_user_id, code)
                try:
                    self.dujiao.affiliate_click(code, f"tg:{telegram_user_id}", referrer="telegram_bot")
                except DujiaoError:
                    LOG.debug("affiliate click failed", exc_info=True)
        _, login_error = self.auto_telegram_login(telegram_user_id, tg_user)
        site_name = self.site_name()
        text = (
            f"欢迎使用 <b>{html_escape(site_name)}</b> Telegram Bot。\n\n"
            "你可以在这里浏览商品、创建订单、支付、查看交付内容，也可以查询钱包、会员和分销数据。"
        )
        if login_error:
            text += f"\n\nTelegram 自动登录暂未完成：{html_escape(login_error)}"
        self.send(chat_id, text, self.main_keyboard(telegram_user_id))

    def show_main(self, chat_id: int, telegram_user_id: int, message: dict[str, Any] | None = None) -> None:
        text = "请选择要操作的功能。"
        if message:
            self.send_or_edit(chat_id, message, text, self.main_keyboard(telegram_user_id))
        else:
            self.send(chat_id, text, self.main_keyboard(telegram_user_id))

    def show_help(self, chat_id: int, telegram_user_id: int) -> None:
        text = (
            "可用命令：\n"
            "/products 商品\n/orders 订单\n/wallet 钱包\n/affiliate 分销\n/login 登录\n/guest 游客查单\n"
            "也可以直接发送关键词搜索商品。"
        )
        self.send(chat_id, text, self.main_keyboard(telegram_user_id))

    def site_name(self) -> str:
        try:
            config = self.dujiao.get_site_config()
            return pick_i18n(_first(config.get("site_name"), config.get("name"), default="Dujiao-Next"), self.settings.default_locale)
        except Exception:
            return "Dujiao-Next"

    def show_categories(self, chat_id: int, telegram_user_id: int, message: dict[str, Any] | None = None) -> None:
        result = self.dujiao.categories(page_size=50)
        categories = _items(result)
        rows: list[list[dict[str, Any]]] = []
        current: list[dict[str, Any]] = []
        for category in categories:
            label = pick_i18n(category.get("name") or category.get("title"), self.settings.default_locale) or f"分类 {category.get('id')}"
            current.append(self.cb(telegram_user_id, label[:24], "products", {"category_id": category.get("id"), "page": 1}))
            if len(current) == 2:
                rows.append(current)
                current = []
        if current:
            rows.append(current)
        rows.append([self.cb(telegram_user_id, "全部商品", "products", {"page": 1}), self.cb(telegram_user_id, "搜索", "search")])
        rows.append([self.cb(telegram_user_id, "返回", "main")])
        text = "请选择商品分类。" if categories else "暂未读取到分类，可直接查看全部商品或搜索。"
        self.send_or_edit(chat_id, message or {}, text, inline_keyboard(rows))

    def show_products(
        self,
        chat_id: int,
        telegram_user_id: int,
        *,
        category_id: Any = None,
        page: int = 1,
        keyword: str | None = None,
        message: dict[str, Any] | None = None,
    ) -> None:
        result = self.dujiao.products(page=page, page_size=8, category_id=category_id, keyword=keyword)
        products = _items(result)
        pagination = _pagination(result)
        lines = ["<b>商品列表</b>"]
        if keyword:
            lines.append(f"关键词：{html_escape(keyword)}")
        rows: list[list[dict[str, Any]]] = []
        for product in products:
            title = self.product_title(product)
            price = self.product_price(product)
            stock = self.product_stock(product)
            lines.append(f"\n<b>{html_escape(title)}</b>\n价格：{html_escape(price)}\n库存：{html_escape(stock)}")
            slug = str(_first(product.get("slug"), product.get("id")))
            rows.append([self.cb(telegram_user_id, f"查看 {title[:16]}", "product", {"slug": slug})])
        nav: list[dict[str, Any]] = []
        if page > 1:
            nav.append(self.cb(telegram_user_id, "上一页", "products", {"category_id": category_id, "page": page - 1, "keyword": keyword}))
        has_next = bool(pagination.get("has_more") or pagination.get("next_page") or (_int(pagination.get("total_pages"), page) > page))
        if has_next or len(products) == 8:
            nav.append(self.cb(telegram_user_id, "下一页", "products", {"category_id": category_id, "page": page + 1, "keyword": keyword}))
        if nav:
            rows.append(nav)
        rows.append([self.cb(telegram_user_id, "分类", "categories"), self.cb(telegram_user_id, "搜索", "search"), self.cb(telegram_user_id, "返回", "main")])
        if not products:
            lines.append("\n没有找到商品。")
        self.send_or_edit(chat_id, message or {}, "\n".join(lines), inline_keyboard(rows))

    def handle_search(self, chat_id: int, telegram_user_id: int, keyword: str) -> None:
        self.db.clear_session(telegram_user_id)
        self.show_products(chat_id, telegram_user_id, keyword=keyword, page=1)

    def show_product_detail(self, chat_id: int, telegram_user_id: int, slug: str, message: dict[str, Any] | None = None) -> None:
        product = self.dujiao.product_detail(slug)
        title = self.product_title(product)
        sku_lines = self.format_sku_lines(product)
        description = pick_i18n(_first(product.get("short_description"), product.get("description"), product.get("content")), self.settings.default_locale)
        description = re.sub(r"<[^>]+>", "", description).strip()
        if len(description) > 800:
            description = description[:800] + "..."
        lines = [
            f"<b>{html_escape(title)}</b>",
            f"价格：{html_escape(self.product_price(product))}",
            f"库存：{html_escape(self.product_stock(product))}",
        ]
        if sku_lines:
            lines.append("\n规格：\n" + sku_lines)
        if description:
            lines.append("\n" + html_escape(description))
        rows: list[list[dict[str, Any]]] = []
        skus = self.product_skus(product)
        if skus:
            for sku in skus[:20]:
                label = self.sku_label(sku)
                rows.append([self.cb(telegram_user_id, f"购买 {label[:24]}", "buy", {"slug": slug, "sku_id": sku.get("id")})])
        else:
            rows.append([self.cb(telegram_user_id, "购买", "buy", {"slug": slug})])
        product_url = self.product_url(product)
        url_row = []
        if product_url:
            url_row.append(self.url_button("网页查看", product_url))
        url_row.append(self.cb(telegram_user_id, "补货通知", "subscribe_stock", {"slug": slug, "title": title}))
        rows.append(url_row)
        url_row = []
        url_row.append(self.cb(telegram_user_id, "返回商品", "products", {"page": 1}))
        rows.append(url_row)
        self.send_or_edit(chat_id, message or {}, "\n".join(lines), inline_keyboard(rows))

    def show_posts(self, chat_id: int, telegram_user_id: int, page: int = 1, message: dict[str, Any] | None = None) -> None:
        result = self.dujiao.posts(page=page, page_size=5)
        posts = _items(result)
        lines = ["<b>站点公告</b>"]
        rows: list[list[dict[str, Any]]] = []
        for post in posts:
            title = pick_i18n(_first(post.get("title"), post.get("name")), self.settings.default_locale)
            summary = pick_i18n(_first(post.get("summary"), post.get("excerpt"), post.get("content")), self.settings.default_locale)
            summary = re.sub(r"<[^>]+>", "", summary).strip()
            if len(summary) > 180:
                summary = summary[:180] + "..."
            lines.append(f"\n<b>{html_escape(title)}</b>")
            if summary:
                lines.append(html_escape(summary))
            url = _first(post.get("url"), post.get("link"))
            if not url and self.settings.shop_base_url and post.get("slug"):
                url = f"{self.settings.shop_base_url}/posts/{post.get('slug')}"
            if url:
                rows.append([self.url_button(f"查看 {title[:16]}", str(url))])
        if not posts:
            lines.append("\n暂无公告。")
        rows.append([self.cb(telegram_user_id, "返回", "main")])
        self.send_or_edit(chat_id, message or {}, "\n".join(lines), inline_keyboard(rows))

    def subscribe_stock(self, chat_id: int, telegram_user_id: int, slug: str, title: str, message: dict[str, Any] | None = None) -> None:
        self.db.subscribe_stock(telegram_user_id, slug, title)
        rows = [[
            self.cb(telegram_user_id, "取消订阅", "unsubscribe_stock", {"slug": slug}),
            self.cb(telegram_user_id, "返回", "product", {"slug": slug}),
        ]]
        self.send_or_edit(chat_id, message or {}, f"已订阅补货通知：{html_escape(title or slug)}", inline_keyboard(rows))

    def unsubscribe_stock(self, chat_id: int, telegram_user_id: int, slug: str, message: dict[str, Any] | None = None) -> None:
        self.db.unsubscribe_stock(telegram_user_id, slug)
        self.send_or_edit(chat_id, message or {}, "已取消该商品的补货通知。", self.main_keyboard(telegram_user_id))

    def start_purchase(self, chat_id: int, telegram_user_id: int, slug: str, sku_id: Any, message: dict[str, Any] | None = None) -> None:
        product = self.dujiao.product_detail(slug)
        skus = self.product_skus(product)
        if sku_id is None and len(skus) > 1:
            rows = [[self.cb(telegram_user_id, self.sku_label(sku), "sku", {"slug": slug, "sku_id": sku.get("id")})] for sku in skus[:20]]
            rows.append([self.cb(telegram_user_id, "返回", "product", {"slug": slug})])
            self.send_or_edit(chat_id, message or {}, "请选择规格。", inline_keyboard(rows))
            return
        selected_sku = self.find_sku(skus, sku_id) if skus else None
        cart = {
            "product_slug": slug,
            "product_id": product.get("id"),
            "product_title": self.product_title(product),
            "sku_id": selected_sku.get("id") if selected_sku else None,
            "sku_title": self.sku_label(selected_sku) if selected_sku else "",
            "quantity": 1,
            "manual_form_fields": self.manual_form_fields(product),
            "form_values": {},
        }
        self.show_cart(chat_id, telegram_user_id, cart, message)

    def show_cart(self, chat_id: int, telegram_user_id: int, cart: dict[str, Any], message: dict[str, Any] | None = None) -> None:
        lines = [
            "<b>订单草稿</b>",
            f"商品：{html_escape(cart.get('product_title'))}",
        ]
        if cart.get("sku_title"):
            lines.append(f"规格：{html_escape(cart.get('sku_title'))}")
        lines.append(f"数量：{cart.get('quantity', 1)}")
        if cart.get("coupon_code"):
            lines.append(f"优惠码：{html_escape(cart.get('coupon_code'))}")
        rows = [
            [
                self.cb(telegram_user_id, "-1", "qty", {"cart": cart, "delta": -1}),
                self.cb(telegram_user_id, "+1", "qty", {"cart": cart, "delta": 1}),
                self.cb(telegram_user_id, "输入数量", "quantity_input", {"cart": cart}),
            ],
            [self.cb(telegram_user_id, "优惠码", "coupon", {"cart": cart}), self.cb(telegram_user_id, "继续", "checkout", {"cart": cart})],
            [self.cb(telegram_user_id, "返回", "product", {"slug": cart.get("product_slug")})],
        ]
        self.send_or_edit(chat_id, message or {}, "\n".join(lines), inline_keyboard(rows))

    def adjust_quantity(self, chat_id: int, telegram_user_id: int, payload: dict[str, Any], message: dict[str, Any]) -> None:
        cart = payload.get("cart") or {}
        cart["quantity"] = max(1, _int(cart.get("quantity"), 1) + _int(payload.get("delta"), 0))
        self.show_cart(chat_id, telegram_user_id, cart, message)

    def handle_quantity_input(self, chat_id: int, telegram_user_id: int, data: dict[str, Any], text: str) -> None:
        qty = _int(text, 0)
        if qty < 1:
            self.send(chat_id, "数量必须大于 0，请重新输入。")
            return
        cart = data.get("cart") or {}
        cart["quantity"] = qty
        self.db.clear_session(telegram_user_id)
        self.show_cart(chat_id, telegram_user_id, cart)

    def before_checkout(self, chat_id: int, telegram_user_id: int, cart: dict[str, Any], message: dict[str, Any] | None = None) -> None:
        fields = cart.get("manual_form_fields") or []
        values = cart.get("form_values") or {}
        missing = [field for field in fields if field.get("required") and not values.get(field["key"])]
        if missing:
            first = missing[0]
            self.db.set_session(telegram_user_id, "manual_form", {"cart": cart, "index": 0, "fields": missing})
            self.send_or_edit(chat_id, message or {}, f"请填写交付信息：{html_escape(first.get('label') or first.get('key'))}")
            return
        self.show_order_preview(chat_id, telegram_user_id, cart, message)

    def handle_manual_form_input(self, chat_id: int, telegram_user_id: int, data: dict[str, Any], text: str) -> None:
        cart = data.get("cart") or {}
        fields = data.get("fields") or []
        index = _int(data.get("index"), 0)
        if index < len(fields):
            field = fields[index]
            values = cart.setdefault("form_values", {})
            values[field["key"]] = text
            index += 1
        if index >= len(fields):
            self.db.clear_session(telegram_user_id)
            self.show_order_preview(chat_id, telegram_user_id, cart)
            return
        data["cart"] = cart
        data["index"] = index
        self.db.set_session(telegram_user_id, "manual_form", data)
        field = fields[index]
        self.send(chat_id, f"请填写交付信息：{html_escape(field.get('label') or field.get('key'))}")

    def show_order_preview(self, chat_id: int, telegram_user_id: int, cart: dict[str, Any], message: dict[str, Any] | None = None) -> None:
        user = self.db.get_user(telegram_user_id) or {}
        token = user.get("user_token")
        if not token:
            token = self.require_auth(chat_id, telegram_user_id, message)
        if not token:
            return
        user = self.db.get_user(telegram_user_id) or user
        body = self.order_payload(cart, user, include_guest=False)
        try:
            preview = self.dujiao.preview_order(token, body)
            lines = self.format_preview(preview, cart)
        except DujiaoError as exc:
            lines = [
                "<b>订单预览</b>",
                f"商品：{html_escape(cart.get('product_title'))}",
                f"数量：{cart.get('quantity', 1)}",
                f"站点预览接口未通过：{html_escape(exc)}",
            ]
        rows = [
            [self.cb(telegram_user_id, "创建订单", "create_order", {"cart": cart})],
            [self.cb(telegram_user_id, "修改数量", "quantity_input", {"cart": cart}), self.cb(telegram_user_id, "优惠码", "coupon", {"cart": cart})],
            [self.cb(telegram_user_id, "返回", "product", {"slug": cart.get("product_slug")})],
        ]
        self.send_or_edit(chat_id, message or {}, "\n".join(lines), inline_keyboard(rows))

    def create_order(self, chat_id: int, telegram_user_id: int, cart: dict[str, Any], message: dict[str, Any] | None = None) -> None:
        user = self.db.get_user(telegram_user_id) or {}
        token = user.get("user_token")
        if not token:
            token = self.require_auth(chat_id, telegram_user_id, message)
        if not token:
            return
        user = self.db.get_user(telegram_user_id) or user
        body = self.order_payload(cart, user, include_guest=False)
        order = self.dujiao.create_order(token, body)
        order_no = str(_first(order.get("order_no"), order.get("no"), order.get("trade_no")))
        status = _first(order.get("status"), order.get("order_status"), default="pending_payment")
        self.db.cache_order(
            order_no,
            telegram_user_id=telegram_user_id,
            status=status,
            payload=order,
            guest_email=user.get("guest_email"),
            order_password=user.get("guest_order_password"),
        )
        self.notify_admins(
            "新订单已创建\n"
            f"订单号：<code>{html_escape(order_no)}</code>\n"
            f"用户：<code>{telegram_user_id}</code>\n"
            f"商品：{html_escape(cart.get('product_title'))} x{cart.get('quantity', 1)}"
        )
        text = f"订单已创建：<code>{html_escape(order_no)}</code>\n状态：{html_escape(order_status_label(status))}"
        rows = [
            [self.cb(telegram_user_id, "去支付", "pay_channels", {"order_no": order_no})],
            [self.cb(telegram_user_id, "订单详情", "order", {"order_no": order_no}), self.cb(telegram_user_id, "返回", "main")],
        ]
        self.send_or_edit(chat_id, message or {}, text, inline_keyboard(rows))

    def show_payment_channels(self, chat_id: int, telegram_user_id: int, order_no: str, message: dict[str, Any] | None = None) -> None:
        channels = self.payment_channels()
        rows = [[self.cb(telegram_user_id, str(ch.get("name") or ch.get("label") or ch.get("id")), "pay", {"order_no": order_no, "channel_id": ch.get("id")})] for ch in channels]
        rows.append([self.cb(telegram_user_id, "订单详情", "order", {"order_no": order_no})])
        self.send_or_edit(chat_id, message or {}, "请选择支付渠道。", inline_keyboard(rows))

    def create_payment(self, chat_id: int, telegram_user_id: int, order_no: str, channel_id: int, message: dict[str, Any] | None = None) -> None:
        user = self.db.get_user(telegram_user_id) or {}
        token = user.get("user_token")
        if not token:
            token = self.require_auth(chat_id, telegram_user_id, message)
        if not token:
            return
        payment = self.dujiao.create_payment(token, order_no, channel_id)
        payment_id = _int(_first(payment.get("payment_id"), payment.get("id")), 0)
        self.db.cache_order(order_no, telegram_user_id=telegram_user_id, status=None, payload={"payment": payment}, payment_id=payment_id, channel_id=channel_id)
        text, rows = self.payment_view(telegram_user_id, order_no, payment)
        qr = _first(payment.get("qr_code_url"), payment.get("qrcode_url"), payment.get("qr_url"), payment.get("qr_code"))
        if qr and str(qr).startswith(("http://", "https://")):
            self.telegram.send_photo(chat_id, str(qr), caption=text, reply_markup=inline_keyboard(rows))
        else:
            self.send_or_edit(chat_id, message or {}, text, inline_keyboard(rows))

    def refresh_payment(self, chat_id: int, telegram_user_id: int, payload: dict[str, Any], message: dict[str, Any] | None = None) -> None:
        order_no = str(payload.get("order_no") or "")
        user = self.db.get_user(telegram_user_id) or {}
        token = user.get("user_token")
        payment_id = _int(payload.get("payment_id"), 0)
        if not token:
            token = self.require_auth(chat_id, telegram_user_id, message)
        if not token:
            return
        payment = self.dujiao.capture_payment(token, payment_id) if payment_id else self.dujiao.latest_payment(token, order_no)
        order = self.dujiao.order_detail(token, order_no)
        status = _first(order.get("status"), order.get("order_status"))
        self.db.cache_order(order_no, telegram_user_id=telegram_user_id, status=status, payload=order, payment_id=payment_id or None)
        if str(status) in {"paid", "processing", "fulfilling", "partially_delivered", "delivered", "completed"}:
            self.notify_admins(f"订单支付状态更新\n订单号：<code>{html_escape(order_no)}</code>\n状态：{html_escape(order_status_label(status))}")
            self.show_order_detail(chat_id, telegram_user_id, order_no, message=message)
            return
        text, rows = self.payment_view(telegram_user_id, order_no, payment)
        self.send_or_edit(chat_id, message or {}, text, inline_keyboard(rows))

    def payment_view(self, telegram_user_id: int, order_no: str, payment: dict[str, Any]) -> tuple[str, list[list[dict[str, Any]]]]:
        payment_id = _int(_first(payment.get("payment_id"), payment.get("id")), 0)
        status = _first(payment.get("status"), payment.get("payment_status"), default="pending")
        pay_url = _first(payment.get("pay_url"), payment.get("payment_url"), payment.get("checkout_url"), payment.get("url"))
        amount = _first(payment.get("amount"), payment.get("actual_amount"), payment.get("pay_amount"))
        lines = [
            "<b>支付信息</b>",
            f"订单号：<code>{html_escape(order_no)}</code>",
            f"状态：{html_escape(payment_status_label(status))}",
        ]
        if amount:
            lines.append(f"金额：{html_escape(amount)}")
        if payment.get("expired_at") or payment.get("expires_at"):
            lines.append(f"过期时间：{html_escape(_first(payment.get('expired_at'), payment.get('expires_at')))}")
        if pay_url:
            lines.append(f"支付链接：{html_escape(pay_url)}")
        rows: list[list[dict[str, Any]]] = []
        if pay_url and str(pay_url).startswith(("http://", "https://")):
            rows.append([self.url_button("打开支付", str(pay_url))])
        rows.append([self.cb(telegram_user_id, "刷新支付", "refresh_payment", {"order_no": order_no, "payment_id": payment_id})])
        rows.append([self.cb(telegram_user_id, "订单详情", "order", {"order_no": order_no})])
        return "\n".join(lines), rows

    def show_orders(self, chat_id: int, telegram_user_id: int, page: int = 1, message: dict[str, Any] | None = None) -> None:
        user = self.db.get_user(telegram_user_id) or {}
        token = user.get("user_token")
        rows: list[list[dict[str, Any]]] = []
        lines = ["<b>我的订单</b>"]
        if not token:
            token = self.require_auth(chat_id, telegram_user_id, message)
        if not token:
            return
        result = self.dujiao.orders(token, page=page, page_size=8)
        orders = _items(result)
        for order in orders:
            order_no = str(_first(order.get("order_no"), order.get("no"), order.get("trade_no")))
            status = _first(order.get("status"), order.get("order_status"))
            amount = _first(order.get("total_amount"), order.get("amount"), order.get("pay_amount"))
            lines.append(f"\n<code>{html_escape(order_no)}</code>\n状态：{html_escape(order_status_label(status))} 金额：{html_escape(amount)}")
            rows.append([self.cb(telegram_user_id, f"查看 {order_no[-8:]}", "order", {"order_no": order_no})])
            self.db.cache_order(order_no, telegram_user_id=telegram_user_id, status=status, payload=order, guest_email=user.get("guest_email"), order_password=user.get("guest_order_password"))
        if page > 1:
            rows.append([self.cb(telegram_user_id, "上一页", "orders", {"page": page - 1}), self.cb(telegram_user_id, "下一页", "orders", {"page": page + 1})])
        elif len(orders) >= 8:
            rows.append([self.cb(telegram_user_id, "下一页", "orders", {"page": page + 1})])
        rows.append([self.cb(telegram_user_id, "按订单号查询", "order_lookup"), self.cb(telegram_user_id, "返回", "main")])
        if not orders:
            lines.append("\n没有订单记录。")
        self.send_or_edit(chat_id, message or {}, "\n".join(lines), inline_keyboard(rows))

    def show_order_detail(
        self,
        chat_id: int,
        telegram_user_id: int,
        order_no: str,
        *,
        message: dict[str, Any] | None = None,
        guest_email: str | None = None,
        guest_password: str | None = None,
    ) -> None:
        user = self.db.get_user(telegram_user_id) or {}
        token = user.get("user_token")
        if not token:
            token = self.require_auth(chat_id, telegram_user_id, message)
        if not token:
            return
        order = self.dujiao.order_detail(token, order_no)
        status = _first(order.get("status"), order.get("order_status"))
        amount = _first(order.get("total_amount"), order.get("amount"), order.get("pay_amount"))
        lines = [
            "<b>订单详情</b>",
            f"订单号：<code>{html_escape(order_no)}</code>",
            f"状态：{html_escape(order_status_label(status))}",
        ]
        if amount:
            lines.append(f"金额：{html_escape(amount)}")
        items = _items(order.get("items") or order.get("order_items"))
        if items:
            lines.append("\n商品：\n" + compact_order_items(items, self.settings.default_locale))
        fulfillment_text = self.fulfillment_text(order)
        if fulfillment_text:
            lines.append("\n<b>交付内容</b>\n" + fulfillment_text)
        elif str(status) in {"paid", "processing", "fulfilling"}:
            lines.append("\n交付状态：已支付，等待系统或管理员交付。")
        rows: list[list[dict[str, Any]]] = []
        if str(status) in {"pending_payment", "created", "unpaid", "pending"}:
            rows.append([self.cb(telegram_user_id, "去支付", "pay_channels", {"order_no": order_no}), self.cb(telegram_user_id, "取消订单", "cancel_order", {"order_no": order_no})])
        rows.append([self.cb(telegram_user_id, "刷新", "order", {"order_no": order_no}), self.cb(telegram_user_id, "订单列表", "orders")])
        self.db.cache_order(order_no, telegram_user_id=telegram_user_id, status=status, payload=order, guest_email=user.get("guest_email"), order_password=user.get("guest_order_password"))
        self.send_or_edit(chat_id, message or {}, "\n".join(lines), inline_keyboard(rows))

    def cancel_order(self, chat_id: int, telegram_user_id: int, order_no: str, message: dict[str, Any] | None = None) -> None:
        user = self.db.get_user(telegram_user_id) or {}
        token = user.get("user_token")
        if not token:
            token = self.require_auth(chat_id, telegram_user_id, message)
        if not token:
            return
        result = self.dujiao.cancel_order(token, order_no)
        status = _first(result.get("status"), result.get("order_status"), default="canceled")
        self.db.cache_order(order_no, telegram_user_id=telegram_user_id, status=status, payload=result)
        self.send_or_edit(chat_id, message or {}, f"订单已取消：<code>{html_escape(order_no)}</code>", self.main_keyboard(telegram_user_id))

    def ask_login_email(self, chat_id: int, telegram_user_id: int, message: dict[str, Any] | None = None) -> None:
        self.db.set_session(telegram_user_id, "login_email", {})
        self.send_or_edit(chat_id, message or {}, "请输入站点账号邮箱。发送“取消”可退出。")

    def ask_register_email(self, chat_id: int, telegram_user_id: int, message: dict[str, Any] | None = None) -> None:
        self.db.set_session(telegram_user_id, "register_email", {})
        self.send_or_edit(chat_id, message or {}, "请输入注册邮箱。发送“取消”可退出。")

    def ask_guest_email(self, chat_id: int, telegram_user_id: int, message: dict[str, Any] | None = None) -> None:
        self.db.set_session(telegram_user_id, "guest_email", {})
        self.send_or_edit(chat_id, message or {}, "请输入游客下单邮箱。发送“取消”可退出。")

    def handle_login_password(self, chat_id: int, telegram_user_id: int, email: str, password: str) -> None:
        result = self.dujiao.login(email, password)
        token = str(_first(result.get("token"), result.get("access_token")))
        if not token:
            raise DujiaoError("登录成功但响应中没有 token")
        self.db.set_user_token(telegram_user_id, token, result.get("expires_at") or result.get("token_expires_at"), result.get("user") or result)
        self.db.clear_session(telegram_user_id)
        self.send(chat_id, "登录成功。", self.main_keyboard(telegram_user_id))

    def handle_register_code(self, chat_id: int, telegram_user_id: int, data: dict[str, Any], code: str) -> None:
        result = self.dujiao.register(data.get("email", ""), data.get("password", ""), code)
        token = str(_first(result.get("token"), result.get("access_token")))
        if token:
            self.db.set_user_token(telegram_user_id, token, result.get("expires_at") or result.get("token_expires_at"), result.get("user") or result)
        self.db.clear_session(telegram_user_id)
        self.send(chat_id, "注册完成。", self.main_keyboard(telegram_user_id))

    def handle_telegram_login(self, chat_id: int, telegram_user_id: int, tg_user: dict[str, Any], message: dict[str, Any] | None = None) -> None:
        token, error = self.auto_telegram_login(telegram_user_id, tg_user)
        if not token:
            raise DujiaoError(error or "Telegram 登录失败")
        self.send_or_edit(chat_id, message or {}, "Telegram 登录成功。", self.main_keyboard(telegram_user_id))

    def auto_telegram_login(self, telegram_user_id: int, tg_user: dict[str, Any] | None = None) -> tuple[str | None, str | None]:
        if not self.settings.enable_synthetic_telegram_login:
            return None, "当前未启用 Telegram 登录"
        user = self.db.get_user(telegram_user_id) or {}
        if not tg_user:
            tg_user = {
                "id": telegram_user_id,
                "first_name": user.get("first_name") or "",
                "last_name": user.get("last_name") or "",
                "username": user.get("username") or "",
            }
        try:
            payload = build_telegram_login_payload(tg_user, self.settings.telegram_login_bot_token)
            result = self.dujiao.telegram_login(payload)
            token = str(_first(result.get("token"), result.get("access_token")))
            if not token:
                return None, "站点没有返回 token"
            self.db.set_user_token(
                telegram_user_id,
                token,
                result.get("expires_at") or result.get("token_expires_at"),
                result.get("user") or result,
            )
            return token, None
        except DujiaoError as exc:
            return None, str(exc)

    def show_wallet(self, chat_id: int, telegram_user_id: int, message: dict[str, Any] | None = None) -> None:
        token = self.require_auth(chat_id, telegram_user_id, message)
        if not token:
            return
        wallet = self.dujiao.wallet_profile(token)
        balance = _first(wallet.get("balance"), wallet.get("available_balance"), wallet.get("amount"), default="0.00")
        frozen = _first(wallet.get("frozen_balance"), wallet.get("locked_balance"))
        currency = _first(wallet.get("currency"), default="")
        lines = ["<b>钱包</b>", f"可用余额：{html_escape(format_money(balance, currency))}"]
        if frozen:
            lines.append(f"冻结余额：{html_escape(format_money(frozen, currency))}")
        rows = [
            [self.cb(telegram_user_id, "交易记录", "wallet_tx"), self.cb(telegram_user_id, "兑换礼品卡", "gift_card")],
            [self.cb(telegram_user_id, "会员", "member"), self.cb(telegram_user_id, "返回", "main")],
        ]
        if self.settings.shop_base_url:
            rows.insert(1, [self.url_button("网页充值", f"{self.settings.shop_base_url}/user/wallet")])
        self.send_or_edit(chat_id, message or {}, "\n".join(lines), inline_keyboard(rows))

    def show_wallet_transactions(self, chat_id: int, telegram_user_id: int, page: int = 1, message: dict[str, Any] | None = None) -> None:
        token = self.require_auth(chat_id, telegram_user_id, message)
        if not token:
            return
        result = self.dujiao.wallet_transactions(token, page=page, page_size=10)
        txs = _items(result)
        lines = ["<b>钱包交易记录</b>"]
        for tx in txs:
            amount = _first(tx.get("amount"), tx.get("change_amount"))
            typ = _first(tx.get("type"), tx.get("direction"), tx.get("title"))
            created = _first(tx.get("created_at"), tx.get("time"))
            lines.append(f"\n{html_escape(created)}\n{html_escape(typ)}：{html_escape(amount)}")
        if not txs:
            lines.append("\n暂无交易记录。")
        rows = [[self.cb(telegram_user_id, "返回钱包", "wallet")]]
        self.send_or_edit(chat_id, message or {}, "\n".join(lines), inline_keyboard(rows))

    def handle_gift_card(self, chat_id: int, telegram_user_id: int, code: str) -> None:
        token = self.require_auth(chat_id, telegram_user_id)
        if not token:
            return
        result = self.dujiao.redeem_gift_card(token, code)
        self.db.clear_session(telegram_user_id)
        amount = _first(result.get("amount"), result.get("balance"), result.get("value"), default="")
        text = "礼品卡兑换成功。" + (f"\n金额：{html_escape(amount)}" if amount else "")
        self.send(chat_id, text, self.main_keyboard(telegram_user_id))

    def show_member(self, chat_id: int, telegram_user_id: int, message: dict[str, Any] | None = None) -> None:
        token = self.require_auth(chat_id, telegram_user_id, message)
        if not token:
            return
        me = self.dujiao.me(token)
        level = me.get("member_level") or me.get("level") or {}
        if isinstance(level, dict):
            level_name = pick_i18n(level.get("name") or level.get("title"), self.settings.default_locale)
        else:
            level_name = str(level or "普通会员")
        lines = [
            "<b>会员资料</b>",
            f"昵称：{html_escape(_first(me.get('nickname'), me.get('name'), me.get('email')))}",
            f"等级：{html_escape(level_name)}",
        ]
        for label, key in [("累计消费", "total_spent"), ("累计充值", "total_recharged"), ("折扣", "discount_rate")]:
            if me.get(key) is not None:
                lines.append(f"{label}：{html_escape(me.get(key))}")
        self.send_or_edit(chat_id, message or {}, "\n".join(lines), inline_keyboard([[self.cb(telegram_user_id, "钱包", "wallet"), self.cb(telegram_user_id, "返回", "main")]]))

    def show_affiliate(self, chat_id: int, telegram_user_id: int, message: dict[str, Any] | None = None) -> None:
        token = self.require_auth(chat_id, telegram_user_id, message)
        if not token:
            return
        try:
            dashboard = self.dujiao.affiliate_dashboard(token)
        except DujiaoError as exc:
            rows = [[self.cb(telegram_user_id, "开通推广员", "affiliate_open")], [self.cb(telegram_user_id, "返回", "main")]]
            self.send_or_edit(chat_id, message or {}, f"分销尚未开通或接口不可用：{html_escape(exc)}", inline_keyboard(rows))
            return
        code = str(_first(dashboard.get("affiliate_code"), dashboard.get("code"), dashboard.get("referral_code")))
        if code:
            self.db.set_affiliate_code(telegram_user_id, code)
        lines = [
            "<b>分销推广</b>",
            f"推广码：<code>{html_escape(code)}</code>" if code else "推广码：未获取",
        ]
        for label, key in [("可提现佣金", "available_commission"), ("累计佣金", "total_commission"), ("订单数", "order_count"), ("点击数", "click_count")]:
            if dashboard.get(key) is not None:
                lines.append(f"{label}：{html_escape(dashboard.get(key))}")
        if code and self.bot_username:
            lines.append(f"\nBot 推广链接：https://t.me/{self.bot_username}?start=aff_{html_escape(code)}")
        rows = [
            [self.cb(telegram_user_id, "佣金记录", "affiliate_commissions"), self.cb(telegram_user_id, "提现记录", "affiliate_withdraws")],
            [self.cb(telegram_user_id, "申请提现", "affiliate_withdraw"), self.cb(telegram_user_id, "返回", "main")],
        ]
        self.send_or_edit(chat_id, message or {}, "\n".join(lines), inline_keyboard(rows))

    def open_affiliate(self, chat_id: int, telegram_user_id: int, message: dict[str, Any] | None = None) -> None:
        token = self.require_auth(chat_id, telegram_user_id, message)
        if not token:
            return
        self.dujiao.affiliate_open(token)
        self.show_affiliate(chat_id, telegram_user_id, message)

    def show_affiliate_commissions(self, chat_id: int, telegram_user_id: int, page: int = 1, message: dict[str, Any] | None = None) -> None:
        token = self.require_auth(chat_id, telegram_user_id, message)
        if not token:
            return
        result = self.dujiao.affiliate_commissions(token, page=page)
        rows_data = _items(result)
        lines = ["<b>佣金记录</b>"]
        for row in rows_data:
            lines.append(f"\n订单：{html_escape(_first(row.get('order_no'), row.get('source_no')))}\n金额：{html_escape(_first(row.get('amount'), row.get('commission_amount')))} 状态：{html_escape(_first(row.get('status'), default=''))}")
        if not rows_data:
            lines.append("\n暂无佣金记录。")
        self.send_or_edit(chat_id, message or {}, "\n".join(lines), inline_keyboard([[self.cb(telegram_user_id, "返回分销", "affiliate")]]))

    def show_affiliate_withdraws(self, chat_id: int, telegram_user_id: int, page: int = 1, message: dict[str, Any] | None = None) -> None:
        token = self.require_auth(chat_id, telegram_user_id, message)
        if not token:
            return
        result = self.dujiao.affiliate_withdraws(token, page=page)
        rows_data = _items(result)
        lines = ["<b>提现记录</b>"]
        for row in rows_data:
            lines.append(f"\n金额：{html_escape(_first(row.get('amount'), row.get('withdraw_amount')))}\n渠道：{html_escape(row.get('channel'))} 状态：{html_escape(row.get('status'))}")
        if not rows_data:
            lines.append("\n暂无提现记录。")
        self.send_or_edit(chat_id, message or {}, "\n".join(lines), inline_keyboard([[self.cb(telegram_user_id, "申请提现", "affiliate_withdraw"), self.cb(telegram_user_id, "返回分销", "affiliate")]]))

    def handle_withdraw_account(self, chat_id: int, telegram_user_id: int, data: dict[str, Any], account: str) -> None:
        token = self.require_auth(chat_id, telegram_user_id)
        if not token:
            return
        result = self.dujiao.affiliate_withdraw(token, data.get("amount", ""), data.get("channel", ""), account)
        self.db.clear_session(telegram_user_id)
        status = _first(result.get("status"), default="submitted")
        self.send(chat_id, f"提现申请已提交，状态：{html_escape(status)}", self.main_keyboard(telegram_user_id))

    def show_admin(self, chat_id: int, telegram_user_id: int, message: dict[str, Any] | None = None) -> None:
        if not self.is_admin(telegram_user_id):
            self.send_or_edit(chat_id, message or {}, "无管理员权限。", self.main_keyboard(telegram_user_id))
            return
        self.send_or_edit(chat_id, message or {}, "管理员功能。", self.admin_keyboard(telegram_user_id))

    def show_admin_status(self, chat_id: int, telegram_user_id: int, message: dict[str, Any] | None = None) -> None:
        if not self.is_admin(telegram_user_id):
            self.send_or_edit(chat_id, message or {}, "无管理员权限。", self.main_keyboard(telegram_user_id))
            return
        users = self.db.list_users()
        try:
            me = self.telegram.get_me()
            bot_status = f"@{me.get('username')}"
        except Exception:
            bot_status = "Telegram API 未连通"
        try:
            site = self.site_name()
            site_status = f"已连接 {site}"
        except Exception as exc:
            site_status = f"站点接口异常：{exc}"
        text = (
            "<b>kabot 状态</b>\n"
            f"Bot：{html_escape(bot_status)}\n"
            f"站点：{html_escape(site_status)}\n"
            f"用户数：{len(users)}\n"
            f"数据库：{html_escape(self.settings.db_path)}\n"
            f"时间：{html_escape(utc_now_iso())}"
        )
        self.send_or_edit(chat_id, message or {}, text, self.admin_keyboard(telegram_user_id))

    def show_broadcast_confirm(self, chat_id: int, telegram_user_id: int, text: str) -> None:
        if not self.is_admin(telegram_user_id):
            self.send(chat_id, "无管理员权限。", self.main_keyboard(telegram_user_id))
            return
        count = len(self.db.list_users())
        preview = text[:1200]
        body = f"<b>群发预览</b>\n目标用户：{count}\n\n{preview}"
        rows = [[self.cb(telegram_user_id, "确认群发", "admin_broadcast_confirm", {"text": text})], [self.cb(telegram_user_id, "取消", "admin")]]
        self.send(chat_id, body, inline_keyboard(rows))

    def run_broadcast(self, chat_id: int, telegram_user_id: int, text: str, message: dict[str, Any] | None = None) -> None:
        if not self.is_admin(telegram_user_id):
            self.send_or_edit(chat_id, message or {}, "无管理员权限。", self.main_keyboard(telegram_user_id))
            return
        result = self.broadcast_message(text, created_by=telegram_user_id)
        self.send_or_edit(
            chat_id,
            message or {},
            f"群发完成。\n成功：{result['success']}\n失败：{result['failure']}",
            self.admin_keyboard(telegram_user_id),
        )

    def broadcast_text(self, text: str, created_by: int | None = None) -> dict[str, int]:
        return self.broadcast_message(text, created_by=created_by)

    def broadcast_message(
        self,
        text: str,
        *,
        created_by: int | None = None,
        attachment: dict[str, str] | None = None,
    ) -> dict[str, int]:
        broadcast_id = self.db.create_broadcast(created_by, text, attachment_json=attachment)
        users = self.db.list_users()
        success = 0
        failure = 0
        errors = []
        self.db.update_broadcast(broadcast_id, status="running", total_count=len(users))
        for user in users:
            try:
                if attachment and attachment.get("type") == "photo":
                    self.telegram.send_photo(int(user["chat_id"]), attachment["url"], caption=text)
                elif attachment and attachment.get("type") == "document":
                    self.telegram.send_document(int(user["chat_id"]), attachment["url"], caption=text)
                else:
                    self.send(int(user["chat_id"]), text)
                success += 1
            except TelegramError as exc:
                failure += 1
                errors.append({"user": user["telegram_user_id"], "error": str(exc)[:200]})
                if "bot was blocked" in str(exc).lower() or "chat not found" in str(exc).lower():
                    self.db.mark_blocked(int(user["telegram_user_id"]))
            time.sleep(0.05)
        self.db.update_broadcast(broadcast_id, status="done", success_count=success, failure_count=failure, error_json=str(errors[:20]))
        return {"success": success, "failure": failure, "total": len(users)}

    def notify_admins(self, text: str) -> None:
        for chat_id in self.settings.admin_chat_ids:
            try:
                self.send(chat_id, text)
            except TelegramError:
                LOG.debug("admin notify failed for %s", chat_id, exc_info=True)

    def handle_business_event(self, event: dict[str, Any]) -> dict[str, Any]:
        kind = str(_first(event.get("event"), event.get("type"), default="unknown"))
        order_no = str(_first(event.get("order_no"), event.get("no"), event.get("trade_no"), default=""))
        unique = str(_first(event.get("id"), event.get("event_id"), default=f"{kind}:{order_no}:{event.get('status', '')}:{event.get('updated_at', '')}"))
        if not self.db.record_event_once(unique, kind, event):
            return {"ok": True, "duplicate": True}
        if order_no:
            self.db.cache_order(order_no, telegram_user_id=None, status=event.get("status"), payload=event)
        message = self.format_event_message(kind, event)
        self.notify_admins(message)
        if order_no:
            cached = self.db.get_cached_order(order_no)
            if cached and cached.get("telegram_user_id") and kind in {"order_paid_success", "order_fulfilled", "fulfillment_ready"}:
                try:
                    self.send(int(cached["telegram_user_id"]), message, self.main_keyboard(int(cached["telegram_user_id"])))
                except TelegramError:
                    pass
        if kind in {"stock_restocked", "stock_available"}:
            slug = str(_first(event.get("product_slug"), event.get("slug"), event.get("product_id"), default=""))
            for subscriber in self.db.stock_subscribers(slug):
                notice = (
                    "你订阅的商品已补货。\n"
                    f"商品：{html_escape(_first(event.get('product_title'), subscriber.get('product_title'), slug))}"
                )
                try:
                    self.send(int(subscriber["chat_id"]), notice, self.main_keyboard(int(subscriber["telegram_user_id"])))
                except TelegramError:
                    pass
        return {"ok": True, "event": kind}

    def format_event_message(self, kind: str, event: dict[str, Any]) -> str:
        labels = {
            "order_paid_success": "订单支付成功",
            "order_created": "新订单",
            "order_fulfilled": "订单已交付",
            "fulfillment_ready": "交付内容已生成",
            "stock_low": "库存预警",
            "stock_empty": "商品售罄",
            "stock_restocked": "商品已补货",
            "stock_available": "商品可购买",
            "wallet_recharged": "钱包充值成功",
            "payment_failed": "支付失败",
        }
        lines = [f"<b>{html_escape(labels.get(kind, kind))}</b>"]
        for label, key in [
            ("订单号", "order_no"),
            ("商品", "items_summary"),
            ("金额", "amount"),
            ("币种", "currency"),
            ("客户", "customer_label"),
            ("状态", "status"),
            ("备注", "message"),
        ]:
            if event.get(key) is not None:
                value = event.get(key)
                if key == "order_no":
                    lines.append(f"{label}：<code>{html_escape(value)}</code>")
                else:
                    lines.append(f"{label}：{html_escape(value)}")
        return "\n".join(lines)

    def order_payload(self, cart: dict[str, Any], user: dict[str, Any], *, include_guest: bool) -> dict[str, Any]:
        item: dict[str, Any] = {"quantity": _int(cart.get("quantity"), 1)}
        if cart.get("sku_id"):
            item[self.settings.order_sku_field or "sku_id"] = cart.get("sku_id")
        if cart.get("product_id"):
            item["product_id"] = cart.get("product_id")
        if cart.get("product_slug"):
            item["product_slug"] = cart.get("product_slug")
        body: dict[str, Any] = {
            "items": [item],
            "source": "telegram_bot",
            "metadata": {"telegram_user_id": user.get("telegram_user_id")},
        }
        if cart.get("coupon_code"):
            body["coupon_code"] = cart.get("coupon_code")
        form_values = cart.get("form_values") or {}
        if form_values:
            body["form_values"] = form_values
            body["form_data"] = form_values
        if include_guest:
            body["email"] = user.get("guest_email")
            body["order_password"] = user.get("guest_order_password")
        affiliate_code = user.get("affiliate_code")
        if affiliate_code:
            body["affiliate_code"] = affiliate_code
        return body

    def format_preview(self, preview: dict[str, Any], cart: dict[str, Any]) -> list[str]:
        lines = [
            "<b>订单预览</b>",
            f"商品：{html_escape(cart.get('product_title'))}",
            f"数量：{cart.get('quantity', 1)}",
        ]
        for label, key in [("原价", "subtotal"), ("优惠", "discount_amount"), ("应付", "total_amount"), ("钱包抵扣", "wallet_deduction")]:
            if preview.get(key) is not None:
                lines.append(f"{label}：{html_escape(preview.get(key))}")
        if preview.get("currency"):
            lines.append(f"币种：{html_escape(preview.get('currency'))}")
        return lines

    def payment_channels(self) -> list[dict[str, Any]]:
        try:
            config = self.dujiao.get_site_config()
            channels = config.get("payment_channels") or config.get("payments") or []
            if isinstance(channels, list) and channels:
                return [ch for ch in channels if isinstance(ch, dict) and ch.get("enabled", True)]
        except Exception:
            pass
        return self.settings.payment_channels

    def product_title(self, product: dict[str, Any]) -> str:
        return pick_i18n(_first(product.get("title"), product.get("name"), product.get("slug")), self.settings.default_locale) or "未命名商品"

    def product_price(self, product: dict[str, Any]) -> str:
        price = _first(product.get("price"), product.get("price_amount"), product.get("sale_price"), product.get("min_price"))
        if not price:
            skus = self.product_skus(product)
            prices = [_first(sku.get("price"), sku.get("price_amount"), sku.get("sale_price")) for sku in skus]
            prices = [p for p in prices if p]
            if prices:
                price = min(prices)
        return str(price or "查看规格")

    def product_stock(self, product: dict[str, Any]) -> str:
        stock = _first(product.get("stock"), product.get("stock_count"), product.get("available_stock"), product.get("inventory"))
        if stock != "":
            return str(stock)
        skus = self.product_skus(product)
        if skus:
            total = sum(_int(_first(sku.get("stock"), sku.get("stock_count"), sku.get("available_stock"), default=0)) for sku in skus)
            return str(total)
        return "充足" if product.get("in_stock", True) else "售罄"

    def product_skus(self, product: dict[str, Any]) -> list[dict[str, Any]]:
        for key in ("skus", "sku_list", "product_skus", "variants"):
            value = product.get(key)
            if isinstance(value, list):
                return [x for x in value if isinstance(x, dict)]
        return []

    def find_sku(self, skus: list[dict[str, Any]], sku_id: Any) -> dict[str, Any] | None:
        for sku in skus:
            if str(sku.get("id")) == str(sku_id):
                return sku
        return skus[0] if skus else None

    def sku_label(self, sku: dict[str, Any] | None) -> str:
        if not sku:
            return "默认规格"
        name = pick_i18n(_first(sku.get("name"), sku.get("title"), sku.get("sku_name"), sku.get("spec")), self.settings.default_locale)
        if not name and isinstance(sku.get("spec_values"), list):
            name = " / ".join(str(x.get("value") or x.get("name") or x) for x in sku["spec_values"])
        price = _first(sku.get("price"), sku.get("price_amount"), sku.get("sale_price"))
        stock = _first(sku.get("stock"), sku.get("available_stock"), sku.get("stock_count"))
        suffix = []
        if price:
            suffix.append(str(price))
        if stock != "":
            suffix.append(f"库存 {stock}")
        return (name or f"SKU {sku.get('id')}") + (f" ({', '.join(suffix)})" if suffix else "")

    def format_sku_lines(self, product: dict[str, Any]) -> str:
        lines = []
        for sku in self.product_skus(product)[:10]:
            lines.append("- " + html_escape(self.sku_label(sku)))
        return "\n".join(lines)

    def manual_form_fields(self, product: dict[str, Any]) -> list[dict[str, Any]]:
        raw = _first(
            product.get("manual_form_fields"),
            product.get("manual_form_config"),
            product.get("manual_delivery_form"),
            product.get("form_fields"),
            default=[],
        )
        if isinstance(raw, str):
            parsed = json_loads(raw, [])
            raw = parsed if isinstance(parsed, list) else []
        fields = []
        if isinstance(raw, list):
            for idx, item in enumerate(raw):
                if isinstance(item, dict):
                    key = str(_first(item.get("key"), item.get("name"), default=f"field_{idx + 1}"))
                    fields.append({"key": key, "label": _first(item.get("label"), item.get("title"), item.get("placeholder"), key), "required": item.get("required", True)})
                elif isinstance(item, str):
                    fields.append({"key": f"field_{idx + 1}", "label": item, "required": True})
        return fields

    def fulfillment_text(self, order: dict[str, Any]) -> str:
        parts: list[str] = []
        for key in ("fulfillment", "fulfillment_content", "delivery_content", "cards", "card_items", "secret"):
            value = order.get(key)
            if not value:
                continue
            if isinstance(value, list):
                for item in value[:50]:
                    if isinstance(item, dict):
                        text = _first(item.get("content"), item.get("code"), item.get("card_no"), item.get("value"))
                    else:
                        text = item
                    if text:
                        parts.append(f"<code>{html_escape(text)}</code>")
            elif isinstance(value, dict):
                for item_value in value.values():
                    if item_value:
                        parts.append(f"<code>{html_escape(item_value)}</code>")
            else:
                parts.append(f"<code>{html_escape(value)}</code>")
        return "\n".join(parts[:50])

    def product_url(self, product: dict[str, Any]) -> str:
        if not self.settings.shop_base_url:
            return ""
        slug = str(_first(product.get("slug"), product.get("id")))
        return f"{self.settings.shop_base_url}/products/{slug}"
