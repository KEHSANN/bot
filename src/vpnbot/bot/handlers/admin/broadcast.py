"""Broadcast campaigns: compose, confirm, start, pause, stop.

A campaign is a persistent row plus one recipient row per target. The worker
drains recipients; nothing is sent from inside this handler, so a restart
mid-campaign resumes where it stopped instead of double-sending.
"""

from __future__ import annotations

import logging

from aiogram import F, Router
from aiogram.fsm.context import FSMContext
from aiogram.types import CallbackQuery, Message
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from vpnbot.bot.handlers.admin import common
from vpnbot.bot.keyboards.callbacks import AdminCB
from vpnbot.bot.states import AdminStates
from vpnbot.db.base import utcnow
from vpnbot.db.enums import CampaignStatus, Permission, RecipientStatus, UserStatus
from vpnbot.db.models.broadcast import BroadcastCampaign, BroadcastRecipient
from vpnbot.db.models.users import Admin, User

log = logging.getLogger(__name__)
router = Router(name="admin.broadcast")

STATUS_FA = {
    CampaignStatus.DRAFT: "پیش‌نویس",
    CampaignStatus.RUNNING: "در حال ارسال",
    CampaignStatus.PAUSED: "متوقف موقت",
    CampaignStatus.STOPPED: "لغو‌شده",
    CampaignStatus.FINISHED: "پایان‌یافته",
    CampaignStatus.FAILED: "ناموفق",
}
AUDIENCES = {
    "ALL": "همه کاربران",
    "BUYERS": "خریداران",
    "NO_BUY": "بدون خرید",
    "TEST": "تست (فقط خودم)",
}
MAX_TEXT = 3800


async def _audience_ids(session: AsyncSession, audience: str, admin: Admin) -> list[int]:
    if audience == "TEST":
        return [admin.user.telegram_id]
    stmt = select(User.telegram_id).where(User.status == UserStatus.ACTIVE)
    if audience == "BUYERS":
        from vpnbot.db.models.orders import Order

        stmt = stmt.where(User.id.in_(select(Order.user_id).distinct()))
    elif audience == "NO_BUY":
        from vpnbot.db.models.orders import Order

        stmt = stmt.where(User.id.not_in(select(Order.user_id).distinct()))
    return [int(row[0]) for row in (await session.execute(stmt)).all()]


async def _audience_count(session: AsyncSession, audience: str, admin: Admin) -> int:
    return len(await _audience_ids(session, audience, admin))


async def _show_campaign(
    target: CallbackQuery | Message, session: AsyncSession, admin: Admin, campaign_id: int
) -> None:
    campaign = await session.get(BroadcastCampaign, campaign_id)
    if campaign is None:
        await common.reply(target, "کمپین یافت نشد.", [[common.back("broadcast")]])
        return
    lines = [
        f"<b>📨 کمپین #{campaign.id}</b>",
        "",
        f"عنوان: {common.escape(campaign.title)}",
        f"مخاطب: {AUDIENCES.get(campaign.audience, campaign.audience)}",
        f"وضعیت: <b>{STATUS_FA[campaign.status]}</b>",
        "",
        f"کل: {common.money(campaign.total_recipients)}",
        f"✅ ارسال‌شده: {common.money(campaign.sent_count)}",
        f"⚠️ ناموفق: {common.money(campaign.failed_count)}",
        f"⏭ رد‌شده: {common.money(campaign.skipped_count)}",
        "",
        "<b>متن:</b>",
        common.escape(campaign.message_text[:900]),
    ]
    rows: list[list] = []
    if campaign.status in (CampaignStatus.DRAFT, CampaignStatus.PAUSED):
        rows.append([common.btn("▶️ شروع ارسال", "broadcast", "start", campaign.id)])
    if campaign.status is CampaignStatus.RUNNING:
        rows.append([common.btn("⏸ توقف موقت", "broadcast", "pause", campaign.id)])
    if campaign.status in (CampaignStatus.DRAFT, CampaignStatus.RUNNING, CampaignStatus.PAUSED):
        rows.append([common.btn("⛔️ لغو کمپین", "broadcast", "stop", campaign.id)])
    rows.append([common.btn("🔄 بروزرسانی", "broadcast", "view", campaign.id)])
    rows.append([common.back("broadcast")])
    await common.reply(target, "\n".join(lines), rows)


async def _show_list(
    target: CallbackQuery | Message, session: AsyncSession, admin: Admin, page: int
) -> None:
    total = int(
        (await session.execute(select(func.count(BroadcastCampaign.id)))).scalar_one()
    )
    total_pages = max(1, (total + common.ADMIN_PAGE_SIZE - 1) // common.ADMIN_PAGE_SIZE)
    page = min(max(0, page), total_pages - 1)
    campaigns = list(
        (
            await session.execute(
                select(BroadcastCampaign)
                .order_by(BroadcastCampaign.created_at.desc(), BroadcastCampaign.id.desc())
                .offset(page * common.ADMIN_PAGE_SIZE)
                .limit(common.ADMIN_PAGE_SIZE)
            )
        ).scalars()
    )
    rows = [
        [
            common.btn(
                f"#{c.id} · {common.escape(c.title[:30])} · {STATUS_FA[c.status]} "
                f"({common.money(c.sent_count)}/{common.money(c.total_recipients)})",
                "broadcast",
                "view",
                c.id,
            )
        ]
        for c in campaigns
    ]
    nav = common.pager("broadcast", "", page, total_pages)
    if nav:
        rows.append(nav)
    rows.append([common.btn("➕ کمپین جدید", "broadcast", "new")])
    rows.append([common.back()])
    await common.reply(
        target, f"<b>📨 پیام همگانی</b>\n\nتعداد کمپین‌ها: {common.money(total)}", rows
    )


@router.callback_query(AdminCB.filter(F.section == "broadcast"))
async def broadcast_router(
    callback: CallbackQuery,
    callback_data: AdminCB,
    session: AsyncSession,
    admin: Admin | None,
    state: FSMContext,
) -> None:
    admin = common.guard(admin, Permission.BROADCAST_MANAGE)
    action = callback_data.action

    if action == "new":
        await state.set_state(AdminStates.broadcast_compose)
        await state.update_data(stage="text")
        await callback.answer()
        await common.reply(
            callback,
            "<b>📨 کمپین جدید · مرحله ۱ از ۳</b>\n\n"
            f"متن پیام را بفرستید (حداکثر {MAX_TEXT} کاراکتر).\n"
            "HTML مجاز است: <code>&lt;b&gt;</code>، <code>&lt;i&gt;</code>، <code>&lt;a href&gt;</code>.",
            [common.cancel_row()],
        )
        return

    if action == "start":
        campaign = await session.get(BroadcastCampaign, callback_data.item_id)
        if campaign is None:
            await common.alert(callback, "کمپین یافت نشد.")
            return
        if campaign.status not in (CampaignStatus.DRAFT, CampaignStatus.PAUSED):
            await common.alert(callback, f"کمپین در وضعیت {STATUS_FA[campaign.status]} است.")
            return
        if campaign.status is CampaignStatus.DRAFT:
            await _materialize_recipients(session, campaign, admin)
        campaign.status = CampaignStatus.RUNNING
        campaign.started_at = campaign.started_at or utcnow()
        await _enqueue_dispatch(session, campaign)
        await common.audit(
            session, admin, "broadcast.started", entity_type="campaign", entity_id=campaign.id
        )
        await callback.answer("ارسال آغاز شد")
        await _show_campaign(callback, session, admin, campaign.id)
        return

    if action == "pause":
        campaign = await session.get(BroadcastCampaign, callback_data.item_id)
        if campaign is not None and campaign.status is CampaignStatus.RUNNING:
            campaign.status = CampaignStatus.PAUSED
            await common.audit(
                session, admin, "broadcast.paused", entity_type="campaign", entity_id=campaign.id
            )
            await callback.answer("متوقف شد")
        else:
            await callback.answer("امکان توقف نیست", show_alert=True)
        await _show_campaign(callback, session, admin, callback_data.item_id)
        return

    if action == "stop":
        campaign = await session.get(BroadcastCampaign, callback_data.item_id)
        if campaign is None:
            await common.alert(callback, "کمپین یافت نشد.")
            return
        if campaign.status in (CampaignStatus.FINISHED, CampaignStatus.STOPPED):
            await common.alert(callback, "این کمپین پایان یافته است.")
            return
        campaign.status = CampaignStatus.STOPPED
        campaign.finished_at = utcnow()
        await common.audit(
            session, admin, "broadcast.stopped", entity_type="campaign", entity_id=campaign.id
        )
        await callback.answer("لغو شد")
        await _show_campaign(callback, session, admin, campaign.id)
        return

    if action.startswith("audience:"):
        audience = callback_data.action.split(":", 1)[1] if ":" in action else "ALL"
        data = await state.get_data()
        text = data.get("broadcast_text")
        title = data.get("broadcast_title")
        if not text:
            await common.alert(callback, "متن پیام از دست رفت؛ از ابتدا شروع کنید.")
            await state.clear()
            await _show_list(callback, session, admin, 0)
            return
        count = await _audience_count(session, audience, admin)
        campaign = BroadcastCampaign(
            title=title or text[:60],
            message_text=text,
            audience=audience,
            status=CampaignStatus.DRAFT,
            created_by_admin_id=admin.id,
        )
        session.add(campaign)
        await session.flush()
        await common.audit(
            session,
            admin,
            "broadcast.created",
            entity_type="campaign",
            entity_id=campaign.id,
            data={"audience": audience, "estimated": count},
        )
        await state.clear()
        await callback.answer()
        await common.reply(
            callback,
            f"<b>📨 کمپین #{campaign.id} ساخته شد</b>\n\n"
            f"مخاطب: {AUDIENCES.get(audience, audience)}\n"
            f"تخمین گیرندگان: <b>{common.money(count)}</b>\n\n"
            "برای شروع، دکمه «شروع ارسال» را بزنید.",
            [
                [common.btn("▶️ شروع ارسال", "broadcast", "start", campaign.id)],
                [common.btn("⬅️ کمپین‌ها", "broadcast")],
            ],
        )
        return

    if action == "view":
        await _show_campaign(callback, session, admin, callback_data.item_id)
        return

    await _show_list(callback, session, admin, callback_data.page)


async def _materialize_recipients(
    session: AsyncSession, campaign: BroadcastCampaign, admin: Admin
) -> None:
    from sqlalchemy.dialects.postgresql import insert as pg_insert

    telegram_ids = await _audience_ids(session, campaign.audience, admin)
    if not telegram_ids:
        campaign.total_recipients = 0
        return
    users = {
        int(user.telegram_id): user.id
        for user in (
            await session.execute(select(User).where(User.telegram_id.in_(telegram_ids)))
        ).scalars()
    }
    rows = [
        {
            "campaign_id": campaign.id,
            "user_id": users[tg],
            "telegram_id": tg,
            "status": RecipientStatus.PENDING,
            "attempts": 0,
        }
        for tg in telegram_ids
        if tg in users
    ]
    if rows:
        await session.execute(
            pg_insert(BroadcastRecipient.__table__)
            .values(rows)
            .on_conflict_do_nothing(index_elements=["campaign_id", "user_id"])
        )
    campaign.total_recipients = len(rows)


async def _enqueue_dispatch(session: AsyncSession, campaign: BroadcastCampaign) -> None:
    from sqlalchemy.dialects.postgresql import insert as pg_insert

    from vpnbot.db.base import utcnow
    from vpnbot.db.enums import JobKind, JobStatus
    from vpnbot.db.models.system import Job

    # The key is the same on first start and on resume (status is RUNNING
    # in both cases), so a plain do-nothing insert silently skipped the
    # enqueue whenever the earlier dispatch job already existed as DONE
    # or FAILED — a resumed campaign stayed RUNNING forever with nothing
    # being sent. Revive a finished job; never reset a live one.
    await session.execute(
        pg_insert(Job)
        .values(
            kind=JobKind.BROADCAST_DISPATCH,
            payload={"campaign_id": campaign.id},
            idempotency_key=f"broadcast:{campaign.id}:{campaign.status.value}",
            status=JobStatus.QUEUED,
        )
        .on_conflict_do_update(
            index_elements=["idempotency_key"],
            set_={
                "status": JobStatus.QUEUED,
                "attempt": 0,
                "scheduled_at": utcnow(),
                "finished_at": None,
                "last_error": None,
            },
            where=Job.status.in_([JobStatus.DONE, JobStatus.FAILED]),
        )
    )


@router.message(AdminStates.broadcast_compose, F.chat.type == "private")
async def compose_input(
    message: Message, session: AsyncSession, admin: Admin | None, state: FSMContext
) -> None:
    admin = common.guard(admin, Permission.BROADCAST_MANAGE)
    data = await state.get_data()
    stage = data.get("stage", "text")
    raw = (message.text or "").strip()

    if stage == "text":
        if len(raw) < 3:
            await message.answer("متن خیلی کوتاه است. دوباره بفرستید یا /cancel را بزنید.")
            return
        if len(raw) > MAX_TEXT:
            await message.answer(f"متن بیش از {MAX_TEXT} کاراکتر است.")
            return
        await state.update_data(broadcast_text=raw, stage="title")
        await message.answer(
            "<b>📨 مرحله ۲ از ۳</b>\n\nیک عنوان کوتاه برای شناسایی کمپین بفرستید "
            "(فقط برای پنل، به کاربران نشان داده نمی‌شود)."
        )
        return

    if stage == "title":
        title = raw[:120] or "کمپین بدون عنوان"
        await state.update_data(broadcast_title=title, stage="audience")
        rows = []
        for key, label in AUDIENCES.items():
            count = await _audience_count(session, key, admin)
            rows.append([common.btn(f"{label} · {common.money(count)}", "broadcast", f"audience:{key}")])
        rows.append(common.cancel_row())
        await message.answer(
            f"<b>📨 مرحله ۳ از ۳</b>\n\nعنوان: {common.escape(title)}\n"
            "گروه مخاطب را انتخاب کنید:",
            reply_markup=_markup(rows),
        )
        return


def _markup(rows):
    from aiogram.types import InlineKeyboardMarkup

    return InlineKeyboardMarkup(inline_keyboard=[list(r) for r in rows])
