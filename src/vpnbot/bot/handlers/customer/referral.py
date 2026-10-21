"""Referral programme: personal invite link and reward totals.

Rewards are granted only after an invited customer's payment is approved
(``services.referrals``), and the link is bound at /start, never here.
"""

from __future__ import annotations

from aiogram import F, Router
from aiogram.types import CallbackQuery
from sqlalchemy.ext.asyncio import AsyncSession

from vpnbot.bot.handlers.customer.common import alert, base_context, money, sender_for
from vpnbot.bot.keyboards.callbacks import MenuCB
from vpnbot.config import get_settings
from vpnbot.db.models.users import User
from vpnbot.services import referrals as referral_service
from vpnbot.services.screens import STAGE_REFERRAL

router = Router(name="customer.referral")


async def invite_link(bot, referral_code: str) -> str:
    """``https://t.me/<bot>?start=ref_<code>``.

    ``config.bot_username`` is authoritative when set; otherwise ask Telegram
    once (``Bot`` exposes ``me()``, not a ``username`` attribute).
    """
    username = get_settings().bot_username.strip().lstrip("@")
    if not username:
        me = await bot.me()
        username = me.username or ""
    if not username:
        return ""
    return f"https://t.me/{username}?start=ref_{referral_code}"


@router.callback_query(MenuCB.filter(F.action == "referral"))
async def open_referral(
    callback: CallbackQuery, session: AsyncSession, user: User | None
) -> None:
    if user is None:
        await callback.answer()
        return
    from vpnbot.services.settings import REFERRAL_SETTINGS, SHOP_SETTINGS, SettingsService

    settings = SettingsService(session)
    referral_cfg = await settings.get(REFERRAL_SETTINGS)
    shop_cfg = await settings.get(SHOP_SETTINGS)
    if not referral_cfg.get("enabled", True) or not shop_cfg.get("referral_enabled", True):
        await alert(callback, "دعوت از دوستان در حال حاضر غیرفعال است")
        return
    stats = await referral_service.referral_stats(session, user.id)
    link = await invite_link(callback.bot, user.referral_code)
    await sender_for(callback.bot, session).show(
        callback,
        STAGE_REFERRAL,
        base_context(
            user,
            invited_count=stats["invited"],
            reward_total=money(stats["reward_total_irt"]),
            invite_link=link,
            bot_username=get_settings().bot_username.strip().lstrip("@"),
        ),
    )
