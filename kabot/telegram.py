from __future__ import annotations

import json
from typing import Any

from .http_client import HttpClient, HttpError


class TelegramError(RuntimeError):
    pass


class TelegramAPI:
    def __init__(self, token: str, client: HttpClient | None = None) -> None:
        self.token = token
        self.client = client or HttpClient(timeout=60, user_agent="kabot-telegram/0.1")
        self.base_url = f"https://api.telegram.org/bot{token}"

    def request(self, method: str, payload: dict[str, Any] | None = None, timeout: int | None = None) -> Any:
        url = f"{self.base_url}/{method}"
        try:
            resp = self.client.request("POST", url, json_body=payload or {}, timeout=timeout)
            data = resp.json()
        except HttpError as exc:
            raise TelegramError(str(exc)) from exc
        if not isinstance(data, dict) or not data.get("ok"):
            raise TelegramError(json.dumps(data, ensure_ascii=False))
        return data.get("result")

    def get_me(self) -> dict[str, Any]:
        return self.request("getMe")

    def get_webhook_info(self) -> dict[str, Any]:
        return self.request("getWebhookInfo")

    def set_webhook(self, url: str, secret_token: str | None = None) -> Any:
        payload: dict[str, Any] = {"url": url, "allowed_updates": ["message", "callback_query", "my_chat_member"]}
        if secret_token:
            payload["secret_token"] = secret_token
        return self.request("setWebhook", payload)

    def delete_webhook(self, drop_pending_updates: bool = False) -> Any:
        return self.request("deleteWebhook", {"drop_pending_updates": drop_pending_updates})

    def set_my_commands(self, commands: list[dict[str, str]]) -> Any:
        return self.request("setMyCommands", {"commands": commands})

    def get_updates(self, offset: int | None, timeout: int) -> list[dict[str, Any]]:
        payload = {"timeout": timeout, "allowed_updates": ["message", "callback_query", "my_chat_member"]}
        if offset is not None:
            payload["offset"] = offset
        return self.request("getUpdates", payload, timeout=timeout + 10)

    def send_message(
        self,
        chat_id: int,
        text: str,
        *,
        reply_markup: dict[str, Any] | None = None,
        disable_web_page_preview: bool = True,
    ) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "chat_id": chat_id,
            "text": text,
            "parse_mode": "HTML",
            "disable_web_page_preview": disable_web_page_preview,
        }
        if reply_markup:
            payload["reply_markup"] = reply_markup
        return self.request("sendMessage", payload)

    def edit_message_text(
        self,
        chat_id: int,
        message_id: int,
        text: str,
        *,
        reply_markup: dict[str, Any] | None = None,
        disable_web_page_preview: bool = True,
    ) -> Any:
        payload: dict[str, Any] = {
            "chat_id": chat_id,
            "message_id": message_id,
            "text": text,
            "parse_mode": "HTML",
            "disable_web_page_preview": disable_web_page_preview,
        }
        if reply_markup:
            payload["reply_markup"] = reply_markup
        return self.request("editMessageText", payload)

    def answer_callback_query(self, callback_query_id: str, text: str = "", show_alert: bool = False) -> Any:
        return self.request("answerCallbackQuery", {
            "callback_query_id": callback_query_id,
            "text": text,
            "show_alert": show_alert,
        })

    def delete_message(self, chat_id: int, message_id: int) -> Any:
        return self.request("deleteMessage", {"chat_id": chat_id, "message_id": message_id})

    def send_photo(
        self,
        chat_id: int,
        photo: str,
        *,
        caption: str = "",
        reply_markup: dict[str, Any] | None = None,
    ) -> Any:
        payload: dict[str, Any] = {"chat_id": chat_id, "photo": photo, "parse_mode": "HTML"}
        if caption:
            payload["caption"] = caption[:1000]
        if reply_markup:
            payload["reply_markup"] = reply_markup
        return self.request("sendPhoto", payload)

    def send_document(
        self,
        chat_id: int,
        document: str,
        *,
        caption: str = "",
        reply_markup: dict[str, Any] | None = None,
    ) -> Any:
        payload: dict[str, Any] = {"chat_id": chat_id, "document": document, "parse_mode": "HTML"}
        if caption:
            payload["caption"] = caption[:1000]
        if reply_markup:
            payload["reply_markup"] = reply_markup
        return self.request("sendDocument", payload)


def inline_keyboard(rows: list[list[dict[str, Any]]]) -> dict[str, Any]:
    return {"inline_keyboard": rows}
