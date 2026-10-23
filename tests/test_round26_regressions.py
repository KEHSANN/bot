"""Round-26 regressions: force-cancel DISABLES the pair, the shared
fulfilment enqueue behind the customer's resend button, throttle
evidence scoping (support state out, one sleeper per user, storage
errors shed) and oldest-first eviction in the rate-limit maps.

Split into its own module like round 25 (the main regressions file
is at the GitHub connector's single-argument push ceiling).
"""

from __future__ import annotations

from tests.test_admin_regressions import _stock  # noqa: F401  (shared helper)


async def test_force_cancel_disables_pair_and_unblocks_product_delete(session) -> None:
    from tests.factories import make_catalog, make_user

    from vpnbot.db.enums import InventoryStatus, LedgerKind, PaymentStatus
    from vpnbot.services import inventory as inventory_service
    from vpnbot.services import orders as order_service
    from vpnbot.services import payments as payment_service
    from vpnbot.services import wallet as wallet_service

    user = await make_user(session, telegram_id=900002701)
    _, product, plan = await make_catalog(session)
    await _stock(session, product, plan, 1, "disablepair")
    await wallet_service.credit(
        session, user_id=user.id, amount_irt=plan.price_irt,
        kind=LedgerKind.TOPUP, key="dp:topup", reason="t",
    )
    order = await order_service.create_order(
        session, user_id=user.id, plan_id=plan.id, product=product, plan=plan
    )
    payment = await payment_service.pay_order_with_wallet(session, order=order, user_id=user.id)
    item = await inventory_service.item_for_order(session, order.id)
    assert item is not None
    await inventory_service.log_attempt(
        session, order_id=order.id, item_id=item.id,
        outcome="FAILED", error_code="TelegramForbiddenError",
    )
    await session.commit()

    await order_service.cancel_order(session, order.id, reason="blocked bot", force=True)
    await session.refresh(item)
    # Quarantined terminally — but OUT of the RESERVED limbo.
    assert item.status is InventoryStatus.DISABLED
    assert item.disabled_reason
    await session.refresh(payment)
    assert payment.status is PaymentStatus.CANCELLED
    assert await wallet_service.balance(session, user.id) == plan.price_irt

    # The lockup is gone: the pair no longer counts as reserved
    # stock anywhere (product deletion itself stays blocked by the
    # order-history FK — a separate, by-design guard).
    counts = await inventory_service.counts_for_product(session, product.id)
    assert counts.reserved == 0
    assert counts.disabled == 1


async def test_enqueue_fulfill_job_revives_failed_job(session) -> None:
    from sqlalchemy import select
    from tests.factories import make_catalog, make_user

    from vpnbot.db.enums import JobStatus, LedgerKind
    from vpnbot.db.models.system import Job
    from vpnbot.services import fulfillment as fulfillment_service
    from vpnbot.services import orders as order_service
    from vpnbot.services import payments as payment_service
    from vpnbot.services import wallet as wallet_service

    user = await make_user(session, telegram_id=900002711)
    _, product, plan = await make_catalog(session)
    await _stock(session, product, plan, 1, "enq")
    await wallet_service.credit(
        session, user_id=user.id, amount_irt=plan.price_irt,
        kind=LedgerKind.TOPUP, key="enq:topup", reason="t",
    )
    order = await order_service.create_order(
        session, user_id=user.id, plan_id=plan.id, product=product, plan=plan
    )
    await payment_service.pay_order_with_wallet(session, order=order, user_id=user.id)
    await session.commit()

    async def the_job() -> Job:
        return (
            await session.execute(
                select(Job).where(Job.idempotency_key == f"fulfill:{order.id}")
            )
        ).scalar_one()

    # The payment already queued it: a second enqueue is a no-op.
    assert await fulfillment_service.enqueue_fulfill_job(session, order) is False
    job = await the_job()
    assert job.status is JobStatus.QUEUED

    # Exhausted/failed (e.g. bot was blocked): the resend revives the
    # SAME canonical job with a fresh attempt budget.
    job.status = JobStatus.FAILED
    job.attempt = 8
    await session.commit()
    assert await fulfillment_service.enqueue_fulfill_job(session, order) is True
    job = await the_job()
    await session.refresh(job)  # the upsert is a Core statement; drop the cached copy
    assert job.status is JobStatus.QUEUED
    assert job.attempt == 0


async def test_throttle_support_state_is_not_evidence() -> None:
    import time as _time
    from datetime import UTC, datetime
    from types import SimpleNamespace

    from aiogram.fsm.storage.base import StorageKey
    from aiogram.fsm.storage.memory import MemoryStorage
    from aiogram.types import Chat, Message, PhotoSize
    from aiogram.types import User as TgUser

    from vpnbot.bot.middlewares.context import ThrottleMiddleware

    storage = MemoryStorage()
    await storage.set_state(
        StorageKey(bot_id=1, chat_id=100, user_id=43), "SupportStates:awaiting_message"
    )
    mw = ThrottleMiddleware(
        storage=storage, message_capacity=1, message_refill=1.0, evidence_max_wait=2.5
    )
    data = {"bot": SimpleNamespace(id=1)}
    chat = Chat(id=100, type="private")
    tg_user = TgUser(id=43, is_bot=False, first_name="تست")

    def photo() -> Message:
        return Message(
            message_id=1, date=datetime.now(UTC), chat=chat, from_user=tg_user,
            photo=[PhotoSize(file_id="p", file_unique_id="u", width=10, height=10)],
        )

    async def handler(event, data):  # noqa: ANN001, ANN202
        return "handled"

    assert await mw(handler, photo(), data) == "handled"
    started = _time.monotonic()
    # The support handler is text-only: a photo there is NOT evidence
    # and must shed at once, not park in the never-drop sleep.
    assert await mw(handler, photo(), data) is None
    assert _time.monotonic() - started < 0.9


async def test_throttle_storage_error_sheds_instead_of_delaying() -> None:
    from datetime import UTC, datetime
    from types import SimpleNamespace

    from aiogram.fsm.storage.memory import MemoryStorage
    from aiogram.types import Chat, Message, PhotoSize
    from aiogram.types import User as TgUser

    from vpnbot.bot.middlewares.context import ThrottleMiddleware

    class BrokenStorage(MemoryStorage):
        async def get_state(self, key):  # noqa: ANN001, ANN202
            raise ConnectionError("redis down")

    mw = ThrottleMiddleware(
        storage=BrokenStorage(), message_capacity=1, message_refill=1.0, evidence_max_wait=2.5
    )
    data = {"bot": SimpleNamespace(id=1)}
    chat = Chat(id=100, type="private")
    tg_user = TgUser(id=44, is_bot=False, first_name="تست")

    def photo() -> Message:
        return Message(
            message_id=1, date=datetime.now(UTC), chat=chat, from_user=tg_user,
            photo=[PhotoSize(file_id="p", file_unique_id="u", width=10, height=10)],
        )

    async def handler(event, data):  # noqa: ANN001, ANN202
        return "handled"

    assert await mw(handler, photo(), data) == "handled"
    # Redis read failed -> fail to the shed path, never the sleep.
    assert await mw(handler, photo(), data) is None
    assert await mw._expects_evidence(photo(), data, 44) is False


async def test_throttle_one_evidence_sleeper_per_user(monkeypatch) -> None:
    import asyncio
    from datetime import UTC, datetime
    from types import SimpleNamespace

    from aiogram.fsm.storage.base import StorageKey
    from aiogram.fsm.storage.memory import MemoryStorage
    from aiogram.types import Chat, Message, PhotoSize
    from aiogram.types import User as TgUser

    import vpnbot.bot.middlewares.context as ctx
    from vpnbot.bot.middlewares.context import ThrottleMiddleware

    storage = MemoryStorage()
    await storage.set_state(
        StorageKey(bot_id=1, chat_id=100, user_id=45), "CheckoutStates:awaiting_receipt"
    )
    mw = ThrottleMiddleware(
        storage=storage, message_capacity=1, message_refill=1.0, evidence_max_wait=2.5
    )
    data = {"bot": SimpleNamespace(id=1)}
    chat = Chat(id=100, type="private")
    tg_user = TgUser(id=45, is_bot=False, first_name="تست")

    def photo() -> Message:
        return Message(
            message_id=1, date=datetime.now(UTC), chat=chat, from_user=tg_user,
            photo=[PhotoSize(file_id="p", file_unique_id="u", width=10, height=10)],
        )

    async def handler(event, data):  # noqa: ANN001, ANN202
        return "handled"

    sleeps = 0
    real_sleep = asyncio.sleep

    async def counting_sleep(delay):  # noqa: ANN001, ANN202
        nonlocal sleeps
        sleeps += 1
        await real_sleep(delay)

    monkeypatch.setattr(ctx.asyncio, "sleep", counting_sleep)

    assert await mw(handler, photo(), data) == "handled"  # burns the token
    results = await asyncio.gather(
        mw(handler, photo(), data), mw(handler, photo(), data)
    )
    # One of the two burst photos parked in the delay and delivered;
    # the other shed. Crucially only ONE sleep happened for the user.
    assert sorted(r or "" for r in results) == ["", "handled"]
    assert sleeps == 1


def test_blocked_notice_map_evicts_oldest_not_clear() -> None:
    from vpnbot.bot.middlewares.context import BlockedUserMiddleware

    mw = BlockedUserMiddleware()
    base = 1_000_000.0
    for i in range(10_001):
        mw._notified_at[50_000 + i] = base + i
    assert mw._notice_allowed(999_999) is True
    # The map shrank by eviction, not by a full clear: recent
    # entries (still inside their 300s gate) survive.
    assert len(mw._notified_at) == 10_001 - 1_000 + 1
    assert (50_000 + 10_000) in mw._notified_at
    assert (50_000 + 0) not in mw._notified_at
    assert 999_999 in mw._notified_at
