"""
Telegram Bot Runner supporting both Long-Polling Mode (for local development / single-instance VPS)
and Authenticated Webhook Mode (with X-Telegram-Bot-Api-Secret-Token verification for high concurrency).
"""
from __future__ import annotations
import hmac
from typing import Any, Dict, Optional

from backend.app.bot.telegram_bot import ApplicationContainer, TelegramBotDispatcher
from backend.app.core.security import AuthenticationError


class TelegramUpdateProcessor:
    """Routes incoming Telegram JSON updates (messages & callback_queries) to TelegramBotDispatcher."""

    def __init__(self, container: Optional[ApplicationContainer] = None) -> None:
        self.container = container or ApplicationContainer()
        self.dispatcher = TelegramBotDispatcher(self.container)

    def verify_webhook_secret_header(self, secret_header: Optional[str]) -> None:
        expected = self.container.settings.telegram_webhook_secret
        if not secret_header or not hmac.compare_digest(secret_header, expected):
            raise AuthenticationError("Invalid X-Telegram-Bot-Api-Secret-Token header")

    def process_update(self, update: Dict[str, Any]) -> Dict[str, Any]:
        """Process a raw Telegram Bot API Update object safely."""
        if "message" in update:
            msg = update["message"]
            from_user = msg.get("from") or {}
            tg_id = int(from_user.get("id", 0))
            full_name = (
                f"{from_user.get('first_name', '')} {from_user.get('last_name', '')}".strip()
                or "کاربر تلگرام"
            )
            username = from_user.get("username")
            text = (msg.get("text") or "").strip()

            if text.startswith("/start"):
                parts = text.split(maxsplit=1)
                start_param = parts[1].strip() if len(parts) > 1 else None
                return self.dispatcher.handle_start(
                    telegram_id=tg_id,
                    full_name=full_name,
                    username=username,
                    start_param=start_param,
                )
            if text == "/admin":
                return self.dispatcher.handle_admin_action(tg_id, "admin.dashboard")

        if "callback_query" in update:
            cb = update["callback_query"]
            from_user = cb.get("from") or {}
            tg_id = int(from_user.get("id", 0))
            data = cb.get("data") or ""
            cb_id = str(cb.get("id") or "cb_default")

            if data.startswith("quote:"):
                prod_id = int(data.split(":", 1)[1])
                return self.dispatcher.handle_customer_quote(tg_id, prod_id)
            if data.startswith("confirm_buy:"):
                quote_id = data.split(":", 1)[1]
                return self.dispatcher.handle_customer_purchase(
                    tg_id, quote_id, idempotency_key=f"tg_cb:{cb_id}"
                )
            if data == "my_orders":
                return self.dispatcher.handle_customer_orders(tg_id)
            if data == "referrals_hub":
                return self.dispatcher.handle_customer_referral_page(tg_id)

        return {"screen": "ignored", "text_fa": "دستور نامعتبر است؛ لطفاً از منوی اصلی استفاده کنید."}
