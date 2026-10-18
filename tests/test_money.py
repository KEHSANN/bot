"""Money parsing and formatting — integer Toman only."""

from __future__ import annotations

import pytest

from vpnbot.money import (
    MoneyError,
    format_irt,
    parse_amount_irt,
    percent_of,
    rial_to_toman,
    toman_to_rial,
)


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("250000", 250_000),
        ("250,000", 250_000),
        ("۲۵۰٬۰۰۰", 250_000),
        ("٢٥٠٠٠٠", 250_000),
        ("250000 تومان", 250_000),
        ("۲۵۰,۰۰۰ تومان", 250_000),
        ("2500000 ریال", 250_000),
        ("2,500,000 ریال", 250_000),
        (" 12000 ", 12_000),
    ],
)
def test_parse_amount_accepts_human_input(raw: str, expected: int) -> None:
    assert parse_amount_irt(raw) == expected


@pytest.mark.parametrize(
    "raw",
    ["", "   ", "abc", "12.5", "-5", "۱٫۵"],
)
def test_parse_amount_rejects_invalid(raw: str) -> None:
    with pytest.raises(MoneyError):
        parse_amount_irt(raw)


def test_parse_amount_accepts_trailing_zeros_only_when_whole() -> None:
    # "10.0" is a whole number written with a decimal point; it is accepted.
    assert parse_amount_irt("10.0") == 10
    # "10.01" is not, and is rejected instead of being rounded.
    with pytest.raises(MoneyError):
        parse_amount_irt("10.01")


def test_rial_conversion_requires_exact_multiple() -> None:
    assert toman_to_rial(1_250) == 12_500
    assert rial_to_toman(12_500) == 1_250
    with pytest.raises(MoneyError):
        rial_to_toman(12_505)


def test_float_is_never_accepted_as_toman() -> None:
    with pytest.raises(MoneyError):
        format_irt(12.0)  # type: ignore[arg-type]
    with pytest.raises(MoneyError):
        percent_of(1_000.0, 10)  # type: ignore[arg-type]


def test_format_is_grouped_with_thousands_separator() -> None:
    assert format_irt(1_250_000) == "1,250,000 تومان"
    assert format_irt(0) == "0 تومان"


def test_percent_of_floors() -> None:
    assert percent_of(999, 10) == 99
    assert percent_of(1_000, 100) == 1_000
    with pytest.raises(MoneyError):
        percent_of(1_000, 101)
