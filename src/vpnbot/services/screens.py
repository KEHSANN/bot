"""Editable customer screens: drafts, immutable versions, publish/rollback.

Rendering contract
------------------
* Handlers render the **published** version; before the first publish they fall
  back to the code defaults below, so the bot always works on a fresh install.
* Text may use only the allowlisted variables in ``SAFE_VARIABLES``. Unknown
  variables are rejected at save/publish time (never rendered as litter).
* Variable *values* are HTML-escaped on insert. Admin-authored HTML is validated
  against Telegram's supported tag/attribute subset.
* Buttons carry either a validated ``action`` (whitelisted callback namespace) or
  an ``https`` URL. Nothing else reaches Telegram.
"""

from __future__ import annotations

import copy
import html
import re
from dataclasses import dataclass, field
from datetime import datetime

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm.attributes import flag_modified

from vpnbot.db.base import utcnow
from vpnbot.db.enums import ButtonStyle, MessageType
from vpnbot.db.models.content import ScreenDefinition, ScreenVersion

# --------------------------------------------------------------------------
# Stages (stable keys — handlers reference these constants, never literals)
# --------------------------------------------------------------------------
STAGE_MAIN_MENU = "main_menu"
STAGE_SHOP = "shop_categories"
STAGE_PRODUCT_LIST = "product_list"
STAGE_PRODUCT_DETAIL = "product_detail"
STAGE_CHECKOUT = "checkout_confirm"
STAGE_PAYMENT_CARD = "payment_card"
STAGE_PAYMENT_CRYPTO = "payment_crypto"
STAGE_PAYMENT_SUBMITTED = "payment_submitted"
STAGE_MY_ORDERS = "my_orders"
STAGE_ORDER_DETAIL = "order_detail"
STAGE_DELIVERED = "delivered"
STAGE_WALLET = "wallet"
STAGE_REFERRAL = "referral"
STAGE_SUPPORT = "support_menu"
STAGE_GUIDE = "payment_guide"
STAGE_TERMS = "terms"
STAGE_NO_STOCK = "no_stock"
STAGE_BLOCKED = "blocked"

ALL_STAGES: tuple[str, ...] = (
    STAGE_MAIN_MENU,
    STAGE_SHOP,
    STAGE_PRODUCT_LIST,
    STAGE_PRODUCT_DETAIL,
    STAGE_CHECKOUT,
    STAGE_PAYMENT_CARD,
    STAGE_PAYMENT_CRYPTO,
    STAGE_PAYMENT_SUBMITTED,
    STAGE_MY_ORDERS,
    STAGE_ORDER_DETAIL,
    STAGE_DELIVERED,
    STAGE_WALLET,
    STAGE_REFERRAL,
    STAGE_SUPPORT,
    STAGE_GUIDE,
    STAGE_TERMS,
    STAGE_NO_STOCK,
    STAGE_BLOCKED,
)

#: Variables handlers may pass to ``render``. Everything else is a typo.
SAFE_VARIABLES: dict[str, str] = {
    "first_name": "نام کاربر",
    "username": "نام کاربری تلگرام",
    "user_id": "شناسه عددی تلگرام",
    "balance": "موجودی کیف پول (تومان)",
    "history": "آخرین تراکنش‌های کیف پول",
    "order_ref": "کد سفارش",
    "status": "وضعیت سفارش/پرداخت",
    "product_title": "نام محصول",
    "plan_title": "نام پلن",
    "plan_details": "مشخصات پلن (مدت/حجم/تعداد دستگاه)",
    "price": "مبلغ (تومان)",
    "date": "تاریخ",
    "count": "عدد/تعداد",
    "invited_count": "تعداد دعوت‌شدگان",
    "reward_total": "مجموع پاداش",
    "invite_link": "لینک دعوت",
    "bot_username": "نام کاربری ربات",
    "support_handle": "آیدی پشتیبانی",
    "card_number": "شماره کارت",
    "card_holder": "نام صاحب کارت",
    "crypto_address": "آدرس کیف پول",
    "crypto_network": "شبکه",
    "config": "کانفیگ (فقط پس از تأیید پرداخت)",
    "sub_url": "لینک اشتراک (فقط پس از تأیید پرداخت)",
}

_VAR_RE = re.compile(r"\{([a-zA-Z_][a-zA-Z0-9_]*)\}")

_ALLOWED_TAGS = {
    "b", "strong", "i", "em", "u", "ins", "s", "strike", "del",
    "code", "pre", "a", "blockquote", "tg-spoiler", "span",
}
_TAG_RE = re.compile(r"</?([a-zA-Z-]+)((?:\s+[^<>]*)?)>")

MAX_TEXT_LEN = 4096
MAX_CAPTION_LEN = 1024
MAX_BUTTON_TEXT_LEN = 64
MAX_BUTTONS = 40
MAX_MESSAGES = 8
CALLBACK_MAX_LEN = 64

#: Callback namespaces a button may target. Handlers own these; anything else
#: is rejected so a mis-edited button cannot invoke an arbitrary handler.
#: Only actions with a real customer handler belong here — ``plan``, ``buy``,
#: ``pay``, ``order`` and ``back`` used to be allowed but nothing handles
#: them (they also need an id the rendered button cannot carry), so an
#: admin could configure a permanently dead button that validated fine.
ALLOWED_ACTIONS: frozenset[str] = frozenset(
    {
        "home",
        "shop",
        "cats",
        "prods",
        "paycard",
        "paycrypto",
        "orders",
        "wallet",
        "referral",
        "support",
        "support_new",
        "guide",
        "terms",
        "cancel",
        "noop",
    }
)


class TemplateError(ValueError):
    """Invalid template, button or style — surfaced to the admin, never sent."""


# --------------------------------------------------------------------------
# Validation helpers
# --------------------------------------------------------------------------
def find_variables(text: str) -> set[str]:
    return set(_VAR_RE.findall(text or ""))


def validate_text(text: str, *, limit: int = MAX_TEXT_LEN, field_name: str = "متن") -> None:
    if not isinstance(text, str) or not text.strip():
        raise TemplateError(f"{field_name} نمی‌تواند خالی باشد")
    if len(text) > limit:
        raise TemplateError(f"{field_name} بیش از {limit} کاراکتر است")
    unknown = find_variables(text) - SAFE_VARIABLES.keys()
    if unknown:
        raise TemplateError(
            "متغیر(های) ناشناخته: " + ", ".join(sorted(unknown))
            + " — تنها این‌ها مجازند: " + ", ".join(sorted(SAFE_VARIABLES))
        )
    _validate_html(text, field_name)


_ANY_ATTR_RE = re.compile(r"""([a-zA-Z-]+)\s*=\s*("[^"]*"|'[^']*')""")


def _validate_html(text: str, field_name: str) -> None:
    """Validate Telegram HTML with a real tag stack.

    The previous version only counted open/close depth, so crossed tags
    (``<b><i></b></i>``) passed and then failed at send time, and it only
    *looked* at class/href attributes — any other attribute slipped
    through unchecked. Now: tags must nest properly, and the only
    attribute allowed anywhere is ``href`` on ``<a>`` with a safe scheme.
    """
    stack: list[str] = []
    # A raw < or > that is not part of a valid tag (e.g. "حجم < 50 گیگ")
    # passes the tag scan below but Telegram rejects it at send time.
    stripped = _TAG_RE.sub("", text)
    if "<" in stripped or ">" in stripped:
        raise TemplateError(
            f"کاراکتر < یا > باز در {field_name} مجاز نیست؛ از &lt; یا &gt; استفاده کنید"
        )
    for match in _TAG_RE.finditer(text):
        raw = match.group(0)
        tag = match.group(1).lower()
        if tag not in _ALLOWED_TAGS:
            raise TemplateError(f"تگ HTML مجاز نیست در {field_name}: <{tag}>")
        attrs = match.group(2) or ""
        for attr, value in _ANY_ATTR_RE.findall(attrs):
            if tag != "a" or attr.lower() != "href":
                raise TemplateError(f"ویژگی «{attr}» در {field_name} مجاز نیست")
            url = value.strip("\"'")
            if not url.startswith(("https://", "http://", "tg://")):
                raise TemplateError("لینک دکمه/متن باید https یا tg باشد")
        # Anything left in the attribute area that is not a parsed
        # name="value" pair (unquoted values, stray words) is rejected.
        if _ANY_ATTR_RE.sub("", attrs).strip().rstrip("/").strip():
            raise TemplateError(f"ویژگی نامعتبر در {field_name}: <{tag}>")
        if raw.startswith("</"):
            if not stack or stack[-1] != tag:
                raise TemplateError(f"تگ HTML نامتوازن در {field_name}: </{tag}>")
            stack.pop()
        elif raw.endswith("/>"):
            continue  # self-closing: never pushed
        else:
            stack.append(tag)
    if stack:
        raise TemplateError(f"تگ HTML بسته‌نشده در {field_name}: <{stack[-1]}>")


def validate_action(action: str) -> None:
    if not action:
        raise TemplateError("اکشن دکمه خالی است")
    if len(action) > CALLBACK_MAX_LEN:
        raise TemplateError("اکشن دکمه طولانی‌تر از حد مجاز است")
    if not re.fullmatch(r"[a-z0-9_:.\-]+", action):
        raise TemplateError("اکشن دکمه شامل کاراکتر غیرمجاز است")
    namespace = action.split(":", 1)[0]
    if namespace not in ALLOWED_ACTIONS:
        raise TemplateError(
            "اکشن ناشناخته: " + namespace + " — مجاز: " + ", ".join(sorted(ALLOWED_ACTIONS))
        )


def validate_button(button: dict) -> None:
    text = str(button.get("text", "")).strip()
    if not text:
        raise TemplateError("متن دکمه خالی است")
    if len(text) > MAX_BUTTON_TEXT_LEN:
        raise TemplateError(f"متن دکمه بیش از {MAX_BUTTON_TEXT_LEN} کاراکتر است")
    url = button.get("url")
    action = button.get("action")
    if bool(url) == bool(action):
        raise TemplateError("هر دکمه باید دقیقاً یکی از «لینک» یا «اکشن» را داشته باشد")
    if url:
        if not str(url).startswith(("https://", "tg://")):
            raise TemplateError("لینک دکمه باید https یا tg باشد")
    else:
        validate_action(str(action))
    style = button.get("style", ButtonStyle.DEFAULT.value)
    if str(style).upper() not in {s.value for s in ButtonStyle}:
        raise TemplateError("استایل دکمه نامعتبر است")
    row = button.get("row", 0)
    if not isinstance(row, int) or row < 0 or row > 20:
        raise TemplateError("شماره ردیف دکمه نامعتبر است")


def validate_message(message: dict) -> None:
    try:
        mtype = MessageType(str(message.get("type", "TEXT")).upper())
    except ValueError as exc:
        raise TemplateError("نوع پیام نامعتبر است") from exc
    if message.get("enabled", True) is False:
        return
    if mtype is MessageType.TEXT:
        validate_text(str(message.get("text", "")), limit=MAX_TEXT_LEN)
    else:
        if not str(message.get("file_id", "")).strip():
            raise TemplateError("شناسه فایل برای پیام رسانه‌ای الزامی است")
        caption = str(message.get("caption", "") or "")
        if caption:
            validate_text(caption, limit=MAX_CAPTION_LEN, field_name="کپشن")
    delay = message.get("delay_ms", 0)
    if not isinstance(delay, int) or delay < 0 or delay > 10_000:
        raise TemplateError("تأخیر پیام باید بین ۰ تا ۱۰۰۰۰ میلی‌ثانیه باشد")


def validate_screen(messages: list[dict], buttons: list[dict]) -> None:
    if not messages:
        raise TemplateError("صفحه باید حداقل یک پیام داشته باشد")
    if len(messages) > MAX_MESSAGES:
        raise TemplateError(f"حداکثر {MAX_MESSAGES} پیام برای هر صفحه")
    for message in messages:
        validate_message(message)
    if len(buttons) > MAX_BUTTONS:
        raise TemplateError(f"حداکثر {MAX_BUTTONS} دکمه")
    for button in buttons:
        validate_button(button)


# --------------------------------------------------------------------------
# Rendering
# --------------------------------------------------------------------------
def _escape_value(value: object) -> str:
    return html.escape("" if value is None else str(value), quote=True)


def render_text(template: str, context: dict[str, object]) -> str:
    """Substitute allowlisted variables; values are escaped, templates trusted."""

    def repl(match: re.Match[str]) -> str:
        name = match.group(1)
        if name not in SAFE_VARIABLES:
            return ""  # validated earlier; be forgiving at render time
        return _escape_value(context.get(name, ""))

    return _VAR_RE.sub(repl, template or "")


def render_message(message: dict, context: dict[str, object]) -> dict:
    mtype = MessageType(str(message.get("type", "TEXT")).upper())
    out: dict[str, object] = {"type": mtype.value, "delay_ms": int(message.get("delay_ms", 0))}
    if mtype is MessageType.TEXT:
        out["text"] = render_text(str(message.get("text", "")), context)
    else:
        out["file_id"] = message.get("file_id")
        caption = message.get("caption")
        out["caption"] = render_text(str(caption), context) if caption else None
    return out


def normalize_style(style: str | None) -> str | None:
    """Map our DB enum to the Bot API value; DEFAULT means 'omit'."""
    if not style:
        return None
    value = str(style).upper()
    if value in ("DEFAULT", "NEUTRAL", ""):
        return None
    return value.lower()  # primary | success | danger


@dataclass(frozen=True)
class RenderedScreen:
    stage: str
    version: int | None
    messages: tuple[dict, ...]
    buttons: tuple[dict, ...] = field(default_factory=tuple)

    @property
    def text(self) -> str:
        return next((m["text"] for m in self.messages if m["type"] == "TEXT"), "")


def render_screen(
    stage: str,
    messages: list[dict],
    buttons: list[dict],
    context: dict[str, object],
    *,
    version: int | None = None,
) -> RenderedScreen:
    rendered_messages = [
        render_message(m, context) for m in messages if m.get("enabled", True) is not False
    ]
    rendered_buttons = [
        {
            "text": render_text(str(b["text"]), context),
            "row": int(b.get("row", 0)),
            "style": normalize_style(b.get("style")),
            "url": b.get("url"),
            "action": b.get("action"),
        }
        for b in buttons
        if b.get("enabled", True) is not False
    ]
    return RenderedScreen(
        stage=stage,
        version=version,
        messages=tuple(rendered_messages),
        buttons=tuple(rendered_buttons),
    )


# --------------------------------------------------------------------------
# Defaults (Persian) — used until an admin publishes a version
# --------------------------------------------------------------------------
def _btn(text: str, action: str, row: int, style: str = "DEFAULT") -> dict:
    return {"text": text, "action": action, "row": row, "style": style, "enabled": True}


DEFAULT_SCREENS: dict[str, dict] = {
    STAGE_MAIN_MENU: {
        "title": "منوی اصلی",
        "messages": [
            {
                "type": "TEXT",
                "text": (
                    "سلام <b>{first_name}</b> 👋\n\n"
                    "به فروشگاه VPN خوش آمدید.\n"
                    "از منوی زیر انتخاب کنید:"
                ),
                "delay_ms": 0,
                "enabled": True,
            }
        ],
        "buttons": [
            _btn("🛒 خرید VPN", "shop", 0, "SUCCESS"),
            _btn("📦 محصولات", "cats", 0),
            _btn("🧾 سفارش‌های من", "orders", 1),
            _btn("👛 کیف پول", "wallet", 1),
            _btn("🎁 دعوت از دوستان", "referral", 2),
            _btn("💬 پشتیبانی", "support", 2),
            _btn("📖 راهنمای پرداخت", "guide", 3),
            _btn("📜 قوانین", "terms", 3),
        ],
    },
    STAGE_SHOP: {
        "title": "دسته‌بندی محصولات",
        "messages": [{"type": "TEXT", "text": "دسته مورد نظر خود را انتخاب کنید:", "delay_ms": 0, "enabled": True}],
        "buttons": [_btn("⬅️ بازگشت", "home", 9)],
    },
    STAGE_PRODUCT_LIST: {
        "title": "لیست محصولات",
        "messages": [{"type": "TEXT", "text": "محصولی را انتخاب کنید:", "delay_ms": 0, "enabled": True}],
        "buttons": [_btn("⬅️ بازگشت", "shop", 9)],
    },
    STAGE_PRODUCT_DETAIL: {
        "title": "جزئیات محصول",
        "messages": [
            {
                "type": "TEXT",
                "text": (
                    "<b>{product_title}</b>{plan_details}\n\n"
                    "برای مشاهده پلن‌ها و قیمت‌ها، یکی از گزینه‌های زیر را انتخاب کنید."
                ),
                "delay_ms": 0,
                "enabled": True,
            }
        ],
        "buttons": [_btn("⬅️ بازگشت", "prods", 9)],
    },
    STAGE_CHECKOUT: {
        "title": "تأیید سفارش",
        "messages": [
            {
                "type": "TEXT",
                "text": (
                    "<b>تأیید سفارش</b>\n\n"
                    "محصول: {product_title}\n"
                    "پلن: {plan_title}\n"
                    "مبلغ: <b>{price} تومان</b>\n\n"
                    "روش پرداخت را انتخاب کنید. کانفیگ فقط پس از تأیید پرداخت توسط پشتیبانی ارسال می‌شود."
                ),
                "delay_ms": 0,
                "enabled": True,
            }
        ],
        "buttons": [
            _btn("💳 کارت به کارت", "paycard", 0, "PRIMARY"),
            _btn("🪙 رمزارز", "paycrypto", 0, "PRIMARY"),
            _btn("❌ انصراف", "cancel", 1, "DANGER"),
        ],
    },
    STAGE_PAYMENT_CARD: {
        "title": "پرداخت کارت به کارت",
        "messages": [
            {
                "type": "TEXT",
                "text": (
                    "<b>پرداخت کارت به کارت</b>\n\n"
                    "مبلغ <b>{price} تومان</b> را به کارت زیر واریز کنید:\n\n"
                    "شماره کارت: <code>{card_number}</code>\n"
                    "به نام: {card_holder}\n\n"
                    "پس از واریز، تصویر رسید را همین‌جا ارسال کنید.\n"
                    "کد سفارش شما: <code>{order_ref}</code>"
                ),
                "delay_ms": 0,
                "enabled": True,
            }
        ],
        "buttons": [_btn("❌ انصراف", "cancel", 9, "DANGER")],
    },
    STAGE_PAYMENT_CRYPTO: {
        "title": "پرداخت رمزارز",
        "messages": [
            {
                "type": "TEXT",
                "text": (
                    "<b>پرداخت با رمزارز</b>\n\n"
                    "معادل <b>{price} تومان</b> را به آدرس زیر واریز کنید:\n\n"
                    "آدرس: <code>{crypto_address}</code>\n"
                    "شبکه: {crypto_network}\n\n"
                    "سپس هش تراکنش (TXID) را ارسال کنید.\n"
                    "کد سفارش: <code>{order_ref}</code>"
                ),
                "delay_ms": 0,
                "enabled": True,
            }
        ],
        "buttons": [_btn("❌ انصراف", "cancel", 9, "DANGER")],
    },
    STAGE_PAYMENT_SUBMITTED: {
        "title": "در انتظار بررسی پرداخت",
        "messages": [
            {
                "type": "TEXT",
                "text": (
                    "✅ اطلاعات پرداخت شما ثبت شد.\n\n"
                    "سفارش <code>{order_ref}</code> در انتظار بررسی است. "
                    "پس از تأیید، کانفیگ در همین گفتگو برای شما ارسال می‌شود."
                ),
                "delay_ms": 0,
                "enabled": True,
            }
        ],
        "buttons": [_btn("🧾 سفارش‌های من", "orders", 0), _btn("🏠 منوی اصلی", "home", 1)],
    },
    STAGE_MY_ORDERS: {
        "title": "سفارش‌های من",
        "messages": [{"type": "TEXT", "text": "🧾 سفارش‌های شما ({count} مورد):", "delay_ms": 0, "enabled": True}],
        "buttons": [_btn("⬅️ بازگشت", "home", 9)],
    },
    STAGE_ORDER_DETAIL: {
        "title": "جزئیات سفارش",
        "messages": [
            {
                "type": "TEXT",
                "text": (
                    "<b>سفارش <code>{order_ref}</code></b>\n\n"
                    "محصول: {product_title}\n"
                    "پلن: {plan_title}\n"
                    "مبلغ: {price} تومان\n"
                    "وضعیت: <b>{status}</b>\n"
                    "تاریخ: {date}"
                ),
                "delay_ms": 0,
                "enabled": True,
            }
        ],
        "buttons": [_btn("⬅️ سفارش‌ها", "orders", 9)],
    },
    STAGE_DELIVERED: {
        "title": "تحویل سرویس",
        "messages": [
            {
                "type": "TEXT",
                "text": (
                    "🎉 سفارش <code>{order_ref}</code> تأیید شد.\n\n"
                    "<b>کانفیگ شما:</b>\n<pre>{config}</pre>\n\n"
                    "<b>لینک اشتراک:</b>\n{sub_url}\n\n"
                    "این اطلاعات فقط برای شما ارسال شده است؛ آن را در اختیار دیگران قرار ندهید."
                ),
                "delay_ms": 0,
                "enabled": True,
            }
        ],
        "buttons": [_btn("🧾 سفارش‌های من", "orders", 0)],
    },
    STAGE_WALLET: {
        "title": "کیف پول",
        "messages": [
            {
                "type": "TEXT",
                "text": "👛 موجودی کیف پول شما: <b>{balance} تومان</b>{history}",
                "delay_ms": 0,
                "enabled": True,
            }
        ],
        "buttons": [_btn("⬅️ بازگشت", "home", 9)],
    },
    STAGE_REFERRAL: {
        "title": "دعوت از دوستان",
        "messages": [
            {
                "type": "TEXT",
                "text": (
                    "🎁 <b>دعوت از دوستان</b>\n\n"
                    "دوستانتان را با لینک زیر دعوت کنید؛ به‌محض اینکه فرد دعوت‌شده "
                    "اولین خریدش را انجام دهد، پاداش دعوت به کیف پول شما واریز می‌شود.\n\n"
                    "تعداد دعوت‌شدگان: {invited_count}\n"
                    "مجموع پاداش: {reward_total} تومان\n\n"
                    "لینک اختصاصی شما:\n{invite_link}"
                ),
                "delay_ms": 0,
                "enabled": True,
            }
        ],
        "buttons": [_btn("⬅️ بازگشت", "home", 9)],
    },
    STAGE_SUPPORT: {
        "title": "پشتیبانی",
        "messages": [
            {
                "type": "TEXT",
                "text": "💬 برای ارتباط با پشتیبانی روی دکمه زیر بزنید:",
                "delay_ms": 0,
                "enabled": True,
            }
        ],
        "buttons": [_btn("✉️ ارسال پیام", "support_new", 0, "PRIMARY"), _btn("⬅️ بازگشت", "home", 9)],
    },
    STAGE_GUIDE: {
        "title": "راهنمای پرداخت",
        "messages": [
            {
                "type": "TEXT",
                "text": (
                    "<b>راهنمای پرداخت</b>\n\n"
                    "۱) محصول و پلن مورد نظر را انتخاب کنید.\n"
                    "۲) روش پرداخت (کارت به کارت یا رمزارز) را انتخاب کنید.\n"
                    "۳) مبلغ را واریز و رسید یا هش تراکنش را ارسال کنید.\n"
                    "۴) پس از تأیید پشتیبانی، کانفیگ برای شما ارسال می‌شود.\n\n"
                    "⏳ بررسی پرداخت معمولاً کمتر از ۳۰ دقیقه طول می‌کشد."
                ),
                "delay_ms": 0,
                "enabled": True,
            }
        ],
        "buttons": [_btn("⬅️ بازگشت", "home", 9)],
    },
    STAGE_TERMS: {
        "title": "قوانین",
        "messages": [
            {
                "type": "TEXT",
                "text": (
                    "<b>قوانین استفاده</b>\n\n"
                    "• هر کانفیگ تک‌کاربره است و فقط برای خریدار مجاز است.\n"
                    "• بازفروش یا اشتراک‌گذاری کانفیگ موجب قطع سرویس می‌شود.\n"
                    "• پس از تحویل، کانفیگ قابل بازگشت نیست.\n"
                    "• استفاده از سرویس در فعالیت‌های غیرقانونی ممنوع است."
                ),
                "delay_ms": 0,
                "enabled": True,
            }
        ],
        "buttons": [_btn("⬅️ بازگشت", "home", 9)],
    },
    STAGE_NO_STOCK: {
        "title": "اتمام موجودی",
        "messages": [
            {
                "type": "TEXT",
                "text": "😔 متأسفانه موجودی این محصول تمام شده است. لطفاً بعداً مراجعه کنید.",
                "delay_ms": 0,
                "enabled": True,
            }
        ],
        "buttons": [_btn("⬅️ بازگشت", "shop", 9)],
    },
    STAGE_BLOCKED: {
        "title": "دسترسی مسدود",
        "messages": [
            {"type": "TEXT", "text": "⛔️ دسترسی شما به این بخش مسدود است.", "delay_ms": 0, "enabled": True}
        ],
        "buttons": [_btn("🏠 منوی اصلی", "home", 9)],
    },
}


# --------------------------------------------------------------------------
# Service
# --------------------------------------------------------------------------
@dataclass(frozen=True)
class ScreenSnapshot:
    stage: str
    title: str
    messages: tuple[dict, ...]
    buttons: tuple[dict, ...]
    version: int | None
    is_custom: bool


class ScreenService:
    def __init__(self, session: AsyncSession) -> None:
        self.session = session

    async def get_or_create(self, stage: str) -> ScreenDefinition:
        if stage not in DEFAULT_SCREENS:
            raise TemplateError(f"صفحه ناشناخته: {stage}")
        screen = (
            await self.session.execute(select(ScreenDefinition).where(ScreenDefinition.stage == stage))
        ).scalar_one_or_none()
        if screen is None:
            default = DEFAULT_SCREENS[stage]
            screen = ScreenDefinition(
                stage=stage,
                title=default["title"],
                # Deep copies: the draft must never alias the module-level
                # defaults, or an in-place edit would corrupt them for
                # every other screen/process.
                draft_messages=copy.deepcopy(default["messages"]),
                draft_buttons=copy.deepcopy(default["buttons"]),
            )
            self.session.add(screen)
            await self.session.flush()
        return screen

    async def snapshot(self, stage: str) -> ScreenSnapshot:
        """Published version if any, else code defaults."""
        screen = (
            await self.session.execute(select(ScreenDefinition).where(ScreenDefinition.stage == stage))
        ).scalar_one_or_none()
        if screen is not None and screen.published_version is not None:
            version = (
                await self.session.execute(
                    select(ScreenVersion).where(
                        ScreenVersion.screen_id == screen.id,
                        ScreenVersion.version == screen.published_version,
                    )
                )
            ).scalar_one_or_none()
            if version is not None:
                return ScreenSnapshot(
                    stage=stage,
                    title=screen.title,
                    messages=tuple(version.messages),
                    buttons=tuple(version.buttons),
                    version=version.version,
                    is_custom=True,
                )
        default = DEFAULT_SCREENS[stage]
        return ScreenSnapshot(
            stage=stage,
            title=default["title"],
            messages=tuple(default["messages"]),
            buttons=tuple(default["buttons"]),
            version=None,
            is_custom=False,
        )

    async def save_draft(
        self,
        stage: str,
        *,
        messages: list[dict] | None = None,
        buttons: list[dict] | None = None,
        title: str | None = None,
    ) -> ScreenDefinition:
        screen = await self.get_or_create(stage)
        if screen.is_locked:
            raise TemplateError("این صفحه قفل است و قابل ویرایش نیست")
        # Deep-copy before assigning. Callers routinely edit the loaded
        # draft dicts in place (``buttons[i]["style"] = ...``, toggling
        # ``enabled``, changing a row). Those dicts *are* the ORM-loaded
        # JSON value, so a shallow ``list(...)`` copy compares equal to the
        # "old" value and SQLAlchemy emits no UPDATE at all — the edit
        # showed in-session, then silently reverted on the next load
        # (admins saw button colours / toggles / message edits not stick).
        # A deep copy plus an explicit flag_modified makes the write
        # unconditional.
        new_messages = copy.deepcopy(
            list(messages if messages is not None else screen.draft_messages)
        )
        new_buttons = copy.deepcopy(
            list(buttons if buttons is not None else screen.draft_buttons)
        )
        validate_screen(new_messages, new_buttons)
        screen.draft_messages = new_messages
        screen.draft_buttons = new_buttons
        flag_modified(screen, "draft_messages")
        flag_modified(screen, "draft_buttons")
        if title:
            screen.title = title[:120]
        await self.session.flush()
        return screen

    async def publish(
        self, stage: str, *, admin_id: int, note: str | None = None
    ) -> ScreenVersion:
        screen = await self.get_or_create(stage)
        validate_screen(list(screen.draft_messages), list(screen.draft_buttons))
        last = (
            await self.session.execute(
                select(ScreenVersion.version)
                .where(ScreenVersion.screen_id == screen.id)
                .order_by(ScreenVersion.version.desc())
                .limit(1)
            )
        ).scalar_one_or_none()
        next_version = int(last or 0) + 1
        version = ScreenVersion(
            screen_id=screen.id,
            version=next_version,
            # Deep copies: a published version is immutable history and must
            # not share dict objects with the still-editable draft.
            messages=copy.deepcopy(list(screen.draft_messages)),
            buttons=copy.deepcopy(list(screen.draft_buttons)),
            published_by_admin_id=admin_id,
            published_at=utcnow(),
            note=(note or "")[:300] or None,
        )
        self.session.add(version)
        screen.published_version = next_version
        await self.session.flush()
        return version

    async def rollback(self, stage: str, version_no: int, *, admin_id: int) -> ScreenDefinition:
        """Publish an old version again as a NEW version (history stays immutable)."""
        screen = await self.get_or_create(stage)
        old = (
            await self.session.execute(
                select(ScreenVersion).where(
                    ScreenVersion.screen_id == screen.id, ScreenVersion.version == version_no
                )
            )
        ).scalar_one_or_none()
        if old is None:
            raise TemplateError("نسخه مورد نظر یافت نشد")
        screen.draft_messages = copy.deepcopy(list(old.messages))
        screen.draft_buttons = copy.deepcopy(list(old.buttons))
        flag_modified(screen, "draft_messages")
        flag_modified(screen, "draft_buttons")
        await self.session.flush()
        await self.publish(stage, admin_id=admin_id, note=f"بازگردانی به نسخه {version_no}")
        return screen

    async def versions(self, stage: str) -> list[ScreenVersion]:
        screen = await self.get_or_create(stage)
        rows = await self.session.execute(
            select(ScreenVersion)
            .where(ScreenVersion.screen_id == screen.id)
            .order_by(ScreenVersion.version.desc())
        )
        return list(rows.scalars())

    async def preview(
        self, stage: str, *, context: dict[str, object] | None = None, draft: bool = True
    ) -> RenderedScreen:
        screen = await self.get_or_create(stage)
        messages = screen.draft_messages if draft else (await self.snapshot(stage)).messages
        buttons = screen.draft_buttons if draft else (await self.snapshot(stage)).buttons
        sample = {
            "first_name": "کاربر",
            "username": "user",
            "user_id": 0,
            "balance": "0",
            "order_ref": "A1B2C3D4",
            "product_title": "نمونه محصول",
            "plan_title": "یک ماهه",
            "price": "250,000",
            "date": datetime.now().strftime("%Y-%m-%d"),
            "count": 0,
            "invited_count": 0,
            "reward_total": "0",
            "invite_link": "https://t.me/example?start=ref",
            "bot_username": "example_bot",
            "support_handle": "@support",
            "card_number": "6037-XXXX-XXXX-XXXX",
            "card_holder": "نمونه",
            "crypto_address": "TXXXX...",
            "crypto_network": "TRC20",
            "config": "vless://sample",
            "sub_url": "https://example.com/sub",
        }
        if context:
            sample.update(context)
        return render_screen(stage, list(messages), list(buttons), sample)
