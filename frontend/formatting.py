"""Indian Rupee (INR), numeric, and IST (+05:30) display formatters for the UI."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from decimal import Decimal, InvalidOperation
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

try:
    IST = ZoneInfo("Asia/Kolkata")
except ZoneInfoNotFoundError:
    IST = timezone(timedelta(hours=5, minutes=30), name="IST")


def now_ist_str() -> str:
    return datetime.now(IST).strftime("%Y-%m-%d %H:%M:%S IST")


def fmt_ist(iso_str: str | None) -> str:
    """Format an ISO-8601 API timestamp into compact IST desk format."""
    if not iso_str:
        return "—"
    try:
        dt = datetime.fromisoformat(iso_str)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=IST)
        else:
            dt = dt.astimezone(IST)
        return dt.strftime("%Y-%m-%d %H:%M:%S IST")
    except ValueError:
        return str(iso_str)


def to_decimal(value: object) -> Decimal:
    if value is None:
        return Decimal("0")
    try:
        return Decimal(str(value))
    except InvalidOperation:
        return Decimal("0")


def fmt_inr(value: object, *, signed: bool = False) -> str:
    """Format Decimal/numeric values using Indian numbering grouping (e.g. ₹5,00,000.00)."""
    dec = to_decimal(value).quantize(Decimal("0.01"))
    is_neg = dec < 0
    abs_val = abs(dec)
    whole_str, frac_str = f"{abs_val:.2f}".split(".")

    if len(whole_str) > 3:
        last3 = whole_str[-3:]
        rest = whole_str[:-3]
        groups: list[str] = []
        while len(rest) > 2:
            groups.insert(0, rest[-2:])
            rest = rest[:-2]
        if rest:
            groups.insert(0, rest)
        formatted_whole = ",".join(groups) + "," + last3
    else:
        formatted_whole = whole_str

    prefix = "-" if is_neg else ("+" if signed and dec > 0 else "")
    return f"{prefix}₹{formatted_whole}.{frac_str}"


def pnl_css_class(value: object) -> str:
    dec = to_decimal(value)
    if dec > 0:
        return "num-pos"
    if dec < 0:
        return "num-neg"
    return "num-flat"
