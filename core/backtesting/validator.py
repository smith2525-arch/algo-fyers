"""Pre-backtest historical OHLCV validator producing a DataQualityReport.

Checks:
1. Null / blank / non-numeric values       -> Hard failure (`FAILED_VALIDATION`)
2. Timestamp parsing & timezone discipline -> Unparseable = Hard failure; naive/non-IST = Warning + normalized to IST
3. Strict ascending timestamp ordering     -> Hard failure (`FAILED_VALIDATION`)
4. Duplicate timestamps                    -> Hard failure (`FAILED_VALIDATION`)
5. Invalid OHLC & volume relationships     -> Hard failure (`FAILED_VALIDATION`)
6. Missing candles within session grid     -> Warning (`COMPLETED_WITH_WARNINGS`)
7. Unexpected session gaps / off-hours     -> Warning (`COMPLETED_WITH_WARNINGS`)
"""

from __future__ import annotations

from collections import defaultdict
from datetime import date, datetime, timedelta
from decimal import Decimal, InvalidOperation
from typing import Any

from core.clock import IST, ensure_ist, format_ist_iso, parse_hms_time
from core.backtesting.models import (
    DataQualityIssue,
    DataQualityReport,
    RawCandleRow,
)
from core.strategies.base import OHLCVBar

TIMEFRAME_MINUTES: dict[str, int] = {
    "1m": 1,
    "3m": 3,
    "5m": 5,
    "10m": 10,
    "15m": 15,
    "30m": 30,
    "60m": 60,
    "1d": 1440,
}

IST_OFFSET = timedelta(hours=5, minutes=30)


def _is_missing(val: Any) -> bool:
    if val is None:
        return True
    if isinstance(val, str) and val.strip() in ("", "null", "NULL", "NaN", "nan", "None"):
        return True
    return False


def _parse_decimal(val: Any) -> Decimal | None:
    if _is_missing(val):
        return None
    try:
        dec = Decimal(str(val).strip())
        if dec.is_nan() or dec.is_infinite():
            return None
        return dec
    except InvalidOperation:
        return None


def _parse_int(val: Any) -> int | None:
    if _is_missing(val):
        return None
    try:
        dec = Decimal(str(val).strip())
        if dec != dec.to_integral_value():
            return None
        return int(dec)
    except InvalidOperation:
        return None


def _parse_timestamp_with_tz_audit(raw_ts: Any) -> tuple[datetime | None, str | None]:
    """Return `(ist_datetime, tz_warning_reason_or_none)`."""
    if _is_missing(raw_ts):
        return None, None

    if isinstance(raw_ts, datetime):
        dt = raw_ts
    elif isinstance(raw_ts, str):
        text = raw_ts.strip()
        if text.endswith("Z"):
            text = text[:-1] + "+00:00"
        try:
            dt = datetime.fromisoformat(text)
        except ValueError:
            return None, None
    else:
        return None, None

    if dt.tzinfo is None or dt.tzinfo.utcoffset(dt) is None:
        return dt.replace(tzinfo=IST), "NAIVE_TIMESTAMP_ASSUMED_IST"

    offset = dt.tzinfo.utcoffset(dt)
    if offset != IST_OFFSET:
        return ensure_ist(dt), f"NON_IST_OFFSET_CONVERTED ({offset})"

    return ensure_ist(dt), None


def validate_ohlcv_rows(
    rows: list[RawCandleRow],
    *,
    instrument: str,
    timeframe: str,
    start_date: date | None = None,
    end_date: date | None = None,
    session_open_ist: str = "09:15:00",
    session_close_ist: str = "15:30:00",
) -> tuple[list[OHLCVBar], DataQualityReport]:
    """Validate raw OHLCV rows, filter by date window, and build DataQualityReport."""
    issues: list[DataQualityIssue] = []
    null_count = 0
    tz_issue_count = 0
    out_of_order_count = 0
    duplicate_count = 0
    invalid_ohlc_count = 0
    missing_candle_count = 0
    unexpected_gap_count = 0

    parsed_bars: list[OHLCVBar] = []
    seen_timestamps: set[datetime] = set()
    prev_ts: datetime | None = None
    clean_symbol = instrument.strip().upper()

    for r in rows:
        # 1. Null / missing / unparseable check
        if (
            _is_missing(r.timestamp_raw)
            or _is_missing(r.open_raw)
            or _is_missing(r.high_raw)
            or _is_missing(r.low_raw)
            or _is_missing(r.close_raw)
            or _is_missing(r.volume_raw)
        ):
            null_count += 1
            issues.append(
                DataQualityIssue(
                    severity="ERROR",
                    check_code="NULL_VALUE",
                    message=f"Row {r.row_index} contains null or empty OHLCV/timestamp field.",
                    row_index=r.row_index,
                )
            )
            continue

        ts_ist, tz_warn = _parse_timestamp_with_tz_audit(r.timestamp_raw)
        if ts_ist is None:
            null_count += 1
            issues.append(
                DataQualityIssue(
                    severity="ERROR",
                    check_code="INVALID_TIMESTAMP",
                    message=f"Row {r.row_index} has unparseable timestamp {r.timestamp_raw!r}.",
                    row_index=r.row_index,
                )
            )
            continue

        open_px = _parse_decimal(r.open_raw)
        high_px = _parse_decimal(r.high_raw)
        low_px = _parse_decimal(r.low_raw)
        close_px = _parse_decimal(r.close_raw)
        vol = _parse_int(r.volume_raw)

        if (
            open_px is None
            or high_px is None
            or low_px is None
            or close_px is None
            or vol is None
        ):
            null_count += 1
            issues.append(
                DataQualityIssue(
                    severity="ERROR",
                    check_code="NON_NUMERIC_VALUE",
                    message=f"Row {r.row_index} contains non-numeric price or non-integer volume.",
                    row_index=r.row_index,
                    timestamp=format_ist_iso(ts_ist),
                )
            )
            continue

        # 2. Duplicate & ordering checks across full input stream
        if ts_ist in seen_timestamps:
            duplicate_count += 1
            issues.append(
                DataQualityIssue(
                    severity="ERROR",
                    check_code="DUPLICATE_TIMESTAMP",
                    message=f"Duplicate bar timestamp {format_ist_iso(ts_ist)} at row {r.row_index}.",
                    row_index=r.row_index,
                    timestamp=format_ist_iso(ts_ist),
                )
            )
            continue

        if prev_ts is not None and ts_ist < prev_ts:
            out_of_order_count += 1
            issues.append(
                DataQualityIssue(
                    severity="ERROR",
                    check_code="OUT_OF_ORDER_TIMESTAMP",
                    message=(
                        f"Timestamp {format_ist_iso(ts_ist)} at row {r.row_index} precedes "
                        f"previous timestamp {format_ist_iso(prev_ts)}."
                    ),
                    row_index=r.row_index,
                    timestamp=format_ist_iso(ts_ist),
                )
            )
            continue

        seen_timestamps.add(ts_ist)
        prev_ts = ts_ist

        # 3. OHLC & Volume validity check
        highest_oc = max(open_px, close_px)
        lowest_oc = min(open_px, close_px)
        if (
            open_px <= 0
            or high_px <= 0
            or low_px <= 0
            or close_px <= 0
            or high_px < low_px
            or high_px < highest_oc
            or low_px > lowest_oc
            or vol < 0
        ):
            invalid_ohlc_count += 1
            issues.append(
                DataQualityIssue(
                    severity="ERROR",
                    check_code="INVALID_OHLC_RELATIONSHIP",
                    message=(
                        f"Invalid OHLCV at row {r.row_index} ({format_ist_iso(ts_ist)}): "
                        f"O={open_px}, H={high_px}, L={low_px}, C={close_px}, V={vol}."
                    ),
                    row_index=r.row_index,
                    timestamp=format_ist_iso(ts_ist),
                )
            )
            continue

        # Date window filter applied after structural integrity verification
        if start_date is not None and ts_ist.date() < start_date:
            continue
        if end_date is not None and ts_ist.date() > end_date:
            continue

        if tz_warn is not None:
            tz_issue_count += 1
            if tz_issue_count <= 10:
                issues.append(
                    DataQualityIssue(
                        severity="WARNING",
                        check_code="TIMEZONE_NORMALIZED",
                        message=f"Row {r.row_index}: {tz_warn} -> normalized to Asia/Kolkata (+05:30).",
                        row_index=r.row_index,
                        timestamp=format_ist_iso(ts_ist),
                    )
                )

        parsed_bars.append(
            OHLCVBar(
                symbol=clean_symbol,
                timeframe=timeframe,
                bar_timestamp=ts_ist,
                open=open_px,
                high=high_px,
                low=low_px,
                close=close_px,
                volume=vol,
            )
        )

    hard_errors = null_count + out_of_order_count + duplicate_count + invalid_ohlc_count

    # 4. Missing candles & unexpected session gaps (evaluated when hard errors == 0 and bars > 0)
    from core.market_data.models import TIMEFRAME_REGISTRY

    tf_spec = TIMEFRAME_REGISTRY.get(timeframe)
    is_intraday = tf_spec.is_intraday if tf_spec is not None else True
    step_mins = TIMEFRAME_MINUTES.get(timeframe, 5)
    step_delta = timedelta(minutes=step_mins)
    open_t = parse_hms_time(session_open_ist)
    close_t = parse_hms_time(session_close_ist)

    if hard_errors == 0 and parsed_bars and is_intraday:
        by_day: dict[date, list[OHLCVBar]] = defaultdict(list)
        for b in parsed_bars:
            by_day[b.bar_timestamp.date()].append(b)

        for trading_day, day_bars in sorted(by_day.items()):
            for b in day_bars:
                bt = b.bar_timestamp.timetz().replace(tzinfo=None)
                if b.bar_timestamp.weekday() >= 5 or bt < open_t or bt > close_t:
                    unexpected_gap_count += 1
                    issues.append(
                        DataQualityIssue(
                            severity="WARNING",
                            check_code="OUTSIDE_SESSION_HOURS",
                            message=(
                                f"Candle {format_ist_iso(b.bar_timestamp)} falls outside regular "
                                f"IST weekday session ({session_open_ist}..{session_close_ist})."
                            ),
                            timestamp=format_ist_iso(b.bar_timestamp),
                        )
                    )

            for idx in range(1, len(day_bars)):
                prev_b = day_bars[idx - 1].bar_timestamp
                curr_b = day_bars[idx].bar_timestamp
                diff = curr_b - prev_b
                if diff > step_delta:
                    missed = int(diff // step_delta) - 1
                    if missed > 0:
                        missing_candle_count += missed
                        if diff >= step_delta * 4:
                            unexpected_gap_count += 1
                            issues.append(
                                DataQualityIssue(
                                    severity="WARNING",
                                    check_code="UNEXPECTED_SESSION_GAP",
                                    message=(
                                        f"Intraday session gap of {int(diff.total_seconds() // 60)}m "
                                        f"({missed} missing {timeframe} candles) on {trading_day} "
                                        f"between {prev_b.strftime('%H:%M')} and {curr_b.strftime('%H:%M')} IST."
                                    ),
                                    timestamp=format_ist_iso(curr_b),
                                )
                            )
                        else:
                            issues.append(
                                DataQualityIssue(
                                    severity="WARNING",
                                    check_code="MISSING_CANDLE",
                                    message=(
                                        f"Missing {missed} {timeframe} candle(s) on {trading_day} "
                                        f"between {prev_b.strftime('%H:%M')} and {curr_b.strftime('%H:%M')} IST."
                                    ),
                                    timestamp=format_ist_iso(curr_b),
                                )
                            )

    has_warnings = (tz_issue_count + missing_candle_count + unexpected_gap_count) > 0
    first_ts = format_ist_iso(parsed_bars[0].bar_timestamp) if parsed_bars else None
    last_ts = format_ist_iso(parsed_bars[-1].bar_timestamp) if parsed_bars else ""

    report = DataQualityReport(
        passed_validation=(hard_errors == 0),
        has_warnings=has_warnings,
        total_rows_inspected=len(rows),
        valid_candle_count=len(parsed_bars),
        null_value_count=null_count,
        timezone_issue_count=tz_issue_count,
        out_of_order_count=out_of_order_count,
        duplicate_timestamp_count=duplicate_count,
        invalid_ohlc_count=invalid_ohlc_count,
        missing_candle_count=missing_candle_count,
        unexpected_session_gap_count=unexpected_gap_count,
        first_timestamp_ist=first_ts,
        last_timestamp_ist=last_ts,
        issues=issues[:100],
    )
    return parsed_bars, report

