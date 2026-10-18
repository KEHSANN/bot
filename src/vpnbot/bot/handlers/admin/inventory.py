"""Inventory inspection: stock levels, batches and single items.

Secrets are masked in lists. Revealing one requires ``inventory.view_secret``
*at the moment of reveal* — the list handler never decrypts anything.
"""

from __future__ import annotations

from aiogram import F, Router
from aiogram.types import CallbackQuery, Message
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from vpnbot.bot.handlers.admin import common
from vpnbot.bot.keyboards.callbacks import AdminCB
from vpnbot.config import get_settings
from vpnbot.db.enums import InventoryStatus, Permission
from vpnbot.db.models.catalog import Plan
from vpnbot.db.models.inventory import InventoryItem
from vpnbot.db.models.users import Admin
from vpnbot.security.crypto import CredentialCipher
from vpnbot.services import catalog as catalog_service
from vpnbot.services import inventory as inventory_service

router = Router(name="admin.inventory")

STATUS_LABELS = {
    InventoryStatus.AVAILABLE: "آماده",
    InventoryStatus.RESERVED: "رزرو",
    InventoryStatus.DELIVERED: "تحویل‌شده",
    InventoryStatus.DISABLED: "غیرفعال",
}
STATUS_FILTERS = {"all": None, "av": InventoryStatus.AVAILABLE, "dl": InventoryStatus.DELIVERED}


def _cipher() -> CredentialCipher:
    return CredentialCipher(get_settings().inventory_encryption_key)


async def _overview(
    target: CallbackQuery | Message, session: AsyncSession, admin: Admin, page: int
) -> None:
    products, total = await catalog_service.list_products(
        session, active_only=False, offset=page * common.ADMIN_PAGE_SIZE, limit=common.ADMIN_PAGE_SIZE
    )
    total_pages = max(1, (total + common.ADMIN_PAGE_SIZE - 1) // common.ADMIN_PAGE_SIZE)
    rows = []
    for product in products:
        counts = await inventory_service.counts_for_product(session, product.id)
        rows.append(
            [
                common.btn(
                    f"📦 {product.title} — آماده: {common.money(counts.available)}",
                    "inventory",
                    "product",
                    product.id,
                )
            ]
        )
    nav = common.pager("inventory", "", page, total_pages)
    if nav:
        rows.append(nav)
    rows.append([common.back()])
    await common.reply(
        target,
        "<b>📦 انبار</b>\n\nمحصول را برای مشاهده موجودی و آیتم‌ها انتخاب کنید.",
        rows,
    )


async def _product_items(
    target: CallbackQuery | Message,
    session: AsyncSession,
    admin: Admin,
    product_id: int,
    status_key: str,
    page: int,
) -> None:
    product = await catalog_service.get_product(session, product_id)
    if product is None:
        await common.reply(target, "محصول یافت نشد.", [[common.back("inventory")]])
        return
    status = STATUS_FILTERS.get(status_key)

    conditions = [InventoryItem.product_id == product_id]
    if status is not None:
        conditions.append(InventoryItem.status == status)
    total = int(
        (await session.execute(select(func.count(InventoryItem.id)).where(*conditions))).scalar_one()
    )
    total_pages = max(1, (total + common.ADMIN_PAGE_SIZE - 1) // common.ADMIN_PAGE_SIZE)
    page = min(max(0, page), total_pages - 1)
    items = list(
        (
            await session.execute(
                select(InventoryItem)
                .where(*conditions)
                .order_by(InventoryItem.id)
                .offset(page * common.ADMIN_PAGE_SIZE)
                .limit(common.ADMIN_PAGE_SIZE)
            )
        ).scalars()
    )
    counts = await inventory_service.counts_for_product(session, product_id)

    rows = [
        [
            common.btn(
                f"#{item.id} · {STATUS_LABELS[item.status]}"
                + (f" · سفارش {item.assigned_order_id}" if item.assigned_order_id else ""),
                "inventory",
                "item",
                item.id,
            )
        ]
        for item in items
    ]
    nav = common.pager("inventory", f"list:{status_key}", page, total_pages, extra=product_id)
    if nav:
        rows.append(nav)
    rows.append(
        [
            common.btn("همه", "inventory", "list:all", product_id),
            common.btn("آماده", "inventory", "list:av", product_id),
            common.btn("تحویل‌شده", "inventory", "list:dl", product_id),
        ]
    )
    rows.append([common.btn("📥 ورود کانفیگ جدید", "import", "start", product_id)])
    rows.append([common.back("inventory")])

    text = (
        f"<b>📦 {common.escape(product.title)}</b>\n\n"
        f"✅ آماده: <b>{common.money(counts.available)}</b>\n"
        f"🔒 رزرو: {common.money(counts.reserved)}\n"
        f"📤 تحویل‌شده: {common.money(counts.delivered)}\n"
        f"⛔️ غیرفعال: {common.money(counts.disabled)}\n\n"
        f"آیتم‌های نمایش‌داده‌شده: {common.money(len(items))} از {common.money(total)}"
    )
    await common.reply(target, text, rows)


async def _show_item(
    target: CallbackQuery | Message, session: AsyncSession, admin: Admin, item_id: int
) -> None:
    item = await session.get(InventoryItem, item_id)
    if item is None:
        await common.reply(target, "آیتم یافت نشد.", [[common.back("inventory")]])
        return
    plan = await session.get(Plan, item.plan_id) if item.plan_id else None
    lines = [
        f"<b>📦 آیتم #{item.id}</b>",
        "",
        f"محصول: {common.money(item.product_id)}"
        + (f" · پلن: {common.escape(plan.title)}" if plan else " · بدون پلن"),
        f"وضعیت: {STATUS_LABELS[item.status]}",
        f"کانفیگ: <code>{common.mask_secret('x' * 24)}</code>",
        f"لینک اشتراک: {'دارد' if item.sub_url_cipher else 'ندارد'}",
        f"دسته ورود: {item.source_batch_id or '—'}",
        f"سفارش: {item.assigned_order_id or '—'}",
        f"تحویل: {common.date(item.delivered_at)}",
    ]
    rows = [
        [
            common.btn("👁 نمایش کانفیگ", "inventory", "reveal", item.id),
            common.btn("🔗 نمایش لینک", "inventory", "reveal_sub", item.id),
        ]
    ]
    if item.status in (InventoryStatus.AVAILABLE, InventoryStatus.DISABLED):
        rows.append([common.btn("⛔️ غیرفعال‌سازی", "inventory", "disable", item.id)])
    rows.append([common.btn("⬅️ بازگشت", "inventory", "product", item.product_id)])
    await common.reply(target, "\n".join(lines), rows)


@router.callback_query(AdminCB.filter(F.section == "inventory"))
async def inventory_router(
    callback: CallbackQuery, callback_data: AdminCB, session: AsyncSession, admin: Admin | None
) -> None:
    admin = common.guard(admin, Permission.INVENTORY_MANAGE)
    action = callback_data.action

    if action == "reveal" or action == "reveal_sub":
        common.guard(admin, Permission.INVENTORY_VIEW_SECRET)
        pair = await inventory_service.reveal_pair(
            session, cipher=_cipher(), item_id=callback_data.item_id
        )
        value = pair.config if action == "reveal" else (pair.subscription_url or "—")
        await common.audit(
            session,
            admin,
            "inventory.secret_revealed",
            entity_type="inventory_item",
            entity_id=callback_data.item_id,
            data={"field": "config" if action == "reveal" else "sub_url"},
        )
        await common.alert(callback, f"{'کانفیگ' if action == 'reveal' else 'لینک اشتراک'}:\n{value}")
        return

    if action == "disable":
        await inventory_service.disable_item(session, callback_data.item_id, reason="غیرفعال‌سازی دستی ادمین")
        await common.audit(
            session,
            admin,
            "inventory.item_disabled",
            entity_type="inventory_item",
            entity_id=callback_data.item_id,
        )
        await callback.answer("غیرفعال شد")
        await _show_item(callback, session, admin, callback_data.item_id)
        return

    if action == "item":
        await _show_item(callback, session, admin, callback_data.item_id)
        return

    if action == "product":
        await _product_items(callback, session, admin, callback_data.item_id, "all", 0)
        return

    if action.startswith("list:"):
        await _product_items(
            callback,
            session,
            admin,
            callback_data.item_id,
            action.split(":", 1)[1],
            callback_data.page,
        )
        return

    await _overview(callback, session, admin, callback_data.page)

