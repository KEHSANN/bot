"""Runtime settings stored in the database with typed defaults.

Secrets (bot token, encryption keys) never live here — only business config
that authorized admins are allowed to edit.
"""

from __future__ import annotations

from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from vpnbot.db.models.system import AppSetting

CARD_SETTINGS = "payment.card"
CRYPTO_SETTINGS = "payment.crypto"
SUPPORT_SETTINGS = "support"
SHOP_SETTINGS = "shop"
REFERRAL_SETTINGS = "referral"
WALLET_SETTINGS = "wallet"

DEFAULTS: dict[str, dict[str, Any]] = {
    CARD_SETTINGS: {
        "enabled": False,
        "recipient_name": "",
        "card_number": "",
        "bank_name": "",
        "instructions": "پس از واریز، تصویر رسید پرداخت را ارسال کنید.",
    },
    CRYPTO_SETTINGS: {
        "enabled": False,
        "asset": "USDT",
        "network": "TRC20",
        "address": "",
        "memo": "",
        "amount_note": "",
        "instructions": "پس از انتقال، تصویر رسید و هش تراکنش (TXID) را ارسال کنید.",
    },
    SUPPORT_SETTINGS: {
        "enabled": True,
        "instructions": "سوال خود را بنویسید؛ کارشناسان ما به‌زودی پاسخ می‌دهند.",
    },
    SHOP_SETTINGS: {
        "wallet_enabled": True,
        "referral_enabled": True,
        "order_ttl_hours": 48,
        "guide_text": "راهنمای پرداخت: کارت‌به‌کارت یا کریپتو، سپس ارسال رسید.",
        "terms_text": "قوانین و شرایط: استفاده از سرویس به معنی پذیرش قوانین است.",
    },
    REFERRAL_SETTINGS: {
        "enabled": True,
        "tier1_reward_irt": 20000,
        "tier2_enabled": True,
        "tier2_threshold": 5,
        "tier2_reward_irt": 10000,
    },
    WALLET_SETTINGS: {
        "topup_enabled": True,
        "min_topup_irt": 10000,
    },
}


class SettingsService:
    def __init__(self, session: AsyncSession) -> None:
        self.session = session

    async def get(self, key: str) -> dict[str, Any]:
        row = await self.session.get(AppSetting, key)
        base = dict(DEFAULTS.get(key, {}))
        if row and isinstance(row.value, dict):
            base.update(row.value)
        return base

    async def get_value(self, key: str, name: str) -> Any:
        return (await self.get(key)).get(name)

    async def set(self, key: str, value: dict[str, Any], admin_id: int | None = None) -> dict[str, Any]:
        merged = dict(DEFAULTS.get(key, {}))
        merged.update(value)
        row = await self.session.get(AppSetting, key)
        if row is None:
            row = AppSetting(key=key, value=merged, updated_by_admin_id=admin_id)
            self.session.add(row)
        else:
            row.value = merged
            row.updated_by_admin_id = admin_id
        await self.session.flush()
        return merged

    async def seed_defaults(self) -> int:
        """Insert defaults ONLY for keys that have no stored row.

        ``cmd_seed`` used to call ``set(key, {})`` for every group,
        which rewrites the stored row with pure defaults — re-running
        the documented seed command wiped the admin's card number,
        crypto address and every other panel edit. Returns how many
        groups were inserted.
        """
        inserted = 0
        for key in DEFAULTS:
            if await self.session.get(AppSetting, key) is None:
                await self.set(key, {})
                inserted += 1
        return inserted

    async def update_fields(self, key: str, **fields: Any) -> dict[str, Any]:
        current = await self.get(key)
        current.update(fields)
        return await self.set(key, current)

    async def all(self) -> list[AppSetting]:
        result = await self.session.execute(select(AppSetting).order_by(AppSetting.key))
        return list(result.scalars())
