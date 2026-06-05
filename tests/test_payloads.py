from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from kabot.bot import BotApp
from kabot.config import Settings


class PayloadTests(unittest.TestCase):
    def make_app(self) -> BotApp:
        db_path = Path(tempfile.mkdtemp()) / "kabot.sqlite3"
        settings = Settings(
            telegram_bot_token="1:test",
            dujiao_base_url="https://example.test",
            db_path=str(db_path),
            payment_channels=[{"id": 1, "name": "Test"}],
        )
        return BotApp(settings)

    def test_order_payload_with_sku_and_guest(self) -> None:
        app = self.make_app()
        body = app.order_payload(
            {
                "product_id": 8,
                "product_slug": "demo",
                "sku_id": 9,
                "quantity": 2,
                "coupon_code": "SAVE",
                "form_values": {"account": "abc"},
            },
            {
                "telegram_user_id": 123,
                "guest_email": "u@example.com",
                "guest_order_password": "pw",
                "affiliate_code": "AFF",
            },
            include_guest=True,
        )
        self.assertEqual(body["items"][0]["sku_id"], 9)
        self.assertEqual(body["items"][0]["quantity"], 2)
        self.assertEqual(body["email"], "u@example.com")
        self.assertEqual(body["coupon_code"], "SAVE")
        self.assertEqual(body["form_values"]["account"], "abc")
        self.assertEqual(body["affiliate_code"], "AFF")

    def test_manual_form_fields_from_json(self) -> None:
        app = self.make_app()
        fields = app.manual_form_fields({"manual_form_config": '[{"key":"account","label":"账号","required":true}]'})
        self.assertEqual(fields[0]["key"], "account")
        self.assertTrue(fields[0]["required"])


if __name__ == "__main__":
    unittest.main()
