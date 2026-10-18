"""Jalali (Solar Hijri) date formatting for the Persian UI.

Telegram customers read dates in the Iranian calendar; storing and computing in
UTC/Gregorian and converting only at the edge keeps the data unambiguous. The
conversion is the standard arithmetic algorithm (no external dependency).
"""

from __future__ import annotations

from datetime import UTC, datetime

_PERSIAN_DIGITS = "۰۱۲۳۴۵۶۷۸۹"
_G_DAYS_IN_MONTH = (0, 31, 59, 90, 120, 151, 181, 212, 243, 273, 304, 334)

#: Iran has observed no DST since 2022; the offset is fixed at +03:30.
IRAN_OFFSET_SECONDS = 3 * 3600 + 30 * 60


def fa_digits(value: int | str) -> str:
    return str(value).translate(str.maketrans("0123456789", _PERSIAN_DIGITS))


def to_jalali(dt: datetime) -> tuple[int, int, int]:
    """Convert a Gregorian datetime (naive = UTC) to ``(year, month, day)``."""
    if dt.tzinfo is not None:
        dt = dt.astimezone(UTC).replace(tzinfo=None)
    gy, gm, gd = dt.year, dt.month, dt.day

    gy2 = gy + 1 if gm > 2 else gy
    days = (
        355_666
        + (365 * gy)
        + ((gy2 + 3) // 4)
        - ((gy2 + 99) // 100)
        + ((gy2 + 399) // 400)
        + gd
        + _G_DAYS_IN_MONTH[gm - 1]
    )

    jy = -1595 + (33 * (days // 12_053))
    days %= 12_053
    jy += 4 * (days // 1461)
    days %= 1461
    if days > 365:
        jy += (days - 1) // 365
        days = (days - 1) % 365
    if days < 186:
        jm, jd = 1 + (days // 31), 1 + (days % 31)
    else:
        jm, jd = 7 + ((days - 186) // 30), 1 + ((days - 186) % 30)
    return jy, jm, jd


def format_jalali(dt: datetime | None, *, with_time: bool = False) -> str:
    """``1404/07/26`` (Tehran time), optionally ``— 14:30``."""
    if dt is None:
        return "—"
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=UTC)
    local = dt.astimezone(UTC).timestamp() + IRAN_OFFSET_SECONDS
    tehran = datetime.fromtimestamp(local, tz=UTC).replace(tzinfo=None)
    jy, jm, jd = to_jalali(tehran)
    date = f"{jy:04d}/{jm:02d}/{jd:02d}"
    if with_time:
        date += f" — {tehran.hour:02d}:{tehran.minute:02d}"
    return fa_digits(date)


def tehran_day_start(dt: datetime) -> datetime:
    """UTC instant of 00:00 Tehran on the day containing ``dt``."""
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=UTC)
    local = dt.astimezone(UTC).timestamp() + IRAN_OFFSET_SECONDS
    midnight_local = local - (local % 86400)
    return datetime.fromtimestamp(midnight_local - IRAN_OFFSET_SECONDS, tz=UTC)
