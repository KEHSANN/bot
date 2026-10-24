"""Round-29 regressions: blocked-delivery gate, block_user order-direct
cancel, card/crypto payable checks + approve-time reservation, the
enqueue freshness/status guard, the resend cooldown, the COMBO message
split, and the HTML-error escape.

Split into its own module like rounds 25-28 (the main regressions file
is at the GitHub connector's single-argument push ceiling).
"""

from __future__ import annotations

from tests.test_admin_regressions import _stock  # noqa: F401  (shared helper)


async def test_blocked_user_gets_no_config(session, session_factory, cipher) -> None:
    """The fulfilment gate SKIPs a blocked customer instead of sending."""
    from tests.factories import make_catalog, make_user

    from vpnbot.db.enums import (
        InventoryStatus,
        LedgerKind,
    )
    from vpnbot.db.models.users import UserStatus
    from vpnbot.services import fulfillment as fulfillment_service
    from vpnbot.services import inventory as inventory_service
    from vpnbot.services import orders as order_service
    from vpnbot.services import payments as payment_service
    from vpnbot.services import wallet as wallet_service
    from vpnbot.services.fulfillment import FulfillOutcome

    user = await make_user(session, telegram_id=900002901)
    _, product, plan = await make_catalog(session)
    await _stock(session, product, plan, 1, "blockedgate")
    await wallet_service.credit(
        session, user_id=user.id, amount_irt=plan.price_irt,
        kind=LedgerKind.TOPUP, key="bg:topup", reason="t",
    )
    order = await order_service.create_order(
        session, user_id=user.id, plan_id=plan.id, product=product, plan=plan
    )
    await payment_service.pay_order_with_wallet(session, order=order, user_id=user.id)
    item = await inventory_service.item_for_order(session, order.id)
    assert item is not None and item.status is InventoryStatus.RESERVED
    user.status = UserStatus.BLOCKED
    await session.commit()


    sent: list[tuple] = []

    async def deliver(telegram_id, config, sub_url, title, public_id):
        sent.append((telegram_id, config))

    result = await fulfillment_service.fulfill_order(
        session_factory,
        order_id=order.id,
        cipher=cipher,
        deliver=deliver,
    )
    assert result.outcome is FulfillOutcome.SKIPPED
    assert result.detail == "user blocked"
    assert sent == []


async def test_block_user_cancels_paymentless_and_rejected_orders(session) -> None:
    """block_user cancels PENDING_PAYMENT orders even without a payment
    row, or with a REJECTED payment — the old Payment-join missed both."""
    from tests.factories import make_catalog, make_user

    from vpnbot.db.enums import OrderStatus, PaymentMethod, PaymentStatus
    from vpnbot.services import orders as order_service
    from vpnbot.services import payments as payment_service
    from vpnbot.services import user_admin as user_admin_service

    user = await make_user(session, telegram_id=900002902)
    _, product, plan = await make_catalog(session)
    await _stock(session, product, plan, 2, "blockuser")
    from vpnbot.services.settings import CARD_SETTINGS, SettingsService

    await SettingsService(session).set(
        CARD_SETTINGS,
        {"enabled": True, "recipient_name": "تست", "card_number": "6037", "bank_name": "تست"},
    )
    # Order A: no payment row at all (checkout preview).
    order_a = await order_service.create_order(
        session, user_id=user.id, plan_id=plan.id, product=product, plan=plan
    )
    # Order B: payment REJECTED, order still PENDING_PAYMENT.
    order_b = await order_service.create_order(
        session, user_id=user.id, plan_id=plan.id, product=product, plan=plan
    )
    payment_b = await payment_service.start_payment(
        session, order=order_b, method=PaymentMethod.CARD
    )
    payment_b.status = PaymentStatus.REJECTED
    await session.commit()

    await user_admin_service.block_user(session, user_id=user.id, admin_id=1)
    await session.refresh(order_a)
    await session.refresh(order_b)
    assert order_a.status is OrderStatus.CANCELLED
    assert order_b.status is OrderStatus.CANCELLED


async def test_receipt_refused_when_product_disabled_or_shelf_empty(session) -> None:
    """_require_order_payable re-checks the catalogue at submit time."""
    from tests.factories import make_catalog, make_user

    from vpnbot.db.enums import PaymentMethod
    from vpnbot.services import orders as order_service
    from vpnbot.services import payments as payment_service

    user = await make_user(session, telegram_id=900002903)
    _, product, plan = await make_catalog(session)
    await _stock(session, product, plan, 1, "payablecheck")
    from vpnbot.services.settings import CARD_SETTINGS, SettingsService

    await SettingsService(session).set(
        CARD_SETTINGS,
        {"enabled": True, "recipient_name": "تست", "card_number": "6037", "bank_name": "تست"},
    )
    order = await order_service.create_order(
        session, user_id=user.id, plan_id=plan.id, product=product, plan=plan
    )
    payment = await payment_service.start_payment(
        session, order=order, method=PaymentMethod.CARD
    )
    await session.commit()

    # Disabled product: receipt refused even though stock exists.
    product.sales_enabled = False
    await session.commit()
    try:
        await payment_service.submit_card_receipt(
            session, payment_id=payment.id, user_id=user.id,
            file_id="f1", file_type="photo",
        )
    except payment_service.PaymentError as exc:
        assert "قابل خرید نیست" in str(exc)
    else:  # pragma: no cover - the fix must refuse here
        raise AssertionError("receipt accepted for a disabled product")

    # Re-enabled but empty shelf: refused too.
    product.sales_enabled = True
    from sqlalchemy import delete

    from vpnbot.db.models.inventory import InventoryItem

    await session.execute(
        delete(InventoryItem).where(InventoryItem.product_id == product.id)
    )
    await session.commit()
    try:
        await payment_service.submit_card_receipt(
            session, payment_id=payment.id, user_id=user.id,
            file_id="f1", file_type="photo",
        )
    except payment_service.PaymentError as exc:
        assert "موجودی" in str(exc)
    else:  # pragma: no cover
        raise AssertionError("receipt accepted with an empty shelf")


async def test_approve_reserves_pair_and_waits_on_empty_shelf(session) -> None:
    """Approve-time reservation (round 29) with the restored wait
    (round 29b): a pair on the shelf is held at approval, closing the
    wallet-buyer race; an empty shelf no longer fails the approval —
    the order waits and a restock revives it."""
    from tests.factories import make_catalog, make_user

    from vpnbot.db.enums import (
        InventoryStatus,
        OrderStatus,
        PaymentMethod,
        PaymentStatus,
    )
    from vpnbot.services import inventory as inventory_service
    from vpnbot.services import orders as order_service
    from vpnbot.services import payments as payment_service

    user = await make_user(session, telegram_id=900002904)
    _, product, plan = await make_catalog(session)
    await _stock(session, product, plan, 2, "approvereserve")
    from vpnbot.services.auth import bootstrap_owner
    from vpnbot.services.settings import CARD_SETTINGS, SettingsService

    admin = await bootstrap_owner(session, telegram_id=900002908)
    await SettingsService(session).set(
        CARD_SETTINGS,
        {"enabled": True, "recipient_name": "تست", "card_number": "6037", "bank_name": "تست"},
    )
    order = await order_service.create_order(
        session, user_id=user.id, plan_id=plan.id, product=product, plan=plan
    )
    payment = await payment_service.start_payment(
        session, order=order, method=PaymentMethod.CARD
    )
    payment.status = PaymentStatus.PENDING_REVIEW
    await session.commit()

    approved = await payment_service.approve_payment(
        session, payment_id=payment.id, admin_id=admin.id
    )
    assert approved.status is PaymentStatus.APPROVED
    item = await inventory_service.item_for_order(session, order.id)
    assert item is not None and item.status is InventoryStatus.RESERVED
    await session.refresh(order)
    assert order.status is OrderStatus.FULFILLMENT_PENDING

    # Second order, empty shelf now: approval still succeeds and the
    # order waits — no pair held, nothing lost, restock revives it.
    user2 = await make_user(session, telegram_id=900002905)
    order2 = await order_service.create_order(
        session, user_id=user2.id, plan_id=plan.id, product=product, plan=plan
    )
    payment2 = await payment_service.start_payment(
        session, order=order2, method=PaymentMethod.CARD
    )
    payment2.status = PaymentStatus.PENDING_REVIEW
    # Empty the shelf AFTER order2 exists: approval must now fail.
    from sqlalchemy import delete

    from vpnbot.db.models.inventory import InventoryItem

    await session.execute(
        delete(InventoryItem).where(
            InventoryItem.product_id == product.id,
            InventoryItem.status == InventoryStatus.AVAILABLE,
        )
    )
    await session.commit()
    approved2 = await payment_service.approve_payment(
        session, payment_id=payment2.id, admin_id=admin.id
    )
    assert approved2.status is PaymentStatus.APPROVED
    await session.refresh(order2)
    assert order2.status is OrderStatus.FULFILLMENT_PENDING
    assert await inventory_service.item_for_order(session, order2.id) is None


async def test_enqueue_refuses_stale_fulfilled_order(session, session_factory) -> None:
    """populate_existing: a cached FULFILLMENT_PENDING copy must not
    gain a fresh job after another session fulfilled the order."""
    from tests.factories import make_catalog, make_user

    from vpnbot.db.enums import (
        LedgerKind,
        OrderStatus,
    )
    from vpnbot.services import fulfillment as fulfillment_service
    from vpnbot.services import orders as order_service
    from vpnbot.services import payments as payment_service
    from vpnbot.services import wallet as wallet_service

    user = await make_user(session, telegram_id=900002906)
    _, product, plan = await make_catalog(session)
    await _stock(session, product, plan, 1, "staleenqueue")
    await wallet_service.credit(
        session, user_id=user.id, amount_irt=plan.price_irt,
        kind=LedgerKind.TOPUP, key="se:topup", reason="t",
    )
    order = await order_service.create_order(
        session, user_id=user.id, plan_id=plan.id, product=product, plan=plan
    )
    await payment_service.pay_order_with_wallet(session, order=order, user_id=user.id)
    await session.commit()
    # `order` is now cached in this session as FULFILLMENT_PENDING.
    assert order.status is OrderStatus.FULFILLMENT_PENDING

    # A second session fulfils it for real (order status only — the
    # test's point is the order row, not the item).
    async with session_factory() as other, other.begin():
        other_order = await order_service.get_order(other, order.id, for_update=True)
        assert other_order is not None
        other_order.status = OrderStatus.FULFILLED

    assert await fulfillment_service.enqueue_fulfill_job(session, order) is False
    # And the cached copy was refreshed, not left stale.
    assert order.status is OrderStatus.FULFILLED


async def test_resend_cooldown_blocks_second_tap(session, monkeypatch) -> None:
    """The resend button accepts one tap per 60s per user."""
    from aiogram.types import CallbackQuery
    from aiogram.types import User as TgUser
    from tests.factories import make_catalog, make_user

    from vpnbot.bot.handlers.customer import orders as orders_handler
    from vpnbot.bot.keyboards.callbacks import OrderResendCB
    from vpnbot.db.enums import LedgerKind
    from vpnbot.services import orders as order_service
    from vpnbot.services import payments as payment_service
    from vpnbot.services import wallet as wallet_service

    user = await make_user(session, telegram_id=900002907)
    _, product, plan = await make_catalog(session)
    await _stock(session, product, plan, 1, "resendcool")
    await wallet_service.credit(
        session, user_id=user.id, amount_irt=plan.price_irt,
        kind=LedgerKind.TOPUP, key="rc:topup", reason="t",
    )
    order = await order_service.create_order(
        session, user_id=user.id, plan_id=plan.id, product=product, plan=plan
    )
    await payment_service.pay_order_with_wallet(session, order=order, user_id=user.id)
    # The wallet pay already queued the job; remove it so the first
    # tap is a genuine fresh enqueue, not the already-queued path.
    from sqlalchemy import delete

    from vpnbot.db.models.system import Job

    await session.execute(delete(Job).where(Job.idempotency_key == f"fulfill:{order.id}"))
    await session.commit()
    # The order instance was created in THIS session, before the
    # payment row existed, so its `payment` relationship is cached
    # as None — a handler session in production always loads fresh.
    # Expunge and re-fetch so the handler sees the real row.
    session.expunge_all()
    order = await order_service.user_order(session, order_id=order.id, user_id=user.id)
    assert order is not None

    answers: list[str] = []

    async def fake_alert(callback, text: str) -> None:
        answers.append(text)

    monkeypatch.setattr(orders_handler, "alert", fake_alert)

    def _callback() -> CallbackQuery:
        return CallbackQuery(
            id="rc1",
            from_user=TgUser(id=user.telegram_id, is_bot=False, first_name="t"),
            chat_instance="rc",
            message=None,
            data="x",
        )

    await orders_handler.resend_config(
        _callback(), OrderResendCB(order_id=order.id), session, user
    )
    assert any("ثبت شد" in a for a in answers)
    await orders_handler.resend_config(
        _callback(), OrderResendCB(order_id=order.id), session, user
    )
    assert any("صبر کنید" in a for a in answers)


async def test_combo_pair_splits_over_telegram_limit() -> None:
    """A 3500-char config + 1000-char link goes out as two messages."""
    from vpnbot.bot.sender import send_config_pair

    class FakeBot:
        def __init__(self) -> None:
            self.sent: list[str] = []

        async def send_message(self, *, chat_id, text, **kwargs):
            self.sent.append(text)

    bot = FakeBot()
    config = "vless://user@host:443#" + "x" * 3480
    sub = "https://sub.example/" + "y" * 980
    assert len(config) + len(sub) > 4000
    await send_config_pair(bot, 123, config, sub, "پلن تست", "ORD-1")
    assert len(bot.sent) == 2
    assert all(len(text) <= 4096 for text in bot.sent)
    assert config in bot.sent[0]
    assert sub in bot.sent[1]

    # A normal pair still goes out as one message.
    bot2 = FakeBot()
    await send_config_pair(bot2, 123, "vless://short", "https://sub.example/short", "پلن", "ORD-2")
    assert len(bot2.sent) == 1


def test_html_error_escape_is_parse_safe() -> None:
    """The escape used at the three TemplateError answer sites turns
    raw tags into entities, so the error message itself cannot crash
    the HTML-parsed answer (verified by reading the call sites)."""
    from vpnbot.bot.handlers.admin.common import escape

    assert escape("تگ HTML مجاز نیست: <script>") == "تگ HTML مجاز نیست: &lt;script&gt;"
    assert escape("کاراکتر < یا > باز") == "کاراکتر &lt; یا &gt; باز"
