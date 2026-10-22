"""Runtime settings editor: payment details, toggles, shop texts, referrals.

Every screen here is generated from :data:`CATALOG`, so adding a field is a
one-line change and can never drift out of sync with the write path. Values are
stored by :class:`~vpnbot.services.settings.SettingsService`, which merges each
write over the code defaults.

Money fields are parsed with :func:`vpnbot.money.parse_amount_irt` (integer
Toman, Persian digits accepted); counts are plain integers.
"""

from __future__ import annotations

from aiogram import F, Router
from aiogram.fsm.context import FSMContext
from aiogram.types import CallbackQuery, InlineKeyboardButton, Message
from sqlalchemy.ext.asyncio import AsyncSession

from vpnbot.bot.handlers.admin import common
from vpnbot.bot.keyboards.callbacks import AdminCB
from vpnbot.bot.states import AdminStates
from vpnbot.db.enums import Permission
from vpnbot.db.models.users import Admin
from vpnbot.money import MoneyError, parse_amount_irt
from vpnbot.services import settings as settings_service

router = Router(name="admin.settings")

S = settings_service

#: (key, title, ((field, label, kind), ...)); kind ∈ bool | text | long | money | count
CATALOG: tuple[tuple[str, str, tuple[tuple[str, str, str], ...]], ...] = (
    (
        S.CARD_SETTINGS,
        "💳 کارت به کارت",
        (
            ("enabled", "فعال بودن روش", "bool"),
            ("recipient_name", "نام صاحب حساب", "text"),
            ("card_number", "شماره کارت", "text"),
            ("bank_name", "نام بانک", "text"),
            ("instructions", "راهنما برای کاربر", "long"),
        ),
    ),
    (
        S.CRYPTO_SETTINGS,
        "₿ رمزارز",
        (
            ("enabled", "فعال بودن روش", "bool"),
            ("asset", "دارایی (مثلاً USDT)", "text"),
            ("network", "شبکه (مثلاً TRC20)", "text"),
            ("address", "آدرس کیف پول", "text"),
            ("memo", "Memo / Tag", "text"),
            ("amount_note", "یادداشت مبلغ", "text"),
            ("instructions", "راهنما برای کاربر", "long"),
        ),
    ),
    (
        S.SUPPORT_SETTINGS,
        "🎫 پشتیبانی",
        (
            ("enabled", "فعال بودن تیکت", "bool"),
            # The welcome text lives in the screen editor (مرحله پشتیبانی);
            # a settings field for it existed here but was never read.
        ),
    ),
    (
        S.SHOP_SETTINGS,
        "🛍 فروشگاه",
        (
            ("wallet_enabled", "کیف پول فعال", "bool"),
            ("referral_enabled", "دعوت از دوستان فعال", "bool"),
            ("order_ttl_hours", "مهلت تکمیل سفارش (ساعت)", "count"),
            # guide_text / terms_text removed: they were editable here
            # but displayed nowhere — the guide and terms texts live in
            # the screen editor (مراحل راهنما و قوانین).
        ),
    ),
    (
        S.REFERRAL_SETTINGS,
        "🎁 پاداش دعوت",
        (
            ("enabled", "فعال بودن پاداش", "bool"),
            ("tier1_reward_irt", "پاداش سطح یک (تومان)", "money"),
            ("tier2_enabled", "پاداش سطح دو فعال", "bool"),
            ("tier2_threshold", "آستانه سطح دو (تعداد دعوت)", "count"),
            ("tier2_reward_irt", "پاداش سطح دو (تومان)", "money"),
        ),
    ),
    (
        S.WALLET_SETTINGS,
        "👛 کیف پول و شارژ",
        (
            ("topup_enabled", "شارژ کیف پول توسط کاربر فعال", "bool"),
            ("min_topup_irt", "حداقل مبلغ شارژ (تومان)", "money"),
        ),
    ),
)

KIND_HINTS = {
    "text": "مقدار جدید را بفرستید.",
    "long": "متن جدید را بفرستید (چند خط هم مجاز است).",
    "money": "مبلغ را به تومان بفرستید (مثلاً 20000 یا ۲۰٬۰۰۰).",
    "count": "یک عدد صحیح بفرستید.",
}


def _at(index: int) -> tuple[str, str, tuple[tuple[str, str, str], ...]] | None:
    if 0 <= index < len(CATALOG):
        return CATALOG[index]
    return None


def _field(section: tuple, name: str) -> tuple[str, str, str] | None:
    for field_name, label, kind in section[2]:
        if field_name == name:
            return field_name, label, kind
    return None


def _render(value: object, kind: str) -> str:
    if kind == "bool":
        return "✅ فعال" if value else "❌ غیرفعال"
    if kind == "money":
        return f"{common.money(int(value or 0))} تومان"
    if kind == "count":
        return common.money(int(value or 0))
    text = str(value or "").strip()
    if not text:
        return "— (خالی)"
    flat = " ".join(text.split())
    return common.escape(flat[:70] + ("…" if len(flat) > 70 else ""))


async def _show_sections(target: CallbackQuery | Message) -> None:
    rows = [
        [common.btn(title, "settings", f"open:{index}")]
        for index, (_, title, _) in enumerate(CATALOG)
    ]
    rows.append([common.back()])
    await common.reply(
        target,
        "<b>⚙️ تنظیمات</b>\n\n"
        "هر بخش را باز کنید تا مقادیر را ببینید یا تغییر دهید. "
        "تغییرات بلافاصله روی ربات اعمال می‌شوند.",
        rows,
    )


async def _show_section(
    target: CallbackQuery | Message, session: AsyncSession, index: int
) -> None:
    entry = _at(index)
    if entry is None:
        await common.reply(target, "بخش یافت نشد.", [[common.back("settings")]])
        return
    key, title, fields = entry
    service = settings_service.SettingsService(session)
    values = await service.get(key)

    lines = [f"<b>{title}</b>", ""]
    rows: list[list[InlineKeyboardButton]] = []
    for name, label, kind in fields:
        value = values.get(name)
        lines.append(f"{label}: {_render(value, kind)}")
        if kind == "bool":
            verb = "غیرفعال کن" if value else "فعال کن"
            rows.append([common.btn(f"{verb} — {label}", "settings", f"toggle:{index}:{name}")])
        else:
            rows.append([common.btn(f"✏️ {label}", "settings", f"edit:{index}:{name}")])
    rows.append([common.btn("⬅️ بخش‌ها", "settings")])
    await common.reply(target, "\n".join(lines), rows)


def _prompt(index: int, label: str, kind: str) -> str:
    return f"<b>✏️ {common.escape(label)}</b>\n\n{KIND_HINTS.get(kind, 'مقدار جدید را بفرستید.')}"


@router.callback_query(AdminCB.filter(F.section == "settings"))
async def settings_router(
    callback: CallbackQuery,
    callback_data: AdminCB,
    session: AsyncSession,
    admin: Admin | None,
    state: FSMContext,
) -> None:
    admin = common.guard(admin, Permission.SETTINGS_MANAGE)
    action = callback_data.action
    await state.clear()  # section navigation abandons other wizards

    if action.startswith("open:"):
        await _show_section(callback, session, int(action.split(":", 1)[1]))
        return

    if action.startswith("toggle:"):
        _, raw_index, name = action.split(":", 2)
        index = int(raw_index)
        entry = _at(index)
        field = _field(entry, name) if entry else None
        if entry is None or field is None or field[2] != "bool":
            await common.alert(callback, "تنظیم یافت نشد.")
            return
        key = entry[0]
        service = settings_service.SettingsService(session)
        current = await service.get(key)
        new_value = not bool(current.get(name))
        await service.update_fields(key, **{name: new_value})
        await common.audit(
            session,
            admin,
            "settings.changed",
            entity_type="setting",
            entity_id=key,
            data={"field": name, "value": new_value},
        )
        await callback.answer("ذخیره شد")
        await _show_section(callback, session, index)
        return

    if action.startswith("edit:"):
        _, raw_index, name = action.split(":", 2)
        index = int(raw_index)
        entry = _at(index)
        field = _field(entry, name) if entry else None
        if entry is None or field is None or field[2] == "bool":
            await common.alert(callback, "تنظیم یافت نشد.")
            return
        await state.set_state(AdminStates.settings_value)
        await state.update_data(
            settings_index=index, settings_key=entry[0], settings_field=name, settings_kind=field[2]
        )
        await callback.answer()
        await common.reply(callback, _prompt(index, field[1], field[2]), [common.cancel_row()])
        return

    await _show_sections(callback)


@router.message(AdminStates.settings_value, F.chat.type == "private")
async def value_input(
    message: Message, session: AsyncSession, admin: Admin | None, state: FSMContext
) -> None:
    admin = common.guard(admin, Permission.SETTINGS_MANAGE)
    data = await state.get_data()
    key = str(data.get("settings_key") or "")
    name = str(data.get("settings_field") or "")
    kind = str(data.get("settings_kind") or "text")
    index = int(data.get("settings_index") or 0)
    raw = (message.text or "").strip()

    if not key or not name:
        await state.clear()
        await message.answer("درخواست نامعتبر است.")
        return

    value: object
    try:
        if kind == "money":
            value = parse_amount_irt(raw)
        elif kind == "count":
            if not raw.isdigit() or int(raw) < 0 or int(raw) > 100_000:
                # The cap matters: an unbounded count (e.g.
                # order_ttl_hours) later feeds timedelta()/range
                # arithmetic that overflows and kills worker loops.
                raise ValueError("عدد نامعتبر است (۰ تا ۱۰۰٬۰۰۰)")
            value = int(raw)
        else:
            if not raw:
                raise ValueError("مقدار خالی است")
            value = raw[:1000]
    except (MoneyError, ValueError) as exc:
        await message.answer(f"⚠️ {exc}\n\nدوباره بفرستید یا /cancel را بزنید.")
        return

    service = settings_service.SettingsService(session)
    await service.update_fields(key, **{name: value})
    await common.audit(
        session,
        admin,
        "settings.changed",
        entity_type="setting",
        entity_id=key,
        data={"field": name, "value": value if kind in ("money", "count") else "[text]"},
    )
    await state.clear()
    await message.answer("✅ ذخیره شد.")
    await _show_section(message, session, index)
