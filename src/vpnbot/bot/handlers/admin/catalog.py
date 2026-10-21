"""Catalog administration: categories, products and plans.

Products are created inactive so a half-configured product is never sellable;
an administrator turns it on explicitly from the product screen.
"""

from __future__ import annotations

import logging

from aiogram import F, Router
from aiogram.fsm.context import FSMContext
from aiogram.types import CallbackQuery, Message
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from vpnbot.bot.handlers.admin import common
from vpnbot.bot.keyboards.callbacks import AdminCB
from vpnbot.bot.states import AdminStates
from vpnbot.db.enums import ButtonStyle, Permission, ProductType
from vpnbot.db.models.catalog import Product
from vpnbot.db.models.users import Admin
from vpnbot.money import MoneyError, parse_amount_irt
from vpnbot.services import catalog as catalog_service
from vpnbot.services.screens import normalize_style

log = logging.getLogger(__name__)
router = Router(name="admin.catalog")

TYPE_LABELS = {
    ProductType.CONFIG: "کانفیگ تکی",
    ProductType.SUBSCRIPTION: "اشتراک (Subscription)",
    ProductType.COMBO: "کانفیگ + اشتراک",
}
TYPE_BY_CODE = {"1": ProductType.CONFIG, "2": ProductType.SUBSCRIPTION, "3": ProductType.COMBO}
BUTTON_STYLE_LABELS = {
    ButtonStyle.DEFAULT: "پیش‌فرض (بدون رنگ)",
    ButtonStyle.PRIMARY: "آبی",
    ButtonStyle.SUCCESS: "سبز",
    ButtonStyle.DANGER: "قرمز",
}


def _button_style_of(product: Product) -> ButtonStyle:
    raw = getattr(product, "button_style", None)
    if isinstance(raw, ButtonStyle):
        return raw
    try:
        return ButtonStyle(str(raw or "DEFAULT").upper())
    except ValueError:
        return ButtonStyle.DEFAULT


# --------------------------------------------------------------------------
# Lists
# --------------------------------------------------------------------------
async def _show_categories(target: CallbackQuery | Message, session: AsyncSession, admin: Admin, page: int) -> None:
    categories = await catalog_service.list_categories(session, active_only=False)
    total_pages = max(1, (len(categories) + common.ADMIN_PAGE_SIZE - 1) // common.ADMIN_PAGE_SIZE)
    page = min(max(0, page), total_pages - 1)
    page = common.clamp_page(page)
    window = categories[page * common.ADMIN_PAGE_SIZE : (page + 1) * common.ADMIN_PAGE_SIZE]
    counts = await catalog_service.available_counts(
        session,
        [
            p.id
            for p in (
                await session.execute(select(Product).where(Product.archived_at.is_(None)))
            ).scalars()
        ],
    )
    rows = [[common.btn(f"📁 {c.title}", "catalog", "products", c.id, 0)] for c in window]
    nav = common.pager("catalog", "", page, total_pages)
    if nav:
        rows.append(nav)
    rows.append(
        [
            common.btn("🗂 همه محصولات", "catalog", "products", 0),
            common.btn("➕ دسته‌بندی جدید", "catalog", "newcat"),
        ]
    )
    rows.append([common.btn("➕ محصول جدید", "catalog", "new")])
    rows.append([common.back()])
    text = (
        "<b>🛍 محصولات</b>\n\n"
        f"دسته‌بندی‌ها: {common.money(len(categories))} — موجودی کل آماده فروش: "
        f"<b>{common.money(sum(counts.values()))}</b>\n\n"
        "برای مدیریت محصولات یک دسته را انتخاب کنید."
    )
    await common.reply(target, text, rows)


async def _show_products(
    target: CallbackQuery | Message, session: AsyncSession, admin: Admin, category_id: int, page: int
) -> None:
    products, total = await catalog_service.list_products(
        session,
        category_id=category_id or None,
        active_only=False,
        offset=common.clamp_page(page) * common.ADMIN_PAGE_SIZE,
        limit=common.ADMIN_PAGE_SIZE,
    )
    total_pages = max(1, (total + common.ADMIN_PAGE_SIZE - 1) // common.ADMIN_PAGE_SIZE)
    clamped = min(max(0, page), total_pages - 1)
    if clamped != page:
        # Clamp before rendering (see customer shop): an out-of-range page
        # otherwise shows an empty list with a working-looking pager.
        page = clamped
        products, total = await catalog_service.list_products(
            session,
            category_id=category_id or None,
            active_only=False,
            offset=common.clamp_page(page) * common.ADMIN_PAGE_SIZE,
            limit=common.ADMIN_PAGE_SIZE,
        )
    stock = await catalog_service.available_counts(session, [p.id for p in products])

    rows = []
    for product in products:
        flag = "🟢" if product.is_on_sale else ("📦" if product.archived_at else "⚪️")
        rows.append(
            [
                common.btn(
                    f"{flag} {product.title} ({common.money(stock.get(product.id, 0))})",
                    "product",
                    "",
                    product.id,
                    style=normalize_style(_button_style_of(product).value),
                )
            ]
        )
    nav = common.pager("catalog", "products", page, total_pages, extra=category_id)
    if nav:
        rows.append(nav)
    rows.append([common.btn("➕ محصول جدید", "catalog", "new")])
    rows.append([common.back("catalog")])
    title = "همه محصولات" if not category_id else "محصولات دسته"
    await common.reply(
        target,
        f"<b>🛍 {title}</b>\n\n"
        f"تعداد: {common.money(total)}\n"
        "🟢 فعال و قابل فروش · ⚪️ غیرفعال · 📦 آرشیو",
        rows,
    )


async def _show_product(target: CallbackQuery | Message, session: AsyncSession, admin: Admin, product_id: int) -> None:
    product = await catalog_service.get_product(session, product_id)
    if product is None:
        await common.reply(target, "محصول یافت نشد.", [[common.back("catalog")]])
        return
    stock = await catalog_service.available_plan_ids(session, product.id)
    plans = [p for p in product.plans if p.archived_at is None]

    # ``product_type`` should always be a ProductType, but tolerate a bare
    # string (legacy rows / manually seeded data) instead of crashing on
    # ``.value`` — a crash here rolls the whole wizard transaction back.
    raw_type = product.product_type
    if isinstance(raw_type, ProductType):
        type_label = TYPE_LABELS.get(raw_type, raw_type.value)
    else:
        try:
            type_label = TYPE_LABELS.get(ProductType(str(raw_type)), str(raw_type))
        except ValueError:
            type_label = str(raw_type)

    lines = [
        f"<b>🛍 {common.escape(product.title)}</b>",
        "",
        f"نوع: {type_label}",
        f"دسته: {common.escape(product.category.title) if product.category else '—'}",
        f"وضعیت: {'فعال' if product.is_active else 'غیرفعال'}"
        f" · فروش: {'روشن' if product.sales_enabled else 'خاموش'}"
        + (" · آرشیو شده" if product.archived_at else ""),
        f"پلن‌ها: {common.money(len(plans))} · موجودی کل: <b>{common.money(sum(stock.values()))}</b>",
        f"🎨 رنگ دکمه در فروشگاه: {BUTTON_STYLE_LABELS[_button_style_of(product)]}",
    ]
    if product.description:
        lines.extend(["", common.escape(product.description[:400])])

    rows: list[list] = []
    for plan in plans:
        rows.append(
            [
                common.btn(
                    f"📦 {plan.title} — {common.money(plan.price_irt)} ({common.money(stock.get(plan.id, 0))})",
                    "plan",
                    "",
                    plan.id,
                )
            ]
        )
    rows.append(
        [
            common.btn("➕ پلن جدید", "plans", "new", product.id),
            common.btn("✏️ ویرایش نام", "product", "rename", product.id),
        ]
    )
    # Per-product shop button colour (applies immediately, no publish step —
    # product buttons are dynamic, not part of the editable screens).
    current_style = _button_style_of(product)
    rows.append(
        [
            common.btn(
                f"{'✓ ' if key is current_style else ''}{label}",
                "product",
                f"style:{key.value}",
                product.id,
                style=normalize_style(key.value),
            )
            for key, label in (
                (ButtonStyle.DEFAULT, "پیش‌فرض"),
                (ButtonStyle.PRIMARY, "🔵 آبی"),
            )
        ]
    )
    rows.append(
        [
            common.btn(
                f"{'✓ ' if key is current_style else ''}{label}",
                "product",
                f"style:{key.value}",
                product.id,
                style=normalize_style(key.value),
            )
            for key, label in (
                (ButtonStyle.SUCCESS, "🟢 سبز"),
                (ButtonStyle.DANGER, "🔴 قرمز"),
            )
        ]
    )
    rows.append(
        [
            common.btn(
                "🔴 غیرفعال کن" if product.is_active else "🟢 فعال کن",
                "product",
                "toggle",
                product.id,
            ),
            common.btn(
                "⏸ فروش را ببند" if product.sales_enabled else "▶️ فروش را باز کن",
                "product",
                "sales",
                product.id,
            ),
        ]
    )
    rows.append(
        [
            common.btn("📥 ورود کانفیگ", "import", "start", product.id),
            common.btn("📦 انبار", "inventory", "product", product.id),
        ]
    )
    rows.append(
        [
            common.btn("🧬 کپی محصول", "product", "duplicate", product.id),
            common.btn("🗄 آرشیو", "product", "archive", product.id),
        ]
    )
    rows.append([common.btn("🗑 حذف کامل محصول", "product", "delete", product.id)])
    rows.append([common.back("catalog")])
    await common.reply(target, "\n".join(lines), rows)


async def _show_plan(target: CallbackQuery | Message, session: AsyncSession, admin: Admin, plan_id: int) -> None:
    plan = await catalog_service.get_plan(session, plan_id)
    if plan is None:
        await common.reply(target, "پلن یافت نشد.", [[common.back("catalog")]])
        return
    stock = await catalog_service.stock_for_plan(session, plan.product_id, plan.id)
    text = "\n".join(
        [
            f"<b>📦 {common.escape(plan.title)}</b>",
            "",
            f"قیمت: <b>{common.money(plan.price_irt)} تومان</b>",
            f"مدت: {common.money(plan.duration_days) + ' روز' if plan.duration_days else '—'}",
            f"حجم: {common.money(plan.traffic_gb) + ' گیگ' if plan.traffic_gb else '—'}",
            f"تعداد دستگاه: {common.money(plan.device_limit) if plan.device_limit else '—'}",
            f"موجودی قابل فروش: <b>{common.money(stock)}</b>",
            f"وضعیت: {'فعال' if plan.is_active else 'غیرفعال'}",
        ]
    )
    rows = [
        [
            common.btn("💰 قیمت", "plan", "edit_price", plan.id),
            common.btn("✏️ عنوان", "plan", "edit_title", plan.id),
        ],
        [
            common.btn("🗓 مدت (روز)", "plan", "edit_days", plan.id),
            common.btn("📶 حجم (گیگ)", "plan", "edit_traffic", plan.id),
        ],
        [
            common.btn("📱 دستگاه", "plan", "edit_devices", plan.id),
            common.btn(
                "🔴 غیرفعال" if plan.is_active else "🟢 فعال",
                "plan",
                "toggle",
                plan.id,
            ),
        ],
        [common.btn("🗄 آرشیو پلن", "plan", "archive", plan.id)],
        [common.btn("⬅️ محصول", "product", "", plan.product_id)],
    ]
    await common.reply(target, text, rows)


# --------------------------------------------------------------------------
# Callbacks: catalog / product / plan
# --------------------------------------------------------------------------
@router.callback_query(AdminCB.filter(F.section == "catalog"))
async def catalog_router(
    callback: CallbackQuery,
    callback_data: AdminCB,
    session: AsyncSession,
    admin: Admin | None,
    state: FSMContext,
) -> None:
    admin = common.guard(admin, Permission.PRODUCT_MANAGE)
    action = callback_data.action
    if action not in ("pick_cat", "pick_type"):
        # Section navigation clears other wizards' state; pick_cat /
        # pick_type are mid-wizard steps that consume the draft.
        # (The main products entry uses action "" — an earlier
        # version cleared only for "products" and missed it.)
        await state.clear()

    if action == "products":
        await _show_products(callback, session, admin, callback_data.item_id, callback_data.page)
    elif action == "newcat":
        await state.set_state(AdminStates.category_create)
        await state.update_data(step="title")
        await callback.answer()
        await common.reply(
            callback,
            "<b>➕ دسته‌بندی جدید</b>\n\nنام دسته را بفرستید. برای تعیین ترتیب:\n"
            "<code>نام | ترتیب</code>",
            [common.cancel_row()],
        )
    elif action == "new":
        await state.set_state(AdminStates.product_create)
        await state.update_data(step="title", draft={})
        await callback.answer()
        await common.reply(
            callback,
            "<b>➕ محصول جدید — گام ۱ از ۳</b>\n\nعنوان محصول را بفرستید.",
            [common.cancel_row()],
        )
    elif action == "pick_cat":
        data = await state.get_data()
        draft = dict(data.get("draft") or {})
        draft["category_id"] = callback_data.item_id or None
        await state.update_data(draft=draft, step="type")
        await callback.answer()
        await _ask_type(callback)
    elif action == "pick_type":
        data = await state.get_data()
        draft = dict(data.get("draft") or {})
        product_type = TYPE_BY_CODE.get(str(callback_data.item_id), ProductType.CONFIG)
        draft["product_type"] = product_type.value
        await callback.answer()
        try:
            product = await catalog_service.create_product(
                session,
                title=str(draft.get("title") or "بدون نام"),
                description=None,
                category_id=draft.get("category_id"),
                product_type=product_type,
            )
        except catalog_service.CatalogError as exc:
            await state.clear()
            await common.reply(callback, f"⚠️ {exc}", [[common.back("catalog")]])
            return
        # Clear the wizard state only once creation has actually succeeded:
        # clearing first meant a failure silently discarded the whole draft
        # while the admin stayed on the same screen with no explanation.
        await state.clear()
        await common.audit(session, admin, "catalog.product_created", entity_type="product", entity_id=product.id)
        await _show_product(callback, session, admin, product.id)
    else:
        await _show_categories(callback, session, admin, callback_data.page)


async def _ask_type(target: CallbackQuery) -> None:
    rows = [
        [common.btn("۱. کانفیگ تکی", "catalog", "pick_type", 1)],
        [common.btn("۲. اشتراک (Subscription)", "catalog", "pick_type", 2)],
        [common.btn("۳. کانفیگ + اشتراک", "catalog", "pick_type", 3)],
        common.cancel_row(),
    ]
    await common.reply(target, "<b>➕ محصول جدید — گام ۳ از ۳</b>\n\nنوع محصول را انتخاب کنید.", rows)


@router.callback_query(AdminCB.filter(F.section == "product"))
async def product_router(
    callback: CallbackQuery,
    callback_data: AdminCB,
    session: AsyncSession,
    admin: Admin | None,
    state: FSMContext,
) -> None:
    admin = common.guard(admin, Permission.PRODUCT_MANAGE)
    await state.clear()  # section navigation abandons other wizards
    product = await catalog_service.get_product(session, callback_data.item_id)
    if product is None:
        await callback.answer("محصول یافت نشد", show_alert=True)
        return

    action = callback_data.action
    if action.startswith("style:"):
        try:
            new_style = ButtonStyle(action.split(":", 1)[1].upper())
        except ValueError:
            await callback.answer("رنگ نامعتبر است", show_alert=True)
            return
        product.button_style = new_style
        await common.audit(
            session,
            admin,
            "catalog.product_button_style",
            entity_type="product",
            entity_id=product.id,
            data={"style": new_style.value},
        )
        await callback.answer(f"رنگ دکمه: {BUTTON_STYLE_LABELS[new_style]}")
        await _show_product(callback, session, admin, product.id)
        return
    if action == "rename":
        await state.set_state(AdminStates.product_edit)
        await state.update_data(field="title", product_id=product.id)
        await callback.answer()
        await common.reply(callback, "عنوان جدید محصول را بفرستید.", [common.cancel_row()])
        return
    if action == "toggle":
        product.is_active = not product.is_active
        if not product.is_active:
            product.sales_enabled = False
        await common.audit(
            session,
            admin,
            "catalog.product_toggled",
            entity_type="product",
            entity_id=product.id,
            data={"is_active": product.is_active},
        )
    elif action == "sales":
        product.sales_enabled = not product.sales_enabled
        if product.sales_enabled and not product.is_active:
            product.is_active = True
        await common.audit(
            session,
            admin,
            "catalog.product_sales_toggled",
            entity_type="product",
            entity_id=product.id,
            data={"sales_enabled": product.sales_enabled},
        )
    elif action == "duplicate":
        clone = await catalog_service.duplicate_product(session, product.id)
        await common.audit(session, admin, "catalog.product_duplicated", entity_type="product", entity_id=clone.id)
        await _show_product(callback, session, admin, clone.id)
        return
    elif action == "archive":
        await catalog_service.archive_product(session, product.id)
        await common.audit(session, admin, "catalog.product_archived", entity_type="product", entity_id=product.id)
        await callback.answer("آرشیو شد")
        await _show_categories(callback, session, admin, 0)
        return
    elif action == "delete":
        from sqlalchemy import func

        from vpnbot.db.models.inventory import InventoryItem

        stock_total = int(
            (
                await session.execute(
                    select(func.count(InventoryItem.id)).where(InventoryItem.product_id == product.id)
                )
            ).scalar_one()
        )
        plan_total = len([p for p in product.plans if p.archived_at is None])
        await callback.answer()
        await common.reply(
            callback,
            f"<b>🗑 حذف کامل «{common.escape(product.title)}»</b>\n\n"
            "این کار <b>برگشت‌ناپذیر</b> است:\n"
            f"• {common.money(plan_total)} پلن حذف می‌شود\n"
            f"• {common.money(stock_total)} ردیف موجودی (فروش‌نرفته) برای همیشه پاک می‌شود\n\n"
            "اگر محصول سفارش ثبت‌شده یا موجودی تحویل‌شده داشته باشد، حذف انجام نمی‌شود "
            "و باید آرشیوش کنید.\n\n"
            "مطمئنید؟",
            [common.yes_no("product", "delete_yes", product.id), [common.back("catalog")]],
        )
        return
    elif action == "delete_yes":
        try:
            removed = await catalog_service.delete_product(session, product.id)
        except catalog_service.CatalogError as exc:
            await common.alert(callback, f"⚠️ {exc}")
            await _show_product(callback, session, admin, product.id)
            return
        await common.audit(
            session,
            admin,
            "catalog.product_deleted",
            entity_type="product",
            entity_id=product.id,
            data={"title": product.title, **removed},
        )
        await callback.answer("محصول حذف شد")
        await _show_categories(callback, session, admin, 0)
        return
    await _show_product(callback, session, admin, product.id)


@router.callback_query(AdminCB.filter(F.section == "plans"))
async def plans_router(
    callback: CallbackQuery,
    callback_data: AdminCB,
    session: AsyncSession,
    admin: Admin | None,
    state: FSMContext,
) -> None:
    admin = common.guard(admin, Permission.PRODUCT_MANAGE)
    if callback_data.action != "new":
        await state.clear()
        await _show_product(callback, session, admin, callback_data.item_id)
        return
    await state.set_state(AdminStates.plan_create)
    await state.update_data(step="title", product_id=callback_data.item_id, draft={})
    await callback.answer()
    await common.reply(
        callback,
        "<b>➕ پلن جدید — گام ۱ از ۴</b>\n\nعنوان پلن را بفرستید (مثال: یک ماهه ۵۰ گیگ).",
        [common.cancel_row()],
    )


@router.callback_query(AdminCB.filter(F.section == "plan"))
async def plan_router(
    callback: CallbackQuery,
    callback_data: AdminCB,
    session: AsyncSession,
    admin: Admin | None,
    state: FSMContext,
) -> None:
    admin = common.guard(admin, Permission.PRODUCT_MANAGE)
    await state.clear()  # section navigation abandons other wizards
    plan = await catalog_service.get_plan(session, callback_data.item_id)
    if plan is None:
        await callback.answer("پلن یافت نشد", show_alert=True)
        return

    action = callback_data.action
    field_states = {
        "edit_price": ("price_irt", "قیمت جدید را به تومان بفرستید (مثال: ۲۵۰,۰۰۰)."),
        "edit_title": ("title", "عنوان جدید پلن را بفرستید."),
        "edit_days": ("duration_days", "مدت اعتبار به روز (۰ = نامحدود)."),
        "edit_traffic": ("traffic_gb", "حجم به گیگابایت (۰ = نامحدود)."),
        "edit_devices": ("device_limit", "تعداد دستگاه مجاز (۰ = نامحدود)."),
    }
    if action in field_states:
        field, prompt = field_states[action]
        await state.set_state(AdminStates.plan_edit)
        await state.update_data(field=field, plan_id=plan.id)
        await callback.answer()
        await common.reply(callback, prompt, [common.cancel_row()])
        return
    if action == "toggle":
        plan.is_active = not plan.is_active
        await common.audit(
            session,
            admin,
            "catalog.plan_toggled",
            entity_type="plan",
            entity_id=plan.id,
            data={"is_active": plan.is_active},
        )
    elif action == "archive":
        plan.is_active = False
        plan.archived_at = common.today_start()
        await common.audit(session, admin, "catalog.plan_archived", entity_type="plan", entity_id=plan.id)
        await callback.answer("آرشیو شد")
        await _show_product(callback, session, admin, plan.product_id)
        return
    await _show_plan(callback, session, admin, plan.id)


# --------------------------------------------------------------------------
# FSM text inputs
# --------------------------------------------------------------------------
@router.message(AdminStates.category_create)
async def category_input(message: Message, session: AsyncSession, admin: Admin | None, state: FSMContext) -> None:
    admin = common.guard(admin, Permission.PRODUCT_MANAGE)
    raw = (message.text or "").strip()
    if not raw:
        await message.answer("نام دسته را وارد کنید.")
        return
    title, _, order_raw = raw.partition("|")
    try:
        sort_order = int(order_raw.strip()) if order_raw.strip() else 0
    except ValueError:
        sort_order = 0
    category = await catalog_service.create_category(session, title=title.strip(), sort_order=sort_order)
    await common.audit(session, admin, "catalog.category_created", entity_type="category", entity_id=category.id)
    await state.clear()
    await message.answer(f"✅ دسته «{common.escape(category.title)}» ساخته شد.")
    await _show_categories(message, session, admin, 0)


@router.message(AdminStates.product_create)
async def product_input(message: Message, session: AsyncSession, admin: Admin | None, state: FSMContext) -> None:
    admin = common.guard(admin, Permission.PRODUCT_MANAGE)
    data = await state.get_data()
    draft = dict(data.get("draft") or {})
    title = (message.text or "").strip()
    if not title:
        await message.answer("عنوان محصول را وارد کنید.")
        return
    draft["title"] = title[:160]
    await state.update_data(draft=draft, step="category")

    categories = await catalog_service.list_categories(session, active_only=False)
    rows = [[common.btn(f"📁 {c.title}", "catalog", "pick_cat", c.id)] for c in categories]
    rows.append([common.btn("بدون دسته", "catalog", "pick_cat", 0)])
    rows.append(common.cancel_row())
    await common.reply(message, "<b>➕ محصول جدید — گام ۲ از ۳</b>\n\nدسته محصول را انتخاب کنید.", rows)


@router.message(AdminStates.product_edit)
async def product_edit_input(message: Message, session: AsyncSession, admin: Admin | None, state: FSMContext) -> None:
    admin = common.guard(admin, Permission.PRODUCT_MANAGE)
    data = await state.get_data()
    product = await catalog_service.get_product(session, int(data.get("product_id") or 0))
    value = (message.text or "").strip()
    if product is None or not value:
        await state.clear()
        await message.answer("عملیات لغو شد.")
        return
    if data.get("field") == "title":
        product.title = value[:160]
        await common.audit(session, admin, "catalog.product_renamed", entity_type="product", entity_id=product.id)
    await state.clear()
    await _show_product(message, session, admin, product.id)


@router.message(AdminStates.plan_create)
async def plan_input(message: Message, session: AsyncSession, admin: Admin | None, state: FSMContext) -> None:
    admin = common.guard(admin, Permission.PRODUCT_MANAGE)
    data = await state.get_data()
    draft = dict(data.get("draft") or {})
    step = data.get("step") or "title"
    raw = (message.text or "").strip()
    if not raw:
        await message.answer("مقدار را وارد کنید.")
        return

    if step == "title":
        draft["title"] = raw[:160]
        await state.update_data(draft=draft, step="price")
        await message.answer("قیمت پلن را به تومان بفرستید (مثال: ۲۵۰,۰۰۰).")
        return
    if step == "price":
        try:
            price = parse_amount_irt(raw)
        except MoneyError as exc:
            await message.answer(f"⚠️ {exc}")
            return
        if price <= 0:
            # Reject here, at the step that can still be corrected;
            # create_plan also refuses 0, but by the final step the
            # admin could no longer fix the price without restarting.
            await message.answer("⚠️ قیمت باید بزرگ‌تر از صفر باشد. دوباره بفرستید.")
            return
        draft["price_irt"] = price
        await state.update_data(draft=draft, step="days")
        await message.answer("مدت اعتبار به روز (۰ = نامحدود).")
        return
    if step == "days":
        draft["duration_days"] = _optional_int(raw)
        await state.update_data(draft=draft, step="traffic")
        await message.answer("حجم به گیگابایت (۰ = نامحدود).")
        return

    draft["traffic_gb"] = _optional_int(raw)
    product_id = int(data.get("product_id") or 0)
    try:
        plan = await catalog_service.create_plan(
            session,
            product_id=product_id,
            title=str(draft.get("title") or "پلن"),
            price_irt=int(draft.get("price_irt") or 0),
            duration_days=draft.get("duration_days"),
            traffic_gb=draft.get("traffic_gb"),
        )
    except catalog_service.CatalogError as exc:
        # Keep the wizard state/draft (same fix as product creation):
        # clearing first silently discarded title/price/days/traffic.
        await message.answer(f"⚠️ {exc}\n\nبرای شروع دوباره /cancel را بزنید.")
        return
    await state.clear()
    await common.audit(session, admin, "catalog.plan_created", entity_type="plan", entity_id=plan.id)
    await _show_plan(message, session, admin, plan.id)


@router.message(AdminStates.plan_edit)
async def plan_edit_input(message: Message, session: AsyncSession, admin: Admin | None, state: FSMContext) -> None:
    admin = common.guard(admin, Permission.PRODUCT_MANAGE)
    data = await state.get_data()
    plan = await catalog_service.get_plan(session, int(data.get("plan_id") or 0))
    field = str(data.get("field") or "")
    raw = (message.text or "").strip()
    if plan is None or not raw:
        await state.clear()
        await message.answer("عملیات لغو شد.")
        return

    try:
        if field == "price_irt":
            price = parse_amount_irt(raw)
            if price <= 0:
                # create_plan enforces > 0 but this edit path assigns the
                # ORM field directly, so a 0-price plan slipped through.
                await message.answer("⚠️ قیمت باید بزرگ‌تر از صفر باشد. دوباره بفرستید یا /cancel را بزنید.")
                return
            plan.price_irt = price
        elif field == "title":
            plan.title = raw[:160]
        elif field in {"duration_days", "traffic_gb", "device_limit"}:
            setattr(plan, field, _optional_int(raw))
    except MoneyError as exc:
        await message.answer(f"⚠️ {exc}")
        return

    await common.audit(
        session, admin, "catalog.plan_updated", entity_type="plan", entity_id=plan.id, data={"field": field}
    )
    await state.clear()
    await _show_plan(message, session, admin, plan.id)


def _optional_int(raw: str) -> int | None:
    try:
        value = int(raw.replace(",", "").replace("٬", ""))
    except ValueError:
        return None
    # The old body was ``return value or None``: negatives passed
    # through (truthy) and huge values reached 32-bit Integer columns
    # (duration_days / traffic_gb / device_limit) and crashed the
    # insert with a raw asyncpg DataError.
    if value <= 0 or value > 1_000_000:
        return None
    return value
