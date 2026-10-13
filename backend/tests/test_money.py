from decimal import Decimal

import pytest

from app.core.money import MAX_AMOUNT, format_irt, from_rials, to_rials, validate_amount


@pytest.mark.parametrize("value", [-1, MAX_AMOUNT + 1, 1.0, True, False, "100", None, Decimal("1")])
def test_invalid_amount(value: object) -> None:
    with pytest.raises(ValueError):
        validate_amount(value)


@pytest.mark.parametrize("currency", ["IRR", "USD", "irt", ""])
def test_wrong_currency(currency: str) -> None:
    with pytest.raises(ValueError):
        validate_amount(100, currency)


@pytest.mark.parametrize("amount", [0, 1, 100000, MAX_AMOUNT // 10])
def test_exact_conversion(amount: int) -> None:
    assert from_rials(to_rials(amount)) == amount


def test_conversion_boundaries() -> None:
    assert validate_amount(MAX_AMOUNT) == MAX_AMOUNT
    with pytest.raises(ValueError):
        to_rials(MAX_AMOUNT // 10 + 1)
    with pytest.raises(ValueError):
        from_rials(11)
    with pytest.raises(ValueError):
        from_rials(-1)
    with pytest.raises(ValueError):
        to_rials(True)


def test_persian_display() -> None:
    assert format_irt(100000) == "۱۰۰٬۰۰۰ تومان"
    assert format_irt(0) == "۰ تومان"
