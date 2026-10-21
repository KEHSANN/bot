"""Money handling.

Canonical internal unit: **Iranian Toman (IRT), integer**.

* 1 Toman == 10 Rial.
* Floats are never used. Inputs are parsed strictly from decimal strings
  (Persian/Arabic digits accepted) and converted explicitly.
* Display: ``1,250,000 تومان``.
"""

from __future__ import annotations

import html
from decimal import Decimal, InvalidOperation

PERSIAN_DIGITS = "۰۱۲۳۴۵۶۷۸۹"
ARABIC_DIGITS = "٠١٢٣٤٥٦٧٨٩"
_DIGIT_MAP = {ord(p): str(i) for i, p in enumerate(PERSIAN_DIGITS)}
_DIGIT_MAP.update({ord(a): str(i) for i, a in enumerate(ARABIC_DIGITS)})

RIAL_PER_TOMAN = 10

#: Hard ceiling for any user/admin-typed amount (Toman). Far above any
#: real plan price or top-up, far below the PostgreSQL BIGINT limit —
#: see parse_amount_irt.
MAX_AMOUNT_IRT = 500_000_000


class MoneyError(ValueError):
    pass


def normalize_digits(text: str) -> str:
    """Convert Persian/Arabic-Indic digits to ASCII digits."""
    return text.translate(_DIGIT_MAP)


def parse_amount_irt(raw: str) -> int:
    """Parse an administrator/customer supplied amount into integer Toman.

    Accepts ``"250000"``, ``"250,000"``, ``"۲۵۰٬۰۰۰"``, ``"250000 تومان"``
    and ``"2500000 ریال"`` (converted to 250000 Toman). Rejects fractional
    values instead of rounding them silently.
    """
    if raw is None:
        raise MoneyError("مبلغ خالی است")
    text = normalize_digits(str(raw)).strip()
    text = text.replace("٬", "").replace(",", "").replace(" ", "").replace("‌", "")
    if not text:
        raise MoneyError("مبلغ خالی است")

    is_rial = False
    for suffix in ("تومان", "تومن", "تومنی"):
        if text.endswith(suffix):
            text = text[: -len(suffix)]
            break
    else:
        if text.endswith("ریال"):
            is_rial = True
            text = text[: -len("ریال")]

    if not text:
        raise MoneyError("مبلغ نامعتبر است")
    try:
        value = Decimal(text)
    except InvalidOperation as exc:
        # The raw input goes into a bot message rendered as HTML —
        # escape it, or an amount like "5<0" breaks the reply itself.
        raise MoneyError(f"مبلغ نامعتبر است: {html.escape(raw)!r}") from exc
    if value != value.to_integral_value():
        raise MoneyError("مبلغ باید عدد صحیح باشد؛ لطفاً بدون اعشار وارد کنید")
    if value < 0:
        raise MoneyError("مبلغ نمی‌تواند منفی باشد")
    amount = int(value)
    result = rial_to_toman(amount) if is_rial else amount
    if result > MAX_AMOUNT_IRT:
        # Python ints are unbounded; PostgreSQL BIGINT is not. Without a
        # cap, a huge typed amount parsed fine here and then crashed the
        # insert with a raw DataError instead of a clean refusal.
        raise MoneyError("مبلغ واردشده بیش از سقف مجاز است")
    return result


def toman_to_rial(toman: int) -> int:
    _ensure_int(toman)
    return toman * RIAL_PER_TOMAN


def rial_to_toman(rial: int) -> int:
    _ensure_int(rial)
    if rial % RIAL_PER_TOMAN != 0:
        raise MoneyError("مبلغ ریالی قابل تبدیل دقیق به تومان نیست")
    return rial // RIAL_PER_TOMAN


def _ensure_int(value: int) -> None:
    if isinstance(value, bool) or not isinstance(value, int):
        raise MoneyError("مبلغ باید عدد صحیح باشد")


def format_irt(toman: int) -> str:
    """``1250000`` -> ``"1,250,000 تومان"``."""
    _ensure_int(toman)
    return f"{toman:,} تومان"


def format_irt_plain(toman: int) -> str:
    _ensure_int(toman)
    return f"{toman:,}"


def percent_of(amount: int, percent: int) -> int:
    """Integer percentage with floor rounding; validation kept explicit."""
    _ensure_int(amount)
    _ensure_int(percent)
    if not 0 <= percent <= 100:
        raise MoneyError("درصد باید بین ۰ تا ۱۰۰ باشد")
    return amount * percent // 100
