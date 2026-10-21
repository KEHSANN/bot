"""Admin dashboard: the first screen after /admin."""

from __future__ import annotations

from aiogram import F, Router
from aiogram.filters import Command
from aiogram.fsm.context import FSMContext
from aiogram.types import CallbackQuery, Message
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from vpnbot.bot.handlers.admin import common
from vpnbot.bot.keyboards.callbacks import AdminCB
from vpnbot.db.enums import InventoryStatus, Permission, TicketStatus
from vpnbot.db.models.inventory import InventoryItem
from vpnbot.db.models.orders import Order
from vpnbot.db.models.support import Ticket
from vpnbot.db.models.users import Admin, User
from vpnbot.services import auth as auth_service
from vpnbot.services import payments as payment_service

router = Router(name="admin.dashboard")


async def dashboard_text(session: AsyncSession) -> str:
    today = common.today_start()

    async def count(stmt) -> int:
        return int((await session.execute(stmt)).scalar_one())

    users_total = await count(select(func.count(User.id)))
    users_today = await count(select(func.count(User.id)).where(User.created_at >= today))
    orders_today = await count(select(func.count(Order.id)).where(Order.created_at >= today))
    pending_payments = await payment_service.count_pending_reviews(session)
    available = await count(
        select(func.count(InventoryItem.id)).where(InventoryItem.status == InventoryStatus.AVAILABLE)
    )
    open_tickets = await count(
        select(func.count(Ticket.id)).where(Ticket.status == TicketStatus.OPEN)
    )

    return (
        "<b>📊 داشبورد</b>\n\n"
        f"👥 کاربران: <b>{common.money(users_total)}</b> (امروز: {common.money(users_today)})\n"
        f"🧾 سفارش‌های امروز: <b>{common.money(orders_today)}</b>\n"
        f"💳 در انتظار بررسی پرداخت: <b>{common.money(pending_payments)}</b>\n"
        f"📦 موجودی آماده فروش: <b>{common.money(available)}</b>\n"
        f"🎫 تیکت‌های باز: <b>{common.money(open_tickets)}</b>"
    )


def dashboard_rows(admin: Admin) -> list[list]:
    has = auth_service.admin_has
    rows: list[list] = []
    first: list = []
    second: list = []
    if has(admin, Permission.PRODUCT_MANAGE):
        first.append(common.btn("🛍 محصولات", "catalog"))
    if has(admin, Permission.INVENTORY_MANAGE):
        first.append(common.btn("📦 انبار", "inventory"))
    if has(admin, Permission.ORDER_VIEW):
        second.append(common.btn("🧾 سفارش‌ها", "orders"))
    if has(admin, Permission.PAYMENT_REVIEW):
        second.append(common.btn("💳 پرداخت‌ها", "payments"))
    if first:
        rows.append(first)
    if second:
        rows.append(second)

    third: list = []
    if has(admin, Permission.SUPPORT_MANAGE):
        third.append(common.btn("🎫 پشتیبانی", "support"))
    if has(admin, Permission.BROADCAST_MANAGE):
        third.append(common.btn("📨 پیام همگانی", "broadcast"))
    if third:
        rows.append(third)

    fourth: list = []
    if has(admin, Permission.CONTENT_MANAGE):
        fourth.append(common.btn("🖼 صفحه‌ها و دکمه‌ها", "screens"))
    if has(admin, Permission.SETTINGS_MANAGE):
        fourth.append(common.btn("⚙️ تنظیمات", "settings"))
    if fourth:
        rows.append(fourth)

    fifth: list = []
    if has(admin, Permission.WALLET_ADJUST):
        fifth.append(common.btn("👛 کیف پول‌ها", "wallet"))
    if has(admin, Permission.ADMIN_MANAGE):
        fifth.append(common.btn("👤 مدیران", "admins"))
    if has(admin, Permission.AUDIT_VIEW):
        fifth.append(common.btn("📜 گزارش فعالیت", "audit"))
    if fifth:
        rows.append(fifth)

    rows.append([common.btn("🔄 بروزرسانی", "home", "refresh")])
    return rows


async def show_dashboard(target: CallbackQuery | Message, session: AsyncSession, admin: Admin) -> None:
    common.guard(admin, Permission.STATS_VIEW)
    text = await dashboard_text(session)
    role = admin.role.title if admin.role else "—"
    text += f"\n\n👤 نقش شما: <b>{common.escape(role)}</b>"
    await common.reply(target, text, dashboard_rows(admin))


@router.message(Command("admin", "panel"))
async def open_panel(message: Message, session: AsyncSession, admin: Admin | None) -> None:
    if message.chat.type != "private":
        return
    if admin is None:
        await message.answer(common.DENIED_TEXT)
        return
    await show_dashboard(message, session, admin)


@router.callback_query(AdminCB.filter(F.section == "home"))
async def home(
    callback: CallbackQuery,
    callback_data: AdminCB,
    session: AsyncSession,
    admin: Admin | None,
    state: FSMContext,
) -> None:
    if admin is None:
        await common.alert(callback, common.DENIED_TEXT)
        return
    if callback_data.action == "noop":
        await callback.answer()
        return
    # Going home abandons any in-progress wizard. Without this clear,
    # the ✖️ انصراف button (which routes here) left e.g. payment_note
    # armed — the admin's next plain text message then APPROVED
    # whatever payment they had meant to cancel out of.
    await state.clear()
    await show_dashboard(callback, session, admin)


@router.callback_query(AdminCB.filter(F.action == "noop"))
async def noop(callback: CallbackQuery) -> None:
    await callback.answer()
