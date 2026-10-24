"""Shop browsing: categories, products, plans and checkout hand-off.

Nothing here creates an order. The customer picks a plan, sees the confirmation
screen, and only then chooses a payment method — ``checkout.py`` creates the
order at that moment. That keeps abandoned browsing from filling the orders
table with empty rows.
"""

from __future__ import annotations

from aiogram import F, Router
from aiogram.fsm.context import FSMContext
from aiogram.types import CallbackQuery, InlineKeyboardButton
from sqlalchemy.ext.asyncio import AsyncSession

from vpnbot.bot.handlers.customer.common import alert, base_context, money, sender_for
from vpnbot.bot.keyboards.callbacks import (
    PRODUCT_PAGE_SIZE,
    BuyCB,
    CategoryCB,
    MenuCB,
    PlanCB,
    ProductCB,
    WalletBuyCB,
    paginator,
)
from vpnbot.db.models.catalog import Plan
from vpnbot.db.models.users import User
from vpnbot.money import format_irt_plain
from vpnbot.services import catalog as catalog_service
from vpnbot.services import wallet as wallet_service
from vpnbot.services.screens import (
    STAGE_CHECKOUT,
    STAGE_NO_STOCK,
    STAGE_PRODUCT_DETAIL,
    STAGE_PRODUCT_LIST,
    STAGE_SHOP,
    normalize_style,
)
from vpnbot.services.settings import SHOP_SETTINGS, SettingsService

router = Router(name="customer.shop")


def _back_row(text: str, action: str) -> list[InlineKeyboardButton]:
    return [InlineKeyboardButton(text=text, callback_data=MenuCB(action=action).pack())]


def _plan_details(plan: Plan) -> str:
    """Spec block for the product-detail screen.

    Rendered through ``{plan_details}``, so every value is HTML-escaped by the
    renderer; an admin-authored description cannot inject markup here.
    """
    lines = [f"\n\nپلن: {plan.title}", f"قیمت: {format_irt_plain(plan.price_irt)} تومان"]
    if plan.duration_days:
        lines.append(f"مدت: {plan.duration_days} روز")
    if plan.traffic_gb:
        lines.append(f"حجم: {plan.traffic_gb} گیگابایت")
    if plan.device_limit:
        lines.append(f"تعداد دستگاه: {plan.device_limit}")
    if plan.description:
        lines.extend(["", plan.description])
    return "\n".join(lines)


@router.callback_query(MenuCB.filter(F.action.in_({"shop", "cats"})))
async def open_shop(
    callback: CallbackQuery, session: AsyncSession, user: User | None, state: FSMContext
) -> None:
    from vpnbot.bot.handlers.customer.menu import cancel_open_order_from_state

    await cancel_open_order_from_state(session, user, state)
    await state.clear()  # navigation resets any in-progress wizard state
    categories = await catalog_service.list_categories(session)
    rows = [
        [InlineKeyboardButton(text=c.title, callback_data=CategoryCB(category_id=c.id).pack())]
        for c in categories
    ]
    rows.append(
        [InlineKeyboardButton(text="🛒 همه محصولات", callback_data=CategoryCB(category_id=0).pack())]
    )
    await sender_for(callback.bot, session).show(
        callback, STAGE_SHOP, base_context(user), extra_rows=rows
    )


async def _show_products(
    callback: CallbackQuery,
    session: AsyncSession,
    user: User | None,
    *,
    category_id: int,
    page: int,
) -> None:
    products, total = await catalog_service.list_products(
        session,
        category_id=category_id or None,
        offset=min(max(0, page), 100_000) * PRODUCT_PAGE_SIZE,
        limit=PRODUCT_PAGE_SIZE,
    )
    total_pages = max(1, (total + PRODUCT_PAGE_SIZE - 1) // PRODUCT_PAGE_SIZE)
    clamped = min(max(0, page), total_pages - 1)
    if clamped != page:
        # A stale/forged page beyond the end used to render an empty list;
        # the query above already ran with the unclamped offset.
        page = clamped
        products, total = await catalog_service.list_products(
            session,
            category_id=category_id or None,
            offset=page * PRODUCT_PAGE_SIZE,
            limit=PRODUCT_PAGE_SIZE,
        )

    stock = await catalog_service.available_counts(session, [p.id for p in products])
    rows: list[list[InlineKeyboardButton]] = []
    for product in products:
        in_stock = stock.get(product.id, 0) > 0
        label = product.title if in_stock else f"{product.title} — ناموجود"
        payload = (
            ProductCB(product_id=product.id).pack()
            if in_stock
            else MenuCB(action="noop").pack()
        )
        # Per-product button colour, set by the admin on the product screen.
        raw_style = getattr(product, "button_style", None)
        style = normalize_style(raw_style.value if hasattr(raw_style, "value") else raw_style)
        rows.append(
            [InlineKeyboardButton(text=label, callback_data=payload, **({"style": style} if style else {}))]
        )

    # Keep the category in the pager, otherwise "next" would fall back to all products.
    if category_id:
        nav = [
            InlineKeyboardButton(
                text="◀️ قبلی", callback_data=CategoryCB(category_id=category_id, page=page - 1).pack()
            )
            if page > 0
            else None,
            InlineKeyboardButton(
                text=f"{page + 1}/{total_pages}",
                callback_data=MenuCB(action="noop").pack(),
            )
            if total_pages > 1
            else None,
            InlineKeyboardButton(
                text="بعدی ▶️", callback_data=CategoryCB(category_id=category_id, page=page + 1).pack()
            )
            if page + 1 < total_pages
            else None,
            InlineKeyboardButton(text="🏠", callback_data=MenuCB(action="home").pack()),
        ]
    else:
        nav = paginator("prods", page, total_pages)

    await sender_for(callback.bot, session).show(
        callback,
        STAGE_PRODUCT_LIST,
        base_context(user),
        extra_rows=rows + [[b for b in nav if b is not None]],
    )


@router.callback_query(CategoryCB.filter())
async def open_category(
    callback: CallbackQuery, callback_data: CategoryCB, session: AsyncSession, user: User | None
) -> None:
    await _show_products(
        callback, session, user, category_id=callback_data.category_id, page=callback_data.page
    )


@router.callback_query(MenuCB.filter(F.action == "prods"))
async def products_page(
    callback: CallbackQuery,
    callback_data: MenuCB,
    session: AsyncSession,
    user: User | None,
    state: FSMContext,
) -> None:
    from vpnbot.bot.handlers.customer.menu import cancel_open_order_from_state

    await cancel_open_order_from_state(session, user, state)
    await state.clear()
    await _show_products(callback, session, user, category_id=0, page=callback_data.page)


@router.callback_query(ProductCB.filter())
async def open_product(
    callback: CallbackQuery, callback_data: ProductCB, session: AsyncSession, user: User | None
) -> None:
    product = await catalog_service.get_product(session, callback_data.product_id)
    if product is None or not product.is_on_sale:
        await alert(callback, "این محصول در دسترس نیست")
        return

    stock_map = await catalog_service.available_plan_ids(session, product.id)
    rows = [
        [
            InlineKeyboardButton(
                text=(
                    f"{plan.title} — {format_irt_plain(plan.price_irt)} تومان"
                    + ("" if stock_map.get(plan.id, 0) > 0 else " (ناموجود)")
                ),
                callback_data=(
                    PlanCB(plan_id=plan.id).pack()
                    if stock_map.get(plan.id, 0) > 0
                    else MenuCB(action="noop").pack()
                ),
            )
        ]
        for plan in product.plans
        if plan.is_on_sale
    ]

    sender = sender_for(callback.bot, session)
    if not any(stock_map.get(plan.id, 0) > 0 for plan in product.plans if plan.is_on_sale):
        await sender.show(callback, STAGE_NO_STOCK, base_context(user, product_title=product.title))
        return

    await sender.show(
        callback,
        STAGE_PRODUCT_DETAIL,
        base_context(user, product_title=product.title),
        extra_rows=rows + [_back_row("🔙 بازگشت", "prods")],
    )


async def _wallet_buy_row(
    session: AsyncSession, user: User | None, plan_id: int
) -> list[InlineKeyboardButton] | None:
    """The buy-with-wallet button, or None when the shop wallet is off.

    Code-side (not part of the editable screens) so it appears on every
    deployment without the owner editing screen templates.
    """
    if user is None:
        return None
    shop = await SettingsService(session).get(SHOP_SETTINGS)
    if not shop.get("wallet_enabled", True):
        return None
    balance = await wallet_service.balance(session, user.id)
    return [
        InlineKeyboardButton(
            text=f"👛 خرید با کیف پول (موجودی: {format_irt_plain(balance)} تومان)",
            callback_data=WalletBuyCB(plan_id=plan_id).pack(),
            style="primary",
        )
    ]


@router.callback_query(PlanCB.filter())
async def open_plan(
    callback: CallbackQuery, callback_data: PlanCB, session: AsyncSession, user: User | None
) -> None:
    plan = await catalog_service.get_plan(session, callback_data.plan_id)
    if plan is None or not plan.is_on_sale:
        await alert(callback, "این پلن در دسترس نیست")
        return
    product = await catalog_service.get_product(session, plan.product_id)
    if product is None or not product.is_on_sale:
        await alert(callback, "این محصول در دسترس نیست")
        return
    if await catalog_service.stock_for_plan(session, product.id, plan.id) <= 0:
        await sender_for(callback.bot, session).show(
            callback, STAGE_NO_STOCK, base_context(user, product_title=product.title)
        )
        return

    rows = [
        [
            InlineKeyboardButton(
                text="💳 انتخاب روش پرداخت",
                callback_data=BuyCB(plan_id=plan.id).pack(),
                style="success",
            )
        ]
    ]
    wallet_row = await _wallet_buy_row(session, user, plan.id)
    if wallet_row is not None:
        rows.append(wallet_row)
    rows.append(
        [InlineKeyboardButton(text="🔙 بازگشت", callback_data=ProductCB(product_id=product.id).pack())]
    )
    await sender_for(callback.bot, session).show(
        callback,
        STAGE_PRODUCT_DETAIL,
        base_context(
            user,
            product_title=product.title,
            plan_title=plan.title,
            plan_details=_plan_details(plan),
            price=money(plan.price_irt),
        ),
        extra_rows=rows,
    )


@router.callback_query(BuyCB.filter())
async def begin_checkout(
    callback: CallbackQuery,
    callback_data: BuyCB,
    session: AsyncSession,
    user: User | None,
    state: FSMContext,
) -> None:
    """Stash the chosen plan and show the confirmation screen.

    Re-validates everything: the callback payload is client-supplied.
    """
    if user is None or user.is_blocked:
        await alert(callback, "حساب شما فعال نیست")
        return
    plan = await catalog_service.get_plan(session, callback_data.plan_id)
    if plan is None or not plan.is_on_sale:
        await alert(callback, "این پلن در دسترس نیست")
        return
    product = await catalog_service.get_product(session, plan.product_id)
    if product is None or not product.is_on_sale:
        await alert(callback, "این محصول در دسترس نیست")
        return
    if await catalog_service.stock_for_plan(session, product.id, plan.id) <= 0:
        await sender_for(callback.bot, session).show(
            callback, STAGE_NO_STOCK, base_context(user, product_title=product.title)
        )
        return

    await state.clear()
    await state.update_data(plan_id=plan.id)
    wallet_row = await _wallet_buy_row(session, user, plan.id)
    await sender_for(callback.bot, session).show(
        callback,
        STAGE_CHECKOUT,
        base_context(
            user,
            product_title=product.title,
            plan_title=plan.title,
            price=money(plan.price_irt),
        ),
        extra_rows=[wallet_row] if wallet_row is not None else [],
    )
