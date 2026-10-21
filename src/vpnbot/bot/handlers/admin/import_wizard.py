"""Bulk import wizard: choose mode and plan, upload a file, review, confirm.

Raw configurations are held only in process memory between preview and
confirmation, keyed by a random token with a short TTL. They are never written
to FSM storage (Redis) or to the database in plaintext; only the encrypted
inventory rows survive.
"""

from __future__ import annotations

import io
import time
import uuid
from collections import OrderedDict
from dataclasses import dataclass

from aiogram import F, Router
from aiogram.fsm.context import FSMContext
from aiogram.types import CallbackQuery, Message
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from vpnbot.bot.handlers.admin import common
from vpnbot.bot.keyboards.callbacks import AdminCB
from vpnbot.bot.states import AdminStates
from vpnbot.config import get_settings
from vpnbot.db.enums import Permission
from vpnbot.db.models.catalog import Plan, Product
from vpnbot.db.models.users import Admin
from vpnbot.security.crypto import CredentialCipher
from vpnbot.services import catalog as catalog_service
from vpnbot.services import import_service

router = Router(name="admin.import")

MAX_FILE_BYTES = 5 * 1024 * 1024
MAX_RECORDS = 20_000
# Raw uploaded configs sit in RAM until confirmed. The old bounds
# (32 previews x up to 5MB, 30 minutes) let repeated uploads pin
# hundreds of MB and OOM the container; an admin only ever needs
# their latest preview, briefly.
PREVIEW_TTL = 300
PREVIEW_CAPACITY = 5

MODE_LABELS = {
    import_service.ImportMode.SKIP_INVALID: "رد کردن رکوردهای نامعتبر",
    import_service.ImportMode.ALL_OR_NOTHING: "همه‌یا‌هیچ",
}


@dataclass
class _Cached:
    admin_id: int
    product_id: int
    plan_id: int | None
    preview: import_service.Preview
    created_at: float


_PREVIEWS: OrderedDict[str, _Cached] = OrderedDict()


def _cache(token: str, item: _Cached) -> None:
    # One live preview per admin: a new upload replaces the admin's
    # previous one instead of accumulating alongside it.
    for old_token in [t for t, c in _PREVIEWS.items() if c.admin_id == item.admin_id]:
        del _PREVIEWS[old_token]
    _PREVIEWS[token] = item
    _PREVIEWS.move_to_end(token)
    while len(_PREVIEWS) > PREVIEW_CAPACITY:
        _PREVIEWS.popitem(last=False)


def _take(token: str, admin: Admin) -> _Cached | None:
    item = _PREVIEWS.get(token)
    if item is None:
        return None
    if item.admin_id != admin.id:
        return None
    if time.monotonic() - item.created_at > PREVIEW_TTL:
        _PREVIEWS.pop(token, None)
        return None
    return item


def _drop(token: str) -> None:
    _PREVIEWS.pop(token, None)


def _cipher() -> CredentialCipher:
    return CredentialCipher(get_settings().inventory_encryption_key)


async def _load_product(
    session: AsyncSession, product_id: int
) -> Product | None:
    product = await catalog_service.get_product(session, product_id)
    if product is None or product.archived_at is not None:
        return None
    return product


async def _show_modes(
    target: CallbackQuery | Message, session: AsyncSession, product: Product
) -> None:
    rows = [
        [common.btn(MODE_LABELS[import_service.ImportMode.SKIP_INVALID], "import", "mode:skip", product.id)],
        [common.btn(MODE_LABELS[import_service.ImportMode.ALL_OR_NOTHING], "import", "mode:strict", product.id)],
        [common.back("catalog")],
    ]
    await common.reply(
        target,
        f"<b>📥 ورود کانفیگ — {common.escape(product.title)}</b>\n\n"
        "هر خط یک رکورد است:\n"
        "<code>config|subscription_url</code>\n\n"
        "فرمت‌های مجاز: <code>|</code>، Tab، یا CSV با سرستون "
        "<code>config,sub_url</code>. خطوط خالی و خطوطی که با <code>#</code> "
        "شروع می‌شوند نادیده گرفته می‌شوند.\n\n"
        "<b>حالت ورود را انتخاب کنید:</b>\n"
        "• <b>رد کردن نامعتبر</b> — رکوردهای سالم وارد می‌شوند و بقیه گزارش می‌شوند.\n"
        "• <b>همه‌یا‌هیچ</b> — اگر حتی یک رکورد مشکل داشته باشد، هیچ‌چیز وارد نمی‌شود.",
        rows,
    )


async def _show_plans(
    target: CallbackQuery | Message,
    session: AsyncSession,
    product: Product,
    mode: import_service.ImportMode,
) -> None:
    plans = list(
        (
            await session.execute(
                select(Plan)
                .where(Plan.product_id == product.id, Plan.archived_at.is_(None))
                .order_by(Plan.sort_order, Plan.id)
            )
        ).scalars()
    )
    rows: list[list] = []
    for plan in plans:
        rows.append([common.btn(f"{plan.title} — {common.money(plan.price_irt)} تومان", "import", f"plan:{plan.id}", product.id)])
    rows.append([common.btn("بدون پلن (مشترک بین همه پلن‌ها)", "import", "plan:0", product.id)])
    rows.append([common.back("catalog")])
    await common.reply(
        target,
        f"<b>📥 {common.escape(product.title)}</b>\n\n"
        f"حالت: <b>{MODE_LABELS[mode]}</b>\n\n"
        "این کانفیگ‌ها به کدام پلن تعلق دارند؟\n"
        "<i>اگر «بدون پلن» را انتخاب کنید، کانفیگ‌ها در استخر مشترک محصول قرار "
        "می‌گیرند و هنگام تحویل به هر پلنی از این محصول اختصاص می‌یابند.</i>",
        rows,
    )


async def _prompt_upload(target: CallbackQuery | Message, product: Product, mode: str, plan_id: int) -> None:
    await common.reply(
        target,
        f"<b>📤 ارسال فایل</b>\n\n"
        f"محصول: {common.escape(product.title)}\n"
        f"حالت: <b>{mode}</b>\n\n"
        f"فایل <code>.txt</code> یا <code>.csv</code> را بفرستید (حداکثر "
        f"{MAX_FILE_BYTES // (1024 * 1024)} مگابایت، حداکثر "
        f"{common.money(MAX_RECORDS)} رکورد).\n"
        "یا متن رکوردها را مستقیم همین‌جا بچسبانید.\n\n"
        "⚠️ محتوای فایل ذخیره نمی‌شود؛ فقط کانفیگ‌های یکتا و رمزنگاری‌شده در انبار ثبت می‌شوند.",
        [common.cancel_row()],
    )


def _summary(preview: import_service.Preview) -> str:
    lines = [
        f"<b>📋 پیش‌نمایش ورود</b> — دسته <code>{preview.batch_id}</code>",
        "",
        f"حالت: <b>{MODE_LABELS[preview.mode]}</b>",
        f"کل رکوردها: {common.money(preview.total)}",
        f"✅ قابل ورود: <b>{common.money(preview.to_import)}</b>",
        f"⛔️ نامعتبر: {common.money(preview.invalid)}",
        f"♻️ تکراری در فایل: {common.money(preview.duplicates_in_batch)}",
        f"♻️ تکراری در انبار: {common.money(preview.duplicates_in_database)}",
    ]
    if preview.errors:
        lines.append("")
        lines.append("<b>خطاها:</b>")
        for line_no, error in preview.errors[:15]:
            lines.append(f"• خط {line_no}: {common.escape(error)}")
        if len(preview.errors) > 15:
            lines.append(f"… و {common.money(len(preview.errors) - 15)} مورد دیگر")
    return "\n".join(lines)


@router.callback_query(AdminCB.filter(F.section == "import"))
async def import_router(
    callback: CallbackQuery,
    callback_data: AdminCB,
    session: AsyncSession,
    admin: Admin | None,
    state: FSMContext,
) -> None:
    admin = common.guard(admin, Permission.INVENTORY_MANAGE)
    action = callback_data.action
    product = await _load_product(session, callback_data.item_id)

    if action == "start":
        if product is None:
            await common.alert(callback, "محصول یافت نشد یا آرشیو شده است.")
            return
        await _show_modes(callback, session, product)
        return

    if action.startswith("mode:"):
        if product is None:
            await common.alert(callback, "محصول یافت نشد یا آرشیو شده است.")
            return
        mode = (
            import_service.ImportMode.SKIP_INVALID
            if action.split(":", 1)[1] == "skip"
            else import_service.ImportMode.ALL_OR_NOTHING
        )
        await state.update_data(import_mode=mode.value)
        await _show_plans(callback, session, product, mode)
        return

    if action.startswith("plan:"):
        if product is None:
            await common.alert(callback, "محصول یافت نشد یا آرشیو شده است.")
            return
        plan_id = int(action.split(":", 1)[1])
        data = await state.get_data()
        mode = import_service.ImportMode(
            str(data.get("import_mode") or import_service.ImportMode.SKIP_INVALID.value)
        )
        await state.set_state(AdminStates.import_upload)
        await state.update_data(
            import_product_id=product.id,
            import_plan_id=plan_id or 0,
            import_mode=mode.value,
            import_stage="upload",
        )
        await callback.answer()
        await _prompt_upload(callback, product, MODE_LABELS[mode], plan_id)
        return

    if action.startswith("confirm:"):
        token = action_token(callback_data)
        item = _take(token, admin)
        if item is None:
            await common.alert(
                callback, "پیش‌نمایش منقضی شده است؛ فایل را دوباره ارسال کنید."
            )
            return
        product = await _load_product(session, item.product_id)
        if product is None:
            _drop(token)
            await common.alert(callback, "محصول یافت نشد یا آرشیو شده است.")
            return
        try:
            result = await import_service.apply_import(
                session,
                cipher=_cipher(),
                secret=get_settings().fingerprint_secret,
                product=product,
                preview=item.preview,
                plan_id=item.plan_id,
                actor_admin_id=admin.id,
            )
        except import_service.ImportError as exc:
            await common.alert(callback, f"⚠️ {exc}")
            return
        _drop(token)
        await common.audit(
            session,
            admin,
            "inventory.imported",
            entity_type="product",
            entity_id=product.id,
            data={
                "batch_id": result.batch_id,
                "imported": result.imported,
                "skipped_invalid": result.skipped_invalid,
                "skipped_duplicate_batch": result.skipped_duplicate_batch,
                "skipped_duplicate_db": result.skipped_duplicate_db,
            },
        )
        await callback.answer("وارد شد")
        await common.reply(
            callback,
            "<b>✅ ورود انجام شد</b>\n\n"
            f"واردشده: <b>{common.money(result.imported)}</b>\n"
            f"ردشده (نامعتبر): {common.money(result.skipped_invalid)}\n"
            f"ردشده (تکراری در فایل): {common.money(result.skipped_duplicate_batch)}\n"
            f"ردشده (تکراری در انبار): {common.money(result.skipped_duplicate_db)}\n"
            f"دسته: <code>{result.batch_id}</code>\n\n"
            f"موجودی آماده محصول: <b>{common.money(result.available_after)}</b>",
            [
                [common.btn("📦 انبار محصول", "inventory", "product", product.id)],
                [common.btn("📥 ورود دسته دیگر", "import", "start", product.id)],
                [common.back()],
            ],
        )
        return

    if action.startswith("cancel:"):
        token = action_token(callback_data)
        if token:
            _drop(token)
        await state.clear()
        await callback.answer("لغو شد")
        await common.reply(
            callback, "ورود لغو شد. هیچ رکوردی ثبت نشد.", [[common.back("catalog")]]
        )
        return

    await common.alert(callback, "درخواست نامعتبر است.")


def action_token(callback_data: AdminCB) -> str:
    """The token travels in ``action`` as ``confirm:<token>`` / ``cancel:<token>``."""
    action = callback_data.action
    return action.split(":", 1)[1] if ":" in action else ""


@router.message(AdminStates.import_upload, F.chat.type == "private")
async def upload_input(
    message: Message, session: AsyncSession, admin: Admin | None, state: FSMContext
) -> None:
    admin = common.guard(admin, Permission.INVENTORY_MANAGE)
    data = await state.get_data()
    product_id = int(data.get("import_product_id") or 0)
    plan_id = int(data.get("import_plan_id") or 0) or None
    mode = import_service.ImportMode(
        str(data.get("import_mode") or import_service.ImportMode.SKIP_INVALID.value)
    )
    product = await _load_product(session, product_id)
    if product is None:
        await state.clear()
        await message.answer("محصول یافت نشد یا آرشیو شده است.")
        return

    raw: str | None = None
    if message.document is not None:
        document = message.document
        if document.file_size and document.file_size > MAX_FILE_BYTES:
            await message.answer(
                f"⚠️ فایل بزرگ‌تر از {MAX_FILE_BYTES // (1024 * 1024)} مگابایت است. "
                "آن را به چند بخش تقسیم کنید یا /cancel را بزنید."
            )
            return
        buffer = io.BytesIO()
        try:
            await message.bot.download(document, destination=buffer)
        except Exception:  # noqa: BLE001 - network/file errors are reported, not raised
            await message.answer("⚠️ دریافت فایل ناموفق بود. دوباره بفرستید یا /cancel.")
            return
        payload = buffer.getvalue()
        if len(payload) > MAX_FILE_BYTES:
            await message.answer("⚠️ فایل بیش از حد بزرگ است.")
            return
        for encoding in ("utf-8-sig", "utf-8", "latin-1"):
            try:
                raw = payload.decode(encoding)
                break
            except UnicodeDecodeError:
                continue
        if raw is None:
            await message.answer("⚠️ قالب فایل پشتیبانی نمی‌شود. آن را با UTF-8 ذخیره کنید.")
            return
    elif message.text:
        raw = message.text
    else:
        await message.answer("⚠️ فقط فایل یا متن قابل قبول است. دوباره بفرستید یا /cancel.")
        return

    try:
        preview = await import_service.build_preview(
            session,
            product=product,
            raw=raw,
            mode=mode,
            secret=get_settings().fingerprint_secret,
            max_records=MAX_RECORDS,
        )
    except import_service.ImportError as exc:
        await message.answer(f"⚠️ {exc}\n\nدوباره بفرستید یا /cancel را بزنید.")
        return

    token = uuid.uuid4().hex[:16]
    _cache(
        token,
        _Cached(
            admin_id=admin.id,
            product_id=product.id,
            plan_id=plan_id,
            preview=preview,
            created_at=time.monotonic(),
        ),
    )
    await state.clear()

    rows: list[list] = []
    if preview.can_confirm:
        rows.append([common.btn(f"✅ تأیید و ورود {common.money(preview.to_import)} رکورد", "import", f"confirm:{token}", 0)])
    rows.append([common.btn("✖️ لغو", "import", f"cancel:{token}", 0)])
    await message.answer(_summary(preview), reply_markup=_markup(rows))


def _markup(rows):
    from aiogram.types import InlineKeyboardMarkup

    return InlineKeyboardMarkup(inline_keyboard=[list(r) for r in rows])
