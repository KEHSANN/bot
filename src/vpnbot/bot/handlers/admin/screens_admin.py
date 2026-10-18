"""Screen + button editor.

Every customer-visible screen is a ``ScreenDefinition`` row with a *draft* and
an immutable history of published versions. Admins edit the draft here; the
customer bot keeps serving the last published version until someone presses
"انتشار". Nothing in this module writes to the live path directly.

Callback addressing: ``item_id`` is an index into :data:`STAGE_ORDER` (callbacks
only carry ints), ``page`` is the sub-index (message or button number).
"""

from __future__ import annotations

import logging

from aiogram import F, Router
from aiogram.fsm.context import FSMContext
from aiogram.types import CallbackQuery, InlineKeyboardButton, InlineKeyboardMarkup, Message
from sqlalchemy.ext.asyncio import AsyncSession

from vpnbot.bot.handlers.admin import common
from vpnbot.bot.keyboards.callbacks import AdminCB, MenuCB
from vpnbot.bot.states import AdminStates
from vpnbot.db.enums import ButtonStyle, Permission
from vpnbot.db.models.users import Admin
from vpnbot.services import screens as screen_service

log = logging.getLogger(__name__)
router = Router(name="admin.screens")

STAGE_ORDER: list[str] = list(screen_service.ALL_STAGES)
STYLE_LABELS = {
    ButtonStyle.DEFAULT: "پیش‌فرض",
    ButtonStyle.PRIMARY: "آبی",
    ButtonStyle.SUCCESS: "سبز",
    ButtonStyle.DANGER: "قرمز",
}
PREVIEW_NOTE = (
    "👇 این پیش‌نمایش است. دکمه‌ها در چت ادمین کار نمی‌کنند؛ "
    "برای اعمال روی ربات، «انتشار» را بزنید."
)


def _stage_at(index: int) -> str | None:
    if 0 <= index < len(STAGE_ORDER):
        return STAGE_ORDER[index]
    return None


def _title(stage: str) -> str:
    default = screen_service.DEFAULT_SCREENS.get(stage, {})
    return str(default.get("title") or stage)


def _excerpt(text: str, limit: int = 42) -> str:
    flat = " ".join(str(text or "").split())
    return flat[:limit] + ("…" if len(flat) > limit else "")


def _preview_markup(rendered: screen_service.RenderedScreen) -> InlineKeyboardMarkup | None:
    if not rendered.buttons:
        return None
    rows: dict[int, list[InlineKeyboardButton]] = {}
    for button in rendered.buttons:
        try:
            if button.get("url"):
                item = InlineKeyboardButton(text=button["text"], url=button["url"])
            else:
                # Inert on purpose: tapping a preview must not run the customer
                # action from inside the admin chat.
                item = InlineKeyboardButton(
                    text=button["text"], callback_data=MenuCB(action="noop").pack()
                )
        except Exception:  # noqa: BLE001 - a preview must never break the panel
            log.debug("skipping malformed preview button", exc_info=True)
            continue
        rows.setdefault(int(button.get("row", 0)), []).append(item)
    return InlineKeyboardMarkup(inline_keyboard=[rows[key] for key in sorted(rows)])


def _preview_text(rendered: screen_service.RenderedScreen) -> str:
    parts = []
    for message in rendered.messages:
        if message.get("type") == "TEXT":
            parts.append(str(message.get("text", "")))
        else:
            parts.append(
                f"[{message.get('type')}] "
                f"{common.escape(str(message.get('caption') or '(بدون کپشن)'))}"
            )
    return "\n\n".join(parts) or "(پیامی ندارد)"


# --------------------------------------------------------------------------
# List / stage detail
# --------------------------------------------------------------------------
async def _show_list(
    target: CallbackQuery | Message, session: AsyncSession, admin: Admin, page: int
) -> None:
    service = screen_service.ScreenService(session)
    rows: list[list[InlineKeyboardButton]] = []
    for index, stage in enumerate(STAGE_ORDER):
        snapshot = await service.snapshot(stage)
        mark = f"v{snapshot.version}" if snapshot.version else "پیش‌فرض"
        rows.append([common.btn(f"🖼 {_title(stage)} · {mark}", "screens", "stage", index)])

    rows.append([common.back()])
    await common.reply(
        target,
        "<b>🖼 صفحه‌ها و دکمه‌ها</b>\n\n"
        "تغییرات ابتدا در «پیش‌نویس» ذخیره می‌شود و با «انتشار» روی ربات اعمال می‌شود.\n"
        "نسخه‌های منتشرشده تغییرناپذیرند و همیشه قابل بازگردانی هستند.",
        rows,
    )


async def _show_stage(
    target: CallbackQuery | Message, session: AsyncSession, admin: Admin, index: int
) -> None:
    stage = _stage_at(index)
    if stage is None:
        await common.reply(target, "صفحه یافت نشد.", [[common.back("screens")]])
        return
    service = screen_service.ScreenService(session)
    definition = await service.get_or_create(stage)
    snapshot = await service.snapshot(stage)
    draft_messages = list(definition.draft_messages or [])
    draft_buttons = list(definition.draft_buttons or [])
    dirty = (
        draft_messages != list(snapshot.messages) or draft_buttons != list(snapshot.buttons)
    )

    lines = [
        f"<b>🖼 {common.escape(_title(stage))}</b>",
        "",
        f"کلید داخلی: <code>{stage}</code>",
        f"منتشرشده: <b>{f'v{snapshot.version}' if snapshot.version else 'پیش‌فرض کد'}</b>",
        f"پیش‌نویس: {len(draft_messages)} پیام · {len(draft_buttons)} دکمه",
    ]
    if definition.is_locked:
        lines.append("🔒 این صفحه قفل است.")
    elif dirty:
        lines.append("✏️ پیش‌نویس با نسخه منتشرشده تفاوت دارد — منتشر نشده.")

    rows: list[list[InlineKeyboardButton]] = [
        [
            common.btn(f"✏️ پیام‌ها ({len(draft_messages)})", "screens", "msgs", index),
            common.btn(f"🔘 دکمه‌ها ({len(draft_buttons)})", "screens", "btns", index),
        ],
        [common.btn("👁 پیش‌نمایش", "screens", "preview", index)],
    ]
    if not definition.is_locked:
        rows.append([common.btn("📤 انتشار پیش‌نویس", "screens", "publish", index)])
    rows.append(
        [
            common.btn("🕘 نسخه‌ها", "screens", "versions", index),
            common.btn("↩️ بازنشانی پیش‌نویس", "screens", "reset", index),
        ]
    )
    rows.append([common.back("screens")])
    await common.reply(target, "\n".join(lines), rows)


# --------------------------------------------------------------------------
# Messages
# --------------------------------------------------------------------------
async def _show_messages(
    target: CallbackQuery | Message, session: AsyncSession, admin: Admin, index: int
) -> None:
    stage = _stage_at(index)
    if stage is None:
        await common.reply(target, "صفحه یافت نشد.", [[common.back("screens")]])
        return
    service = screen_service.ScreenService(session)
    definition = await service.get_or_create(stage)
    messages = list(definition.draft_messages or [])

    rows = []
    for position, message in enumerate(messages):
        kind = message.get("type", "TEXT")
        body = message.get("text") if kind == "TEXT" else message.get("caption")
        enabled = message.get("enabled", True) is not False
        prefix = "" if enabled else "🚫 "
        rows.append(
            [
                common.btn(
                    f"{prefix}{position + 1}. [{kind}] {_excerpt(str(body or ''))}",
                    "screens",
                    "msg",
                    index,
                    position,
                )
            ]
        )
    rows.append([common.btn("➕ افزودن پیام", "screens", "msgnew", index)])
    rows.append([common.btn("⬅️ بازگشت", "screens", "stage", index)])
    await common.reply(
        target,
        f"<b>✏️ پیام‌های «{common.escape(_title(stage))}»</b>\n\n"
        f"ترتیب نمایش از بالا به پایین است. حداکثر {screen_service.MAX_MESSAGES} پیام.",
        rows,
    )


async def _show_message(
    target: CallbackQuery | Message, session: AsyncSession, admin: Admin, index: int, position: int
) -> None:
    stage = _stage_at(index)
    if stage is None:
        await common.reply(target, "صفحه یافت نشد.", [[common.back("screens")]])
        return
    service = screen_service.ScreenService(session)
    definition = await service.get_or_create(stage)
    messages = list(definition.draft_messages or [])
    if not 0 <= position < len(messages):
        await common.reply(
            target, "پیام یافت نشد.", [[common.btn("⬅️ پیام‌ها", "screens", "msgs", index)]]
        )
        return
    message = messages[position]
    kind = message.get("type", "TEXT")
    body = message.get("text") if kind == "TEXT" else message.get("caption")
    enabled = message.get("enabled", True) is not False
    delay = int(message.get("delay_ms") or 0)
    variables = ", ".join(f"{{{v}}}" for v in sorted(screen_service.find_variables(str(body or ""))))

    lines = [
        f"<b>✏️ پیام {position + 1} از {len(messages)}</b>",
        "",
        f"نوع: {kind}",
        f"وضعیت: {'فعال' if enabled else 'غیرفعال'}",
        f"تأخیر: {common.money(delay)} میلی‌ثانیه",
        f"متغیرها: {variables or '—'}",
        "",
        "<b>متن:</b>",
        common.escape(str(body or "")),
    ]
    rows = [
        [common.btn("✏️ ویرایش متن", "screens", "msgedit", index, position)],
        [
            common.btn(
                "🚫 غیرفعال کن" if enabled else "✅ فعال کن", "screens", "msgtoggle", index, position
            ),
            common.btn("⏱ تأخیر", "screens", "msgdelay", index, position),
        ],
    ]
    if len(messages) > 1:
        rows.append([common.btn("🗑 حذف پیام", "screens", "msgdel", index, position)])
    rows.append([common.btn("⬅️ پیام‌ها", "screens", "msgs", index)])
    await common.reply(target, "\n".join(lines), rows)


# --------------------------------------------------------------------------
# Buttons
# --------------------------------------------------------------------------
def _button_label(position: int, button: dict) -> str:
    style = str(button.get("style", "DEFAULT")).upper()
    suffix = "" if style in ("DEFAULT", "") else f" · {STYLE_LABELS.get(ButtonStyle(style), style)}"
    target = button.get("url") or button.get("action") or "?"
    return f"{position + 1}. {button.get('text', '?')} → {target}{suffix}"[:64]


async def _show_buttons(
    target: CallbackQuery | Message, session: AsyncSession, admin: Admin, index: int
) -> None:
    stage = _stage_at(index)
    if stage is None:
        await common.reply(target, "صفحه یافت نشد.", [[common.back("screens")]])
        return
    service = screen_service.ScreenService(session)
    definition = await service.get_or_create(stage)
    buttons = list(definition.draft_buttons or [])

    rows = [
        [common.btn(_button_label(pos, b), "screens", "btn", index, pos)]
        for pos, b in enumerate(buttons)
    ]
    rows.append([common.btn("➕ افزودن دکمه", "screens", "btnnew", index)])
    rows.append([common.btn("⬅️ بازگشت", "screens", "stage", index)])
    await common.reply(
        target,
        f"<b>🔘 دکمه‌های «{common.escape(_title(stage))}»</b>\n\n"
        "دکمه‌ها بر اساس «ردیف» کنار هم قرار می‌گیرند. حداکثر "
        f"{screen_service.MAX_BUTTONS} دکمه.",
        rows,
    )


async def _show_button(
    target: CallbackQuery | Message, session: AsyncSession, admin: Admin, index: int, position: int
) -> None:
    stage = _stage_at(index)
    if stage is None:
        await common.reply(target, "صفحه یافت نشد.", [[common.back("screens")]])
        return
    service = screen_service.ScreenService(session)
    definition = await service.get_or_create(stage)
    buttons = list(definition.draft_buttons or [])
    if not 0 <= position < len(buttons):
        await common.reply(
            target, "دکمه یافت نشد.", [[common.btn("⬅️ دکمه‌ها", "screens", "btns", index)]]
        )
        return
    button = buttons[position]
    style = str(button.get("style", "DEFAULT")).upper()
    lines = [
        f"<b>🔘 دکمه {position + 1}</b>",
        "",
        f"متن: {common.escape(str(button.get('text', '')))}",
        f"مقصد: <code>{common.escape(str(button.get('url') or button.get('action') or '—'))}</code>",
        f"ردیف: {button.get('row', 0)}",
        f"استایل: {STYLE_LABELS.get(ButtonStyle(style), style)}",
        f"وضعیت: {'فعال' if button.get('enabled', True) is not False else 'غیرفعال'}",
    ]
    rows = [
        [common.btn("✏️ متن", "screens", "btntext", index, position)],
        [common.btn("🔗 لینک یا اکشن", "screens", "btntarget", index, position)],
        [
            common.btn("⬅️ ردیف", "screens", "btnrow", index, position),
            common.btn("👉 ردیف", "screens", "btnrowplus", index, position),
        ],
        [
            common.btn(label, "screens", f"style:{style_key.value}", index, position)
            for style_key, label in (
                (ButtonStyle.DEFAULT, "پیش‌فرض"),
                (ButtonStyle.PRIMARY, "آبی"),
            )
        ],
        [
            common.btn(label, "screens", f"style:{style_key.value}", index, position)
            for style_key, label in (
                (ButtonStyle.SUCCESS, "سبز"),
                (ButtonStyle.DANGER, "قرمز"),
            )
        ],
        [
            common.btn(
                "🚫 غیرفعال" if button.get("enabled", True) is not False else "✅ فعال",
                "screens",
                "btntoggle",
                index,
                position,
            ),
            common.btn("🗑 حذف", "screens", "btndel", index, position),
        ],
        [common.btn("⬅️ دکمه‌ها", "screens", "btns", index)],
    ]
    await common.reply(target, "\n".join(lines), rows)


# --------------------------------------------------------------------------
# Router
# --------------------------------------------------------------------------
@router.callback_query(AdminCB.filter(F.section == "screens"))
async def screens_router(
    callback: CallbackQuery,
    callback_data: AdminCB,
    session: AsyncSession,
    admin: Admin | None,
    state: FSMContext,
) -> None:
    admin = common.guard(admin, Permission.CONTENT_MANAGE)
    action = callback_data.action
    index = callback_data.item_id
    position = callback_data.page
    stage = _stage_at(index)
    service = screen_service.ScreenService(session)

    if action in ("stage", "msgs", "btns", "preview", "publish", "versions", "reset") and stage is None:
        await common.alert(callback, "صفحه یافت نشد.")
        return

    if action == "msgs":
        await _show_messages(callback, session, admin, index)
        return
    if action == "btns":
        await _show_buttons(callback, session, admin, index)
        return
    if action == "msg":
        await _show_message(callback, session, admin, index, position)
        return
    if action == "btn":
        await _show_button(callback, session, admin, index, position)
        return

    if action == "preview":
        rendered = await service.preview(str(stage), draft=True)
        markup = _preview_markup(rendered)
        await callback.answer()
        message = callback.message
        if isinstance(message, Message):
            await message.answer(
                _preview_text(rendered) + "\n\n" + PREVIEW_NOTE,
                reply_markup=markup,
                parse_mode="HTML",
                disable_web_page_preview=True,
            )
        return

    if action == "publish":
        await common.reply(
            callback,
            f"<b>📤 انتشار «{common.escape(_title(str(stage)))}»</b>\n\n"
            "پیش‌نویس فعلی برای همه کاربران فعال می‌شود. ادامه می‌دهید؟",
            [common.yes_no("screens", "publish_yes", index)],
        )
        return

    if action == "publish_yes":
        try:
            version = await service.publish(
                str(stage), admin_id=admin.id, note="انتشار از پنل تلگرام"
            )
        except screen_service.TemplateError as exc:
            await common.alert(callback, f"⚠️ {exc}")
            return
        await common.audit(
            session,
            admin,
            "screen.published",
            entity_type="screen",
            entity_id=str(stage),
            data={"version": version.version},
        )
        await callback.answer(f"نسخه {version.version} منتشر شد")
        await _show_stage(callback, session, admin, index)
        return

    if action == "reset":
        default = screen_service.DEFAULT_SCREENS[str(stage)]
        try:
            await service.save_draft(
                str(stage),
                messages=list(default["messages"]),
                buttons=list(default["buttons"]),
            )
        except screen_service.TemplateError as exc:
            await common.alert(callback, f"⚠️ {exc}")
            return
        await common.audit(
            session, admin, "screen.draft_reset", entity_type="screen", entity_id=str(stage)
        )
        await callback.answer("پیش‌نویس بازنشانی شد")
        await _show_stage(callback, session, admin, index)
        return

    if action == "versions":
        versions = await service.versions(str(stage))
        rows = []
        for version in versions[:20]:
            rows.append(
                [
                    common.btn(
                        f"v{version.version} · {common.date(version.published_at)}",
                        "screens",
                        "rollback",
                        index,
                        version.version,
                    )
                ]
            )
        rows.append([common.btn("⬅️ بازگشت", "screens", "stage", index)])
        await common.reply(
            callback,
            f"<b>🕘 نسخه‌های «{common.escape(_title(str(stage)))}»</b>\n\n"
            "برای بازگردانی یک نسخه، روی آن بزنید. نسخه قدیمی به‌عنوان نسخه جدید منتشر می‌شود.",
            rows,
        )
        return

    if action == "rollback":
        version_no = position
        try:
            await service.rollback(str(stage), version_no, admin_id=admin.id)
        except screen_service.TemplateError as exc:
            await common.alert(callback, f"⚠️ {exc}")
            return
        await common.audit(
            session,
            admin,
            "screen.rolled_back",
            entity_type="screen",
            entity_id=str(stage),
            data={"from_version": version_no},
        )
        await callback.answer(f"نسخه {version_no} بازگردانی شد")
        await _show_stage(callback, session, admin, index)
        return

    # ---- message editing -------------------------------------------------
    if action in ("msgnew", "msgedit", "msgdelay"):
        definition = await service.get_or_create(str(stage))
        messages = list(definition.draft_messages or [])
        if action == "msgnew":
            if len(messages) >= screen_service.MAX_MESSAGES:
                await common.alert(callback, "حداکثر تعداد پیام‌ها پر شده است.")
                return
            await state.set_state(AdminStates.screen_message)
            await state.update_data(
                screen_stage=str(stage), screen_index=index, screen_field="new"
            )
            await callback.answer()
            await common.reply(
                callback,
                "<b>➕ پیام جدید</b>\n\nمتن پیام را بفرستید. "
                "متغیرهای مجاز: "
                + ", ".join(f"{{{v}}}" for v in sorted(screen_service.SAFE_VARIABLES)),
                [common.cancel_row()],
            )
            return
        if not 0 <= position < len(messages):
            await common.alert(callback, "پیام یافت نشد.")
            return
        message = messages[position]
        kind = str(message.get("type", "TEXT")).upper()
        field = "text" if kind == "TEXT" else "caption"
        if action == "msgedit":
            await state.set_state(AdminStates.screen_message)
            await state.update_data(
                screen_stage=str(stage),
                screen_index=index,
                screen_position=position,
                screen_field=field,
            )
            await callback.answer()
            await common.reply(
                callback,
                f"<b>✏️ ویرایش پیام {position + 1}</b>\n\nمتن جدید را بفرستید.\n\n"
                "متغیرهای مجاز: "
                + ", ".join(f"{{{v}}}" for v in sorted(screen_service.SAFE_VARIABLES)),
                [common.cancel_row()],
            )
            return
        # msgdelay
        await state.set_state(AdminStates.screen_message)
        await state.update_data(
            screen_stage=str(stage),
            screen_index=index,
            screen_position=position,
            screen_field="delay",
        )
        await callback.answer()
        await common.reply(
            callback,
            f"<b>⏱ تأخیر پیام {position + 1}</b>\n\n"
            "تأخیر قبل از ارسال را به میلی‌ثانیه بفرستید (۰ تا ۱۰۰۰۰).",
            [common.cancel_row()],
        )
        return

    if action == "msgtoggle":
        definition = await service.get_or_create(str(stage))
        messages = list(definition.draft_messages or [])
        if not 0 <= position < len(messages):
            await common.alert(callback, "پیام یافت نشد.")
            return
        messages[position]["enabled"] = messages[position].get("enabled", True) is False
        try:
            await service.save_draft(str(stage), messages=messages)
        except screen_service.TemplateError as exc:
            await common.alert(callback, f"⚠️ {exc}")
            return
        await callback.answer("وضعیت تغییر کرد")
        await _show_message(callback, session, admin, index, position)
        return

    if action == "msgdel":
        definition = await service.get_or_create(str(stage))
        messages = list(definition.draft_messages or [])
        if len(messages) <= 1:
            await common.alert(callback, "صفحه باید حداقل یک پیام داشته باشد.")
            return
        if not 0 <= position < len(messages):
            await common.alert(callback, "پیام یافت نشد.")
            return
        removed = messages.pop(position)
        try:
            await service.save_draft(str(stage), messages=messages)
        except screen_service.TemplateError as exc:
            await common.alert(callback, f"⚠️ {exc}")
            return
        await common.audit(
            session,
            admin,
            "screen.message_deleted",
            entity_type="screen",
            entity_id=str(stage),
            data={"index": position, "type": removed.get("type")},
        )
        await callback.answer("حذف شد")
        await _show_messages(callback, session, admin, index)
        return

    # ---- button editing --------------------------------------------------
    if action in ("btnnew", "btntext", "btntarget"):
        definition = await service.get_or_create(str(stage))
        buttons = list(definition.draft_buttons or [])
        if action == "btnnew":
            if len(buttons) >= screen_service.MAX_BUTTONS:
                await common.alert(callback, "حداکثر تعداد دکمه‌ها پر شده است.")
                return
            await state.set_state(AdminStates.screen_button)
            await state.update_data(
                screen_stage=str(stage), screen_index=index, button_field="new"
            )
            await callback.answer()
            await common.reply(
                callback,
                "<b>➕ دکمه جدید</b>\n\n"
                "در یک پیام بفرستید: <code>متن | مقصد | ردیف</code>\n\n"
                "مقصد یا یک اکشن داخلی است (مثل <code>home</code>، <code>shop</code>، "
                "<code>orders</code>، <code>wallet</code>، <code>support</code>) "
                "یا یک لینک <code>https://</code>.\n"
                "ردیف عدد کوچکی است (۰ به بالا)؛ دکمه‌های هم‌ردیف کنار هم می‌آیند.\n\n"
                "مثال: <code>🛍 محصولات | shop | 0</code>",
                [common.cancel_row()],
            )
            return
        if not 0 <= position < len(buttons):
            await common.alert(callback, "دکمه یافت نشد.")
            return
        await state.set_state(AdminStates.screen_button)
        if action == "btntext":
            await state.update_data(
                screen_stage=str(stage),
                screen_index=index,
                screen_position=position,
                button_field="text",
            )
            prompt = f"متن جدید دکمه {position + 1} را بفرستید:"
        else:
            await state.update_data(
                screen_stage=str(stage),
                screen_index=index,
                screen_position=position,
                button_field="target",
            )
            prompt = (
                f"مقصد جدید دکمه {position + 1} را بفرستید "
                "(اکشن داخلی یا لینک https):"
            )
        await callback.answer()
        await common.reply(callback, f"<b>✏️ ویرایش دکمه</b>\n\n{prompt}", [common.cancel_row()])
        return

    if action in ("btnrow", "btnrowplus"):
        definition = await service.get_or_create(str(stage))
        buttons = list(definition.draft_buttons or [])
        if not 0 <= position < len(buttons):
            await common.alert(callback, "دکمه یافت نشد.")
            return
        current = int(buttons[position].get("row", 0))
        buttons[position]["row"] = max(0, current - 1) if action == "btnrow" else min(20, current + 1)
        try:
            await service.save_draft(str(stage), buttons=buttons)
        except screen_service.TemplateError as exc:
            await common.alert(callback, f"⚠️ {exc}")
            return
        await callback.answer(f"ردیف {buttons[position]['row']}")
        await _show_button(callback, session, admin, index, position)
        return

    if action.startswith("style:"):
        definition = await service.get_or_create(str(stage))
        buttons = list(definition.draft_buttons or [])
        if not 0 <= position < len(buttons):
            await common.alert(callback, "دکمه یافت نشد.")
            return
        buttons[position]["style"] = action.split(":", 1)[1]
        try:
            await service.save_draft(str(stage), buttons=buttons)
        except screen_service.TemplateError as exc:
            await common.alert(callback, f"⚠️ {exc}")
            return
        await callback.answer("استایل تغییر کرد")
        await _show_button(callback, session, admin, index, position)
        return

    if action == "btntoggle":
        definition = await service.get_or_create(str(stage))
        buttons = list(definition.draft_buttons or [])
        if not 0 <= position < len(buttons):
            await common.alert(callback, "دکمه یافت نشد.")
            return
        buttons[position]["enabled"] = buttons[position].get("enabled", True) is False
        try:
            await service.save_draft(str(stage), buttons=buttons)
        except screen_service.TemplateError as exc:
            await common.alert(callback, f"⚠️ {exc}")
            return
        await callback.answer("وضعیت تغییر کرد")
        await _show_button(callback, session, admin, index, position)
        return

    if action == "btndel":
        definition = await service.get_or_create(str(stage))
        buttons = list(definition.draft_buttons or [])
        if not 0 <= position < len(buttons):
            await common.alert(callback, "دکمه یافت نشد.")
            return
        removed = buttons.pop(position)
        try:
            await service.save_draft(str(stage), buttons=buttons)
        except screen_service.TemplateError as exc:
            await common.alert(callback, f"⚠️ {exc}")
            return
        await common.audit(
            session,
            admin,
            "screen.button_deleted",
            entity_type="screen",
            entity_id=str(stage),
            data={"text": str(removed.get("text", ""))[:64]},
        )
        await callback.answer("حذف شد")
        await _show_buttons(callback, session, admin, index)
        return

    await _show_stage(callback, session, admin, index)


# --------------------------------------------------------------------------
# FSM inputs
# --------------------------------------------------------------------------
@router.message(AdminStates.screen_message, F.chat.type == "private")
async def message_input(
    message: Message, session: AsyncSession, admin: Admin | None, state: FSMContext
) -> None:
    admin = common.guard(admin, Permission.CONTENT_MANAGE)
    data = await state.get_data()
    stage = str(data.get("screen_stage") or "")
    index = int(data.get("screen_index") or 0)
    position = int(data.get("screen_position") or 0)
    field = str(data.get("screen_field") or "")
    raw = message.text or ""

    service = screen_service.ScreenService(session)
    if stage not in screen_service.DEFAULT_SCREENS:
        await state.clear()
        await message.answer("صفحه نامعتبر است.")
        return
    definition = await service.get_or_create(stage)
    messages = list(definition.draft_messages or [])

    try:
        if field == "delay":
            value = raw.strip()
            if not value.isdigit() or int(value) > 10_000:
                raise screen_service.TemplateError("تأخیر باید عددی بین ۰ تا ۱۰۰۰۰ باشد")
            if not 0 <= position < len(messages):
                raise screen_service.TemplateError("پیام یافت نشد")
            messages[position]["delay_ms"] = int(value)
        elif field == "new":
            screen_service.validate_text(raw, limit=screen_service.MAX_TEXT_LEN)
            messages.append(
                {"type": "TEXT", "text": raw, "delay_ms": 0, "enabled": True}
            )
        else:
            if not 0 <= position < len(messages):
                raise screen_service.TemplateError("پیام یافت نشد")
            screen_service.validate_text(
                raw,
                limit=(
                    screen_service.MAX_TEXT_LEN
                    if field == "text"
                    else screen_service.MAX_CAPTION_LEN
                ),
            )
            messages[position][field] = raw
        await service.save_draft(stage, messages=messages)
    except screen_service.TemplateError as exc:
        await message.answer(f"⚠️ {exc}\n\nدوباره بفرستید یا /cancel را بزنید.")
        return

    await common.audit(
        session,
        admin,
        "screen.draft_saved",
        entity_type="screen",
        entity_id=stage,
        data={"field": field, "index": position},
    )
    await state.clear()
    await message.answer("✅ پیش‌نویس ذخیره شد. برای اعمال، «انتشار» را بزنید.")
    if field == "new":
        await _show_messages(message, session, admin, index)
    else:
        await _show_message(message, session, admin, index, position)


@router.message(AdminStates.screen_button, F.chat.type == "private")
async def button_input(
    message: Message, session: AsyncSession, admin: Admin | None, state: FSMContext
) -> None:
    admin = common.guard(admin, Permission.CONTENT_MANAGE)
    data = await state.get_data()
    stage = str(data.get("screen_stage") or "")
    index = int(data.get("screen_index") or 0)
    position = int(data.get("screen_position") or 0)
    field = str(data.get("button_field") or "")
    raw = (message.text or "").strip()

    service = screen_service.ScreenService(session)
    if stage not in screen_service.DEFAULT_SCREENS:
        await state.clear()
        await message.answer("صفحه نامعتبر است.")
        return
    definition = await service.get_or_create(stage)
    buttons = list(definition.draft_buttons or [])

    try:
        if field == "new":
            parts = [part.strip() for part in raw.split("|")]
            if len(parts) < 2:
                raise screen_service.TemplateError(
                    "قالب درست: متن | مقصد | ردیف"
                )
            text, target = parts[0], parts[1]
            row = int(parts[2]) if len(parts) > 2 and parts[2].isdigit() else 0
            candidate: dict = {"text": text, "row": row, "style": "DEFAULT", "enabled": True}
            if target.startswith(("https://", "tg://")):
                candidate["url"] = target
            else:
                candidate["action"] = target
            screen_service.validate_button(candidate)
            buttons.append(candidate)
            position = len(buttons) - 1
        else:
            if not 0 <= position < len(buttons):
                raise screen_service.TemplateError("دکمه یافت نشد")
            button = dict(buttons[position])
            if field == "text":
                button["text"] = raw
            elif field == "target":
                button.pop("url", None)
                button.pop("action", None)
                if raw.startswith(("https://", "tg://")):
                    button["url"] = raw
                else:
                    button["action"] = raw
            screen_service.validate_button(button)
            buttons[position] = button
        await service.save_draft(stage, buttons=buttons)
    except screen_service.TemplateError as exc:
        await message.answer(f"⚠️ {exc}\n\nدوباره بفرستید یا /cancel را بزنید.")
        return
    except ValueError:
        await message.answer("⚠️ ردیف باید عدد باشد. دوباره بفرستید یا /cancel را بزنید.")
        return

    await common.audit(
        session,
        admin,
        "screen.button_saved",
        entity_type="screen",
        entity_id=stage,
        data={"field": field, "index": position},
    )
    await state.clear()
    await message.answer("✅ دکمه ذخیره شد. برای اعمال، «انتشار» را بزنید.")
    await _show_button(message, session, admin, index, position)
