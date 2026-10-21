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
    assert result == {"receipts_cleared": 3, "payments_cancelled": 1, "topups_cancelled": 1}
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
    await cmd_cancel(message, ctx)
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

