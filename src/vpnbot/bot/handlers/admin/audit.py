"""Read-only audit log: paged event list, filters and a single-event view.

The table is append-only at the database level (a rule rejects UPDATE and
DELETE), so this module never writes. Filters are namespace prefixes of
``action``; ``all`` removes the filter entirely.
"""

from __future__ import annotations

import json

from aiogram import F, Router
from aiogram.types import CallbackQuery, Message
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from vpnbot.bot.handlers.admin import common
from vpnbot.bot.keyboards.callbacks import AdminCB
from vpnbot.db.enums import AuditActor, Permission
from vpnbot.db.models.system import AuditEvent
from vpnbot.db.models.users import Admin

router = Router(name="admin.audit")

FILTERS: dict[str, str] = {
    "all": "همه",
    "order": "سفارش‌ها",
    "payment": "پرداخت‌ها",
    "inventory": "انبار",
    "import": "ورود کانفیگ",
    "wallet": "کیف پول",
    "admin": "مدیران",
    "settings": "تنظیمات",
    "screen": "صفحه‌ها",
    "broadcast": "پیام همگانی",
    "support": "پشتیبانی",
}
ACTOR_LABELS = {
    AuditActor.ADMIN: "🛠 مدیر",
    AuditActor.CUSTOMER: "👤 کاربر",
    AuditActor.SYSTEM: "⚙️ سیستم",
    AuditActor.WORKER: "🔄 پردازشگر",
}
MAX_DATA_CHARS = 1200


def _filter_clause(prefix: str):
    if prefix in ("", "all"):
        return None
    return AuditEvent.action.startswith(f"{prefix}.")


async def _show_list(
    target: CallbackQuery | Message,
    session: AsyncSession,
    admin: Admin,
    prefix: str,
    page: int,
) -> None:
    clause = _filter_clause(prefix)
    count_stmt = select(func.count(AuditEvent.id))
    if clause is not None:
        count_stmt = count_stmt.where(clause)
    total = int((await session.execute(count_stmt)).scalar_one())
    total_pages = max(1, (total + common.ADMIN_PAGE_SIZE - 1) // common.ADMIN_PAGE_SIZE)
    page = min(max(0, page), total_pages - 1)

    stmt = select(AuditEvent).order_by(AuditEvent.id.desc())
    if clause is not None:
        stmt = stmt.where(clause)
    events = list(
        (
            await session.execute(
                stmt.offset(page * common.ADMIN_PAGE_SIZE).limit(common.ADMIN_PAGE_SIZE)
            )
        ).scalars()
    )

    rows = []
    for event in events:
        entity = f" · {event.entity_type}#{event.entity_id}" if event.entity_type else ""
        rows.append(
            [
                common.btn(
                    f"{event.action}{entity} · {common.date(event.created_at, with_time=False)}",
                    "audit",
                    "view",
                    event.id,
                )
            ]
        )
    nav = common.pager("audit", f"list:{prefix}", page, total_pages)
    if nav:
        rows.append(nav)
    rows.append(
        [common.btn(label, "audit", f"list:{key}") for key, label in list(FILTERS.items())[:5]]
    )
    rows.append(
        [common.btn(label, "audit", f"list:{key}") for key, label in list(FILTERS.items())[5:]]
    )
    rows.append([common.back()])
    await common.reply(
        target,
        f"<b>📜 گزارش فعالیت</b>\n\n"
        f"فیلتر: {FILTERS.get(prefix, 'همه')} · تعداد: <b>{common.money(total)}</b>\n"
        "<i>این گزارش فقط خواندنی است و قابل حذف یا ویرایش نیست.</i>",
        rows,
    )


async def _show_event(
    target: CallbackQuery | Message, session: AsyncSession, event_id: int
) -> None:
    event = await session.get(AuditEvent, event_id)
    if event is None:
        await common.reply(target, "رویداد یافت نشد.", [[common.back("audit")]])
        return
    raw = json.dumps(event.data or {}, ensure_ascii=False, indent=2, default=str)
    if len(raw) > MAX_DATA_CHARS:
        raw = raw[:MAX_DATA_CHARS] + "\n…"
    lines = [
        f"<b>📜 رویداد #{event.id}</b>",
        "",
        f"زمان: {common.date(event.created_at)}",
        f"کنش: <code>{common.escape(event.action)}</code>",
        f"عامل: {ACTOR_LABELS.get(event.actor_type, event.actor_type.value)}"
        + (f" #{event.actor_id}" if event.actor_id else ""),
        f"موجودیت: {common.escape(event.entity_type or '—')}"
        + (f" #{common.escape(event.entity_id)}" if event.entity_id else ""),
        "",
        "<b>داده:</b>",
        f"<pre>{common.escape(raw)}</pre>",
    ]
    await common.reply(target, "\n".join(lines), [[common.back("audit")]])


@router.callback_query(AdminCB.filter(F.section == "audit"))
async def audit_router(
    callback: CallbackQuery,
    callback_data: AdminCB,
    session: AsyncSession,
    admin: Admin | None,
) -> None:
    admin = common.guard(admin, Permission.AUDIT_VIEW)
    action = callback_data.action

    if action == "view":
        await _show_event(callback, session, callback_data.item_id)
        return

    if action.startswith("list:"):
        await _show_list(callback, session, admin, action.split(":", 1)[1], callback_data.page)
        return

    await _show_list(callback, session, admin, "all", callback_data.page)
