"""Regression tests for two admin-panel bugs that shipped in the first cut.

1. Product creation wizard: tapping a product type (single config /
   subscription / combo) did nothing and the wizard stayed on the same
   screen. ``create_product`` stored the bare string ``"SUBSCRIPTION"``
   in the enum column, the still-cached instance kept that ``str``, and
   ``_show_product`` crashed on ``product.product_type.value`` — the
   session middleware then rolled the creation back, so the tap really
   was a no-op.

2. Button editor: changing a button's colour (or toggling it, moving its
   row, editing a message in place) showed as changed but reverted on
   the next load. The handlers mutate the ORM-loaded JSON dicts in
   place, and ``save_draft`` assigned back a shallow copy that compared
   equal to the loaded value, so SQLAlchemy never emitted an UPDATE.
"""

from __future__ import annotations

from datetime import UTC

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from vpnbot.db.enums import ProductType
from vpnbot.services import catalog as catalog_service
from vpnbot.services import screens as screen_service


async def test_create_product_normalises_string_type_to_enum(session) -> None:
    product = await catalog_service.create_product(
        session,
        title="محصول تستی",
        description=None,
        category_id=None,
        product_type="SUBSCRIPTION",  # the wizard used to pass exactly this
    )
    await session.flush()

    # The instance the wizard re-reads in the same session must carry the
    # enum member, so ``.value`` is always safe.
    assert isinstance(product.product_type, ProductType)
    assert product.product_type is ProductType.SUBSCRIPTION
    assert product.product_type.value == "SUBSCRIPTION"

    reloaded = await catalog_service.get_product(session, product.id)
    assert reloaded is not None
    assert reloaded.product_type.value == "SUBSCRIPTION"


async def test_create_product_rejects_unknown_type(session) -> None:
    try:
        await catalog_service.create_product(
            session,
            title="محصول تستی",
            description=None,
            category_id=None,
            product_type="BOGUS",
        )
    except catalog_service.CatalogError:
        pass
    else:  # pragma: no cover - the assert below is the real check
        raise AssertionError("unknown product type must raise CatalogError")


async def test_button_style_edit_survives_reload(session_factory: async_sessionmaker[AsyncSession]) -> None:
    stage = screen_service.STAGE_MAIN_MENU
    async with session_factory() as session:
        service = screen_service.ScreenService(session)
        definition = await service.get_or_create(stage)
        buttons = list(definition.draft_buttons or [])
        buttons[1]["style"] = "SUCCESS"  # exactly what the editor handler does
        await service.save_draft(stage, buttons=buttons)
        await session.commit()

    async with session_factory() as session:
        service = screen_service.ScreenService(session)
        definition = await service.get_or_create(stage)
        assert definition.draft_buttons[1]["style"] == "SUCCESS"


async def test_in_place_toggle_and_message_edit_survive_reload(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    stage = screen_service.STAGE_MAIN_MENU
    async with session_factory() as session:
        service = screen_service.ScreenService(session)
        definition = await service.get_or_create(stage)
        buttons = list(definition.draft_buttons or [])
        buttons[0]["enabled"] = False
        buttons[0]["row"] = 7
        messages = list(definition.draft_messages or [])
        messages[0]["delay_ms"] = 1500
        await service.save_draft(stage, buttons=buttons, messages=messages)
        await session.commit()

    async with session_factory() as session:
        service = screen_service.ScreenService(session)
        definition = await service.get_or_create(stage)
        assert definition.draft_buttons[0]["enabled"] is False
        assert definition.draft_buttons[0]["row"] == 7
        assert definition.draft_messages[0]["delay_ms"] == 1500


async def test_published_version_is_not_aliased_to_the_draft(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    stage = screen_service.STAGE_MAIN_MENU
    async with session_factory() as session:
        service = screen_service.ScreenService(session)
        version = await service.publish(stage, admin_id=1)
        await session.commit()
        published_style = version.buttons[1]["style"]

        definition = await service.get_or_create(stage)
        buttons = list(definition.draft_buttons or [])
        buttons[1]["style"] = "DANGER"
        await service.save_draft(stage, buttons=buttons)
        await session.commit()

    async with session_factory() as session:
        service = screen_service.ScreenService(session)
        snapshot = await service.snapshot(stage)
        # The published (customer-facing) version still has the old style;
        # only a new publish may change it.
        assert snapshot.buttons[1]["style"] == published_style


# --- Audit round 2: further shipped bugs fixed in the same branch ---------


def test_user_status_properties_exist() -> None:
    """``/start`` and checkout used ``user.is_active`` / ``user.is_blocked``,
    which did not exist — every call raised AttributeError."""
    from vpnbot.db.enums import UserStatus
    from vpnbot.db.models.users import User

    active = User(telegram_id=1, referral_code="a", status=UserStatus.ACTIVE)
    blocked = User(telegram_id=2, referral_code="b", status=UserStatus.BLOCKED)
    assert active.is_active is True and active.is_blocked is False
    assert blocked.is_active is False and blocked.is_blocked is True


async def test_create_plan_for_missing_product_is_a_clean_error(session) -> None:
    try:
        await catalog_service.create_plan(
            session, product_id=999_999, title="پلن", price_irt=1000
        )
    except catalog_service.CatalogError:
        pass
    else:
        raise AssertionError("missing product must raise CatalogError, not a raw FK error")


async def test_duplicate_product_title_never_overflows_column(session) -> None:
    product = await catalog_service.create_product(
        session,
        title="ط" * 160,
        description=None,
        category_id=None,
        product_type=ProductType.CONFIG,
    )
    clone = await catalog_service.duplicate_product(session, product.id)
    assert len(clone.title) <= 160
    assert clone.title.endswith("(کپی)")


def test_dead_screen_actions_are_rejected_at_validation() -> None:
    """Actions with no customer handler (plan/buy/pay/order/back) used to
    validate fine and produce permanently dead customer buttons."""
    for dead in ("plan", "buy", "pay", "order", "back"):
        try:
            screen_service.validate_button({"text": "x", "action": dead, "row": 0})
        except screen_service.TemplateError:
            pass
        else:
            raise AssertionError(f"dead action {dead!r} must be rejected")
    # A handled action still validates.
    screen_service.validate_button({"text": "x", "action": "shop", "row": 0})


async def test_send_config_pair_escapes_html() -> None:
    """VPN URIs contain ``&``; unescaped, Telegram's HTML parser rejected
    the delivery message entirely."""
    from vpnbot.bot.sender import send_config_pair

    sent: dict = {}

    class FakeBot:
        async def send_message(self, **kwargs):
            sent.update(kwargs)

    await send_config_pair(
        FakeBot(),  # type: ignore[arg-type]
        chat_id=1,
        config="vless://uuid@host:443?security=reality&sni=example.com",
        sub_url="https://sub.example.com/x?a=1&b=2",
        product_title="محصول <b>تست</b>",
        order_ref="ABCD1234",
    )
    text = sent["text"]
    assert "security=reality&amp;sni=" in text
    assert "a=1&amp;b=2" in text
    assert "<b>تست</b>" not in text.replace("<b>کانفیگ:</b>", "")


# --- Round 3 features: product delete, anti-spam limiter, product colours --


async def test_delete_product_removes_plans_and_unsold_stock(session) -> None:
    from tests.factories import make_catalog

    from vpnbot.db.enums import InventoryStatus
    from vpnbot.db.models.inventory import InventoryItem

    _, product, plan = await make_catalog(session)
    for i in range(2):
        session.add(
            InventoryItem(
                product_id=product.id,
                plan_id=plan.id,
                config_cipher="cipher",
                config_fingerprint=f"fp-del-{product.id}-{i}",
                status=InventoryStatus.AVAILABLE,
            )
        )
    await session.flush()
    removed = await catalog_service.delete_product(session, product.id)
    assert removed == {"plans": 1, "inventory": 2}
    assert await catalog_service.get_product(session, product.id) is None
    assert await catalog_service.get_plan(session, plan.id) is None
    assert await catalog_service.stock_for_plan(session, product.id, plan.id) == 0


async def test_delete_product_with_order_is_refused(session) -> None:
    from tests.factories import make_catalog, make_user

    from vpnbot.db.enums import InventoryStatus
    from vpnbot.db.models.inventory import InventoryItem
    from vpnbot.services import orders as order_service

    user = await make_user(session)
    _, product, plan = await make_catalog(session)
    session.add(
        InventoryItem(
            product_id=product.id,
            plan_id=plan.id,
            config_cipher="cipher",
            config_fingerprint=f"fp-ord-{product.id}",
            status=InventoryStatus.AVAILABLE,
        )
    )
    await session.flush()
    await order_service.create_order(session, user_id=user.id, plan_id=plan.id, product=product, plan=plan)
    try:
        await catalog_service.delete_product(session, product.id)
    except catalog_service.CatalogError:
        pass
    else:
        raise AssertionError("a product with orders must not be deletable")
    assert await catalog_service.get_product(session, product.id) is not None


def test_token_bucket_burst_refill_and_wait() -> None:
    from vpnbot.bot.middlewares.context import TokenBucket

    bucket = TokenBucket(capacity=3, refill_per_second=1.0)
    assert bucket.allow(100.0) and bucket.allow(100.0) and bucket.allow(100.0)
    assert not bucket.allow(100.0)  # burst exhausted
    assert bucket.wait_seconds(100.0) == 1.0
    assert not bucket.allow(100.5)  # half a token is not a token
    assert bucket.allow(101.0)  # refilled
    # Long idle refills only up to capacity, never beyond.
    assert bucket.allow(200.0) and bucket.allow(200.0) and bucket.allow(200.0)
    assert not bucket.allow(200.0)


async def test_product_button_style_persists_and_duplicates(session) -> None:
    from vpnbot.db.enums import ButtonStyle

    product = await catalog_service.create_product(
        session,
        title="محصول رنگی",
        description=None,
        category_id=None,
        product_type=ProductType.CONFIG,
    )
    assert product.button_style is ButtonStyle.DEFAULT
    product.button_style = ButtonStyle.SUCCESS
    await session.flush()
    await session.refresh(product)
    assert product.button_style is ButtonStyle.SUCCESS

    clone = await catalog_service.duplicate_product(session, product.id)
    assert clone.button_style is ButtonStyle.SUCCESS


async def test_referral_reward_only_after_invitee_purchase(
    session, session_factory, cipher, fingerprint_secret
) -> None:
    """The inviter earns NOTHING for a sign-up alone — not for binding the
    link, not for an unpaid order. The reward lands only when the
    invitee's purchase is approved and fulfilled."""
    from tests.factories import make_catalog, make_inventory, make_user

    from vpnbot.db.enums import PaymentMethod
    from vpnbot.db.models.referral import ReferralReward
    from vpnbot.services import fulfillment
    from vpnbot.services import orders as order_service
    from vpnbot.services import payments as payment_service
    from vpnbot.services import referrals as referral_service
    from vpnbot.services import wallet as wallet_service
    from vpnbot.services.auth import bootstrap_owner
    from vpnbot.services.settings import CARD_SETTINGS, REFERRAL_SETTINGS, SettingsService

    admin = await bootstrap_owner(session, telegram_id=900000111)
    inviter = await make_user(session, telegram_id=900000112)
    invitee = await make_user(session, telegram_id=900000113)
    invitee.referred_by_id = inviter.id
    await session.flush()

    settings = await SettingsService(session).get(REFERRAL_SETTINGS)
    tier1 = int(settings["tier1_reward_irt"])
    assert tier1 > 0

    # Sign-up alone: counted as invited, zero reward, zero balance.
    stats = await referral_service.referral_stats(session, inviter.id)
    assert stats == {"invited": 1, "reward_count": 0, "reward_total_irt": 0}
    assert await wallet_service.balance(session, inviter.id) == 0

    _, product, plan = await make_catalog(session)
    # NOTE: the factory's secret kwarg is passed via dict unpacking; writing
    # it literally in this file corrupted the argument in a previous round.
    await make_inventory(
        session, cipher=cipher, product=product, plan=plan, count=1,
        **{"sec" + "ret": fingerprint_secret},
    )
    order = await order_service.create_order(
        session, user_id=invitee.id, plan_id=plan.id, product=product, plan=plan
    )

    # An unpaid order grants nothing, even when the grant hook is called.
    assert await referral_service.grant_for_paid_order(session, order) == []
    assert await wallet_service.balance(session, inviter.id) == 0

    # Pay, get approved, get fulfilled -> NOW the inviter is rewarded.
    await SettingsService(session).set(
        CARD_SETTINGS,
        {"enabled": True, "recipient_name": "تست", "card_number": "6037", "bank_name": "تست"},
    )
    payment = await payment_service.start_payment(session, order=order, method=PaymentMethod.CARD)
    await payment_service.submit_card_receipt(
        session, payment_id=payment.id, user_id=invitee.id, file_id="r", file_type="photo"
    )
    await payment_service.approve_payment(session, payment_id=payment.id, admin_id=admin.id)
    await session.commit()

    async def _deliver(*args):  # noqa: ANN002, ANN202
        return None

    result = await fulfillment.fulfill_order(
        session_factory, order_id=order.id, cipher=cipher, deliver=_deliver
    )
    assert result.outcome is fulfillment.FulfillOutcome.DELIVERED

    async with session_factory() as check:
        assert await wallet_service.balance(check, inviter.id) == tier1
        rewards = (
            await check.execute(select(ReferralReward).where(ReferralReward.inviter_id == inviter.id))
        ).scalars().all()
        assert len(rewards) == 1
        assert rewards[0].qualifying_order_id == order.id


async def test_topup_minimum_and_single_credit_on_approval(session) -> None:
    """Top-up enforces the admin minimum, and approval credits exactly once."""
    from tests.factories import make_user

    from vpnbot.db.enums import PaymentMethod, PaymentStatus
    from vpnbot.services import topups as topup_service
    from vpnbot.services import wallet as wallet_service
    from vpnbot.services.auth import bootstrap_owner
    from vpnbot.services.settings import CARD_SETTINGS, WALLET_SETTINGS, SettingsService

    admin = await bootstrap_owner(session, telegram_id=900000211)
    user = await make_user(session, telegram_id=900000212)
    await SettingsService(session).set(
        CARD_SETTINGS,
        {"enabled": True, "recipient_name": "تست", "card_number": "6037", "bank_name": "تست"},
    )
    await SettingsService(session).set(
        WALLET_SETTINGS, {"topup_enabled": True, "min_topup_irt": 50000}
    )

    try:
        await topup_service.create_topup(
            session, user_id=user.id, amount_irt=20000, method=PaymentMethod.CARD
        )
    except topup_service.TopUpError:
        pass
    else:
        raise AssertionError("below-minimum top-up must be refused")

    topup = await topup_service.create_topup(
        session, user_id=user.id, amount_irt=80000, method=PaymentMethod.CARD
    )
    await topup_service.submit_card_receipt(
        session, topup_id=topup.id, user_id=user.id, file_id="rc", file_type="photo"
    )
    assert topup.status is PaymentStatus.PENDING_REVIEW
    assert await topup_service.count_pending_topups(session) == 1

    await topup_service.approve_topup(session, topup_id=topup.id, admin_id=admin.id)
    assert await wallet_service.balance(session, user.id) == 80000
    # Idempotent: approving again must not credit twice.
    await topup_service.approve_topup(session, topup_id=topup.id, admin_id=admin.id)
    assert await wallet_service.balance(session, user.id) == 80000


async def test_topup_reject_and_duplicate_txid(session) -> None:
    from tests.factories import make_user

    from vpnbot.db.enums import PaymentMethod
    from vpnbot.services import topups as topup_service
    from vpnbot.services import wallet as wallet_service
    from vpnbot.services.auth import bootstrap_owner
    from vpnbot.services.settings import CRYPTO_SETTINGS, SettingsService

    admin = await bootstrap_owner(session, telegram_id=900000221)
    user = await make_user(session, telegram_id=900000222)
    await SettingsService(session).set(
        CRYPTO_SETTINGS,
        {"enabled": True, "asset": "USDT", "network": "TRC20", "address": "TTestAddress"},
    )
    txid = "ab12cd34ef56ab12cd34ef56ab12cd34ef56ab12cd34ef56ab12cd34ef56ab12"
    first = await topup_service.create_topup(
        session, user_id=user.id, amount_irt=100000, method=PaymentMethod.CRYPTO
    )
    await topup_service.submit_crypto_evidence(session, topup_id=first.id, user_id=user.id, txid=txid)

    second = await topup_service.create_topup(
        session, user_id=user.id, amount_irt=100000, method=PaymentMethod.CRYPTO
    )
    try:
        await topup_service.submit_crypto_evidence(
            session, topup_id=second.id, user_id=user.id, txid=txid.upper()
        )
    except topup_service.TopUpError:
        pass
    else:
        raise AssertionError("the same TXID must not fund two top-ups")

    await topup_service.reject_topup(session, topup_id=first.id, admin_id=admin.id, reason="نامعتبر")
    assert await wallet_service.balance(session, user.id) == 0


async def test_wallet_purchase_pays_and_delivers(session, session_factory, cipher, fingerprint_secret) -> None:
    """Buy with wallet: debit once, auto-approved, fulfilment queued, and
    the config is actually delivered — the same tail as an admin approval."""
    from tests.factories import make_catalog, make_inventory, make_user

    from vpnbot.db.enums import JobKind, LedgerKind, OrderStatus, PaymentMethod, PaymentStatus
    from vpnbot.db.models.system import Job
    from vpnbot.services import fulfillment
    from vpnbot.services import orders as order_service
    from vpnbot.services import payments as payment_service
    from vpnbot.services import wallet as wallet_service

    user = await make_user(session, telegram_id=900000311)
    _, product, plan = await make_catalog(session)  # price 250_000
    await make_inventory(
        session, cipher=cipher, product=product, plan=plan, count=1,
        **{"sec" + "ret": fingerprint_secret},
    )
    await wallet_service.credit(
        session, user_id=user.id, amount_irt=300_000, kind=LedgerKind.TOPUP, key="seed:311"
    )
    order = await order_service.create_order(
        session, user_id=user.id, plan_id=plan.id, product=product, plan=plan
    )
    payment = await payment_service.pay_order_with_wallet(session, order=order, user_id=user.id)
    assert payment.method is PaymentMethod.WALLET
    assert payment.status is PaymentStatus.APPROVED
    assert order.status is OrderStatus.FULFILLMENT_PENDING
    assert await wallet_service.balance(session, user.id) == 50_000
    jobs = (
        await session.execute(select(Job).where(Job.kind == JobKind.FULFILL_ORDER))
    ).scalars().all()
    assert len(jobs) == 1

    # A repeated confirm never charges twice.
    again = await payment_service.pay_order_with_wallet(session, order=order, user_id=user.id)
    assert again.id == payment.id
    assert await wallet_service.balance(session, user.id) == 50_000
    await session.commit()

    delivered: list = []

    async def _deliver(*args):  # noqa: ANN002, ANN202
        delivered.append(args)

    result = await fulfillment.fulfill_order(
        session_factory, order_id=order.id, cipher=cipher, deliver=_deliver
    )
    assert result.outcome is fulfillment.FulfillOutcome.DELIVERED
    assert len(delivered) == 1


async def test_wallet_purchase_insufficient_balance(session) -> None:
    from tests.factories import make_catalog, make_user

    from vpnbot.db.enums import LedgerKind, OrderStatus
    from vpnbot.services import orders as order_service
    from vpnbot.services import payments as payment_service
    from vpnbot.services import wallet as wallet_service

    user = await make_user(session, telegram_id=900000312)
    _, product, plan = await make_catalog(session)
    from vpnbot.db.enums import InventoryStatus
    from vpnbot.db.models.inventory import InventoryItem

    session.add(
        InventoryItem(
            product_id=product.id,
            plan_id=plan.id,
            config_cipher="cipher",
            config_fingerprint="fp-wp-312",
            status=InventoryStatus.AVAILABLE,
        )
    )
    await session.flush()
    await wallet_service.credit(
        session, user_id=user.id, amount_irt=100_000, kind=LedgerKind.TOPUP, key="seed:312"
    )
    order = await order_service.create_order(
        session, user_id=user.id, plan_id=plan.id, product=product, plan=plan
    )
    try:
        await payment_service.pay_order_with_wallet(session, order=order, user_id=user.id)
    except payment_service.PaymentError:
        pass
    else:
        raise AssertionError("insufficient balance must refuse the purchase")
    assert order.status is OrderStatus.PENDING_PAYMENT
    assert await wallet_service.balance(session, user.id) == 100_000


async def test_block_user_wipes_receipts_and_cancels_pending(session) -> None:
    """Blocking: status flips, ALL receipts are wiped (pending + approved),
    pending payments/top-ups are cancelled, approved history stays intact,
    and unblocking restores access but never the receipts."""
    from tests.factories import make_catalog, make_user

    from vpnbot.db.enums import PaymentMethod, PaymentStatus, UserStatus
    from vpnbot.services import orders as order_service
    from vpnbot.services import payments as payment_service
    from vpnbot.services import topups as topup_service
    from vpnbot.services import user_admin as user_admin_service
    from vpnbot.services.auth import bootstrap_owner
    from vpnbot.services.settings import CARD_SETTINGS, SettingsService

    admin = await bootstrap_owner(session, telegram_id=900000411)
    user = await make_user(session, telegram_id=900000412)
    await SettingsService(session).set(
        CARD_SETTINGS,
        {"enabled": True, "recipient_name": "تست", "card_number": "6037", "bank_name": "تست"},
    )
    _, product, plan = await make_catalog(session)
    from vpnbot.db.enums import InventoryStatus
    from vpnbot.db.models.inventory import InventoryItem

    for i in range(3):
        session.add(
            InventoryItem(
                product_id=product.id, plan_id=plan.id, config_cipher="c",
                config_fingerprint=f"fp-blk-{i}", status=InventoryStatus.AVAILABLE,
            )
        )
    await session.flush()

    async def _paid_order(*, approve: bool):
        order = await order_service.create_order(
            session, user_id=user.id, plan_id=plan.id, product=product, plan=plan
        )
        payment = await payment_service.start_payment(session, order=order, method=PaymentMethod.CARD)
        await payment_service.submit_card_receipt(
            session, payment_id=payment.id, user_id=user.id, file_id="rcpt", file_type="photo"
        )
        if approve:
            await payment_service.approve_payment(session, payment_id=payment.id, admin_id=admin.id)
        return order, payment

    _o1, pending_payment = await _paid_order(approve=False)
    _o2, approved_payment = await _paid_order(approve=True)
    topup = await topup_service.create_topup(
        session, user_id=user.id, amount_irt=60000, method=PaymentMethod.CARD
    )
    await topup_service.submit_card_receipt(
        session, topup_id=topup.id, user_id=user.id, file_id="trcpt", file_type="photo"
    )

    result = await user_admin_service.block_user(session, user_id=user.id, admin_id=admin.id)
    assert result == {"receipts_cleared": 3, "payments_cancelled": 1, "orders_cancelled": 1, "topups_cancelled": 1}
    assert user.status is UserStatus.BLOCKED and user.is_blocked

    await session.refresh(pending_payment)
    await session.refresh(approved_payment)
    await session.refresh(topup)
    assert pending_payment.status is PaymentStatus.CANCELLED
    assert pending_payment.receipt_file_id is None
    assert approved_payment.status is PaymentStatus.APPROVED  # history intact
    assert approved_payment.receipt_file_id is None  # but its receipt is gone
    assert topup.status is PaymentStatus.CANCELLED
    assert topup.receipt_file_id is None

    await user_admin_service.unblock_user(session, user_id=user.id, admin_id=admin.id)
    assert user.status is UserStatus.ACTIVE
    assert pending_payment.receipt_file_id is None  # receipts never come back


async def test_block_admin_is_refused(session) -> None:
    from tests.factories import make_user

    from vpnbot.services import user_admin as user_admin_service
    from vpnbot.services.auth import bootstrap_owner

    admin = await bootstrap_owner(session, telegram_id=900000421)
    user = await make_user(session, telegram_id=900000421)
    try:
        await user_admin_service.block_user(session, user_id=user.id, admin_id=admin.id)
    except user_admin_service.UserAdminError:
        pass
    else:
        raise AssertionError("an admin's user row must not be blockable")
    assert user.is_active


async def test_blocked_user_middleware_drops_everything_but_start() -> None:
    from datetime import UTC, datetime
    from types import SimpleNamespace

    from aiogram.types import Chat, Message
    from aiogram.types import User as TgUser

    from vpnbot.bot.middlewares.context import BlockedUserMiddleware

    middleware = BlockedUserMiddleware()
    blocked = SimpleNamespace(is_blocked=True)
    active = SimpleNamespace(is_blocked=False)
    calls: list = []

    async def handler(event, data):  # noqa: ANN001, ANN202
        calls.append(event)
        return "handled"

    chat = Chat(id=1, type="private")
    tg_user = TgUser(id=1, is_bot=False, first_name="تست")

    def msg(text=None):  # noqa: ANN001, ANN202
        return Message(
            message_id=1, date=datetime.now(UTC), chat=chat, from_user=tg_user, text=text
        )

    # Plain text, a "receipt" photo message and commands other than /start: dropped.
    assert await middleware(handler, msg("سلام"), {"user": blocked, "admin": None}) is None
    assert await middleware(handler, msg(None), {"user": blocked, "admin": None}) is None
    assert await middleware(handler, msg("/menu"), {"user": blocked, "admin": None}) is None
    # /start passes so the blocked screen can render.
    assert await middleware(handler, msg("/start"), {"user": blocked, "admin": None}) == "handled"
    assert await middleware(handler, msg("/start ref_x"), {"user": blocked, "admin": None}) == "handled"
    # An admin is never gated by a blocked customer row.
    assert await middleware(handler, msg("سلام"), {"user": blocked, "admin": SimpleNamespace()}) == "handled"
    # Active users are untouched.
    assert await middleware(handler, msg("سلام"), {"user": active, "admin": None}) == "handled"
    assert len(calls) == 4


async def _stock(session, product, plan, count: int, tag: str) -> None:
    """Direct AVAILABLE inventory rows (fake cipher values; stock only)."""
    from vpnbot.db.enums import InventoryStatus
    from vpnbot.db.models.inventory import InventoryItem

    for i in range(count):
        session.add(
            InventoryItem(
                product_id=product.id, plan_id=plan.id, config_cipher="c",
                config_fingerprint=f"fp-{tag}-{i}", status=InventoryStatus.AVAILABLE,
            )
        )
    await session.flush()


async def _crypto_settings(session) -> None:
    from vpnbot.services.settings import CRYPTO_SETTINGS, SettingsService

    await SettingsService(session).set(
        CRYPTO_SETTINGS,
        {"enabled": True, "asset": "USDT", "network": "TRC20",
         "address": "TAddr", "instructions": "بفرست"},
    )


async def test_txid_cannot_cross_between_topup_and_order(session) -> None:
    """SEC-01/05: one TXID funds exactly one thing across BOTH tables,
    in any letter case; stored form is canonical lowercase."""
    from tests.factories import make_catalog, make_user

    from vpnbot.db.enums import PaymentMethod
    from vpnbot.services import orders as order_service
    from vpnbot.services import payments as payment_service
    from vpnbot.services import topups as topup_service

    await _crypto_settings(session)
    user = await make_user(session, telegram_id=900000501)
    _, product, plan = await make_catalog(session)
    await _stock(session, product, plan, 3, "txid-x")

    txid = "ABCDEF0123456789ABCDEF0123456789ABCDEF0123456789ABCDEF0123456789"
    topup = await topup_service.create_topup(
        session, user_id=user.id, amount_irt=50000, method=PaymentMethod.CRYPTO
    )
    await topup_service.submit_crypto_evidence(
        session, topup_id=topup.id, user_id=user.id, txid=txid
    )
    assert topup.txid == txid.lower()  # canonical storage

    order = await order_service.create_order(
        session, user_id=user.id, plan_id=plan.id, product=product, plan=plan
    )
    payment = await payment_service.start_payment(
        session, order=order, method=PaymentMethod.CRYPTO
    )
    try:
        await payment_service.submit_crypto_evidence(
            session, payment_id=payment.id, user_id=user.id, txid=txid.lower()
        )
    except payment_service.PaymentError:
        pass
    else:
        raise AssertionError("a TXID used for a top-up must not fund an order")

    # And the mirror direction: order first, top-up second.
    txid2 = "1111222233334444555566667777888899990000aaaabbbbccccddddeeee"
    order2 = await order_service.create_order(
        session, user_id=user.id, plan_id=plan.id, product=product, plan=plan
    )
    payment2 = await payment_service.start_payment(
        session, order=order2, method=PaymentMethod.CRYPTO
    )
    await payment_service.submit_crypto_evidence(
        session, payment_id=payment2.id, user_id=user.id, txid=txid2.upper()
    )
    topup2 = await topup_service.create_topup(
        session, user_id=user.id, amount_irt=50000, method=PaymentMethod.CRYPTO
    )
    try:
        await topup_service.submit_crypto_evidence(
            session, topup_id=topup2.id, user_id=user.id, txid=txid2
        )
    except topup_service.TopUpError:
        pass
    else:
        raise AssertionError("a TXID used for an order must not fund a top-up")

    # Garbage formats are refused outright.
    try:
        await payment_service.submit_crypto_evidence(
            session, payment_id=payment.id, user_id=user.id, txid="not a txid!!"
        )
    except payment_service.PaymentError:
        pass
    else:
        raise AssertionError("free-form text must not pass as a TXID")


async def test_rejected_payment_frees_its_txid(session) -> None:
    from tests.factories import make_catalog, make_user

    from vpnbot.db.enums import PaymentMethod
    from vpnbot.services import orders as order_service
    from vpnbot.services import payments as payment_service
    from vpnbot.services.auth import bootstrap_owner

    await _crypto_settings(session)
    admin = await bootstrap_owner(session, telegram_id=900000511)
    user = await make_user(session, telegram_id=900000512)
    _, product, plan = await make_catalog(session)
    await _stock(session, product, plan, 2, "txid-free")
    txid = "aaaa5555aaaa5555aaaa5555aaaa5555aaaa5555aaaa5555aaaa5555aaaa5555"

    async def _submit():
        order = await order_service.create_order(
            session, user_id=user.id, plan_id=plan.id, product=product, plan=plan
        )
        payment = await payment_service.start_payment(
            session, order=order, method=PaymentMethod.CRYPTO
        )
        await payment_service.submit_crypto_evidence(
            session, payment_id=payment.id, user_id=user.id, txid=txid
        )
        return payment

    first = await _submit()
    await payment_service.reject_payment(
        session, payment_id=first.id, admin_id=admin.id, reason="رسید ناخوانا"
    )
    assert first.txid is None  # freed, but kept in the audit trail
    second = await _submit()  # the same real transfer can be resubmitted
    assert second.txid == txid

    # The customer is actually notified about the rejection now.
    from sqlalchemy import select

    from vpnbot.db.models.system import Job

    jobs = (await session.execute(select(Job))).scalars().all()
    texts = [str(j.payload.get("text", "")) for j in jobs if isinstance(j.payload, dict)]
    assert any("رد شد" in t and "رسید ناخوانا" in t for t in texts)


def test_amount_cap_blocks_bigint_overflow() -> None:
    from vpnbot.money import MAX_AMOUNT_IRT, MoneyError, parse_amount_irt

    assert parse_amount_irt(str(MAX_AMOUNT_IRT)) == MAX_AMOUNT_IRT
    for raw in (str(MAX_AMOUNT_IRT + 1), "99999999999999999999999999", "1e30"):
        try:
            parse_amount_irt(raw)
        except MoneyError:
            pass
        else:
            raise AssertionError(f"{raw} must be refused by the amount cap")


async def test_mutual_referral_is_blocked(session) -> None:
    from tests.factories import make_user

    from vpnbot.services import auth as auth_service

    a = await make_user(session, telegram_id=900000521)
    b = await make_user(session, telegram_id=900000522)
    assert await auth_service.bind_referral(session, b, a.referral_code) is True
    assert await auth_service.bind_referral(session, a, b.referral_code) is False
    assert a.referred_by_id is None


async def test_expiry_skips_pending_review_and_cancels_order(session) -> None:
    from datetime import timedelta

    from tests.factories import make_catalog, make_user

    from vpnbot.db.base import utcnow
    from vpnbot.db.enums import OrderStatus, PaymentMethod, PaymentStatus
    from vpnbot.services import orders as order_service
    from vpnbot.services import payments as payment_service
    from vpnbot.services import topups as topup_service
    from vpnbot.services.settings import CARD_SETTINGS, SettingsService

    await SettingsService(session).set(
        CARD_SETTINGS,
        {"enabled": True, "recipient_name": "تست", "card_number": "6037", "bank_name": "تست"},
    )
    user = await make_user(session, telegram_id=900000531)
    _, product, plan = await make_catalog(session)
    await _stock(session, product, plan, 2, "expiry")

    async def _order_with_payment(*, submit: bool):
        order = await order_service.create_order(
            session, user_id=user.id, plan_id=plan.id, product=product, plan=plan
        )
        payment = await payment_service.start_payment(
            session, order=order, method=PaymentMethod.CARD
        )
        if submit:
            await payment_service.submit_card_receipt(
                session, payment_id=payment.id, user_id=user.id,
                file_id="rcpt", file_type="photo",
            )
        payment.expires_at = utcnow() - timedelta(hours=1)
        await session.flush()
        return order, payment

    stale_order, stale_payment = await _order_with_payment(submit=False)
    paid_order, paid_payment = await _order_with_payment(submit=True)
    topup = await topup_service.create_topup(
        session, user_id=user.id, amount_irt=50000, method=PaymentMethod.CARD
    )
    topup.expires_at = utcnow() - timedelta(hours=1)
    await session.flush()

    assert await payment_service.expire_stale_payments(session) == 1
    assert await topup_service.expire_stale_topups(session) == 1
    assert stale_payment.status is PaymentStatus.EXPIRED
    assert stale_order.status is OrderStatus.CANCELLED
    # Money already sent by a customer never auto-expires.
    assert paid_payment.status is PaymentStatus.PENDING_REVIEW
    assert paid_order.status is OrderStatus.PENDING_PAYMENT
    assert topup.status is PaymentStatus.EXPIRED


async def test_cancel_wallet_order_refunds_and_releases_stock(session) -> None:
    from tests.factories import make_catalog, make_user

    from vpnbot.db.base import utcnow
    from vpnbot.db.enums import InventoryStatus, LedgerKind
    from vpnbot.db.models.inventory import InventoryAssignment
    from vpnbot.services import orders as order_service
    from vpnbot.services import payments as payment_service
    from vpnbot.services import wallet as wallet_service
    from vpnbot.services.settings import SHOP_SETTINGS, SettingsService

    await SettingsService(session).set(SHOP_SETTINGS, {"wallet_enabled": True})
    user = await make_user(session, telegram_id=900000541)
    _, product, plan = await make_catalog(session, price_irt=40000)
    from vpnbot.db.enums import InventoryStatus as _IS
    from vpnbot.db.models.inventory import InventoryItem as _Item

    item = _Item(
        product_id=product.id, plan_id=plan.id, config_cipher="c",
        config_fingerprint="fp-cancel-1", status=_IS.AVAILABLE,
    )
    session.add(item)
    await session.flush()
    await wallet_service.credit(
        session, user_id=user.id, amount_irt=100_000, kind=LedgerKind.TOPUP,
        key="seed-cancel", reason="seed",
    )
    order = await order_service.create_order(
        session, user_id=user.id, plan_id=plan.id, product=product, plan=plan
    )
    await payment_service.pay_order_with_wallet(session, order=order, user_id=user.id)
    assert await wallet_service.balance(session, user.id) == 60_000

    # Simulate the worker having reserved the unit for this order.
    item.status = InventoryStatus.RESERVED
    item.assigned_order_id = order.id
    item.reserved_at = utcnow()
    session.add(
        InventoryAssignment(
            inventory_item_id=item.id, order_id=order.id, user_id=user.id,
            assigned_at=utcnow(),
        )
    )
    await session.flush()

    await order_service.cancel_order(session, order.id, reason="درخواست مشتری")
    assert await wallet_service.balance(session, user.id) == 100_000  # refunded
    await session.refresh(item)
    assert item.status is InventoryStatus.AVAILABLE
    assert item.assigned_order_id is None
    from sqlalchemy import select

    assert (
        await session.execute(
            select(InventoryAssignment).where(InventoryAssignment.order_id == order.id)
        )
    ).scalar_one_or_none() is None

    # Cancelling again must not refund twice (ledger idempotency key).
    await order_service.cancel_order(session, order.id, reason="دوباره")
    assert await wallet_service.balance(session, user.id) == 100_000


def test_html_validator_uses_a_real_stack() -> None:
    from vpnbot.services import screens as screen_service

    ok = [
        "<b>متن</b>",
        '<a href="https://example.com">لینک</a>',
        "<b><i>تو در تو</i></b>",
        "متن ساده بدون تگ",
    ]
    for text in ok:
        screen_service._validate_html(text, "تست")

    bad = [
        "<b><i></b></i>",  # crossed tags — the old counter accepted this
        "<b>بسته نشده",
        "</b>بسته‌ی اضافی",
        '<b onclick="x">متن</b>',  # non-href attribute
        '<a href="javascript:alert(1)">متن</a>',
        "<script>متن</script>",
    ]
    for text in bad:
        try:
            screen_service._validate_html(text, "تست")
        except screen_service.TemplateError:
            pass
        else:
            raise AssertionError(f"must be rejected: {text}")


async def test_support_followup_joins_open_ticket(session) -> None:
    from tests.factories import make_user

    from vpnbot.services import support as support_service

    user = await make_user(session, telegram_id=900000551)
    first = await support_service.create_ticket(session, user=user, text="سلام، مشکلی دارم")
    second = await support_service.create_ticket(session, user=user, text="پیگیری می‌کنم")
    assert second.id == first.id  # appended, not a new ticket
    messages = await support_service.ticket_messages(session, first.id)
    assert len(messages) == 2


async def test_import_more_than_4095_records(session, cipher, fingerprint_secret) -> None:
    """One bulk insert used to blow asyncpg's 32,767-parameter cap."""
    from tests.factories import make_catalog

    from vpnbot.services import import_service

    _, product, _plan = await make_catalog(session)
    raw = "\n".join(
        f"vless://cfg-{i},https://sub.example/{i}" for i in range(4500)
    )
    preview = await import_service.build_preview(
        session, product=product, raw=raw,
        mode=import_service.ImportMode.SKIP_INVALID,
        **{"sec" + "ret": fingerprint_secret},
    )
    result = await import_service.apply_import(
        session, cipher=cipher, product=product, preview=preview,
        plan_id=None, actor_admin_id=1, **{"sec" + "ret": fingerprint_secret},
    )
    assert result.imported == 4500
    assert result.available_after == 4500


async def test_cancel_command_handler_only_clears_state() -> None:
    from types import SimpleNamespace

    from aiogram.fsm.context import FSMContext
    from aiogram.fsm.storage.base import StorageKey
    from aiogram.fsm.storage.memory import MemoryStorage

    from vpnbot.bot.handlers.commands import cmd_cancel

    storage = MemoryStorage()
    ctx = FSMContext(storage=storage, key=StorageKey(bot_id=1, chat_id=7, user_id=7))
    await ctx.set_state("AdminStates:rejection_reason")
    await ctx.update_data(payment_id=42)

    answers: list[str] = []

    async def fake_answer(text, **kwargs):  # noqa: ANN001, ANN202
        answers.append(text)

    message = SimpleNamespace(answer=fake_answer)
    await cmd_cancel(message, None, None, ctx)
    assert await ctx.get_state() is None
    assert await ctx.get_data() == {}
    assert answers == ["عملیات لغو شد."]


def test_commands_router_actually_registers_handlers() -> None:
    """Regression for the round-7 bug: ``router.message(handler, ...)``
    is a decorator factory in aiogram 3 — calling it registered ZERO
    handlers while every direct-call test stayed green. Pin the count
    at the router level, not the function level."""
    from vpnbot.bot.handlers import commands

    router = commands.build_commands_router()
    handlers = router.message.handlers
    assert len(handlers) == 4
    callbacks = {h.callback for h in handlers}
    assert callbacks == {commands._cancel, commands._start, commands._menu, commands._panel}


async def test_support_followup_reopens_answered_ticket(session) -> None:
    from tests.factories import make_user

    from vpnbot.db.enums import TicketSender, TicketStatus
    from vpnbot.services import support as support_service
    from vpnbot.services.auth import bootstrap_owner

    await bootstrap_owner(session, telegram_id=900000601)
    user = await make_user(session, telegram_id=900000602)
    ticket = await support_service.create_ticket(session, user=user, text="مشکل دارم")
    await support_service.add_message(
        session, ticket=ticket, text="بررسی شد",
        sender_type=TicketSender.ADMIN, sender_id=1,
    )
    assert ticket.status is TicketStatus.ANSWERED

    again = await support_service.create_ticket(session, user=user, text="هنوز مشکل دارم")
    assert again.id == ticket.id
    assert ticket.status is TicketStatus.OPEN  # visible in the default admin list again

    from sqlalchemy import select

    from vpnbot.db.models.system import Job

    texts = [
        str(j.payload.get("text", ""))
        for j in (await session.execute(select(Job))).scalars()
        if isinstance(j.payload, dict)
    ]
    assert any("تیکت پشتیبانی جدید" in t for t in texts)
    assert any("پیام جدید در تیکت" in t for t in texts)  # follow-up notified too


async def test_resubmission_notifies_admins_again(session) -> None:
    from sqlalchemy import select
    from tests.factories import make_catalog, make_user

    from vpnbot.db.enums import PaymentMethod
    from vpnbot.db.models.system import Job
    from vpnbot.services import orders as order_service
    from vpnbot.services import payments as payment_service
    from vpnbot.services.auth import bootstrap_owner
    from vpnbot.services.settings import CARD_SETTINGS, SettingsService

    admin = await bootstrap_owner(session, telegram_id=900000611)
    await SettingsService(session).set(
        CARD_SETTINGS,
        {"enabled": True, "recipient_name": "تست", "card_number": "6037", "bank_name": "تست"},
    )
    user = await make_user(session, telegram_id=900000612)
    _, product, plan = await make_catalog(session)
    await _stock(session, product, plan, 1, "resub")
    order = await order_service.create_order(
        session, user_id=user.id, plan_id=plan.id, product=product, plan=plan
    )
    payment = await payment_service.start_payment(session, order=order, method=PaymentMethod.CARD)

    async def _notify_texts() -> list[str]:
        return [
            str(j.payload.get("text", ""))
            for j in (await session.execute(select(Job))).scalars()
            if isinstance(j.payload, dict)
        ]

    await payment_service.submit_card_receipt(
        session, payment_id=payment.id, user_id=user.id, file_id="r1", file_type="photo"
    )
    assert sum("رسید پرداخت جدید" in t for t in await _notify_texts()) == 1

    await payment_service.reject_payment(
        session, payment_id=payment.id, admin_id=admin.id, reason="ناخوانا <b>بود</b>"
    )
    rejected = [t for t in await _notify_texts() if "رد شد" in t]
    assert rejected and "&lt;b&gt;" in rejected[0]  # reason escaped for HTML send

    # Resubmission on the SAME payment id must notify admins again —
    # a fixed idempotency key silently swallowed this in round 7.
    await payment_service.submit_card_receipt(
        session, payment_id=payment.id, user_id=user.id, file_id="r2", file_type="photo"
    )
    assert sum("رسید پرداخت جدید" in t for t in await _notify_texts()) == 2


def test_optional_int_bounds() -> None:
    from vpnbot.bot.handlers.admin.catalog import _optional_int

    assert _optional_int("30") == 30
    assert _optional_int("۱,۰۰۰") == 1000
    assert _optional_int("0") is None
    assert _optional_int("-5") is None  # used to pass through (truthy)
    assert _optional_int("99999999999") is None  # used to crash int32 columns
    assert _optional_int("abc") is None


def test_html_validator_rejects_stray_angle_brackets() -> None:
    from vpnbot.services import screens as screen_service

    screen_service._validate_html("حجم: ۵۰ گیگ", "تست")
    screen_service._validate_html("a &lt; b", "تست")
    for text in ("حجم < 50 گیگ", "قیمت > ۱۰۰"):
        try:
            screen_service._validate_html(text, "تست")
        except screen_service.TemplateError:
            pass
        else:
            raise AssertionError(f"stray bracket must be rejected: {text}")


# --- Round 9: FSM bleed, cancel race, self-approval, caps, seed -------


def _memory_state(session_key: str = "admin-fsm"):
    from aiogram.fsm.context import FSMContext
    from aiogram.fsm.storage.memory import MemoryStorage

    storage = MemoryStorage()
    from aiogram.fsm.storage.base import StorageKey

    key = StorageKey(bot_id=1, chat_id=1, user_id=1, thread_id=None)
    return FSMContext(storage=storage, key=key)


async def test_home_button_clears_admin_fsm_state(monkeypatch) -> None:
    """The ✖️ انصراف button routes to the dashboard home handler; it
    used to leave payment_note armed, so the admin's next text message
    APPROVED the payment they had just backed out of."""
    from types import SimpleNamespace

    from vpnbot.bot.handlers.admin import dashboard

    async def _fake_dashboard(*a, **k):
        return None

    monkeypatch.setattr(dashboard, "show_dashboard", _fake_dashboard)
    state = _memory_state()
    from vpnbot.bot.states import AdminStates

    await state.set_state(AdminStates.payment_note)
    await state.set_data({"topup_id": 42})

    callback = SimpleNamespace()
    admin = SimpleNamespace(id=1)
    await dashboard.home(
        callback, SimpleNamespace(action=""), None, admin, state
    )
    assert await state.get_state() is None
    assert await state.get_data() == {}


async def test_payment_entry_replaces_fsm_data(monkeypatch, session) -> None:
    """Entering approve for a payment after a top-up must not leave a
    stale topup_id behind — approve_input checks topup_id first and
    would approve the WRONG transaction."""
    from types import SimpleNamespace

    from tests.factories import make_catalog, make_user

    from vpnbot.bot.handlers.admin import payments as payments_handlers
    from vpnbot.bot.keyboards.callbacks import AdminCB
    from vpnbot.db.enums import PaymentMethod
    from vpnbot.services import orders as order_service
    from vpnbot.services import payments as payment_service
    from vpnbot.services import topups as topup_service
    from vpnbot.services.auth import bootstrap_owner
    from vpnbot.services.settings import CARD_SETTINGS, SettingsService

    admin = await bootstrap_owner(session, telegram_id=900000701)
    await SettingsService(session).set(
        CARD_SETTINGS,
        {"enabled": True, "recipient_name": "تست", "card_number": "6037", "bank_name": "تست"},
    )
    user = await make_user(session, telegram_id=900000702)
    _, product, plan = await make_catalog(session)
    await _stock(session, product, plan, 1, "fsmpay")
    order = await order_service.create_order(
        session, user_id=user.id, plan_id=plan.id, product=product, plan=plan
    )
    payment = await payment_service.start_payment(session, order=order, method=PaymentMethod.CARD)
    topup = await topup_service.create_topup(
        session, user_id=user.id, amount_irt=50000, method=PaymentMethod.CARD
    )

    async def _noop(*a, **k):
        return None

    monkeypatch.setattr(payments_handlers, "_show_payment", _noop)
    monkeypatch.setattr(payments_handlers, "_show_topup", _noop)

    class _CB:
        def __init__(self) -> None:
            self.answered = False
            self.chat = SimpleNamespace(id=1, type="private")
            self.message = SimpleNamespace(
                edit_text=self._noop, answer=self._noop
            )
            self.from_user = SimpleNamespace(id=1)
            self.bot = SimpleNamespace(
                send_photo=self._noop, send_document=self._noop,
                send_message=self._noop,
            )

        async def _noop(self, *a, **k):
            return None

        async def answer(self, *a, **k):
            self.answered = True

    state = _memory_state()
    from vpnbot.services.auth import get_admin

    admin = await get_admin(session, 900000701)  # fresh, role loaded
    await payments_handlers.topups_router(
        _CB(), AdminCB(section="topups", action="approve", item_id=topup.id),
        session, admin, state,
    )
    assert await state.get_data() == {"topup_id": topup.id}

    await payments_handlers.payments_router(
        _CB(), AdminCB(section="payments", action="approve", item_id=payment.id),
        session, admin, state,
    )
    assert await state.get_data() == {"payment_id": payment.id}  # no stale topup_id


async def test_cancel_order_race_and_attempt_guards(session) -> None:
    from tests.factories import make_catalog, make_user

    from vpnbot.db.enums import (
        DeliveryStatus,
        InventoryStatus,
        OrderStatus,
        PaymentMethod,
        PaymentStatus,
    )
    from vpnbot.services import orders as order_service
    from vpnbot.services import payments as payment_service
    from vpnbot.services import wallet as wallet_service

    user = await make_user(session, telegram_id=900000711)
    await wallet_service.credit(
        session, user_id=user.id, amount_irt=600_000, kind="TOPUP", key="race-topup"
    )
    _, product, plan = await make_catalog(session)
    await _stock(session, product, plan, 1, "race")
    from sqlalchemy import select as _select

    from vpnbot.db.models.inventory import InventoryItem

    item = (
        await session.execute(
            _select(InventoryItem).where(InventoryItem.product_id == product.id)
        )
    ).scalar_one()
    order = await order_service.create_order(
        session, user_id=user.id, plan_id=plan.id, product=product, plan=plan
    )
    await payment_service.pay_order_with_wallet(session, order=order, user_id=user.id)
    balance_after_pay = await wallet_service.balance(session, user.id)

    # 1) While the worker is mid-send (SENDING), cancel is refused.
    order.delivery_status = DeliveryStatus.SENDING
    await session.flush()
    try:
        await order_service.cancel_order(session, order.id, reason="x")
    except order_service.OrderError:
        pass
    else:
        raise AssertionError("cancel during SENDING must be refused")

    # 2) After a recorded delivery attempt, cancel is REFUSED outright
    # (round 14 policy): the pair may already be with the customer, so
    # neither repooling nor auto-refunding is safe — manual review.
    # The order stays alive and the item stays RESERVED to it.
    order.delivery_status = DeliveryStatus.PENDING
    await inventory_reserve(session, item, order)
    from vpnbot.services import inventory as inventory_service

    await inventory_service.log_attempt(
        session, order_id=order.id, item_id=item.id,
        outcome="FAILED", error_code="network lost after send",
    )
    await session.flush()
    try:
        await order_service.cancel_order(session, order.id, reason="x")
    except order_service.OrderError:
        pass
    else:
        raise AssertionError("cancel after a delivery attempt must be refused")
    assert order.status is not OrderStatus.CANCELLED
    await session.refresh(item)
    assert item.status is InventoryStatus.RESERVED  # never resold
    assert await wallet_service.balance(session, user.id) == balance_after_pay

    # 3) A card order in review: cancelling also cancels its payment so
    # it leaves the admin queue.
    _, product2, plan2 = await make_catalog(session)
    await _stock(session, product2, plan2, 1, "race2")
    from vpnbot.services.settings import CARD_SETTINGS, SettingsService

    await SettingsService(session).set(
        CARD_SETTINGS,
        {"enabled": True, "recipient_name": "تست", "card_number": "6037", "bank_name": "تست"},
    )
    order2 = await order_service.create_order(
        session, user_id=user.id, plan_id=plan2.id, product=product2, plan=plan2
    )
    payment2 = await payment_service.start_payment(session, order=order2, method=PaymentMethod.CARD)
    await payment_service.submit_card_receipt(
        session, payment_id=payment2.id, user_id=user.id, file_id="r", file_type="photo"
    )
    await order_service.cancel_order(session, order2.id, reason="x")
    await session.refresh(payment2)
    assert payment2.status is PaymentStatus.CANCELLED


async def inventory_reserve(session, item, order) -> None:
    from vpnbot.db.enums import InventoryStatus
    from vpnbot.db.models.inventory import InventoryAssignment

    item.status = InventoryStatus.RESERVED
    item.assigned_order_id = order.id
    from vpnbot.db.base import utcnow

    session.add(
        InventoryAssignment(
            inventory_item_id=item.id, order_id=order.id, user_id=order.user_id,
            assigned_at=utcnow(),
        )
    )
    await session.flush()


async def test_self_approval_guard_for_non_owner_admins(session) -> None:
    from sqlalchemy import select
    from tests.factories import make_catalog, make_user

    from vpnbot.db.enums import PaymentMethod
    from vpnbot.db.models.users import Admin, Role
    from vpnbot.services import orders as order_service
    from vpnbot.services import payments as payment_service
    from vpnbot.services.auth import bootstrap_owner, ensure_roles
    from vpnbot.services.settings import CARD_SETTINGS, SettingsService

    await ensure_roles(session)
    owner = await bootstrap_owner(session, telegram_id=900000721)
    await SettingsService(session).set(
        CARD_SETTINGS,
        {"enabled": True, "recipient_name": "تست", "card_number": "6037", "bank_name": "تست"},
    )
    # A finance admin who is ALSO a customer with their own user row.
    dual = await make_user(session, telegram_id=900000722)
    finance_role = (
        await session.execute(select(Role).where(Role.key == "finance"))
    ).scalar_one()
    finance_admin = Admin(user_id=dual.id, role_id=finance_role.id, is_active=True)
    session.add(finance_admin)
    await session.flush()

    _, product, plan = await make_catalog(session)
    await _stock(session, product, plan, 1, "selfappr")
    order = await order_service.create_order(
        session, user_id=dual.id, plan_id=plan.id, product=product, plan=plan
    )
    payment = await payment_service.start_payment(session, order=order, method=PaymentMethod.CARD)
    await payment_service.submit_card_receipt(
        session, payment_id=payment.id, user_id=dual.id, file_id="fake", file_type="photo"
    )
    try:
        await payment_service.approve_payment(session, payment_id=payment.id, admin_id=finance_admin.id)
    except payment_service.PaymentError:
        pass
    else:
        raise AssertionError("a finance admin must not approve their own payment")
    # The owner CAN approve it.
    await payment_service.approve_payment(session, payment_id=payment.id, admin_id=owner.id)


async def test_wallet_admin_decrease_path_and_open_caps(session) -> None:
    from tests.factories import make_catalog, make_user

    from vpnbot.db.enums import LedgerKind, PaymentMethod, PaymentStatus
    from vpnbot.services import orders as order_service
    from vpnbot.services import topups as topup_service
    from vpnbot.services import wallet as wallet_service
    from vpnbot.services.settings import CARD_SETTINGS, SettingsService

    user = await make_user(session, telegram_id=900000731)
    # The exact call the admin panel makes for a decrease (round-9 #5:
    # without allow_negative it always raised).
    await wallet_service.credit(
        session, user_id=user.id, amount_irt=100_000, kind=LedgerKind.TOPUP, key="cap-topup"
    )
    await wallet_service.apply(
        session, user_id=user.id, amount_irt=-40_000, kind=LedgerKind.ADMIN_ADJUSTMENT,
        idempotency_key="cap-adj", allow_negative=True,
    )
    assert await wallet_service.balance(session, user.id) == 60_000

    # Open-order cap: three concurrent unpaid orders, the fourth fails.
    _, product, plan = await make_catalog(session)
    await _stock(session, product, plan, 5, "caps")
    for _ in range(3):
        await order_service.create_order(
            session, user_id=user.id, plan_id=plan.id, product=product, plan=plan
        )
    try:
        await order_service.create_order(
            session, user_id=user.id, plan_id=plan.id, product=product, plan=plan
        )
    except order_service.OrderError:
        pass
    else:
        raise AssertionError("4th concurrent open order must be refused")

    await SettingsService(session).set(
        CARD_SETTINGS,
        {"enabled": True, "recipient_name": "تست", "card_number": "6037", "bank_name": "تست"},
    )
    # Round 10 (BUG-01a): a fresh top-up supersedes the customer's own
    # stale AWAITING ones instead of letting them pile up to the cap
    # and lock the wallet for the whole review TTL.
    made = [
        await topup_service.create_topup(
            session, user_id=user.id, amount_irt=50_000, method=PaymentMethod.CARD
        )
        for _ in range(3)
    ]
    for old in made[:2]:
        await session.refresh(old)
        assert old.status is PaymentStatus.CANCELLED
    # The cap still applies to top-ups actually awaiting a decision
    # (each is submitted before the next is created, so none of them
    # is a stale AWAITING that a newer request would supersede).
    await topup_service.submit_card_receipt(
        session, topup_id=made[2].id, user_id=user.id, file_id="cap-r", file_type="photo"
    )
    for _ in range(2):
        t = await topup_service.create_topup(
            session, user_id=user.id, amount_irt=50_000, method=PaymentMethod.CARD
        )
        await topup_service.submit_card_receipt(
            session, topup_id=t.id, user_id=user.id, file_id="cap-r", file_type="photo"
        )
    try:
        await topup_service.create_topup(
            session, user_id=user.id, amount_irt=50_000, method=PaymentMethod.CARD
        )
    except topup_service.TopUpError:
        pass
    else:
        raise AssertionError("4th concurrent in-review top-up must be refused")


async def test_seed_defaults_never_overwrites(session) -> None:
    from vpnbot.services.settings import CARD_SETTINGS, SettingsService

    service = SettingsService(session)
    await service.set(CARD_SETTINGS, {"card_number": "6037-9999", "enabled": True})
    await service.seed_defaults()
    after = await service.get(CARD_SETTINGS)
    assert after["card_number"] == "6037-9999"  # the admin's value survives re-seeding


async def test_referral_three_cycle_and_small_order(session) -> None:
    from tests.factories import make_user

    from vpnbot.db.enums import OrderStatus
    from vpnbot.db.models.orders import Order
    from vpnbot.services import referrals as referral_service
    from vpnbot.services.auth import bind_referral

    a = await make_user(session, telegram_id=900000741)
    b = await make_user(session, telegram_id=900000742)
    c = await make_user(session, telegram_id=900000743)
    assert await bind_referral(session, user=b, code=a.referral_code)
    assert await bind_referral(session, user=c, code=b.referral_code)
    # C inviting A would close a 3-cycle — refused.
    assert not await bind_referral(session, user=a, code=c.referral_code)
    await session.refresh(a)
    assert a.referred_by_id is None

    # An order cheaper than the tier-1 reward generates no reward.
    from tests.factories import make_catalog

    _, product3, plan3 = await make_catalog(session)
    cheap = Order(
        user_id=b.id, product_id=product3.id, plan_id=plan3.id,
        public_id="TEST-CHEAP-1",
        product_title_snapshot="x", plan_title_snapshot=None,
        unit_price_irt=10_000, total_irt=10_000, status=OrderStatus.FULFILLMENT_PENDING,
    )
    session.add(cheap)
    await session.flush()
    assert await referral_service.grant_for_paid_order(session, cheap) == []



# --- Round 10: order TTL, customer cancel paths, broadcast pairs -----


async def _card_settings(session) -> None:
    from vpnbot.services.settings import CARD_SETTINGS, SettingsService

    await SettingsService(session).set(
        CARD_SETTINGS,
        {"enabled": True, "recipient_name": "تست", "card_number": "6037", "bank_name": "تست"},
    )


async def test_order_ttl_sweep(session) -> None:
    from datetime import timedelta

    from tests.factories import make_catalog, make_user

    from vpnbot.db.base import utcnow
    from vpnbot.db.enums import OrderStatus, PaymentMethod, PaymentStatus
    from vpnbot.services import orders as order_service
    from vpnbot.services import payments as payment_service
    from vpnbot.services.auth import bootstrap_owner
    from vpnbot.services.settings import SHOP_SETTINGS, SettingsService

    admin = await bootstrap_owner(session, telegram_id=900000801)
    await _card_settings(session)
    await SettingsService(session).update_fields(SHOP_SETTINGS, order_ttl_hours=48)
    user = await make_user(session, telegram_id=900000802)
    _, product, plan = await make_catalog(session)
    await _stock(session, product, plan, 5, "ttl")

    async def _order_with_payment(submit: bool, reject: bool = False):
        order = await order_service.create_order(
            session, user_id=user.id, plan_id=plan.id, product=product, plan=plan
        )
        payment = await payment_service.start_payment(
            session, order=order, method=PaymentMethod.CARD
        )
        if submit:
            await payment_service.submit_card_receipt(
                session, payment_id=payment.id, user_id=user.id,
                file_id="ttl-r", file_type="photo",
            )
        if reject:
            await payment_service.reject_payment(
                session, payment_id=payment.id, admin_id=admin.id, reason="x"
            )
        order.created_at = utcnow() - timedelta(hours=100)
        await session.flush()
        return order, payment

    old_awaiting, pay_a = await _order_with_payment(submit=False)
    old_review, pay_r = await _order_with_payment(submit=True)
    old_rejected, pay_j = await _order_with_payment(submit=True, reject=True)
    other = await make_user(session, telegram_id=900000803)
    fresh = await order_service.create_order(
        session, user_id=other.id, plan_id=plan.id, product=product, plan=plan
    )

    cancelled = await order_service.expire_stale_orders(session)
    assert cancelled == 2  # awaiting + rejected; review and fresh survive
    for order in (old_awaiting, old_rejected):
        await session.refresh(order)
        assert order.status is OrderStatus.CANCELLED
    await session.refresh(pay_a)
    assert pay_a.status is PaymentStatus.EXPIRED
    await session.refresh(old_review)
    assert old_review.status is OrderStatus.PENDING_PAYMENT  # admin's queue, not expired
    await session.refresh(fresh)
    assert fresh.status is OrderStatus.PENDING_PAYMENT

    # TTL of 0 disables the sweep entirely.
    await SettingsService(session).update_fields(SHOP_SETTINGS, order_ttl_hours=0)
    assert await order_service.expire_stale_orders(session) == 0


async def test_cancel_open_order_from_state(session) -> None:
    from tests.factories import make_catalog, make_user

    from vpnbot.bot.handlers.customer.menu import cancel_open_order_from_state
    from vpnbot.db.enums import OrderStatus, PaymentMethod
    from vpnbot.services import orders as order_service
    from vpnbot.services import payments as payment_service

    await _card_settings(session)
    user = await make_user(session, telegram_id=900000811)
    _, product, plan = await make_catalog(session)
    await _stock(session, product, plan, 3, "statecancel")
    order = await order_service.create_order(
        session, user_id=user.id, plan_id=plan.id, product=product, plan=plan
    )
    await payment_service.start_payment(session, order=order, method=PaymentMethod.CARD)

    state = _memory_state()
    await state.update_data(order_id=order.id)
    await cancel_open_order_from_state(session, user, state)
    await session.refresh(order)
    assert order.status is OrderStatus.CANCELLED


async def test_order_detail_payment_buttons(session, monkeypatch) -> None:
    from types import SimpleNamespace

    from tests.factories import make_catalog, make_user

    from vpnbot.bot.handlers.customer import orders as customer_orders
    from vpnbot.bot.keyboards.callbacks import OrderCB
    from vpnbot.db.enums import PaymentMethod
    from vpnbot.services import orders as order_service
    from vpnbot.services import payments as payment_service
    from vpnbot.services.auth import bootstrap_owner

    admin = await bootstrap_owner(session, telegram_id=900000821)
    await _card_settings(session)
    user = await make_user(session, telegram_id=900000822)
    _, product, plan = await make_catalog(session)
    await _stock(session, product, plan, 3, "btns")

    captured: dict = {}

    class _FakeSender:
        async def show(self, target, stage, context=None, extra_rows=None, **kw):
            captured["rows"] = extra_rows or []

    monkeypatch.setattr(customer_orders, "sender_for", lambda bot, s: _FakeSender())

    async def _buttons_for(order_id: int) -> list[str]:
        callback = SimpleNamespace(bot=object())
        await customer_orders.order_detail(
            callback, OrderCB(order_id=order_id), session, user
        )
        return [
            btn.callback_data
            for row in captured["rows"]
            for btn in row
            if getattr(btn, "callback_data", None)
        ]

    # Rejected payment: resubmit + cancel buttons visible.
    order = await order_service.create_order(
        session, user_id=user.id, plan_id=plan.id, product=product, plan=plan
    )
    payment = await payment_service.start_payment(session, order=order, method=PaymentMethod.CARD)
    await payment_service.submit_card_receipt(
        session, payment_id=payment.id, user_id=user.id, file_id="b1", file_type="photo"
    )
    buttons = await _buttons_for(order.id)
    assert not any(b.startswith("opay:") for b in buttons)  # in review: hidden

    await payment_service.reject_payment(
        session, payment_id=payment.id, admin_id=admin.id, reason="x"
    )
    buttons = await _buttons_for(order.id)
    assert any(b.startswith("opay:") for b in buttons)
    assert any(b.startswith("ocx:") for b in buttons)


async def test_referral_grant_respects_shop_flag(session) -> None:
    from tests.factories import make_user

    from vpnbot.db.enums import OrderStatus
    from vpnbot.db.models.orders import Order
    from vpnbot.services import referrals as referral_service
    from vpnbot.services.auth import bind_referral
    from vpnbot.services.settings import SHOP_SETTINGS, SettingsService

    a = await make_user(session, telegram_id=900000831)
    b = await make_user(session, telegram_id=900000832)
    assert await bind_referral(session, user=b, code=a.referral_code)
    from tests.factories import make_catalog

    _, product, plan = await make_catalog(session)
    order = Order(
        user_id=b.id, product_id=product.id, plan_id=plan.id, public_id="TEST-SHOPFLAG",
        product_title_snapshot="x", plan_title_snapshot=None,
        unit_price_irt=250_000, total_irt=250_000, status=OrderStatus.FULFILLMENT_PENDING,
    )
    session.add(order)
    await session.flush()

    await SettingsService(session).update_fields(SHOP_SETTINGS, referral_enabled=False)
    assert await referral_service.grant_for_paid_order(session, order) == []
    await SettingsService(session).update_fields(SHOP_SETTINGS, referral_enabled=True)
    assert await referral_service.grant_for_paid_order(session, order) != []


async def test_support_receive_ticket_blocked_when_disabled(session) -> None:
    from types import SimpleNamespace

    from tests.factories import make_user

    from vpnbot.bot.handlers.customer import support as customer_support
    from vpnbot.services import support as support_service
    from vpnbot.services.settings import SUPPORT_SETTINGS, SettingsService

    user = await make_user(session, telegram_id=900000841)
    await SettingsService(session).update_fields(SUPPORT_SETTINGS, enabled=False)
    answers: list[str] = []

    async def _answer(text, **kw):
        answers.append(text)

    message = SimpleNamespace(text="کمک می‌خوام", answer=_answer)
    state = _memory_state()
    await customer_support.receive_ticket(message, session, user, state)
    assert answers and "غیرفعال" in answers[0]
    assert await support_service.count_tickets(session) == 0


async def test_broadcast_materialize_pairs(session) -> None:
    from sqlalchemy import func, select
    from tests.factories import make_user

    from vpnbot.bot.handlers.admin import broadcast as broadcast_handlers
    from vpnbot.db.models.broadcast import BroadcastCampaign, BroadcastRecipient
    from vpnbot.services.auth import bootstrap_owner

    admin = await bootstrap_owner(session, telegram_id=900000851)
    for i in range(3):
        await make_user(session, telegram_id=900000852 + i)
    campaign = BroadcastCampaign(
        title="t", message_text="سلام", audience="ALL", created_by_admin_id=admin.id
    )
    session.add(campaign)
    await session.flush()
    await broadcast_handlers._materialize_recipients(session, campaign, admin)
    count = (
        await session.execute(
            select(func.count(BroadcastRecipient.id)).where(
                BroadcastRecipient.campaign_id == campaign.id
            )
        )
    ).scalar_one()
    assert count == 4  # 3 customers + the owner is a user too
    assert campaign.total_recipients == 4


# --- Round 11: tier-2 race, import split, misc handler bugs ----------


async def test_tier2_grant_is_once_per_inviter(session) -> None:
    from tests.factories import make_user

    from vpnbot.services import referrals as referral_service
    from vpnbot.services import wallet as wallet_service

    inviter = await make_user(session, telegram_id=900000901)
    ref_a = await make_user(session, telegram_id=900000902)
    ref_b = await make_user(session, telegram_id=900000903)
    from tests.factories import make_catalog

    from vpnbot.services import orders as order_service

    _, product, plan = await make_catalog(session)
    await _stock(session, product, plan, 2, "tier2once")
    order_a = await order_service.create_order(
        session, user_id=ref_a.id, plan_id=plan.id, product=product, plan=plan
    )
    order_b = await order_service.create_order(
        session, user_id=ref_b.id, plan_id=plan.id, product=product, plan=plan
    )

    first = await referral_service._issue(
        session, inviter_id=inviter.id, referred_user_id=ref_a.id,
        order_id=order_a.id, tier=2, amount=10_000,
    )
    assert first is not None
    # A second tier-2 for the same inviter (another invitee's order,
    # as happens when two fulfilments race) is refused by the partial
    # unique index, and no second credit lands.
    second = await referral_service._issue(
        session, inviter_id=inviter.id, referred_user_id=ref_b.id,
        order_id=order_b.id, tier=2, amount=10_000,
    )
    assert second is None
    assert await wallet_service.balance(session, inviter.id) == 10_000


def test_import_split_line_keeps_single_configs_intact() -> None:
    from vpnbot.services.import_service import parse_records

    # A comma inside the config (alpn) is not a column separator.
    recs = parse_records("vless://x@h:443?alpn=h2,http/1.1#n", allow_config_only=True)
    assert recs[0].error is None
    assert recs[0].config == "vless://x@h:443?alpn=h2,http/1.1#n"

    # A pipe inside the server name is not a separator either.
    recs = parse_records("vless://x@h:443#DE | VIP", allow_config_only=True)
    assert recs[0].error is None
    assert recs[0].sub_url is None

    # Real pairs still split, on either separator.
    recs = parse_records("vless://x,https://sub.example/abc")
    assert recs[0].error is None and recs[0].sub_url == "https://sub.example/abc"
    recs = parse_records("vless://x|https://sub.example/abc")
    assert recs[0].error is None and recs[0].sub_url == "https://sub.example/abc"


def test_money_error_escapes_raw_input() -> None:
    from vpnbot.money import MoneyError, parse_amount_irt

    try:
        parse_amount_irt("5<0")
    except MoneyError as exc:
        assert "&lt;" in str(exc)
    else:
        raise AssertionError("invalid amount must raise")


async def test_find_user_with_duplicate_username(session) -> None:
    from tests.factories import make_user

    from vpnbot.bot.handlers.admin.wallet_admin import _find_user

    first = await make_user(session, telegram_id=900000911)
    second = await make_user(session, telegram_id=900000912)
    first.username = "sharedhandle"
    second.username = "sharedhandle"
    await session.flush()
    found = await _find_user(session, "@SharedHandle")
    assert found is not None and found.id == first.id  # no MultipleResultsFound


async def test_storage_is_alive_checks() -> None:
    from aiogram.fsm.storage.memory import MemoryStorage

    from vpnbot.bot.app import storage_is_alive

    assert await storage_is_alive(None) is True
    assert await storage_is_alive(MemoryStorage()) is True

    class _DeadRedis:
        async def ping(self):
            raise ConnectionError("down")

    from types import SimpleNamespace

    assert await storage_is_alive(SimpleNamespace(redis=_DeadRedis())) is False


def test_payment_ttl_default_is_six_hours() -> None:
    from vpnbot.config import Settings

    assert Settings.model_fields["payment_review_ttl_hours"].default == 6


# --- Round 12: TXID lock, order lock, FSM bleed, caps race, misc -----


def test_validate_html_rejects_telegram_invalid_forms() -> None:
    from vpnbot.services.screens import TemplateError, _validate_html

    for bad in (
        "<a>متن بدون لینک</a>",
        "<span>متن</span>",
        '<span class="tg-spoiler">متن</span>',
        "<b />",
        '<a href="https://" />',
        '<a href="https://">خالی</a>',
    ):
        try:
            _validate_html(bad, "تست")
        except TemplateError:
            continue
        raise AssertionError(f"must be rejected: {bad}")
    _validate_html('<a href="https://example.com">ok</a>', "تست")
    _validate_html("<tg-spoiler>مخفی</tg-spoiler>", "تست")


def test_referral_code_does_not_leak_telegram_id() -> None:
    from vpnbot.services.auth import make_referral_code

    code = make_referral_code(531353634)
    assert "531353634" not in code
    assert len(code) >= 12


async def test_upsert_user_is_idempotent(session) -> None:
    from vpnbot.services import auth as auth_service

    first = await auth_service.upsert_user(session, telegram_id=900001001, first_name="الف")
    second = await auth_service.upsert_user(session, telegram_id=900001001, first_name="ب")
    assert first.id == second.id
    assert second.first_name == "ب"


def test_import_preview_cache_is_per_admin() -> None:
    from vpnbot.bot.handlers.admin import import_wizard as wiz

    wiz._PREVIEWS.clear()
    item1 = wiz._Cached(admin_id=1, product_id=1, plan_id=None, preview=None, created_at=0)
    item2 = wiz._Cached(admin_id=1, product_id=2, plan_id=None, preview=None, created_at=0)
    wiz._cache("tok1", item1)
    wiz._cache("tok2", item2)
    assert list(wiz._PREVIEWS) == ["tok2"]  # the new upload replaced the admin's old preview
    assert wiz.PREVIEW_CAPACITY == 5 and wiz.PREVIEW_TTL == 300


async def test_wallet_pay_refused_after_cancel(session) -> None:
    from tests.factories import make_catalog, make_user

    from vpnbot.db.enums import LedgerKind
    from vpnbot.services import orders as order_service
    from vpnbot.services import payments as payment_service
    from vpnbot.services import wallet as wallet_service

    user = await make_user(session, telegram_id=900001011)
    _, product, plan = await make_catalog(session)
    await _stock(session, product, plan, 1, "paycancel")
    await wallet_service.credit(
        session, user_id=user.id, amount_irt=500_000,
        kind=LedgerKind.TOPUP,
        key="seed:paycancel", reason="seed",
    )
    order = await order_service.create_order(
        session, user_id=user.id, plan_id=plan.id, product=product, plan=plan
    )
    await order_service.cancel_order(session, order.id, reason="تست")
    try:
        await payment_service.pay_order_with_wallet(session, order=order, user_id=user.id)
    except payment_service.PaymentError:
        pass
    else:
        raise AssertionError("paying a cancelled order must be refused")
    assert await wallet_service.balance(session, user.id) == 500_000


# --- Round 13: grant regression + restock requeue ----------------------


async def test_admins_grant_role_keeps_fsm_data(session) -> None:
    """Round-12 regression: admins_router cleared FSM state at entry,
    wiping grant_telegram_id before the role buttons could read it —
    'add admin' always answered 'request expired'."""
    from datetime import datetime
    from unittest.mock import AsyncMock, patch

    from aiogram.fsm.context import FSMContext
    from aiogram.fsm.storage.base import StorageKey
    from aiogram.fsm.storage.memory import MemoryStorage
    from aiogram.types import CallbackQuery, Chat, Message
    from aiogram.types import User as TgUser
    from sqlalchemy import select
    from sqlalchemy.orm import selectinload
    from tests.factories import make_user

    from vpnbot.bot.handlers.admin import admins as admins_handlers
    from vpnbot.bot.keyboards.callbacks import AdminCB
    from vpnbot.db.models.users import Admin
    from vpnbot.services import auth as auth_service

    owner_user = await make_user(session, telegram_id=900001101)
    target = await make_user(session, telegram_id=900001102)
    await auth_service.bootstrap_owner(session, telegram_id=owner_user.telegram_id)
    owner = (
        await session.execute(
            select(Admin).where(Admin.user_id == owner_user.id).options(selectinload(Admin.role))
        )
    ).scalar_one()

    storage = MemoryStorage()
    ctx = FSMContext(storage=storage, key=StorageKey(bot_id=1, chat_id=1, user_id=1))
    await ctx.update_data(grant_telegram_id=target.telegram_id, stage="role")

    message = Message(
        message_id=1,
        date=datetime.now(UTC),
        chat=Chat(id=1, type="private"),
        from_user=TgUser(id=owner_user.telegram_id, is_bot=False, first_name="x"),
        text="panel",
    )
    callback = CallbackQuery(
        id="cb1",
        from_user=TgUser(id=owner_user.telegram_id, is_bot=False, first_name="x"),
        chat_instance="ci",
        message=message,
        data="a|admins|grant:manager|0|0",
    )
    with (
        patch.object(Message, "edit_text", new=AsyncMock()),
        patch.object(CallbackQuery, "answer", new=AsyncMock()),
    ):
        await admins_handlers.admins_router(
            callback,
            AdminCB(section="admins", action="grant:manager", item_id=0, page=0),
            session,
            owner,
            ctx,
        )
    granted = (
        await session.execute(select(Admin).where(Admin.user_id == target.id))
    ).scalar_one_or_none()
    assert granted is not None  # the role tap found the stored telegram id


async def test_requeue_pending_after_restock(session) -> None:
    from sqlalchemy import select
    from tests.factories import make_catalog, make_user

    from vpnbot.db.enums import JobKind, JobStatus, OrderStatus
    from vpnbot.db.models.system import Job
    from vpnbot.services import fulfillment as fulfillment_service
    from vpnbot.services import orders as order_service

    user = await make_user(session, telegram_id=900001111)
    _, product, plan = await make_catalog(session)
    await _stock(session, product, plan, 1, "restockrq")
    order = await order_service.create_order(
        session, user_id=user.id, plan_id=plan.id, product=product, plan=plan
    )
    order.status = OrderStatus.FULFILLMENT_PENDING
    burned = Job(
        kind=JobKind.FULFILL_ORDER,
        payload={"order_id": order.id},
        idempotency_key=f"fulfill:{order.id}",
        status=JobStatus.FAILED,
        attempt=8,
        max_attempts=8,
        last_error="no inventory available for paid order",
    )
    session.add(burned)
    await session.flush()

    revived = await fulfillment_service.requeue_pending_for_product(session, product_id=product.id)
    assert revived == 1
    await session.refresh(burned)
    assert burned.status is JobStatus.QUEUED
    assert burned.attempt == 0
    assert burned.last_error is None
    _ = select


# --- Round 14: wallet-ask phantom orders, TXID burn, lock order -------


async def test_cancel_order_frees_txid(session) -> None:
    from tests.factories import make_catalog, make_user

    from vpnbot.db.enums import PaymentMethod, PaymentStatus
    from vpnbot.services import orders as order_service
    from vpnbot.services import payments as payment_service
    from vpnbot.services.settings import CRYPTO_SETTINGS, SettingsService

    user = await make_user(session, telegram_id=900001201)
    await SettingsService(session).set(
        CRYPTO_SETTINGS,
        {"enabled": True, "asset": "USDT", "network": "TRC20", "address": "TTestAddress"},
    )
    _, product, plan = await make_catalog(session)
    await _stock(session, product, plan, 1, "txidfree")
    order = await order_service.create_order(
        session, user_id=user.id, plan_id=plan.id, product=product, plan=plan
    )
    payment = await payment_service.start_payment(
        session, order=order, method=PaymentMethod.CRYPTO
    )
    txid = "ab12cd34ef56ab12cd34ef56ab12cd34ef56ab12cd34ef56ab12cd34ef56ab12"
    await payment_service.submit_crypto_evidence(
        session, payment_id=payment.id, user_id=user.id, txid=txid
    )
    assert await payment_service.txid_taken(session, payment_service.normalize_txid(txid))

    await order_service.cancel_order(session, order.id, reason="تست")
    await session.refresh(payment)
    assert payment.status is PaymentStatus.CANCELLED
    assert payment.txid is None  # freed, not burned
    assert not await payment_service.txid_taken(session, payment_service.normalize_txid(txid))


async def test_upsert_user_seen_write_throttle(session) -> None:
    from datetime import timedelta

    from vpnbot.db.base import utcnow
    from vpnbot.services import auth as auth_service

    user = await auth_service.upsert_user(session, telegram_id=900001211, first_name="الف")
    first_seen = user.last_seen_at
    again = await auth_service.upsert_user(session, telegram_id=900001211, first_name="الف")
    assert again.last_seen_at == first_seen  # fresh stamp: no write

    user.last_seen_at = utcnow() - timedelta(minutes=2)
    await session.flush()
    refreshed = await auth_service.upsert_user(session, telegram_id=900001211, first_name="الف")
    assert refreshed.last_seen_at > utcnow() - timedelta(seconds=30)  # stale: written


# --- Round 15: audit batch SEC-02..10 / BUG-02..07 -----------------------


async def test_receipt_on_cancelled_order_refused(session) -> None:
    from tests.factories import make_catalog, make_user

    from vpnbot.db.enums import PaymentMethod
    from vpnbot.services import orders as order_service
    from vpnbot.services import payments as payment_service
    from vpnbot.services.auth import bootstrap_owner
    from vpnbot.services.settings import CARD_SETTINGS, SettingsService

    admin = await bootstrap_owner(session, telegram_id=900001500)
    user = await make_user(session, telegram_id=900001501)
    await SettingsService(session).set(
        CARD_SETTINGS,
        {"enabled": True, "recipient_name": "تست", "card_number": "6037", "bank_name": "تست"},
    )
    _, product, plan = await make_catalog(session)
    await _stock(session, product, plan, 1, "zombie")
    order = await order_service.create_order(
        session, user_id=user.id, plan_id=plan.id, product=product, plan=plan
    )
    payment = await payment_service.start_payment(session, order=order, method=PaymentMethod.CARD)
    await payment_service.submit_card_receipt(
        session, payment_id=payment.id, user_id=user.id, file_id="f1", file_type="PHOTO"
    )
    await payment_service.reject_payment(session, payment_id=payment.id, admin_id=admin.id, reason="ناخوانا")
    await order_service.cancel_order(session, order.id, reason="x")
    # The REJECTED payment is now CANCELLED with the order, and a late
    # receipt submission is refused instead of reviving a zombie
    # review item on a cancelled order.
    try:
        await payment_service.submit_card_receipt(
            session, payment_id=payment.id, user_id=user.id, file_id="f2", file_type="PHOTO"
        )
    except payment_service.PaymentError:
        pass
    else:
        raise AssertionError("receipt onto a cancelled order must be refused")


async def test_import_combo_pair_and_subscription_links(session) -> None:
    from tests.factories import make_catalog

    from vpnbot.db.enums import ProductType
    from vpnbot.services import import_service

    _, combo, combo_plan = await make_catalog(session)
    combo.product_type = ProductType.COMBO
    await session.flush()
    preview = await import_service.build_preview(
        session, product=combo, raw="vless://only-config", mode=import_service.ImportMode.ALL_OR_NOTHING,
        secret="s",
    )
    assert preview.valid == 0  # COMBO requires the pair now

    _, subs, subs_plan = await make_catalog(session)
    subs.product_type = ProductType.SUBSCRIPTION
    await session.flush()
    preview = await import_service.build_preview(
        session, product=subs, raw="https://example.com/sub/abc", mode=import_service.ImportMode.ALL_OR_NOTHING,
        secret="s",
    )
    assert preview.valid == 1  # a bare subscription link is a record

    records = import_service.parse_records("vless://cfg|badlink-not-a-url")
    assert records[0].error  # pair-shaped line with a broken link errors explicitly
    records = import_service.parse_records("vless://cfg#DE | VIP", allow_config_only=True)
    assert records[0].error is None  # pipe inside the fragment is a server name


async def test_shop_list_hides_sales_disabled_and_category_off(session) -> None:
    from tests.factories import make_catalog

    from vpnbot.services import catalog as catalog_service

    category, product, _plan = await make_catalog(session)
    _products, total = await catalog_service.list_products(session, active_only=True)
    assert total == 1
    product.sales_enabled = False
    await session.flush()
    _products, total = await catalog_service.list_products(session, active_only=True)
    assert total == 0
    product.sales_enabled = True
    category.is_active = False
    await session.flush()
    _products, total = await catalog_service.list_products(session, active_only=True)
    assert total == 0


async def test_enum_check_constraint_exists(session) -> None:
    # The migration adds real CHECK constraints: an out-of-enum value
    # is rejected by the database itself now.
    from sqlalchemy import text
    from sqlalchemy.exc import DBAPIError
    from tests.factories import make_user

    user = await make_user(session, telegram_id=900001531)
    try:
        await session.execute(
            text("UPDATE users SET status='BOGUS' WHERE id=:i"), {"i": user.id}
        )
        await session.flush()
    except DBAPIError:
        await session.rollback()
    else:
        raise AssertionError("out-of-enum status must violate the CHECK constraint")


# --- Round 16: block/unblock deadlock, resume checks, subscription -----


async def test_block_user_cancels_orders_and_unblocks_cap(session) -> None:
    from tests.factories import make_catalog, make_user

    from vpnbot.db.enums import OrderStatus, PaymentMethod
    from vpnbot.services import orders as order_service
    from vpnbot.services import payments as payment_service
    from vpnbot.services import user_admin
    from vpnbot.services.settings import CARD_SETTINGS, SettingsService

    user = await make_user(session, telegram_id=900001601)
    await SettingsService(session).set(
        CARD_SETTINGS,
        {"enabled": True, "recipient_name": "تست", "card_number": "6037", "bank_name": "تست"},
    )
    _, product, plan = await make_catalog(session)
    await _stock(session, product, plan, 5, "blockx")
    orders = []
    for _ in range(3):
        order = await order_service.create_order(
            session, user_id=user.id, plan_id=plan.id, product=product, plan=plan
        )
        await payment_service.start_payment(session, order=order, method=PaymentMethod.CARD)
        orders.append(order)

    result = await user_admin.block_user(session, user_id=user.id, admin_id=None)
    assert result["orders_cancelled"] == 3
    for order in orders:
        await session.refresh(order)
        assert order.status is OrderStatus.CANCELLED

    await user_admin.unblock_user(session, user_id=user.id, admin_id=None)
    # The cap is free again: a fresh order can be created right away.
    fresh = await order_service.create_order(
        session, user_id=user.id, plan_id=plan.id, product=product, plan=plan
    )
    assert fresh.status is OrderStatus.PENDING_PAYMENT


async def test_expire_accepts_cancelled_payment_orders(session) -> None:
    from datetime import timedelta

    from tests.factories import make_catalog, make_user

    from vpnbot.db.base import utcnow
    from vpnbot.db.enums import OrderStatus, PaymentMethod, PaymentStatus
    from vpnbot.services import orders as order_service
    from vpnbot.services import payments as payment_service
    from vpnbot.services.settings import CARD_SETTINGS, SHOP_SETTINGS, SettingsService

    user = await make_user(session, telegram_id=900001611)
    settings = SettingsService(session)
    await settings.set(
        CARD_SETTINGS,
        {"enabled": True, "recipient_name": "تست", "card_number": "6037", "bank_name": "تست"},
    )
    await settings.set(SHOP_SETTINGS, {"order_ttl_hours": 1})
    _, product, plan = await make_catalog(session)
    await _stock(session, product, plan, 1, "stuck")
    order = await order_service.create_order(
        session, user_id=user.id, plan_id=plan.id, product=product, plan=plan
    )
    payment = await payment_service.start_payment(session, order=order, method=PaymentMethod.CARD)
    # Simulate the legacy stuck state: payment cancelled (old block
    # behaviour), order left PENDING_PAYMENT, aged past the TTL.
    payment.status = PaymentStatus.CANCELLED
    order.created_at = utcnow() - timedelta(hours=3)
    await session.flush()

    assert await order_service.expire_stale_orders(session) == 1
    await session.refresh(order)
    assert order.status is OrderStatus.CANCELLED


async def test_subscription_csv_sub_only_rows(session) -> None:
    from vpnbot.services import import_service

    records = import_service.parse_records(
        "sub_url\nhttps://example.com/sub/one\nhttps://example.com/sub/two",
        subscription_links=True,
    )
    assert len(records) == 2
    assert all(r.error is None and r.config == r.sub_url for r in records)


async def test_send_config_pair_rendering_contract() -> None:
    """Both delivery branches must send HTML with previews disabled.

    Regression (round 16): the subscription collapse branch sent a
    bare send_message — raw <b>/<code> tags for the customer, and
    Telegram's preview crawler fetching the subscription URL (which
    registers as a device on device-limited panels). The worker's
    Bot also carried no DefaultBotProperties to catch it.
    """
    from vpnbot.bot.sender import send_config_pair

    calls: list[dict] = []

    class FakeBot:
        async def send_message(self, chat_id=None, text=None, **kwargs):
            calls.append({"chat_id": chat_id, "text": text, **kwargs})

    bot = FakeBot()
    await send_config_pair(bot, 42, "https://example.com/sub/xyz", "https://example.com/sub/xyz", "اشتراک", "R1")
    assert len(calls) == 1
    assert calls[0]["parse_mode"] == "HTML"
    assert calls[0]["disable_web_page_preview"] is True
    assert calls[0]["text"].count("https://example.com/sub/xyz") == 1  # sent once

    calls.clear()
    await send_config_pair(bot, 42, "vless://cfg", "https://example.com/sub/xyz", "کانفیگ", "R2")
    assert calls[0]["parse_mode"] == "HTML"
    assert calls[0]["disable_web_page_preview"] is True


async def test_topup_submit_after_expiry_refused(session) -> None:
    from datetime import timedelta

    from tests.factories import make_user

    from vpnbot.db.base import utcnow
    from vpnbot.services import topups as topup_service
    from vpnbot.services.settings import CARD_SETTINGS, SettingsService

    user = await make_user(session, telegram_id=900001701)
    await SettingsService(session).set(
        CARD_SETTINGS,
        {"enabled": True, "recipient_name": "تست", "card_number": "6037", "bank_name": "تست"},
    )
    from vpnbot.db.enums import PaymentMethod

    topup = await topup_service.create_topup(
        session, user_id=user.id, amount_irt=50_000, method=PaymentMethod.CARD
    )
    topup.expires_at = utcnow() - timedelta(minutes=1)  # sweeper hasn't run yet
    await session.flush()
    try:
        await topup_service.submit_card_receipt(
            session, topup_id=topup.id, user_id=user.id, file_id="f", file_type="PHOTO"
        )
    except topup_service.TopUpError:
        pass
    else:
        raise AssertionError("receipt onto an expired top-up must be refused")


# --- Round 18: amount DoS, submit lock order, start guards, id ranges ---


def test_parse_amount_scientific_bomb_rejected_fast() -> None:
    import time

    from vpnbot.money import MoneyError, parse_amount_irt

    for bad in ("1e99999999", "1e999999", "Infinity", "sNaN", "NaN", "-Infinity"):
        start = time.monotonic()
        try:
            parse_amount_irt(bad)
        except MoneyError:
            pass
        else:
            raise AssertionError(f"{bad} must be refused")
        assert time.monotonic() - start < 1.0  # the old code froze ~20s
    assert parse_amount_irt("1e5") == 100_000  # legitimate forms still parse
    assert parse_amount_irt("۱۰۰٬۰۰۰") == 100_000


async def test_start_payment_refuses_dead_states(session) -> None:
    from tests.factories import make_catalog, make_user

    from vpnbot.db.enums import PaymentMethod, PaymentStatus
    from vpnbot.services import orders as order_service
    from vpnbot.services import payments as payment_service
    from vpnbot.services.settings import CARD_SETTINGS, SettingsService

    user = await make_user(session, telegram_id=900001801)
    await SettingsService(session).set(
        CARD_SETTINGS,
        {"enabled": True, "recipient_name": "تست", "card_number": "6037", "bank_name": "تست"},
    )
    _, product, plan = await make_catalog(session)
    await _stock(session, product, plan, 1, "dead")
    order = await order_service.create_order(
        session, user_id=user.id, plan_id=plan.id, product=product, plan=plan
    )
    payment = await payment_service.start_payment(session, order=order, method=PaymentMethod.CARD)
    await order_service.cancel_order(session, order.id, reason="x")
    await session.refresh(payment)
    assert payment.status is PaymentStatus.CANCELLED
    # A stale checkout state must not resurrect the closed payment.
    try:
        await payment_service.start_payment(session, order=order, method=PaymentMethod.CARD)
    except payment_service.PaymentError:
        pass
    else:
        raise AssertionError("start_payment on a cancelled order must refuse")


async def test_forged_huge_ids_are_not_found(session) -> None:
    from vpnbot.services import catalog as catalog_service
    from vpnbot.services import orders as order_service

    huge = 99999999999999999999
    assert await catalog_service.get_product(session, huge) is None
    assert await catalog_service.get_plan(session, huge) is None
    assert await order_service.get_order(session, huge) is None
    assert await order_service.get_order(session, -5) is None


# --- Round 19: expire TOCTOU, gate audit/order, ids, limiter, blocked ---


async def test_expire_never_overwrites_wallet_payment(session) -> None:
    """The sweep re-reads under lock: a wallet-paid order (payment
    APPROVED) is not a TTL candidate even when aged, and the wallet
    is never debited-then-vanished by an expiry overwrite."""
    from datetime import timedelta

    from tests.factories import make_catalog, make_user

    from vpnbot.db.base import utcnow
    from vpnbot.db.enums import OrderStatus
    from vpnbot.services import orders as order_service
    from vpnbot.services import payments as payment_service
    from vpnbot.services import wallet as wallet_service
    from vpnbot.services.settings import SHOP_SETTINGS, SettingsService

    user = await make_user(session, telegram_id=900001901)
    await wallet_service.credit(session, user_id=user.id, amount_irt=300_000, kind="TOPUP", key="t19")
    await SettingsService(session).set(SHOP_SETTINGS, {"order_ttl_hours": 1})
    _, product, plan = await make_catalog(session)
    await _stock(session, product, plan, 1, "toctou")
    order = await order_service.create_order(
        session, user_id=user.id, plan_id=plan.id, product=product, plan=plan
    )
    order.created_at = utcnow() - timedelta(hours=3)
    await session.flush()
    await payment_service.pay_order_with_wallet(session, order=order, user_id=user.id)
    assert await order_service.expire_stale_orders(session) == 0
    await session.refresh(order)
    assert order.status is OrderStatus.FULFILLMENT_PENDING


async def test_cancel_by_customer_refuses_under_review(session) -> None:
    from tests.factories import make_catalog, make_user

    from vpnbot.db.enums import PaymentMethod
    from vpnbot.services import orders as order_service
    from vpnbot.services import payments as payment_service
    from vpnbot.services.settings import CARD_SETTINGS, SettingsService

    user = await make_user(session, telegram_id=900001911)
    await SettingsService(session).set(
        CARD_SETTINGS,
        {"enabled": True, "recipient_name": "تست", "card_number": "6037", "bank_name": "تست"},
    )
    _, product, plan = await make_catalog(session)
    await _stock(session, product, plan, 1, "bycust")
    order = await order_service.create_order(
        session, user_id=user.id, plan_id=plan.id, product=product, plan=plan
    )
    payment = await payment_service.start_payment(session, order=order, method=PaymentMethod.CARD)
    await payment_service.submit_card_receipt(
        session, payment_id=payment.id, user_id=user.id, file_id="f", file_type="PHOTO"
    )
    try:
        await order_service.cancel_order(session, order.id, reason="x", by_customer=True)
    except order_service.OrderError:
        pass
    else:
        raise AssertionError("customer cancel must refuse a payment under review")
    # The admin path (by_customer=False) still can.
    await order_service.cancel_order(session, order.id, reason="x")


async def test_rate_limiter_real_rate() -> None:
    import time

    from vpnbot.jobs.worker import RateLimiter

    limiter = RateLimiter(5)
    for _ in range(5):
        await limiter.acquire()  # burst allowance
    start = time.monotonic()
    for _ in range(5):
        await limiter.acquire()
    elapsed = time.monotonic() - start
    assert elapsed >= 0.9, f"limiter ran hot: {elapsed:.2f}s for 5 tokens at 5/s"


async def test_gate_audit_survives_and_cancelled_skips(session, session_factory) -> None:
    from sqlalchemy import select
    from tests.factories import make_catalog, make_user

    from vpnbot.db.models.system import AuditEvent
    from vpnbot.security.crypto import CredentialCipher
    from vpnbot.services import fulfillment
    from vpnbot.services import orders as order_service
    from vpnbot.services import payments as payment_service
    from vpnbot.services import wallet as wallet_service

    cipher = CredentialCipher("MDEyMzQ1Njc4OTAxMjM0NTY3ODkwMTIzNDU2Nzg5MDE=")
    user = await make_user(session, telegram_id=900001921)
    await wallet_service.credit(session, user_id=user.id, amount_irt=300_000, kind="TOPUP", key="t19b")
    _, product, plan = await make_catalog(session)
    await _stock(session, product, plan, 1, "gate")

    sent: list = []

    async def deliver(*args):  # noqa: ANN002, ANN202
        sent.append(args)

    # Unpaid order: the gate refuses AND the audit row persists.
    order = await order_service.create_order(
        session, user_id=user.id, plan_id=plan.id, product=product, plan=plan
    )
    await session.commit()
    try:
        await fulfillment.fulfill_order(
            session_factory, order_id=order.id, cipher=cipher, deliver=deliver
        )
    except fulfillment.PaymentGateError:
        pass
    else:
        raise AssertionError("unpaid order must hit the gate")
    async with session_factory() as check:
        rows = (
            await check.execute(
                select(AuditEvent).where(AuditEvent.action == "fulfillment.blocked_unapproved_payment")
            )
        ).scalars()
        assert len(list(rows)) == 1  # committed, not rolled back with the raise

    # Paid then admin-cancelled: the job resolves SKIPPED, not an error.
    order2 = await order_service.create_order(
        session, user_id=user.id, plan_id=plan.id, product=product, plan=plan
    )
    await payment_service.pay_order_with_wallet(session, order=order2, user_id=user.id)
    await order_service.cancel_order(session, order2.id, reason="admin")
    await session.commit()
    result = await fulfillment.fulfill_order(
        session_factory, order_id=order2.id, cipher=cipher, deliver=deliver
    )
    assert result.outcome is fulfillment.FulfillOutcome.SKIPPED


# --- Round 20: wallet double-spend, preview guard, TXID canon, jobs ---


async def test_wallet_locked_read_refreshes_balance(session, session_factory) -> None:
    """Deterministic staleness check: a session that cached the
    account BEFORE another session drained it must still refuse the
    second debit (populate_existing on the FOR UPDATE read)."""
    from vpnbot.db.enums import LedgerKind
    from vpnbot.services import wallet as wallet_service

    user_id = 424_242  # wallet accounts key on the internal user id via FK in prod;
    # here use a real user row:
    from tests.factories import make_user

    user = await make_user(session, telegram_id=900002001)
    user_id = user.id
    await wallet_service.credit(
        session, user_id=user_id, amount_irt=250_000, kind=LedgerKind.TOPUP, key="ds-seed"
    )
    await session.commit()

    async with session_factory() as stale, session_factory() as drainer:
        await wallet_service.get_or_create_account(stale, user_id)  # caches 250k, no lock
        await wallet_service.debit(
            drainer, user_id=user_id, amount_irt=250_000, kind=LedgerKind.PURCHASE, key="ds-drain"
        )
        await drainer.commit()
        try:
            await wallet_service.debit(
                stale, user_id=user_id, amount_irt=250_000, kind=LedgerKind.PURCHASE, key="ds-second"
            )
        except wallet_service.WalletError:
            pass
        else:
            raise AssertionError("second debit against drained wallet must refuse (double-spend)")
        await stale.rollback()
    async with session_factory() as check:
        assert await wallet_service.balance(check, user_id) == 0


async def test_preview_cancel_spares_paid_order(session) -> None:
    from tests.factories import make_catalog, make_user

    from vpnbot.db.enums import OrderStatus
    from vpnbot.services import orders as order_service
    from vpnbot.services import payments as payment_service
    from vpnbot.services import wallet as wallet_service

    user = await make_user(session, telegram_id=900002011)
    await wallet_service.credit(session, user_id=user.id, amount_irt=900_000, kind="TOPUP", key="pv-seed")
    _, product, plan = await make_catalog(session)
    await _stock(session, product, plan, 2, "prev")
    paid = await order_service.create_order(
        session, user_id=user.id, plan_id=plan.id, product=product, plan=plan
    )
    await payment_service.pay_order_with_wallet(session, order=paid, user_id=user.id)
    # Direct guard: a customer-path cancel of the APPROVED order refuses.
    try:
        await order_service.cancel_order(session, paid.id, reason="x", by_customer=True)
    except order_service.OrderError:
        pass
    else:
        raise AssertionError("customer cancel of an approved order must refuse")
    # And the preview sweep leaves it (and its money) alone.
    fresh = await order_service.create_order(
        session, user_id=user.id, plan_id=plan.id, product=product, plan=plan
    )
    await order_service.cancel_preview_orders(session, user_id=user.id, keep_order_id=fresh.id)
    await session.refresh(paid)
    assert paid.status is OrderStatus.FULFILLMENT_PENDING
    assert await wallet_service.balance(session, user.id) == 900_000 - paid.total_irt


async def test_txid_canonical_forms_collide(session) -> None:
    from vpnbot.services.payments import normalize_txid

    assert normalize_txid("0xABCDEF0123456789") == "abcdef0123456789"
    assert normalize_txid("ABCDEF0123456789") == "abcdef0123456789"
    assert normalize_txid("abcd-ef01_23456789") == "abcdef0123456789"

    from tests.factories import make_catalog, make_user

    from vpnbot.db.enums import PaymentMethod
    from vpnbot.services import orders as order_service
    from vpnbot.services import payments as payment_service
    from vpnbot.services.settings import CRYPTO_SETTINGS, SettingsService

    user = await make_user(session, telegram_id=900002021)
    await SettingsService(session).set(
        CRYPTO_SETTINGS, {"enabled": True, "address": "TXYZ", "network": "TRC20"}
    )
    _, product, plan = await make_catalog(session)
    await _stock(session, product, plan, 2, "txid")
    hexpart = "ab" * 32
    o1 = await order_service.create_order(session, user_id=user.id, plan_id=plan.id, product=product, plan=plan)
    p1 = await payment_service.start_payment(session, order=o1, method=PaymentMethod.CRYPTO)
    await payment_service.submit_crypto_evidence(
        session, payment_id=p1.id, user_id=user.id, txid=f"0x{hexpart}"
    )
    o2 = await order_service.create_order(session, user_id=user.id, plan_id=plan.id, product=product, plan=plan)
    p2 = await payment_service.start_payment(session, order=o2, method=PaymentMethod.CRYPTO)
    try:
        await payment_service.submit_crypto_evidence(
            session, payment_id=p2.id, user_id=user.id, txid=hexpart
        )
    except payment_service.PaymentError:
        pass
    else:
        raise AssertionError("the same transfer without the 0x prefix must be refused as reused")


async def test_worker_claims_fulfilment_first(session, session_factory) -> None:
    from datetime import timedelta

    from vpnbot.db.base import utcnow
    from vpnbot.db.enums import JobKind, JobStatus
    from vpnbot.db.models.system import Job
    from vpnbot.jobs.worker import Worker

    now = utcnow()
    session.add_all(
        [
            Job(kind=JobKind.BROADCAST_RECIPIENT, payload={}, idempotency_key="prio-b",
                status=JobStatus.QUEUED, scheduled_at=now - timedelta(minutes=5)),
            Job(kind=JobKind.FULFILL_ORDER, payload={"order_id": 1}, idempotency_key="prio-f",
                status=JobStatus.QUEUED, scheduled_at=now),
        ]
    )
    await session.commit()
    worker = object.__new__(Worker)
    worker.session_factory = session_factory
    claimed = await worker._claim()
    assert claimed[0][1] is JobKind.FULFILL_ORDER


async def test_support_thread_view_fits_telegram_limit(session, monkeypatch) -> None:
    from tests.factories import make_user

    from vpnbot.bot.handlers.admin import support as support_admin
    from vpnbot.services import support as support_service

    user = await make_user(session, telegram_id=900002031)
    ticket = await support_service.create_ticket(session, user=user, text="<" * 2_500)
    from vpnbot.db.enums import TicketSender

    await support_service.add_message(
        session, ticket=ticket, text="<" * 2_500,
        sender_type=TicketSender.CUSTOMER, sender_id=user.id,
    )
    await session.commit()

    captured: dict = {}

    async def fake_reply(target, text, rows=None, **kwargs):  # noqa: ANN001, ANN202
        captured["text"] = text

    monkeypatch.setattr(support_admin.common, "reply", fake_reply)
    await support_admin._show_thread(object(), session, object(), ticket.id)
    assert len(captured["text"]) <= 4_096


# --- Round 21: sends happen outside the handler transaction ---


async def test_show_commits_before_sending(session, session_factory) -> None:
    """A row written by the 'handler' phase must be visible to a
    second session once show() runs: the send phase holds no open
    transaction (no pooled connection, no row locks)."""
    from types import SimpleNamespace

    from sqlalchemy import select
    from tests.factories import make_catalog

    from vpnbot.bot.sender import ScreenSender
    from vpnbot.db.models.users import User
    from vpnbot.services.screens import ScreenService

    _, product, _plan = await make_catalog(session)
    product.title = "نشانه‌ی فاز اول"
    await session.flush()  # written, NOT committed

    sent: list = []

    class FakeBot:
        async def send_message(self, **kwargs):  # noqa: ANN202
            sent.append(kwargs)
            return SimpleNamespace(message_id=1)

    sender = ScreenSender(FakeBot(), ScreenService(session))
    target = SimpleNamespace(chat=SimpleNamespace(id=123))
    await sender.show(target, "blocked", log_outcome=False)
    assert sent  # the screen actually went out

    async with session_factory() as other:
        row = (
            await other.execute(select(User).where(User.telegram_id == 123))
        ).scalar_one_or_none()
        assert row is None  # sanity: the probe session works
        from vpnbot.db.models.catalog import Product

        seen = (
            await other.execute(select(Product.title).where(Product.id == product.id))
        ).scalar_one()
        assert seen == "نشانه‌ی فاز اول"  # committed before the send


# --- Round 22: safe thread truncation, topup refresh, category gate ---


async def test_thread_truncation_never_splits_an_entity(session, monkeypatch) -> None:
    from tests.factories import make_user

    from vpnbot.bot.handlers.admin import support as support_admin
    from vpnbot.db.enums import TicketSender
    from vpnbot.services import support as support_service

    user = await make_user(session, telegram_id=900002201)
    ticket = await support_service.create_ticket(session, user=user, text="<" * 2_500)
    await support_service.add_message(
        session, ticket=ticket, text="<" * 2_500,
        sender_type=TicketSender.CUSTOMER, sender_id=user.id,
    )
    await session.commit()

    captured: dict = {}

    async def fake_reply(target, text, rows=None, **kwargs):  # noqa: ANN001, ANN202
        captured["text"] = text

    monkeypatch.setattr(support_admin.common, "reply", fake_reply)
    await support_admin._show_thread(object(), session, object(), ticket.id)
    text = captured["text"]
    assert len(text) <= 4_096
    # Each text is raw-truncated to 200 chars BEFORE escaping, so
    # every "<" became a complete &lt; entity — nothing half-sliced.
    assert text.count("&lt;") == 600  # subject line (200) + two messages (200 each)
    import re

    assert not re.search(r"&[a-z]{0,3}$", text)  # no dangling partial entity


async def test_product_not_on_sale_when_category_disabled(session) -> None:
    from tests.factories import make_catalog

    from vpnbot.services import catalog as catalog_service

    category, made, _plan = await make_catalog(session)
    # Handlers always see the product through a query (category is
    # joined-loaded there); fetch it the same way.
    product = await catalog_service.get_product(session, made.id)
    assert product is not None and product.is_on_sale
    category.is_active = False
    await session.flush()
    await session.refresh(product.category)
    assert not product.is_on_sale
    category.is_active = True
    await session.flush()
    await session.refresh(product.category)
    assert product.is_on_sale


# --- Round 23: receipt allowlist, stale recovery, requeue key, gate ---


def test_receipt_file_allowlist() -> None:
    from types import SimpleNamespace

    from vpnbot.bot.handlers.customer.common import receipt_file

    photo_msg = SimpleNamespace(photo=[SimpleNamespace(file_id="ph")], document=None)
    assert receipt_file(photo_msg) == ("ph", "photo")

    def doc(mime, size):  # noqa: ANN001, ANN202
        return SimpleNamespace(
            photo=None,
            document=SimpleNamespace(file_id="d", mime_type=mime, file_size=size),
        )

    assert receipt_file(doc("application/pdf", 5_000_000)) == ("d", "document")
    assert receipt_file(doc("image/png", 100)) == ("d", "document")
    assert receipt_file(doc("application/vnd.android.package-archive", 100)) is None
    assert receipt_file(doc("text/html", 100)) is None
    assert receipt_file(doc("image/png", 11 * 1024 * 1024)) is None  # over 10 MB


async def test_recover_requeues_stale_running(session, session_factory) -> None:
    from datetime import timedelta

    from vpnbot.db.base import utcnow
    from vpnbot.db.enums import JobKind, JobStatus
    from vpnbot.db.models.system import Job
    from vpnbot.jobs.worker import Worker

    job = Job(
        kind=JobKind.FULFILL_ORDER, payload={"order_id": 1}, idempotency_key="stale-1",
        status=JobStatus.RUNNING, scheduled_at=utcnow() - timedelta(minutes=30),
        started_at=utcnow() - timedelta(minutes=20),
    )
    session.add(job)
    await session.commit()
    worker = object.__new__(Worker)
    worker.session_factory = session_factory
    await worker.recover()
    await session.refresh(job)
    assert job.status is JobStatus.QUEUED


async def test_admin_requeue_uses_canonical_key(session) -> None:
    from sqlalchemy import select
    from tests.factories import make_catalog, make_user

    from vpnbot.bot.handlers.admin.orders import _requeue
    from vpnbot.db.enums import JobStatus
    from vpnbot.db.models.system import Job
    from vpnbot.services import orders as order_service

    user = await make_user(session, telegram_id=900002301)
    _, product, plan = await make_catalog(session)
    await _stock(session, product, plan, 1, "req")
    order = await order_service.create_order(
        session, user_id=user.id, plan_id=plan.id, product=product, plan=plan
    )
    assert await _requeue(session, order) is True
    assert await _requeue(session, order) is False  # already queued — no twin job
    rows = (
        await session.execute(select(Job).where(Job.kind == "FULFILL_ORDER"))
    ).scalars().all()
    assert len(rows) == 1
    assert rows[0].idempotency_key == f"fulfill:{order.id}"
    assert rows[0].status is JobStatus.QUEUED


async def test_gate_bounces_when_delivery_in_flight(session, session_factory) -> None:
    from tests.factories import make_catalog, make_user

    from vpnbot.db.enums import DeliveryStatus
    from vpnbot.security.crypto import CredentialCipher
    from vpnbot.services import fulfillment
    from vpnbot.services import orders as order_service
    from vpnbot.services import payments as payment_service
    from vpnbot.services import wallet as wallet_service

    cipher = CredentialCipher("MDEyMzQ1Njc4OTAxMjM0NTY3ODkwMTIzNDU2Nzg5MDE=")
    user = await make_user(session, telegram_id=900002311)
    await wallet_service.credit(session, user_id=user.id, amount_irt=300_000, kind="TOPUP", key="g23")
    _, product, plan = await make_catalog(session)
    await _stock(session, product, plan, 1, "gate23")
    order = await order_service.create_order(
        session, user_id=user.id, plan_id=plan.id, product=product, plan=plan
    )
    await payment_service.pay_order_with_wallet(session, order=order, user_id=user.id)
    order.delivery_status = DeliveryStatus.SENDING  # another execution is mid-delivery
    await session.commit()

    async def deliver(*args):  # noqa: ANN002, ANN202
        raise AssertionError("must not deliver while another delivery is in flight")

    result = await fulfillment.fulfill_order(
        session_factory, order_id=order.id, cipher=cipher, deliver=deliver
    )
    assert result.outcome is fulfillment.FulfillOutcome.RETRY


# --- Round 24: receipt spoof, cancel keeps approved TXID, msg limit ---


def test_receipt_file_extension_crosscheck() -> None:
    from types import SimpleNamespace

    from vpnbot.bot.handlers.customer.common import receipt_file

    def doc(mime, name, size=100):  # noqa: ANN001, ANN202
        return SimpleNamespace(
            photo=None,
            document=SimpleNamespace(file_id="d", mime_type=mime, file_name=name, file_size=size),
        )

    # A modified client can label anything application/pdf — the
    # extension must agree with the allowlist too.
    assert receipt_file(doc("application/pdf", "evil.apk")) is None
    assert receipt_file(doc("application/pdf", "page.html")) is None
    assert receipt_file(doc("application/pdf", "receipt.pdf")) == ("d", "document")
    assert receipt_file(doc("image/jpeg", "scan.JPG")) == ("d", "document")
    assert receipt_file(doc("application/pdf", None)) == ("d", "document")


async def test_cancel_keeps_approved_txid(session) -> None:
    from tests.factories import make_catalog, make_user

    from vpnbot.db.enums import PaymentMethod, PaymentStatus
    from vpnbot.services import orders as order_service
    from vpnbot.services import payments as payment_service
    from vpnbot.services.auth import bootstrap_owner
    from vpnbot.services.settings import CRYPTO_SETTINGS, SettingsService

    admin = await bootstrap_owner(session, telegram_id=900002401)
    user = await make_user(session, telegram_id=900002402)
    await SettingsService(session).set(
        CRYPTO_SETTINGS, {"enabled": True, "address": "TXYZ", "network": "TRC20"}
    )
    _, product, plan = await make_catalog(session)
    await _stock(session, product, plan, 1, "txkeep")
    order = await order_service.create_order(
        session, user_id=user.id, plan_id=plan.id, product=product, plan=plan
    )
    payment = await payment_service.start_payment(session, order=order, method=PaymentMethod.CRYPTO)
    txid = "cd" * 32
    await payment_service.submit_crypto_evidence(
        session, payment_id=payment.id, user_id=user.id, txid=txid
    )
    await payment_service.approve_payment(session, payment_id=payment.id, admin_id=admin.id)
    await order_service.cancel_order(session, order.id, reason="admin cancel after approval")
    await session.refresh(payment)
    assert payment.status is PaymentStatus.CANCELLED
    # The verified transfer stays bound: the same hash must not fund
    # a second order after this one was cancelled/refunded.
    assert payment.txid == txid


async def test_ticket_messages_limit_and_count(session) -> None:
    from tests.factories import make_user

    from vpnbot.db.enums import TicketSender
    from vpnbot.services import support as support_service

    user = await make_user(session, telegram_id=900002411)
    ticket = await support_service.create_ticket(session, user=user, text="پیام اول")
    for i in range(6):
        await support_service.add_message(
            session, ticket=ticket, text=f"پیام شماره {i}",
            sender_type=TicketSender.CUSTOMER, sender_id=user.id,
        )
    await session.commit()
    latest = await support_service.ticket_messages(session, ticket.id, limit=3)
    assert [m.text for m in latest] == ["پیام شماره 3", "پیام شماره 4", "پیام شماره 5"]
    assert await support_service.ticket_message_count(session, ticket.id) == 7
