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

