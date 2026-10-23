"""Round-25 regressions: pay-time reservation, forced cancel, throttle scope.

Split out of test_admin_regressions.py: that module outgrew the
GitHub connector's single-argument push limit, so new rounds get
their own module. Fixtures come from tests/conftest.py; the
``_stock`` helper is imported from the main regressions module.
"""

from __future__ import annotations

import pytest
from tests.test_admin_regressions import _stock  # noqa: F401  (re-exported helper)

# --- Round 25: pay-time reservation, forced cancel, throttle scope ---


async def test_wallet_pay_reserves_and_refuses_oversell(session) -> None:
    from tests.factories import make_catalog, make_user

    from vpnbot.db.enums import InventoryStatus, LedgerKind, OrderStatus
    from vpnbot.services import inventory as inventory_service
    from vpnbot.services import orders as order_service
    from vpnbot.services import payments as payment_service
    from vpnbot.services import wallet as wallet_service

    _, product, plan = await make_catalog(session)
    await _stock(session, product, plan, 1, "oversell")
    buyers = []
    for i in range(2):
        buyer = await make_user(session, telegram_id=900002601 + i)
        await wallet_service.credit(
            session, user_id=buyer.id, amount_irt=plan.price_irt,
            kind=LedgerKind.TOPUP, key=f"oversell:topup:{i}", reason="t",
        )
        order = await order_service.create_order(
            session, user_id=buyer.id, plan_id=plan.id, product=product, plan=plan
        )
        buyers.append((buyer, order))
    await session.commit()

    # First payer claims the only pair at payment time.
    await payment_service.pay_order_with_wallet(session, order=buyers[0][1], user_id=buyers[0][0].id)
    item = await inventory_service.item_for_order(session, buyers[0][1].id)
    assert item is not None and item.status is InventoryStatus.RESERVED

    # Second payer is refused BEFORE any money moves.
    with pytest.raises(payment_service.PaymentError):
        await payment_service.pay_order_with_wallet(session, order=buyers[1][1], user_id=buyers[1][0].id)
    assert await wallet_service.balance(session, buyers[1][0].id) == plan.price_irt
    loser = await order_service.get_order(session, buyers[1][1].id)
    assert loser is not None and loser.status is OrderStatus.PENDING_PAYMENT


async def test_admin_force_cancel_settles_failed_delivery(session) -> None:
    from tests.factories import make_catalog, make_user

    from vpnbot.db.enums import InventoryStatus, LedgerKind, OrderStatus, PaymentStatus
    from vpnbot.services import inventory as inventory_service
    from vpnbot.services import orders as order_service
    from vpnbot.services import payments as payment_service
    from vpnbot.services import wallet as wallet_service

    user = await make_user(session, telegram_id=900002611)
    _, product, plan = await make_catalog(session)
    await _stock(session, product, plan, 1, "forcecancel")
    await wallet_service.credit(
        session, user_id=user.id, amount_irt=plan.price_irt,
        kind=LedgerKind.TOPUP, key="fc:topup", reason="t",
    )
    order = await order_service.create_order(
        session, user_id=user.id, plan_id=plan.id, product=product, plan=plan
    )
    payment = await payment_service.pay_order_with_wallet(session, order=order, user_id=user.id)
    item = await inventory_service.item_for_order(session, order.id)
    assert item is not None and item.status is InventoryStatus.RESERVED
    # The worker tried; the customer had blocked the bot.
    await inventory_service.log_attempt(
        session, order_id=order.id, item_id=item.id,
        outcome="FAILED", error_code="TelegramForbiddenError",
    )
    await session.commit()

    # Plain cancel still refuses; the admin's forced cancel settles:
    # refunded in full, the pair quarantined (no repool) — and since
    # round 26 it leaves the RESERVED limbo for DISABLED.
    with pytest.raises(order_service.OrderError):
        await order_service.cancel_order(session, order.id, reason="x")
    settled = await order_service.cancel_order(session, order.id, reason="ربات بلاک شده", force=True)
    assert settled.status is OrderStatus.CANCELLED
    assert await wallet_service.balance(session, user.id) == plan.price_irt
    item_after = await inventory_service.item_for_order(session, order.id)
    assert item_after is not None and item_after.status is InventoryStatus.DISABLED
    await session.refresh(payment)
    assert payment.status is PaymentStatus.CANCELLED


async def test_force_cancel_still_refused_after_sent_attempt(session) -> None:
    from tests.factories import make_catalog, make_user

    from vpnbot.db.enums import LedgerKind
    from vpnbot.services import inventory as inventory_service
    from vpnbot.services import orders as order_service
    from vpnbot.services import payments as payment_service
    from vpnbot.services import wallet as wallet_service

    user = await make_user(session, telegram_id=900002621)
    _, product, plan = await make_catalog(session)
    await _stock(session, product, plan, 1, "sentguard")
    await wallet_service.credit(
        session, user_id=user.id, amount_irt=plan.price_irt,
        kind=LedgerKind.TOPUP, key="sg:topup", reason="t",
    )
    order = await order_service.create_order(
        session, user_id=user.id, plan_id=plan.id, product=product, plan=plan
    )
    await payment_service.pay_order_with_wallet(session, order=order, user_id=user.id)
    item = await inventory_service.item_for_order(session, order.id)
    assert item is not None
    await inventory_service.log_attempt(
        session, order_id=order.id, item_id=item.id, outcome="SENT"
    )
    await session.commit()
    with pytest.raises(order_service.OrderError):
        await order_service.cancel_order(session, order.id, reason="x", force=True)


async def test_throttle_evidence_delay_scoped_to_fsm_state() -> None:
    import time as _time
    from datetime import UTC, datetime
    from types import SimpleNamespace

    from aiogram.fsm.storage.base import StorageKey
    from aiogram.fsm.storage.memory import MemoryStorage
    from aiogram.types import Chat, Message, PhotoSize
    from aiogram.types import User as TgUser

    from vpnbot.bot.middlewares.context import ThrottleMiddleware

    storage = MemoryStorage()
    mw = ThrottleMiddleware(
        storage=storage, message_capacity=1, message_refill=1.0, evidence_max_wait=2.5
    )
    key = StorageKey(bot_id=1, chat_id=100, user_id=42)
    data = {"bot": SimpleNamespace(id=1)}
    chat = Chat(id=100, type="private")
    tg_user = TgUser(id=42, is_bot=False, first_name="تست")

    def photo():  # noqa: ANN202
        return Message(
            message_id=1,
            date=datetime.now(UTC),
            chat=chat,
            from_user=tg_user,
            photo=[PhotoSize(file_id="p", file_unique_id="u", width=10, height=10)],
        )

    async def handler(event, data):  # noqa: ANN001, ANN202
        return "handled"

    # First photo burns the single token; the second is over budget.
    assert await mw(handler, photo(), data) == "handled"
    # NOT in an evidence state: shed at once, no never-drop sleep.
    started = _time.monotonic()
    assert await mw(handler, photo(), data) is None
    assert _time.monotonic() - started < 0.9

    # In a receipt state the same burst photo is delayed, not dropped.
    await storage.set_state(key, "CheckoutStates:awaiting_receipt")
    mw2 = ThrottleMiddleware(
        storage=storage, message_capacity=1, message_refill=1.0, evidence_max_wait=2.5
    )
    assert await mw2(handler, photo(), data) == "handled"
    assert await mw2(handler, photo(), data) == "handled"
