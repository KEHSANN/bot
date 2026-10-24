"""Administrators and roles.

Only the ``owner`` preset carries ``admin.manage``, so this whole section is
effectively owner-only. Two invariants are enforced here in addition to the
permission check:

* nobody can revoke or demote themselves;
* the last active owner can never be removed or demoted, which would lock the
  panel for everyone.

Every change is audited.
"""

from __future__ import annotations

import re

from aiogram import F, Router
from aiogram.fsm.context import FSMContext
from aiogram.types import CallbackQuery, Message
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from vpnbot.bot.handlers.admin import common
from vpnbot.bot.keyboards.callbacks import AdminCB
from vpnbot.bot.states import AdminStates
from vpnbot.db.enums import ROLE_PRESETS, Permission
from vpnbot.db.models.users import Admin, User
from vpnbot.services import auth as auth_service

router = Router(name="admin.admins")

ROLE_TITLES = {key: str(preset["title"]) for key, preset in ROLE_PRESETS.items()}
TG_ID_RE = re.compile(r"^\d{5,15}$")


async def _active_owner_count(session: AsyncSession) -> int:
    # Lock the active-owner rows while counting: two owners revoking
    # each other concurrently could otherwise both count 2, both pass
    # the last-owner check, and leave the panel with no active owner.
    rows = (
        await session.execute(
            select(Admin.id)
            .join(Admin.role)
            .where(Admin.is_active.is_(True), Admin.role.has(key="owner"))
            .with_for_update()
        )
    ).scalars()
    return len(list(rows))


def _user_line(admin: Admin) -> str:
    user = admin.user
    name = common.escape(user.display_name) if user else "—"
    handle = f" · @{user.username}" if user and user.username else ""
    return f"{name}{handle}"


async def _show_list(
    target: CallbackQuery | Message, session: AsyncSession, admin: Admin, page: int
) -> None:
    total = int((await session.execute(select(func.count(Admin.id)))).scalar_one())
    total_pages = max(1, (total + common.ADMIN_PAGE_SIZE - 1) // common.ADMIN_PAGE_SIZE)
    page = min(max(0, page), total_pages - 1)
    admins = list(
        (
            await session.execute(
                select(Admin)
                .order_by(Admin.is_active.desc(), Admin.id)
                .offset(common.clamp_page(page) * common.ADMIN_PAGE_SIZE)
                .limit(common.ADMIN_PAGE_SIZE)
            )
        ).scalars()
    )

    rows = []
    for item in admins:
        mark = "🟢" if item.is_active else "⚪️"
        rows.append(
            [
                common.btn(
                    f"{mark} {_user_line(item)} · {ROLE_TITLES.get(item.role.key, item.role.title)}",
                    "admins",
                    "view",
                    item.id,
                )
            ]
        )
    nav = common.pager("admins", "", page, total_pages)
    if nav:
        rows.append(nav)
    rows.append([common.btn("➕ افزودن مدیر", "admins", "new")])
    rows.append([common.back()])

    owners = await _active_owner_count(session)
    await common.reply(
        target,
        f"<b>👤 مدیران</b>\n\n"
        f"تعداد کل: {common.money(total)} · مالک فعال: {common.money(owners)}",
        rows,
        session=session,
    )


async def _show_admin(
    target: CallbackQuery | Message, session: AsyncSession, admin: Admin, admin_id: int
) -> None:
    # Explicit eager load: after a grant, session.get() returns the
    # just-created identity-mapped Admin whose user/role were never
    # loaded, and touching them below raised MissingGreenlet.
    from sqlalchemy import select
    from sqlalchemy.orm import selectinload

    item = (
        await session.execute(
            select(Admin)
            .where(Admin.id == admin_id)
            .options(selectinload(Admin.user), selectinload(Admin.role))
            .execution_options(populate_existing=True)
        )
    ).scalar_one_or_none()
    if item is None:
        await common.reply(target, "مدیر یافت نشد.", [[common.back("admins")]], session=session)
        return
    user = item.user
    lines = [
        f"<b>👤 {_user_line(item)}</b>",
        "",
        f"شناسه عددی تلگرام: <code>{user.telegram_id if user else '—'}</code>",
        f"نقش: <b>{ROLE_TITLES.get(item.role.key, item.role.title)}</b>",
        f"وضعیت: {'🟢 فعال' if item.is_active else '⚪️ غیرفعال'}",
        f"افزوده‌شده: {common.date(item.created_at)}",
        "",
        "<b>دسترسی‌ها:</b>",
    ]
    permissions = list(item.role.permissions or [])
    if permissions:
        lines.extend(f"• <code>{common.escape(perm)}</code>" for perm in permissions)
    else:
        lines.append("—")

    rows: list[list] = []
    role_buttons = [
        common.btn(
            ("✅ " if key == item.role.key else "") + ROLE_TITLES[key],
            "admins",
            f"setrole:{key}",
            item.id,
        )
        for key in ROLE_PRESETS
    ]
    for index in range(0, len(role_buttons), 2):
        rows.append(role_buttons[index : index + 2])
    if item.is_active:
        rows.append([common.btn("⛔️ غیرفعال کردن", "admins", "revoke_ask", item.id)])
    else:
        rows.append([common.btn("✅ فعال کردن", "admins", "activate", item.id)])
    rows.append([common.back("admins")])
    await common.reply(target, "\n".join(lines), rows, session=session)


def _check_self_and_last_owner(admin: Admin, item: Admin, owners: int) -> str | None:
    """Return an error message when the change would lock out the panel."""
    if item.id == admin.id:
        return "نمی‌توانید دسترسی خودتان را حذف یا تغییر دهید."
    # Root owners come from OWNER_TELEGRAM_IDS in .env: they are the
    # recovery path for the whole panel, so no panel action — revoke,
    # demote, or a grant that overwrites their role — may touch them.
    # They are managed only by editing .env and re-running
    # bootstrap-owner.
    from vpnbot.config import get_settings

    root_ids = get_settings().owner_ids
    if item.user is not None and item.user.telegram_id in root_ids:
        return "مالک اصلی (تعریف‌شده در تنظیمات سرور) از داخل پنل قابل تغییر نیست."
    if item.is_active and item.role.key == "owner" and owners <= 1:
        return "این حساب تنها مالک فعال است و قابل تغییر نیست."
    return None


@router.callback_query(AdminCB.filter(F.section == "admins"))
async def admins_router(
    callback: CallbackQuery,
    callback_data: AdminCB,
    session: AsyncSession,
    admin: Admin | None,
    state: FSMContext,
) -> None:
    admin = common.guard(admin, Permission.ADMIN_MANAGE)
    action = callback_data.action
    # Navigating in this section abandons any half-finished wizard
    # (see the note in the payments router) — EXCEPT the grant role
    # buttons: they consume grant_telegram_id from FSM data that the
    # preceding message step stored. Clearing unconditionally here
    # broke "add admin" entirely (id read back as 0 -> "expired").
    if not action.startswith("grant:"):
        await state.clear()

    if action == "new":
        await state.set_state(AdminStates.admin_grant)
        await state.update_data(stage="telegram_id")
        await callback.answer()
        await common.reply(
            callback,
            "<b>➕ افزودن مدیر</b>\n\n"
            "شناسه عددی تلگرام کاربر را بفرستید.\n"
            "برای پیدا کردن شناسه، کاربر می‌تواند از ربات @userinfobot استفاده کند.",
            [common.cancel_row()],
            session=session,
        )
        return

    if action == "view":
        await _show_admin(callback, session, admin, callback_data.item_id)
        return

    if action.startswith("setrole:"):
        role_key = action.split(":", 1)[1]
        item = await session.get(Admin, callback_data.item_id)
        if item is None or role_key not in ROLE_PRESETS:
            await common.alert(callback, "درخواست نامعتبر است.")
            return
        owners = await _active_owner_count(session)
        problem = _check_self_and_last_owner(admin, item, owners)
        if problem:
            await common.alert(callback, problem)
            return
        previous = item.role.key
        item.role_id = (await _role_id(session, role_key))
        item.granted_by_admin_id = admin.id
        await common.audit(
            session,
            admin,
            "admin.role_changed",
            entity_type="admin",
            entity_id=item.id,
            data={"from": previous, "to": role_key},
        )
        await callback.answer("نقش تغییر کرد")
        await _show_admin(callback, session, admin, item.id)
        return

    if action == "revoke_ask":
        item = await session.get(Admin, callback_data.item_id)
        if item is None:
            await common.alert(callback, "مدیر یافت نشد.")
            return
        owners = await _active_owner_count(session)
        problem = _check_self_and_last_owner(admin, item, owners)
        if problem:
            await common.alert(callback, problem)
            return
        await common.reply(
            callback,
            f"<b>⛔️ غیرفعال کردن دسترسی</b>\n\n{_user_line(item)}\n\n"
            "با این کار این حساب دیگر به پنل دسترسی نخواهد داشت. مطمئن هستید؟",
            [common.yes_no("admins", "revoke", item.id), [common.back("admins")]],
            session=session,
        )
        return

    if action == "revoke":
        item = await session.get(Admin, callback_data.item_id)
        if item is None:
            await common.alert(callback, "مدیر یافت نشد.")
            return
        owners = await _active_owner_count(session)
        problem = _check_self_and_last_owner(admin, item, owners)
        if problem:
            await common.alert(callback, problem)
            return
        await auth_service.revoke_admin(session, item.id)
        await common.audit(
            session, admin, "admin.revoked", entity_type="admin", entity_id=item.id
        )
        await callback.answer("غیرفعال شد")
        await _show_admin(callback, session, admin, item.id)
        return

    if action == "activate":
        item = await session.get(Admin, callback_data.item_id)
        if item is None:
            await common.alert(callback, "مدیر یافت نشد.")
            return
        item.is_active = True
        item.granted_by_admin_id = admin.id
        await common.audit(
            session, admin, "admin.reactivated", entity_type="admin", entity_id=item.id
        )
        await callback.answer("فعال شد")
        await _show_admin(callback, session, admin, item.id)
        return

    if action.startswith("grant:"):
        role_key = action.split(":", 1)[1]
        data = await state.get_data()
        telegram_id = int(data.get("grant_telegram_id") or 0)
        if telegram_id and role_key in ROLE_PRESETS:
            # Re-granting an EXISTING admin overwrites their role —
            # the same protections as revoke/demote apply (notably:
            # a root owner cannot be demoted through this path).
            from sqlalchemy import select as _select

            from vpnbot.db.models.users import User as _User

            existing = (
                await session.execute(
                    _select(Admin)
                    .join(_User, _User.id == Admin.user_id)
                    .where(_User.telegram_id == telegram_id)
                )
            ).scalar_one_or_none()
            if existing is not None:
                owners = await _active_owner_count(session)
                problem = _check_self_and_last_owner(admin, existing, owners)
                if problem is not None:
                    await state.clear()
                    await common.alert(callback, problem)
                    return
        if not telegram_id or role_key not in ROLE_PRESETS:
            await state.clear()
            await common.alert(callback, "درخواست منقضی شده است؛ دوباره شروع کنید.")
            await _show_list(callback, session, admin, 0)
            return
        try:
            item = await auth_service.grant_admin(
                session,
                actor_admin_id=admin.id,
                telegram_id=telegram_id,
                role_key=role_key,
            )
        except ValueError as exc:
            await common.alert(callback, str(exc))
            return
        await common.audit(
            session,
            admin,
            "admin.granted",
            entity_type="admin",
            entity_id=item.id,
            data={"telegram_id": telegram_id, "role": role_key},
        )
        await state.clear()
        await callback.answer("دسترسی داده شد")
        await _show_admin(callback, session, admin, item.id)
        return

    await _show_list(callback, session, admin, callback_data.page)


async def _role_id(session: AsyncSession, role_key: str) -> int:
    roles = await auth_service.ensure_roles(session)
    return roles[role_key].id


@router.message(AdminStates.admin_grant, F.chat.type == "private")
async def grant_input(
    message: Message, session: AsyncSession, admin: Admin | None, state: FSMContext
) -> None:
    admin = common.guard(admin, Permission.ADMIN_MANAGE)
    raw = (message.text or "").strip()
    if not TG_ID_RE.match(raw):
        await message.answer("⚠️ شناسه تلگرام باید فقط عدد باشد (۵ تا ۱۵ رقم). دوباره بفرستید یا /cancel.")
        return

    telegram_id = int(raw)
    user = (
        await session.execute(select(User).where(User.telegram_id == telegram_id))
    ).scalar_one_or_none()
    if user is not None and user.id == admin.user_id:
        await message.answer("این حساب خود شماست. شناسه دیگری بفرستید یا /cancel.")
        return

    existing = None
    if user is not None:
        existing = (
            await session.execute(select(Admin).where(Admin.user_id == user.id))
        ).scalar_one_or_none()

    await state.update_data(grant_telegram_id=telegram_id, stage="role")
    who = common.escape(user.display_name) if user else "کاربر جدید (هنوز ربات را شروع نکرده)"
    lines = [
        "<b>➕ انتخاب نقش</b>",
        "",
        f"کاربر: {who}",
        f"شناسه: <code>{telegram_id}</code>",
    ]
    if existing is not None:
        lines.append(f"نقش فعلی: {ROLE_TITLES.get(existing.role.key, existing.role.title)}")
    lines.append("")
    lines.append("نقش جدید را انتخاب کنید:")

    rows = [
        [common.btn(ROLE_TITLES[key], "admins", f"grant:{key}")]
        for key in ROLE_PRESETS
    ]
    rows.append(common.cancel_row())
    await message.answer("\n".join(lines), reply_markup=_markup(rows))


def _markup(rows) -> object:
    from aiogram.types import InlineKeyboardMarkup

    return InlineKeyboardMarkup(inline_keyboard=[list(r) for r in rows])
