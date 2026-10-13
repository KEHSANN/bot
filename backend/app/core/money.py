from typing import Final

CURRENCY: Final = "IRT"
MAX_AMOUNT: Final = 2**63 - 1
PERSIAN_DIGITS: Final = str.maketrans("0123456789", "۰۱۲۳۴۵۶۷۸۹")


def validate_amount(value: object, currency: str = CURRENCY) -> int:
    """One stored IRT unit is one toman. No floats, booleans, coercion, or negative money."""
    if currency != CURRENCY:
        raise ValueError("Unsupported currency")
    if type(value) is not int or not 0 <= value <= MAX_AMOUNT:
        raise ValueError("Amount must be a nonnegative signed-64-bit integer")
    return value


def to_rials(amount_irt: object) -> int:
    amount = validate_amount(amount_irt)
    if amount > MAX_AMOUNT // 10:
        raise ValueError("Converted rial amount exceeds signed-64-bit range")
    return amount * 10


def from_rials(amount_rial: object) -> int:
    amount = validate_amount(amount_rial)
    if amount % 10:
        raise ValueError("Rial amount is not an exact whole toman; rounding is forbidden")
    return amount // 10


def format_irt(amount: object) -> str:
    value = validate_amount(amount)
    return f"{value:,}".replace(",", "٬").translate(PERSIAN_DIGITS) + " تومان"
