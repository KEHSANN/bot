"""Invariant #2: nothing is delivered until an admin approves the payment.

The whole manual-payment path is exercised here against the real database:
order -> card details -> receipt -> review -> approval -> queued job ->
fulfilment. The delivery callback is the only fake, because it is the network
boundary; everything behind it (locking, reservation, decryption, audit) is
real.
"""

from __future__ import annotations

import pytest
from sqlalchemy import func, select
from tests.factories import make_catalog, make_inventory, make_user

from vpnbot.db.enums import (
    DeliveryStatus,
    InventoryStatus,
    JobKind,
    OrderStatus,
    PaymentMethod,
    PaymentStatus,
)
from vpnbot.db.models.orders import Payment
from vpnbot.db.models.system import Job
from vpnbot.services import fulfillment
from vpnbot.services import inventory as inventory_service
from vpnbot.services import orders as order_service
from vpnbot.services import payments as payment_service
from vpnbot.services.auth import bootstrap_owner
from vpnbot.services.settings import CARD_SETTINGS, SettingsService


async def _prepare_card_payment(session, cipher, secret):
    user = await make_user(session)
    _, product, plan = await make_catalog(session)
    await make_inventory(
        session, cipher=cipher, product=product, plan=plan, secret=secret, count=1
    )
    await SettingsService(session).set(
        CARD_SETTINGS,
        {
            "enabled": True,
            "recipient_name": "تست",
            "card_number": "6037-0000-0000-0000",
            "bank_name": "تست",
        },
    )
    order = await order_service.create_order(
        session, user_id=user.id, plan_id=plan.id, product=product, plan=plan
    )
    payment = await payment_service.start_payment(
        session, order=order, method=PaymentMethod.CARD
    )
    return user, order, payment


@pytest.fixture
async def admin(session):
    """A real admin row, so review FKs are satisfied."""
    return await bootstrap_owner(session, telegram_id=900000999)


class DeliverRecorder:
    def __init__(self) -> None:
        self.calls: list[tuple[int, str, str | None, str, str]] = []

    async def __call__(self, telegram_id, config, sub_url, product_title, order_ref):
        self.calls.append((telegram_id, config, sub_url, product_title, order_ref))


async def test_delivery_is_refused_before_approval(
    session, session_factory, cipher, fingerprint_secret
) -> None:
    user, order, payment = await _prepare_card_payment(session, cipher, fingerprint_secret)
    await payment_service.submit_card_receipt(
        session, payment_id=payment.id, user_id=user.id, file_id="receipt-1", file_type="photo"
    )
    await session.commit()

    recorder = DeliverRecorder()
    with pytest.raises(fulfillment.PaymentGateError):
        await fulfillment.fulfill_order(
            session_factory, order_id=order.id, cipher=cipher, deliver=recorder
        )
    assert recorder.calls == []

    async with session_factory() as check:
        item = await inventory_service.item_for_order(check, order.id)
        assert item is None  # nothing was even reserved


async def test_approval_queues_exactly_one_fulfilment_job(
    session, cipher, fingerprint_secret, admin
) -> None:
    user, order, payment = await _prepare_card_payment(session, cipher, fingerprint_secret)
    await payment_service.submit_card_receipt(
        session, payment_id=payment.id, user_id=user.id, file_id="receipt-1", file_type="photo"
    )
    await payment_service.approve_payment(session, payment_id=payment.id, admin_id=admin.id)
    await payment_service.approve_payment(session, payment_id=payment.id, admin_id=admin.id)
    await session.commit()

    count = (
        await session.execute(
            select(func.count(Job.id)).where(Job.kind == JobKind.FULFILL_ORDER)
        )
    ).scalar_one()
    assert count == 1

    refreshed = await session.get(Payment, payment.id)
    assert refreshed is not None and refreshed.status is PaymentStatus.APPROVED
    order = await order_service.get_order(session, order.id)
    assert order is not None and order.status is OrderStatus.FULFILLMENT_PENDING


async def test_approved_payment_delivers_the_encrypted_pair_once(
    session, session_factory, cipher, fingerprint_secret, admin
) -> None:
    user, order, payment = await _prepare_card_payment(session, cipher, fingerprint_secret)
    await payment_service.submit_card_receipt(
        session, payment_id=payment.id, user_id=user.id, file_id="r", file_type="photo"
    )
    await payment_service.approve_payment(session, payment_id=payment.id, admin_id=admin.id)
    await session.commit()

    recorder = DeliverRecorder()
    result = await fulfillment.fulfill_order(
        session_factory, order_id=order.id, cipher=cipher, deliver=recorder
    )
    assert result.outcome is fulfillment.FulfillOutcome.DELIVERED
    assert len(recorder.calls) == 1

    telegram_id, config, sub_url, product_title, order_ref = recorder.calls[0]
    assert telegram_id == user.telegram_id
    assert config.startswith("vless://")
    assert sub_url and sub_url.startswith("https://")
    assert product_title
    assert order_ref

    # A second run is a no-op: no second message, no second reservation.
    second = await fulfillment.fulfill_order(
        session_factory, order_id=order.id, cipher=cipher, deliver=recorder
    )
    assert second.outcome is fulfillment.FulfillOutcome.ALREADY
    assert len(recorder.calls) == 1

    async with session_factory() as check:
        refreshed = await order_service.get_order(check, order.id)
        assert refreshed is not None
        assert refreshed.status is OrderStatus.FULFILLED
        assert refreshed.delivery_status is DeliveryStatus.DELIVERED
        item = await inventory_service.item_for_order(check, order.id)
        assert item is not None and item.status is InventoryStatus.DELIVERED
        assert item.delivered_at is not None


async def test_failed_send_keeps_the_reservation_for_retry(
    session, session_factory, cipher, fingerprint_secret, admin
) -> None:
    user, order, payment = await _prepare_card_payment(session, cipher, fingerprint_secret)
    await payment_service.submit_card_receipt(
        session, payment_id=payment.id, user_id=user.id, file_id="r", file_type="photo"
    )
    await payment_service.approve_payment(session, payment_id=payment.id, admin_id=admin.id)
    await session.commit()

    async def boom(*args):
        raise RuntimeError("telegram down")

    result = await fulfillment.fulfill_order(
        session_factory, order_id=order.id, cipher=cipher, deliver=boom
    )
    assert result.outcome is fulfillment.FulfillOutcome.RETRY
    assert result.needs_retry

    async with session_factory() as check:
        refreshed = await order_service.get_order(check, order.id)
        assert refreshed is not None
        assert refreshed.delivery_status is DeliveryStatus.RETRY_REQUIRED
        item = await inventory_service.item_for_order(check, order.id)
        assert item is not None
        assert item.status is InventoryStatus.RESERVED  # never released, never reused

    # Recovery: the retry delivers the same pair.
    recorder = DeliverRecorder()
    retry = await fulfillment.fulfill_order(
        session_factory, order_id=order.id, cipher=cipher, deliver=recorder
    )
    assert retry.outcome is fulfillment.FulfillOutcome.DELIVERED
    assert len(recorder.calls) == 1


async def test_paid_order_without_stock_waits_instead_of_being_lost(
    session, session_factory, cipher, fingerprint_secret, admin
) -> None:
    # Round 29 moved the wait: approval now reserves the pair (wallet
    # parity), so an approval with an empty shelf FAILS instead of
    # queueing a job that discovers NO_INVENTORY at worker time.
    # Nothing is lost — the receipt stays in review and the same
    # approval succeeds once the pair is back on the shelf.
    user, order, payment = await _prepare_card_payment(session, cipher, fingerprint_secret)
    await payment_service.submit_card_receipt(
        session, payment_id=payment.id, user_id=user.id, file_id="r", file_type="photo"
    )
    await session.commit()

    # Someone sold the last pair between receipt and approval.
    async with session_factory() as other:
        await other.execute(
            inventory_service.InventoryItem.__table__.update().values(
                status=InventoryStatus.DISABLED
            )
        )
        await other.commit()
    with pytest.raises(payment_service.PaymentError, match="موجودی"):
        await payment_service.approve_payment(session, payment_id=payment.id, admin_id=admin.id)
    await session.refresh(payment)
    await session.refresh(order)
    assert payment.status is PaymentStatus.PENDING_REVIEW
    assert order.status is OrderStatus.PENDING_PAYMENT

    # A fresh pair on the shelf; the same approval now succeeds.
    from tests.test_admin_regressions import _stock

    from vpnbot.db.models.catalog import Plan, Product

    product = await session.get(Product, order.product_id)
    plan = await session.get(Plan, order.plan_id)
    await _stock(session, product, plan, 1, "restock")
    await session.commit()
    approved = await payment_service.approve_payment(
        session, payment_id=payment.id, admin_id=admin.id
    )
    assert approved.status is PaymentStatus.APPROVED
    await session.refresh(order)
    assert order.status is OrderStatus.FULFILLMENT_PENDING


async def test_rejected_payment_cannot_be_fulfilled(
    session, session_factory, cipher, fingerprint_secret, admin
) -> None:
    user, order, payment = await _prepare_card_payment(session, cipher, fingerprint_secret)
    await payment_service.submit_card_receipt(
        session, payment_id=payment.id, user_id=user.id, file_id="r", file_type="photo"
    )
    await payment_service.reject_payment(
        session, payment_id=payment.id, admin_id=admin.id, reason="رسید ناخوانا"
    )
    await session.commit()

    recorder = DeliverRecorder()
    with pytest.raises(fulfillment.PaymentGateError):
        await fulfillment.fulfill_order(
            session_factory, order_id=order.id, cipher=cipher, deliver=recorder
        )
    assert recorder.calls == []


async def test_duplicate_txid_is_rejected_across_orders(
    session, cipher, fingerprint_secret
) -> None:
    user = await make_user(session)
    _, product, plan = await make_catalog(session)
    await make_inventory(
        session, cipher=cipher, product=product, plan=plan,
        secret=fingerprint_secret, count=2,
    )
    settings = SettingsService(session)
    from vpnbot.services.settings import CRYPTO_SETTINGS

    await settings.set(
        CRYPTO_SETTINGS, {"enabled": True, "address": "TXtest", "asset": "USDT", "network": "TRC20"}
    )

    payments = []
    for _ in range(2):
        order = await order_service.create_order(
            session, user_id=user.id, plan_id=plan.id, product=product, plan=plan
        )
        payments.append(
            await payment_service.start_payment(
                session, order=order, method=PaymentMethod.CRYPTO
            )
        )

    await payment_service.submit_crypto_evidence(
        session, payment_id=payments[0].id, user_id=user.id, txid="abcdef1234567890"
    )
    with pytest.raises(payment_service.PaymentError):
        await payment_service.submit_crypto_evidence(
            session, payment_id=payments[1].id, user_id=user.id, txid="ABCDEF1234567890"
        )
