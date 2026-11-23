"""Deterministic timestamp conversion, Decimal normalization, deduplication, and CSV serialization.

Canonical Timezone Policy:
- FYERS returns candle timestamps as Unix epoch seconds (UTC instant).
- Intraday candles (`1m..60m`) represent the exact IST bar open (e.g. `09:15:00+05:30`).
- Daily candles (`1d` / FYERS `1D`) are stamped by FYERS at `00:00:00 UTC` of the trading date,
  which deterministically maps to `05:30:00+05:30` on that exact same IST calendar date.
- Every timestamp in this application is converted via UTC -> `ZoneInfo("Asia/Kolkata")`
  with ZERO dependence on machine-local OS timezone settings.
"""

from __future__ import annotations

import csv
import hashlib
import io
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import date, datetime, time, timedelta, timezone
from decimal import Decimal, InvalidOperation
from typing import Any

from core.clock import IST, ensure_ist, now_ist, parse_hms_time
from core.exceptions import HistoricalDataError
from core.market_data.models import NormalizedCandle, get_timeframe_spec

CSV_EXPORT_COLUMNS: tuple[str, ...] = (
    "timestamp",
    "open",
    "high",
    "low",
    "close",
    "volume",
)


def epoch_seconds_to_ist(epoch_value: Any) -> datetime:
    """Deterministically convert Unix epoch seconds (or ms if >= 1e11) to timezone-aware IST (+05:30).

    Never relies on machine-local timezone (`datetime.fromtimestamp` without `tz` is forbidden).
    """
    if epoch_value is None or isinstance(epoch_value, bool):
        raise HistoricalDataError(
            f"Invalid null/boolean epoch timestamp: {epoch_value!r}.",
            code="INVALID_TIMESTAMP",
        )
    try:
        dec = Decimal(str(epoch_value).strip())
        if dec.is_nan() or dec.is_infinite():
            raise HistoricalDataError(
                f"Non-finite epoch timestamp: {epoch_value!r}.",
                code="INVALID_TIMESTAMP",
            )
    except InvalidOperation as exc:
        raise HistoricalDataError(
            f"Unparseable epoch timestamp: {epoch_value!r}.",
            code="INVALID_TIMESTAMP",
        ) from exc

    # Guard against negative or wildly out-of-range epochs (pre-1990 or post-2100)
    if dec >= Decimal("100000000000"):
        # Millisecond epoch supplied -> convert exactly to seconds
        dec = dec / Decimal("1000")

    if dec < Decimal("631152000") or dec > Decimal("4102444800"):
        raise HistoricalDataError(
            f"Epoch timestamp {epoch_value!r} outside valid market history bounds.",
            code="INVALID_TIMESTAMP",
        )

    whole_seconds = int(dec)
    micros = int(((dec - Decimal(whole_seconds)) * Decimal("1000000")).to_integral_value())
    utc_dt = datetime.fromtimestamp(whole_seconds, tz=timezone.utc).replace(microsecond=micros)
    return utc_dt.astimezone(IST)


def normalize_decimal_price(raw_value: Any, field_name: str = "price") -> Decimal:
    """Parse numeric or string price into exact Decimal without binary float drift."""
    if raw_value is None or isinstance(raw_value, bool):
        raise HistoricalDataError(
            f"Missing or boolean {field_name} value.",
            code="NON_NUMERIC_VALUE",
        )
    text = str(raw_value).strip()
    if not text or text.lower() in {"null", "none", "nan", "inf", "-inf"}:
        raise HistoricalDataError(
            f"Blank or non-finite {field_name} value: {raw_value!r}.",
            code="NON_NUMERIC_VALUE",
        )
    try:
        dec = Decimal(text)
        if dec.is_nan() or dec.is_infinite():
            raise HistoricalDataError(
                f"Non-finite {field_name} value: {raw_value!r}.",
                code="NON_NUMERIC_VALUE",
            )
        return dec
    except InvalidOperation as exc:
        raise HistoricalDataError(
            f"Invalid numeric {field_name} value: {raw_value!r}.",
            code="NON_NUMERIC_VALUE",
        ) from exc


def normalize_int_volume(raw_value: Any) -> int:
    """Parse integer volume faithfully without fabricating or altering zero volume."""
    if raw_value is None or isinstance(raw_value, bool):
        raise HistoricalDataError("Missing or boolean volume value.", code="NON_NUMERIC_VALUE")
    text = str(raw_value).strip()
    if not text or text.lower() in {"null", "none", "nan", "inf", "-inf"}:
        raise HistoricalDataError(f"Blank volume value: {raw_value!r}.", code="NON_NUMERIC_VALUE")
    try:
        dec = Decimal(text)
        if dec.is_nan() or dec.is_infinite() or dec != dec.to_integral_value():
            raise HistoricalDataError(
                f"Non-integer volume value: {raw_value!r}.",
                code="NON_NUMERIC_VALUE",
            )
        return int(dec)
    except InvalidOperation as exc:
        raise HistoricalDataError(
            f"Invalid volume value: {raw_value!r}.",
            code="NON_NUMERIC_VALUE",
        ) from exc


@dataclass(frozen=True)
class RawCandleCandidate:
    """Pre-validation candidate row retaining row/chunk origin for error reporting."""

    row_index: int
    timestamp_raw: Any
    open_raw: Any
    high_raw: Any
    low_raw: Any
    close_raw: Any
    volume_raw: Any
    is_epoch_timestamp: bool = False


def fyers_tuples_to_candidates(raw_candles: Sequence[Any]) -> list[RawCandleCandidate]:
    """Convert raw FYERS `[epoch, open, high, low, close, volume]` arrays into RawCandleCandidates."""
    candidates: list[RawCandleCandidate] = []
    for idx, item in enumerate(raw_candles, start=1):
        if not isinstance(item, (list, tuple)) or len(item) < 6:
            candidates.append(
                RawCandleCandidate(
                    row_index=idx,
                    timestamp_raw=None,
                    open_raw=None,
                    high_raw=None,
                    low_raw=None,
                    close_raw=None,
                    volume_raw=None,
                    is_epoch_timestamp=True,
                )
            )
            continue
        candidates.append(
            RawCandleCandidate(
                row_index=idx,
                timestamp_raw=item[0],
                open_raw=item[1],
                high_raw=item[2],
                low_raw=item[3],
                close_raw=item[4],
                volume_raw=item[5],
                is_epoch_timestamp=True,
            )
        )
    return candidates


def is_candle_incomplete_at_reference_time(
    candle_timestamp: datetime,
    *,
    timeframe: str,
    reference_now: datetime | None = None,
    session_close_ist: str = "15:30:00",
) -> bool:
    """Return True if `candle_timestamp` belongs to an in-progress bar not yet closed in IST."""
    ref = ensure_ist(reference_now or now_ist())
    bar_ts = ensure_ist(candle_timestamp)
    spec = get_timeframe_spec(timeframe)

    if spec.is_intraday:
        bar_close = bar_ts + timedelta(minutes=spec.bar_duration_minutes)
        return bar_close > ref

    # Daily (`1d`) bar: incomplete if bar's IST date is today (or future) and market session has not closed
    if bar_ts.date() > ref.date():
        return True
    if bar_ts.date() == ref.date():
        close_t = parse_hms_time(session_close_ist)
        ref_t = ref.timetz().replace(tzinfo=None)
        return ref_t < close_t
    return False


def exclude_incomplete_current_candle(
    candles: Sequence[NormalizedCandle],
    *,
    timeframe: str,
    reference_now: datetime | None = None,
    session_close_ist: str = "15:30:00",
    allow_partial_current_candle: bool = False,
) -> tuple[list[NormalizedCandle], bool]:
    """Filter out any incomplete current/future candle unless `allow_partial_current_candle=True`.

    Returns `(filtered_candles, excluded_any)`.
    """
    if not candles:
        return [], False

    ref = ensure_ist(reference_now or now_ist())
    kept: list[NormalizedCandle] = []
    excluded = False
    for c in candles:
        incomplete = is_candle_incomplete_at_reference_time(
            c.timestamp,
            timeframe=timeframe,
            reference_now=ref,
            session_close_ist=session_close_ist,
        )
        if incomplete and not allow_partial_current_candle:
            excluded = True
            continue
        kept.append(c)
    return kept, excluded


def render_normalized_csv(candles: Sequence[NormalizedCandle]) -> str:
    """Render normalized candles into canonical UTF-8 CSV (`timestamp,open,high,low,close,volume`)."""
    ordered = sorted(candles, key=lambda c: c.timestamp)
    buf = io.StringIO(newline="")
    writer = csv.DictWriter(buf, fieldnames=list(CSV_EXPORT_COLUMNS), lineterminator="\n")
    writer.writeheader()
    for c in ordered:
        writer.writerow(c.to_csv_row())
    return buf.getvalue()


def compute_candles_fingerprint(candles: Sequence[NormalizedCandle]) -> str:
    """Compute canonical SHA-256 fingerprint over sorted normalized CSV representation."""
    csv_text = render_normalized_csv(candles).replace("\r\n", "\n").replace("\r", "\n").strip()
    return hashlib.sha256(csv_text.encode("utf-8")).hexdigest()


def build_deterministic_csv_filename(
    *,
    instrument: str,
    timeframe: str,
    provider: str,
    start_date: date | datetime | None,
    end_date: date | datetime | None,
) -> str:
    """Generate deterministic export filename, e.g. `NIFTY_5MIN_FYERS_2026-01-01_2026-10-31.csv`."""
    sym = "".join(ch for ch in instrument.strip().upper() if ch.isalnum()) or "INSTRUMENT"
    spec = get_timeframe_spec(timeframe)
    prov = "".join(ch for ch in provider.strip().upper() if ch.isalnum()) or "HISTORICAL"

    def _fmt_d(val: date | datetime | None, fallback: str) -> str:
        if val is None:
            return fallback
        if isinstance(val, datetime):
            return ensure_ist(val).date().isoformat()
        return val.isoformat()

    d_from = _fmt_d(start_date, "START")
    d_to = _fmt_d(end_date, "END")
    return f"{sym}_{spec.csv_filename_tag}_{prov}_{d_from}_{d_to}.csv"


def ist_day_start(d: date) -> datetime:
    """Return `00:00:00+05:30` for calendar date `d`."""
    return datetime.combine(d, time(0, 0, 0), tzinfo=IST)


def ist_day_end(d: date) -> datetime:
    """Return `23:59:59+05:30` for calendar date `d`."""
    return datetime.combine(d, time(23, 59, 59), tzinfo=IST)
