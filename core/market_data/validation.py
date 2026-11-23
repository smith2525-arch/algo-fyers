"""21-point Historical OHLCV Data Quality Validator.

Validates:
 1. Required fields present
 2. Timestamp parseable
 3. Timestamp unique within instrument/timeframe
 4. Timestamps ordered (or deterministically sorted during provider chunk merge)
 5. OHLC values numeric (`Decimal`)
 6. Open > 0
 7. High > 0
 8. Low > 0
 9. Close > 0
10. Volume >= 0 (plus zero-volume index preservation & truthful warning)
11. High >= Open
12. High >= Close
13. High >= Low
14. Low <= Open
15. Low <= Close
16. Missing-candle analysis
17. Duplicate timestamps
18. Conflicting duplicate timestamps
19. Timezone consistency (`Asia/Kolkata`, `+05:30`)
20. Requested range vs returned range & failed chunk detection
21. Partial-current-candle protection

Never fabricates missing prices or forward-fills OHLC data.
Never deletes special-market sessions (e.g. Muhurat/Saturday sessions); flags as WARNING.
"""

from __future__ import annotations

from collections import defaultdict
from collections.abc import Sequence
from datetime import date, datetime, timedelta
from decimal import Decimal
from typing import Any

from core.clock import IST, ensure_ist, format_ist_iso, now_ist, parse_hms_time
from core.exceptions import HistoricalDataError
from core.market_data.models import (
    DataQualityStatus,
    DatasetQualityResult,
    NormalizedCandle,
    get_timeframe_spec,
)
from core.market_data.normalization import (
    RawCandleCandidate,
    epoch_seconds_to_ist,
    exclude_incomplete_current_candle,
    normalize_decimal_price,
    normalize_int_volume,
)

IST_OFFSET = timedelta(hours=5, minutes=30)


def _is_missing_raw(val: Any) -> bool:
    if val is None or isinstance(val, bool):
        return True
    if isinstance(val, str) and val.strip() in ("", "null", "NULL", "NaN", "nan", "None"):
        return True
    return False


def _parse_iso_timestamp_with_tz_audit(raw_ts: Any) -> tuple[datetime | None, str | None]:
    if _is_missing_raw(raw_ts):
        return None, None
    if isinstance(raw_ts, datetime):
        dt = raw_ts
    elif isinstance(raw_ts, str):
        txt = raw_ts.strip()
        if txt.endswith("Z"):
            txt = txt[:-1] + "+00:00"
        try:
            dt = datetime.fromisoformat(txt)
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


def validate_and_normalize_candles(
    candidates: Sequence[RawCandleCandidate],
    *,
    instrument: str,
    timeframe: str,
    source: str,
    provider_symbol: str | None = None,
    retrieval_timestamp: datetime | None = None,
    dataset_id: str | None = None,
    requested_from: date | datetime | None = None,
    requested_to: date | datetime | None = None,
    session_open_ist: str = "09:15:00",
    session_close_ist: str = "15:30:00",
    sort_and_deduplicate_exact: bool = True,
    protect_partial_current_candle: bool = True,
    allow_partial_current_candle: bool = False,
    reference_now: datetime | None = None,
    missing_chunk_intervals: Sequence[dict[str, str]] | None = None,
) -> tuple[list[NormalizedCandle], DatasetQualityResult]:
    """Execute full 21-point normalization and data-quality validation."""
    clean_inst = instrument.strip().upper()
    tf_spec = get_timeframe_spec(timeframe)
    retr_ts = ensure_ist(retrieval_timestamp or now_ist())
    ref_now = ensure_ist(reference_now or retr_ts)

    issues: list[dict[str, Any]] = []
    null_count = 0
    tz_issue_count = 0
    out_of_order_count = 0
    duplicate_count = 0
    exact_dup_removed_count = 0
    conflicting_dup_count = 0
    invalid_ohlc_count = 0
    missing_candle_count = 0
    unexpected_gap_count = 0
    zero_volume_count = 0

    parsed_rows: list[tuple[int, NormalizedCandle]] = []
    prev_raw_ts: datetime | None = None

    for c in candidates:
        # 1. Required fields present
        if (
            _is_missing_raw(c.timestamp_raw)
            or _is_missing_raw(c.open_raw)
            or _is_missing_raw(c.high_raw)
            or _is_missing_raw(c.low_raw)
            or _is_missing_raw(c.close_raw)
            or _is_missing_raw(c.volume_raw)
        ):
            null_count += 1
            issues.append(
                {
                    "severity": "ERROR",
                    "check_code": "NULL_OR_MISSING_FIELD",
                    "message": f"Row {c.row_index} is missing one or more required OHLCV fields.",
                    "row_index": c.row_index,
                    "timestamp": None,
                }
            )
            continue

        # 2 & 19. Timestamp parseable & timezone consistency
        tz_warn: str | None = None
        if c.is_epoch_timestamp:
            try:
                ts_ist = epoch_seconds_to_ist(c.timestamp_raw)
            except HistoricalDataError as exc:
                null_count += 1
                issues.append(
                    {
                        "severity": "ERROR",
                        "check_code": "INVALID_TIMESTAMP",
                        "message": f"Row {c.row_index}: {exc}",
                        "row_index": c.row_index,
                        "timestamp": None,
                    }
                )
                continue
        else:
            parsed_dt, tz_warn = _parse_iso_timestamp_with_tz_audit(c.timestamp_raw)
            if parsed_dt is None:
                null_count += 1
                issues.append(
                    {
                        "severity": "ERROR",
                        "check_code": "INVALID_TIMESTAMP",
                        "message": f"Row {c.row_index} has unparseable timestamp {c.timestamp_raw!r}.",
                        "row_index": c.row_index,
                        "timestamp": None,
                    }
                )
                continue
            ts_ist = parsed_dt

        if tz_warn is not None:
            tz_issue_count += 1
            if tz_issue_count <= 10:
                issues.append(
                    {
                        "severity": "WARNING",
                        "check_code": "TIMEZONE_NORMALIZED",
                        "message": f"Row {c.row_index}: {tz_warn} -> normalized to Asia/Kolkata (+05:30).",
                        "row_index": c.row_index,
                        "timestamp": format_ist_iso(ts_ist),
                    }
                )

        # 5. OHLC values numeric & integer volume
        try:
            open_px = normalize_decimal_price(c.open_raw, "open")
            high_px = normalize_decimal_price(c.high_raw, "high")
            low_px = normalize_decimal_price(c.low_raw, "low")
            close_px = normalize_decimal_price(c.close_raw, "close")
            vol = normalize_int_volume(c.volume_raw)
        except HistoricalDataError as exc:
            null_count += 1
            issues.append(
                {
                    "severity": "ERROR",
                    "check_code": "NON_NUMERIC_VALUE",
                    "message": f"Row {c.row_index}: {exc}",
                    "row_index": c.row_index,
                    "timestamp": format_ist_iso(ts_ist),
                }
            )
            continue

        # 4. Ordering check (when not sorting merged chunks, out-of-order is an error)
        if not sort_and_deduplicate_exact and prev_raw_ts is not None and ts_ist < prev_raw_ts:
            out_of_order_count += 1
            issues.append(
                {
                    "severity": "ERROR",
                    "check_code": "OUT_OF_ORDER_TIMESTAMP",
                    "message": (
                        f"Timestamp {format_ist_iso(ts_ist)} at row {c.row_index} precedes "
                        f"previous timestamp {format_ist_iso(prev_raw_ts)}."
                    ),
                    "row_index": c.row_index,
                    "timestamp": format_ist_iso(ts_ist),
                }
            )
            continue
        prev_raw_ts = ts_ist

        # 6–15. Strict OHLC & volume invariants
        highest_oc = max(open_px, close_px)
        lowest_oc = min(open_px, close_px)
        if (
            open_px <= Decimal("0")
            or high_px <= Decimal("0")
            or low_px <= Decimal("0")
            or close_px <= Decimal("0")
            or vol < 0
            or high_px < low_px
            or high_px < highest_oc
            or low_px > lowest_oc
        ):
            invalid_ohlc_count += 1
            issues.append(
                {
                    "severity": "ERROR",
                    "check_code": "INVALID_OHLC_RELATIONSHIP",
                    "message": (
                        f"Invalid OHLCV at row {c.row_index} ({format_ist_iso(ts_ist)}): "
                        f"O={open_px}, H={high_px}, L={low_px}, C={close_px}, V={vol}."
                    ),
                    "row_index": c.row_index,
                    "timestamp": format_ist_iso(ts_ist),
                }
            )
            continue

        candle = NormalizedCandle(
            timestamp=ts_ist,
            open=open_px,
            high=high_px,
            low=low_px,
            close=close_px,
            volume=vol,
            instrument=clean_inst,
            timeframe=tf_spec.code,
            source=source,
            provider_symbol=provider_symbol,
            retrieval_timestamp=retr_ts,
            dataset_id=dataset_id,
        )
        parsed_rows.append((c.row_index, candle))

    # 3, 4, 17, 18. Deduplication, Conflict Detection & Chronological Sorting
    if sort_and_deduplicate_exact:
        parsed_rows.sort(key=lambda item: (item[1].timestamp, item[0]))

    by_ts: dict[datetime, NormalizedCandle] = {}
    conflicting_timestamps: set[datetime] = set()

    for row_idx, candle in parsed_rows:
        existing = by_ts.get(candle.timestamp)
        if existing is None:
            by_ts[candle.timestamp] = candle
            continue

        duplicate_count += 1
        same_ohlcv = (
            existing.open == candle.open
            and existing.high == candle.high
            and existing.low == candle.low
            and existing.close == candle.close
            and existing.volume == candle.volume
        )
        if same_ohlcv and sort_and_deduplicate_exact:
            exact_dup_removed_count += 1
            if exact_dup_removed_count <= 10:
                issues.append(
                    {
                        "severity": "WARNING",
                        "check_code": "EXACT_DUPLICATE_REMOVED",
                        "message": (
                            f"Removed exact duplicate candle at {format_ist_iso(candle.timestamp)} "
                            f"(row {row_idx})."
                        ),
                        "row_index": row_idx,
                        "timestamp": format_ist_iso(candle.timestamp),
                    }
                )
        elif not same_ohlcv:
            conflicting_dup_count += 1
            conflicting_timestamps.add(candle.timestamp)
            issues.append(
                {
                    "severity": "ERROR",
                    "check_code": "CONFLICTING_DUPLICATE_TIMESTAMP",
                    "message": (
                        f"Conflicting duplicate candle at {format_ist_iso(candle.timestamp)} (row {row_idx}): "
                        f"existing=({existing.open},{existing.high},{existing.low},{existing.close},{existing.volume}) "
                        f"vs duplicate=({candle.open},{candle.high},{candle.low},{candle.close},{candle.volume})."
                    ),
                    "row_index": row_idx,
                    "timestamp": format_ist_iso(candle.timestamp),
                }
            )
        else:
            # Strict mode (e.g. raw CSV without exact-dedup flag): treat duplicate as hard error
            conflicting_dup_count += 1
            issues.append(
                {
                    "severity": "ERROR",
                    "check_code": "DUPLICATE_TIMESTAMP",
                    "message": f"Duplicate timestamp {format_ist_iso(candle.timestamp)} at row {row_idx}.",
                    "row_index": row_idx,
                    "timestamp": format_ist_iso(candle.timestamp),
                }
            )

    deduped_candles = list(by_ts.values())
    if sort_and_deduplicate_exact:
        deduped_candles.sort(key=lambda x: x.timestamp)

    # 21. Partial-current-candle protection
    partial_excluded = False
    has_unclosed_bar_kept = False
    if protect_partial_current_candle and deduped_candles:
        deduped_candles, partial_excluded = exclude_incomplete_current_candle(
            deduped_candles,
            timeframe=tf_spec.code,
            reference_now=ref_now,
            session_close_ist=session_close_ist,
            allow_partial_current_candle=allow_partial_current_candle,
        )
        if partial_excluded:
            issues.append(
                {
                    "severity": "WARNING",
                    "check_code": "PARTIAL_CURRENT_CANDLE_EXCLUDED",
                    "message": (
                        "Excluded incomplete in-progress current market candle so finalized "
                        "historical dataset contains only closed bars."
                    ),
                    "row_index": None,
                    "timestamp": format_ist_iso(ref_now),
                }
            )
        elif allow_partial_current_candle:
            _, would_exclude = exclude_incomplete_current_candle(
                deduped_candles,
                timeframe=tf_spec.code,
                reference_now=ref_now,
                session_close_ist=session_close_ist,
                allow_partial_current_candle=False,
            )
            if would_exclude:
                has_unclosed_bar_kept = True
                issues.append(
                    {
                        "severity": "WARNING",
                        "check_code": "PARTIAL_CURRENT_CANDLE_INCLUDED",
                        "message": "Dataset explicitly retains an incomplete current candle and is marked PARTIAL.",
                        "row_index": None,
                        "timestamp": format_ist_iso(deduped_candles[-1].timestamp),
                    }
                )

    hard_errors = (
        null_count
        + out_of_order_count
        + conflicting_dup_count
        + invalid_ohlc_count
    )

    if len(deduped_candles) == 0 and hard_errors == 0:
        issues.append(
            {
                "severity": "ERROR",
                "check_code": "EMPTY_DATASET",
                "message": "Zero closed historical candles available for the requested symbol and interval.",
                "row_index": None,
                "timestamp": None,
            }
        )

    # 10b. Volume availability / zero-volume index semantics check
    for c in deduped_candles:
        if c.volume == 0:
            zero_volume_count += 1

    if deduped_candles and zero_volume_count > 0:
        if zero_volume_count == len(deduped_candles) or (
            zero_volume_count / len(deduped_candles) >= 0.25
        ):
            issues.append(
                {
                    "severity": "WARNING",
                    "check_code": "ZERO_OR_UNAVAILABLE_INDEX_VOLUME",
                    "message": (
                        f"{zero_volume_count}/{len(deduped_candles)} candles report volume=0 "
                        "(common for cash index feeds such as NIFTY/BANKNIFTY). Values preserved "
                        "truthfully without fabrication; EMA/RSI calculations do not depend on volume."
                    ),
                    "row_index": None,
                    "timestamp": None,
                }
            )

    # 16. Missing-candle & special-session analysis (warn on special sessions, never delete!)
    open_t = parse_hms_time(session_open_ist)
    close_t = parse_hms_time(session_close_ist)
    step_delta = timedelta(minutes=tf_spec.bar_duration_minutes)

    if hard_errors == 0 and deduped_candles and tf_spec.is_intraday:
        by_day: dict[date, list[NormalizedCandle]] = defaultdict(list)
        for c in deduped_candles:
            by_day[c.timestamp.date()].append(c)

        special_session_days: set[date] = set()
        for trading_day, day_bars in sorted(by_day.items()):
            for b in day_bars:
                bt = b.timestamp.timetz().replace(tzinfo=None)
                if b.timestamp.weekday() >= 5 or bt < open_t or bt > close_t:
                    special_session_days.add(trading_day)
                    unexpected_gap_count += 1
                    if len(special_session_days) <= 10:
                        issues.append(
                            {
                                "severity": "WARNING",
                                "check_code": "SPECIAL_OR_OFF_HOURS_SESSION",
                                "message": (
                                    f"Candle {format_ist_iso(b.timestamp)} falls outside standard "
                                    f"weekday hours ({session_open_ist}..{session_close_ist} IST); "
                                    "preserved as special/off-hours market session."
                                ),
                                "row_index": None,
                                "timestamp": format_ist_iso(b.timestamp),
                            }
                        )
                    break

            for idx in range(1, len(day_bars)):
                prev_b = day_bars[idx - 1].timestamp
                curr_b = day_bars[idx].timestamp
                diff = curr_b - prev_b
                if diff > step_delta:
                    missed = int(diff // step_delta) - 1
                    if missed > 0:
                        missing_candle_count += missed
                        if diff >= step_delta * 4:
                            unexpected_gap_count += 1
                            if unexpected_gap_count <= 25:
                                issues.append(
                                    {
                                        "severity": "WARNING",
                                        "check_code": "UNEXPECTED_SESSION_GAP",
                                        "message": (
                                            f"Intraday gap of {int(diff.total_seconds() // 60)}m "
                                            f"({missed} missing {tf_spec.code} candles) on {trading_day} "
                                            f"between {prev_b.strftime('%H:%M')} and {curr_b.strftime('%H:%M')} IST."
                                        ),
                                        "row_index": None,
                                        "timestamp": format_ist_iso(curr_b),
                                    }
                                )
                        else:
                            if missing_candle_count <= 30:
                                issues.append(
                                    {
                                        "severity": "WARNING",
                                        "check_code": "MISSING_CANDLE",
                                        "message": (
                                            f"Missing {missed} {tf_spec.code} candle(s) on {trading_day} "
                                            f"between {prev_b.strftime('%H:%M')} and {curr_b.strftime('%H:%M')} IST."
                                        ),
                                        "row_index": None,
                                        "timestamp": format_ist_iso(curr_b),
                                    }
                                )

    # 20. Requested range vs returned range & failed chunk interval detection
    missing_intervals_out: list[dict[str, str]] = [
        dict(item) for item in (missing_chunk_intervals or [])
    ]
    is_partial = len(missing_intervals_out) > 0 or has_unclosed_bar_kept

    def _to_date(v: date | datetime | None) -> date | None:
        if v is None:
            return None
        if isinstance(v, datetime):
            return ensure_ist(v).date()
        return v

    req_from_d = _to_date(requested_from)
    req_to_d = _to_date(requested_to)

    if missing_intervals_out:
        for interval in missing_intervals_out[:10]:
            issues.append(
                {
                    "severity": "WARNING",
                    "check_code": "PARTIAL_DOWNLOAD_MISSING_CHUNK",
                    "message": (
                        f"Requested chunk {interval.get('from')}..{interval.get('to')} failed or "
                        f"returned incomplete coverage ({interval.get('reason', 'upstream error')})."
                    ),
                    "row_index": None,
                    "timestamp": None,
                }
            )

    if hard_errors == 0 and deduped_candles and req_from_d and req_to_d:
        actual_first_d = deduped_candles[0].timestamp.date()
        actual_last_d = deduped_candles[-1].timestamp.date()
        # Allow up to 5 calendar days for normal weekend + exchange holiday clusters;
        # gaps > 7 calendar days at start or end of requested range indicate partial history coverage.
        if (actual_first_d - req_from_d).days > 7:
            is_partial = True
            missing_intervals_out.append(
                {
                    "from": req_from_d.isoformat(),
                    "to": (actual_first_d - timedelta(days=1)).isoformat(),
                    "reason": "NO_DATA_AT_REQUEST_START",
                }
            )
            issues.append(
                {
                    "severity": "WARNING",
                    "check_code": "REQUESTED_START_RANGE_UNAVAILABLE",
                    "message": (
                        f"Requested start {req_from_d.isoformat()} but earliest returned candle is "
                        f"{actual_first_d.isoformat()}."
                    ),
                    "row_index": None,
                    "timestamp": format_ist_iso(deduped_candles[0].timestamp),
                }
            )
        if (req_to_d - actual_last_d).days > 7:
            is_partial = True
            missing_intervals_out.append(
                {
                    "from": (actual_last_d + timedelta(days=1)).isoformat(),
                    "to": req_to_d.isoformat(),
                    "reason": "NO_DATA_AT_REQUEST_END",
                }
            )
            issues.append(
                {
                    "severity": "WARNING",
                    "check_code": "REQUESTED_END_RANGE_UNAVAILABLE",
                    "message": (
                        f"Requested end {req_to_d.isoformat()} but latest returned candle is "
                        f"{actual_last_d.isoformat()}."
                    ),
                    "row_index": None,
                    "timestamp": format_ist_iso(deduped_candles[-1].timestamp),
                }
            )

    passed_validation = hard_errors == 0 and len(deduped_candles) > 0
    has_warnings = any(i["severity"] == "WARNING" for i in issues)

    if not passed_validation:
        quality_status = DataQualityStatus.INVALID
        is_complete = False
    elif is_partial:
        quality_status = DataQualityStatus.PARTIAL
        is_complete = False
    elif has_warnings:
        quality_status = DataQualityStatus.VALID_WITH_WARNINGS
        is_complete = True
    else:
        quality_status = DataQualityStatus.VALID
        is_complete = True

    first_iso = format_ist_iso(deduped_candles[0].timestamp) if deduped_candles else None
    last_iso = format_ist_iso(deduped_candles[-1].timestamp) if deduped_candles else None
    req_from_iso = (
        format_ist_iso(requested_from)
        if isinstance(requested_from, datetime)
        else (requested_from.isoformat() if isinstance(requested_from, date) else None)
    )
    req_to_iso = (
        format_ist_iso(requested_to)
        if isinstance(requested_to, datetime)
        else (requested_to.isoformat() if isinstance(requested_to, date) else None)
    )

    quality_result = DatasetQualityResult(
        quality_status=quality_status,
        passed_validation=passed_validation,
        has_warnings=has_warnings,
        is_complete=is_complete,
        total_rows_inspected=len(candidates),
        valid_candle_count=len(deduped_candles),
        null_value_count=null_count,
        timezone_issue_count=tz_issue_count,
        out_of_order_count=out_of_order_count,
        duplicate_timestamp_count=duplicate_count,
        exact_duplicate_removed_count=exact_dup_removed_count,
        conflicting_duplicate_count=conflicting_dup_count,
        invalid_ohlc_count=invalid_ohlc_count,
        missing_candle_count=missing_candle_count,
        unexpected_session_gap_count=unexpected_gap_count,
        zero_volume_candle_count=zero_volume_count,
        partial_current_candle_excluded=partial_excluded,
        missing_intervals=missing_intervals_out,
        first_timestamp_ist=first_iso,
        last_timestamp_ist=last_iso,
        requested_from_ist=req_from_iso,
        requested_to_ist=req_to_iso,
        checked_timezone="Asia/Kolkata",
        issues=issues,
    )
    return deduped_candles, quality_result
