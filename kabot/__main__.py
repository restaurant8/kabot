from __future__ import annotations

import argparse
import json
import logging
import sys

from .bot import BotApp
from .config import Settings
from .server import run_webhook_server


def configure_logging(level: str) -> None:
    logging.basicConfig(
        level=getattr(logging, level.upper(), logging.INFO),
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="kabot", description="Telegram bot extension for Dujiao-Next")
    parser.add_argument("--env", default=".env", help="env file path")
    sub = parser.add_subparsers(dest="command")

    run = sub.add_parser("run", help="run bot")
    run.add_argument("--mode", choices=["polling", "webhook"], default=None)

    sub.add_parser("init-db", help="initialize SQLite database")
    sub.add_parser("status", help="print bot and webhook status")
    sub.add_parser("set-webhook", help="register Telegram webhook")
    sub.add_parser("set-commands", help="set Telegram bot command menu")
    delete = sub.add_parser("delete-webhook", help="delete Telegram webhook")
    delete.add_argument("--drop-pending-updates", action="store_true")

    broadcast = sub.add_parser("broadcast", help="broadcast HTML text to known Telegram users")
    broadcast.add_argument("--text", required=True)
    broadcast.add_argument("--photo-url")
    broadcast.add_argument("--document-url")

    sub.add_parser("notify-test", help="send test notification to admins")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    settings = Settings.load(args.env)
    configure_logging(settings.log_level)
    app = BotApp(settings)

    command = args.command or "run"
    if command == "init-db":
        app.init()
        print(f"initialized database: {settings.db_path}")
        return 0

    settings.validate_runtime()

    if command == "run":
        mode = args.mode or settings.run_mode
        if mode == "webhook":
            run_webhook_server(app, settings)
        else:
            app.start_polling()
        return 0

    if command == "status":
        app.init()
        me = app.telegram.get_me()
        webhook = app.telegram.get_webhook_info()
        print(json.dumps({"bot": me, "webhook": webhook}, ensure_ascii=False, indent=2))
        return 0

    if command == "set-webhook":
        if not settings.webhook_public_url:
            print("WEBHOOK_PUBLIC_URL is required", file=sys.stderr)
            return 2
        result = app.telegram.set_webhook(settings.webhook_public_url, settings.webhook_secret_token or None)
        print(json.dumps(result, ensure_ascii=False, indent=2))
        return 0

    if command == "set-commands":
        commands = [
            {"command": "menu", "description": "打开主菜单"},
            {"command": "products", "description": "浏览商品"},
            {"command": "orders", "description": "查看订单"},
            {"command": "wallet", "description": "查看钱包"},
            {"command": "affiliate", "description": "分销推广"},
            {"command": "login", "description": "Telegram 登录"},
            {"command": "admin", "description": "管理员菜单"},
        ]
        result = app.telegram.set_my_commands(commands)
        print(json.dumps(result, ensure_ascii=False, indent=2))
        return 0

    if command == "delete-webhook":
        result = app.telegram.delete_webhook(drop_pending_updates=args.drop_pending_updates)
        print(json.dumps(result, ensure_ascii=False, indent=2))
        return 0

    if command == "broadcast":
        app.init()
        attachment = None
        if args.photo_url:
            attachment = {"type": "photo", "url": args.photo_url}
        elif args.document_url:
            attachment = {"type": "document", "url": args.document_url}
        result = app.broadcast_message(args.text, created_by=None, attachment=attachment)
        print(json.dumps(result, ensure_ascii=False, indent=2))
        return 0

    if command == "notify-test":
        app.init()
        app.notify_admins("测试通知：kabot 管理员通知通道正常。")
        print("sent")
        return 0

    return 1


if __name__ == "__main__":
    raise SystemExit(main())
