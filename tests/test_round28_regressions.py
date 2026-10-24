"""Round-28 regressions: bugs in the round-26/27 code itself, plus
the leftovers from the previous review that were not yet covered.

Split into its own module like rounds 25-27 (the main regressions
file is at the GitHub connector's single-argument push ceiling).
"""

from __future__ import annotations

from datetime import timedelta

from tests.test_admin_regressions import _stock  # noqa: F401  (shared helper)


async def _paid_order_with_failed_attempt(session, *, error_code: str, tag: str):
    """Wallet-paid order whose delivery failed once; returns (order, item)."""
    from tests.factories import make_catalog, make_user

    from vpnbot.db.enums import LedgerKind
    from vpnbot.services import inventory as inventory_service
    from vpnbot.services import orders as order_service
    from vpnbot.services import payments as payment_service
    from vpnbot.services import wallet as wallet_service

    user = await make_user(session, telegram_id=900002901)
    _, product, plan = await make_catalog(session)
    await _stock(session, product, plan, 1, tag)
    await wallet_service.credit(
        session, user_id=user.id, amount_irt=plan.price_irt,
        kind=LedgerKind.TOPUP, key=f"{tag}:topup", reason="t",
    )
    order = await order_service.create_order(
        session, user_id=user.id, plan_id=plan.id, product=product, plan=plan
    )
    await payment_service.pay_order_with_wallet(session, order=order, user_id=user.id)
    item = await inventory_service.item_for_order(session, order.id)
    assert item is not None
    await inventory_service.log_attempt(
        session, order_id=order.id, item_id=item.id,
        outcome="FAILED", error_code=error_code,
    )
    await session.commit()
    return order, item, product, plan


async def test_enqueue_fast_forwards_backoff_job(session) -> None:
    from sqlalchemy import select

    from vpnbot.db.base import utcnow
    from vpnbot.db.enums import JobStatus
    from vpnbot.db.models.system import Job
    from vpnbot.services import fulfillment as fulfillment_service

    order, _, _, _ = await _paid_order_with_failed_attempt(
        session, error_code="TelegramForbiddenError", tag="ffwd"
    )
    # Simulate the worker's backoff: QUEUED with a future schedule.
    job = (
        await session.execute(
            select(Job).where(Job.idempotency_key == f"fulfill:{order.id}")
        )
    ).scalar_one()
    job.status = JobStatus.QUEUED
    job.scheduled_at = utcnow() + timedelta(seconds=200)
    await session.commit()

    assert await fulfillment_service.enqueue_fulfill_job(session, order) is True
    await session.refresh(job)
    assert job.status is JobStatus.QUEUED
    assert job.scheduled_at <= utcnow()


async def test_enqueue_refuses_while_sending(session) -> None:
    from vpnbot.db.enums import DeliveryStatus
    from vpnbot.services import fulfillment as fulfillment_service

    order, _, _, _ = await _paid_order_with_failed_attempt(
        session, error_code="TelegramForbiddenError", tag="sending"
    )
    order.delivery_status = DeliveryStatus.SENDING
    await session.commit()

    # A live send in flight: the resend must not rewrite SENDING
    # back to PENDING (that would defeat cancel_order's guard).
    assert await fulfillment_service.enqueue_fulfill_job(session, order) is False
    await session.refresh(order)
    assert order.delivery_status is DeliveryStatus.SENDING


async def test_force_cancel_repools_all_forbidden_pair(session) -> None:
    from vpnbot.db.enums import InventoryStatus
    from vpnbot.services import inventory as inventory_service
    from vpnbot.services import orders as order_service
    from vpnbot.services import wallet as wallet_service

    order, item, product, plan = await _paid_order_with_failed_attempt(
        session, error_code="TelegramForbiddenError", tag="forbid"
    )
    await order_service.cancel_order(session, order.id, reason="blocked bot", force=True)
    await session.refresh(item)
    # Provably undelivered: the secret never reached anyone, so the
    # pair returns to the pool instead of burning to DISABLED.
    assert item.status is InventoryStatus.AVAILABLE
    assert item.assigned_order_id is None
    counts = await inventory_service.counts_for_product(session, product.id)
    assert counts.available == 1
    assert counts.disabled == 0
    assert await wallet_service.balance(session, order.user_id) == plan.price_irt

    # And the freed pair is immediately resellable to the next buyer.
    from tests.factories import make_user

    from vpnbot.db.enums import LedgerKind
    from vpnbot.services import payments as payment_service

    user2 = await make_user(session, telegram_id=900002902)
    await wallet_service.credit(
        session, user_id=user2.id, amount_irt=plan.price_irt,
        kind=LedgerKind.TOPUP, key="forbid:topup2", reason="t",
    )
    order2 = await order_service.create_order(
        session, user_id=user2.id, plan_id=plan.id, product=product, plan=plan
    )
    await payment_service.pay_order_with_wallet(session, order=order2, user_id=user2.id)
    assert (await inventory_service.item_for_order(session, order2.id)).id == item.id


async def test_force_cancel_still_disables_ambiguous_failure(session) -> None:
    from vpnbot.db.enums import InventoryStatus
    from vpnbot.services import orders as order_service

    order, item, _, _ = await _paid_order_with_failed_attempt(
        session, error_code="TelegramNetworkError", tag="ambig"
    )
    await order_service.cancel_order(session, order.id, reason="net fail", force=True)
    await session.refresh(item)
    assert item.status is InventoryStatus.DISABLED


async def test_cancel_frees_pair_wakes_waiting_order(session) -> None:
    """3.1: a cancel-freed pair revives a FULFILLMENT_PENDING order."""
    from sqlalchemy import select
    from tests.factories import make_catalog, make_user

    from vpnbot.db.enums import JobStatus, LedgerKind, PaymentMethod
    from vpnbot.db.models.system import Job
    from vpnbot.services import orders as order_service
    from vpnbot.services import payments as payment_service
    from vpnbot.services import wallet as wallet_service
    from vpnbot.services.auth import bootstrap_owner

    admin = await bootstrap_owner(session, telegram_id=900002912)
    user = await make_user(session, telegram_id=900002911)
    from vpnbot.services.settings import CARD_SETTINGS, SettingsService

    await SettingsService(session).set(
        CARD_SETTINGS,
        {"enabled": True, "recipient_name": "تست", "card_number": "6037", "bank_name": "تست"},
    )
    _, product, plan = await make_catalog(session)
    await _stock(session, product, plan, 1, "wake")
    await wallet_service.credit(
        session, user_id=user.id, amount_irt=plan.price_irt,
        kind=LedgerKind.TOPUP, key="wake:topup", reason="t",
    )
    # Order B is created and card-approved FIRST: approval does not
    # reserve, so its fulfilment job waits on the shelf. (create_order
    # itself refuses once the shelf is empty, so B cannot be created
    # after A takes the pair.)
    order_b = await order_service.create_order(
        session, user_id=user.id, plan_id=plan.id, product=product, plan=plan
    )
    payment_b = await payment_service.start_payment(
        session, order=order_b, method=PaymentMethod.CARD
    )
    await payment_service.submit_card_receipt(
        session, payment_id=payment_b.id, user_id=user.id,
        file_id="rcpt", file_type="photo",
    )
    await payment_service.approve_payment(
        session, payment_id=payment_b.id, admin_id=admin.id
    )
    # Order A then wallet-pays and takes the only pair; B's job
    # exhausts into FAILED on NO_INVENTORY.
    order_a = await order_service.create_order(
        session, user_id=user.id, plan_id=plan.id, product=product, plan=plan
    )
    await payment_service.pay_order_with_wallet(session, order=order_a, user_id=user.id)
    job_b = (
        await session.execute(
            select(Job).where(Job.idempotency_key == f"fulfill:{order_b.id}")
        )
    ).scalar_one()
    job_b.status = JobStatus.FAILED
    await session.commit()

    # Cancelling A (no attempts) frees its pair — B must wake.
    await order_service.cancel_order(session, order_a.id, reason="changed mind")
    await session.refresh(job_b)
    assert job_b.status is JobStatus.QUEUED
    assert job_b.attempt == 0


def test_split_line_case_insensitive_and_tab_label() -> None:
    from vpnbot.services.import_service import _split_line

    # An uppercase scheme still splits at the separator before it.
    # The match is case-insensitive, but the returned sub keeps
    # the user's original casing (only the lookup is lowered).
    assert _split_line("vless://u@h:443,HTTPS://sub.example.com/sub") == (
        "vless://u@h:443",
        "HTTPS://sub.example.com/sub",
    )
    # A tab-separated line's trailing label column is dropped.
    assert _split_line("vless://u@h:443\thttps://sub.example.com/sub\tlabel") == (
        "vless://u@h:443",
        "https://sub.example.com/sub",
    )
    # And the pipe label still drops (round-27 behaviour preserved).
    assert _split_line("vless://u@h:443|https://sub.example.com/sub|label") == (
        "vless://u@h:443",
        "https://sub.example.com/sub",
    )


def test_hash_from_link_with_slash_in_query() -> None:
    from vpnbot.services.payments import normalize_txid

    h = "abcdef1234567890abcdef1234567890abcdef1234567890abcdef1234567890"
    link = f"https://etherscan.io/tx/0x{h}?redirect=https://etherscan.io/tx"
    assert normalize_txid(link) == h
    # The plain query case still works.
    assert normalize_txid(f"https://etherscan.io/tx/0x{h}?utm=share") == h
