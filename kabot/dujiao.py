from __future__ import annotations

import hashlib
import hmac
from typing import Any
from urllib.parse import quote

from .http_client import HttpClient, HttpError
from .utils import ensure_api_prefix, unix_now


class DujiaoError(RuntimeError):
    def __init__(self, message: str, *, status: int | None = None, request_id: str | None = None) -> None:
        super().__init__(message)
        self.status = status
        self.request_id = request_id


class DujiaoClient:
    def __init__(
        self,
        base_url: str,
        api_prefix: str = "/api/v1",
        endpoints: dict[str, str] | None = None,
        client: HttpClient | None = None,
    ) -> None:
        self.base_url = base_url.rstrip("/")
        self.api_prefix = ensure_api_prefix(api_prefix).rstrip("/")
        self.endpoints = endpoints or {}
        self.client = client or HttpClient(timeout=30, user_agent="kabot-dujiao/0.1")

    def endpoint(self, name: str, default: str, **params: Any) -> str:
        path = self.endpoints.get(name, default)
        for key, value in params.items():
            path = path.replace("{" + key + "}", quote(str(value), safe=""))
        return path

    def url(self, path: str) -> str:
        path = ensure_api_prefix(path)
        return f"{self.base_url}{self.api_prefix}{path}"

    def request(
        self,
        method: str,
        path: str,
        *,
        token: str | None = None,
        params: dict[str, Any] | None = None,
        json_body: Any = None,
        raw: bool = False,
    ) -> Any:
        headers: dict[str, str] = {}
        if token:
            headers["Authorization"] = f"Bearer {token}"
        try:
            resp = self.client.request(method, self.url(path), params=params, json_body=json_body, headers=headers)
        except HttpError as exc:
            message = exc.body or str(exc)
            raise DujiaoError(message, status=exc.status) from exc
        if raw:
            return resp.body
        try:
            data = resp.json()
        except Exception as exc:  # noqa: BLE001
            raise DujiaoError(f"Invalid JSON response: {resp.text()[:200]}") from exc
        if isinstance(data, dict) and "status_code" in data:
            if data.get("status_code") != 0:
                detail = data.get("data") if isinstance(data.get("data"), dict) else {}
                raise DujiaoError(
                    str(data.get("msg") or "Dujiao API error"),
                    status=data.get("status_code"),
                    request_id=detail.get("request_id"),
                )
            if "pagination" in data:
                return {"data": data.get("data"), "pagination": data.get("pagination")}
            return data.get("data")
        return data

    def get_site_config(self) -> Any:
        return self.request("GET", self.endpoint("site_config", "/public/config"))

    def categories(self, page: int = 1, page_size: int = 50) -> Any:
        return self.request("GET", self.endpoint("categories", "/public/categories"), params={"page": page, "page_size": page_size})

    def products(
        self,
        *,
        page: int = 1,
        page_size: int = 8,
        category_id: int | None = None,
        keyword: str | None = None,
    ) -> Any:
        params = {"page": page, "page_size": page_size, "category_id": category_id, "search": keyword}
        return self.request("GET", self.endpoint("products", "/public/products"), params=params)

    def product_detail(self, slug: str) -> Any:
        return self.request("GET", self.endpoint("product_detail", "/public/products/{slug}", slug=slug))

    def posts(self, post_type: str | None = None, page: int = 1, page_size: int = 5) -> Any:
        return self.request("GET", self.endpoint("posts", "/public/posts"), params={"type": post_type, "page": page, "page_size": page_size})

    def send_verify_code(self, email: str, purpose: str = "register") -> Any:
        return self.request("POST", "/auth/send-verify-code", json_body={"email": email, "purpose": purpose})

    def login(self, email: str, password: str, remember_me: bool = True) -> Any:
        return self.request("POST", "/auth/login", json_body={"email": email, "password": password, "remember_me": remember_me})

    def register(self, email: str, password: str, code: str, nickname: str | None = None) -> Any:
        body = {"email": email, "password": password, "code": code, "agreement_accepted": True}
        if nickname:
            body["nickname"] = nickname
        return self.request("POST", "/auth/register", json_body=body)

    def telegram_login(self, payload: dict[str, Any]) -> Any:
        return self.request("POST", "/auth/telegram/login", json_body=payload)

    def me(self, token: str) -> Any:
        return self.request("GET", "/me", token=token)

    def update_profile(self, token: str, **fields: Any) -> Any:
        return self.request("PUT", "/me/profile", token=token, json_body={k: v for k, v in fields.items() if v is not None})

    def telegram_status(self, token: str) -> Any:
        return self.request("GET", "/me/telegram", token=token)

    def preview_order(self, token: str, body: dict[str, Any]) -> Any:
        return self.request("POST", "/orders/preview", token=token, json_body=body)

    def create_order(self, token: str, body: dict[str, Any]) -> Any:
        return self.request("POST", "/orders", token=token, json_body=body)

    def orders(self, token: str, page: int = 1, page_size: int = 10, status: str | None = None) -> Any:
        return self.request("GET", "/orders", token=token, params={"page": page, "page_size": page_size, "status": status})

    def order_detail(self, token: str, order_no: str) -> Any:
        return self.request("GET", f"/orders/{quote(order_no, safe='')}", token=token)

    def cancel_order(self, token: str, order_no: str) -> Any:
        return self.request("POST", f"/orders/{quote(order_no, safe='')}/cancel", token=token)

    def fulfillment_download(self, token: str, order_no: str) -> bytes:
        return self.request("GET", f"/orders/{quote(order_no, safe='')}/fulfillment/download", token=token, raw=True)

    def guest_fulfillment_download(self, order_no: str, email: str, order_password: str) -> bytes:
        return self.request(
            "GET",
            f"/guest/orders/{quote(order_no, safe='')}/fulfillment/download",
            params={"email": email, "order_password": order_password},
            raw=True,
        )

    def create_payment(self, token: str, order_no: str, channel_id: int) -> Any:
        return self.request("POST", "/payments", token=token, json_body={"order_no": order_no, "channel_id": channel_id})

    def capture_payment(self, token: str, payment_id: int) -> Any:
        return self.request("POST", f"/payments/{int(payment_id)}/capture", token=token)

    def latest_payment(self, token: str, order_no: str) -> Any:
        return self.request("GET", "/payments/latest", token=token, params={"order_no": order_no})

    def guest_preview_order(self, body: dict[str, Any]) -> Any:
        return self.request("POST", "/guest/orders/preview", json_body=body)

    def guest_create_order(self, body: dict[str, Any]) -> Any:
        return self.request("POST", "/guest/orders", json_body=body)

    def guest_orders(self, email: str, order_password: str, page: int = 1, page_size: int = 10, order_no: str | None = None) -> Any:
        return self.request(
            "GET",
            "/guest/orders",
            params={"email": email, "order_password": order_password, "page": page, "page_size": page_size, "order_no": order_no},
        )

    def guest_order_detail(self, order_no: str, email: str, order_password: str) -> Any:
        return self.request(
            "GET",
            f"/guest/orders/{quote(order_no, safe='')}",
            params={"email": email, "order_password": order_password},
        )

    def guest_cancel_order(self, order_no: str, email: str, order_password: str) -> Any:
        return self.request(
            "POST",
            f"/guest/orders/{quote(order_no, safe='')}/cancel",
            json_body={"email": email, "order_password": order_password},
        )

    def guest_create_payment(self, email: str, order_password: str, order_no: str, channel_id: int) -> Any:
        return self.request(
            "POST",
            "/guest/payments",
            json_body={"email": email, "order_password": order_password, "order_no": order_no, "channel_id": channel_id},
        )

    def guest_capture_payment(self, payment_id: int, email: str, order_password: str) -> Any:
        return self.request("POST", f"/guest/payments/{int(payment_id)}/capture", json_body={"email": email, "order_password": order_password})

    def guest_latest_payment(self, email: str, order_password: str, order_no: str) -> Any:
        return self.request(
            "GET",
            "/guest/payments/latest",
            params={"email": email, "order_password": order_password, "order_no": order_no},
        )

    def wallet_profile(self, token: str) -> Any:
        return self.request("GET", self.endpoint("wallet_profile", "/wallet"), token=token)

    def wallet_transactions(self, token: str, page: int = 1, page_size: int = 10) -> Any:
        return self.request("GET", self.endpoint("wallet_transactions", "/wallet/transactions"), token=token, params={"page": page, "page_size": page_size})

    def redeem_gift_card(self, token: str, code: str) -> Any:
        return self.request("POST", self.endpoint("wallet_redeem_gift_card", "/wallet/gift-cards/redeem"), token=token, json_body={"code": code})

    def affiliate_click(self, affiliate_code: str, visitor_key: str, landing_path: str = "", referrer: str = "telegram") -> Any:
        return self.request(
            "POST",
            "/public/affiliate/click",
            json_body={
                "affiliate_code": affiliate_code,
                "visitor_key": visitor_key,
                "landing_path": landing_path,
                "referrer": referrer,
            },
        )

    def affiliate_open(self, token: str) -> Any:
        return self.request("POST", "/affiliate/open", token=token)

    def affiliate_dashboard(self, token: str) -> Any:
        return self.request("GET", "/affiliate/dashboard", token=token)

    def affiliate_commissions(self, token: str, page: int = 1, status: str | None = None) -> Any:
        return self.request("GET", "/affiliate/commissions", token=token, params={"page": page, "page_size": 10, "status": status})

    def affiliate_withdraws(self, token: str, page: int = 1, status: str | None = None) -> Any:
        return self.request("GET", "/affiliate/withdraws", token=token, params={"page": page, "page_size": 10, "status": status})

    def affiliate_withdraw(self, token: str, amount: str, channel: str, account: str) -> Any:
        return self.request("POST", "/affiliate/withdraws", token=token, json_body={"amount": amount, "channel": channel, "account": account})


def build_telegram_login_payload(user: dict[str, Any], bot_token: str, auth_date: int | None = None) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "id": int(user["id"]),
        "first_name": user.get("first_name") or "",
        "last_name": user.get("last_name") or "",
        "username": user.get("username") or "",
        "photo_url": user.get("photo_url") or "",
        "auth_date": auth_date or unix_now(),
    }
    payload = {k: v for k, v in payload.items() if v != ""}
    check_string = "\n".join(f"{key}={payload[key]}" for key in sorted(payload))
    secret_key = hashlib.sha256(bot_token.encode("utf-8")).digest()
    payload["hash"] = hmac.new(secret_key, check_string.encode("utf-8"), hashlib.sha256).hexdigest()
    return payload
