"""Unit tests for FYERS Historical Market Data parsing, IST epoch conversion, timeframe/symbol
mappings, 21-point OHLCV validation, chunking, retry/backoff, CSV export round-trip,
exact Decimal preservation, and dataset immutability.
"""

from __future__ import annotations

from datetime import date, datetime, timezone
from decimal import Decimal
import json

import httpx
import pytest
from sqlalchemy import select

from core.clock import IST
from core.config import AppSettings
from core.exceptions import (
    DatasetInUseError,
    FyersAuthenticationRequiredError,
    HistoricalDataError,
    HistoricalProviderUnavailableError,
    ImmutabilityViolationError,
)
from core.market_data.csv_provider import CsvHistoricalDataProvider
from core.market_data.dataset_service import HistoricalDatasetService
from core.market_data.fyers_provider import (
    FYERS_AUTH_RENEWAL_UI_MESSAGE,
    FyersHistoricalDataProvider,
    plan_fyers_date_chunks,
)
from core.market_data.models import (
    INSTRUMENT_PROVIDER_REGISTRY,
    TIMEFRAME_REGISTRY,
    DataQualityStatus,
    DataSourceType,
    classify_data_source,
    get_instrument_provider_spec,
    get_timeframe_spec,
)
from core.market_data.normalization import (
    build_deterministic_csv_filename,
    compute_candles_fingerprint,
    epoch_seconds_to_ist,
    fyers_tuples_to_candidates,
    render_normalized_csv,
)
from core.market_data.provider import sanitize_provider_error_text
from core.market_data.validation import validate_and_normalize_candles
from core.persistence.models import HistoricalDatasetModel
from core.persistence.uow import unit_of_work


def _ist_epoch(y: int, m: int, d: int, hh: int, mm: int) -> int:
    return int(datetime(y, m, d, hh, mm, 0, tzinfo=IST).timestamp())


def test_epoch_timestamp_conversion_preserves_exact_ist_without_5h30m_shift() -> None:
    """Verify intraday 09:15 IST and daily 00:00 UTC epochs convert deterministically to IST."""
    # 2026-01-15 09:15:00 IST == 2026-01-15 03:45:00 UTC
    utc_dt = datetime(2026, 1, 15, 3, 45, 0, tzinfo=timezone.utc)
    epoch_sec = int(utc_dt.timestamp())

    ist_dt = epoch_seconds_to_ist(epoch_sec)
    assert ist_dt.tzinfo == IST
    assert ist_dt.utcoffset().total_seconds() == 5.5 * 3600
    assert ist_dt.strftime("%Y-%m-%d %H:%M:%S") == "2026-01-15 09:15:00"

    # Millisecond epoch also normalizes identically
    ist_from_ms = epoch_seconds_to_ist(epoch_sec * 1000)
    assert ist_from_ms == ist_dt

    # Daily ('1D') candle stamped at 00:00:00 UTC maps to 05:30:00 IST on SAME calendar date
    daily_utc = datetime(2026, 1, 15, 0, 0, 0, tzinfo=timezone.utc)
    daily_ist = epoch_seconds_to_ist(int(daily_utc.timestamp()))
    assert daily_ist.date() == date(2026, 1, 15)
    assert daily_ist.strftime("%H:%M:%S") == "05:30:00"

    with pytest.raises(HistoricalDataError):
        epoch_seconds_to_ist("not-an-epoch")
    with pytest.raises(HistoricalDataError):
        epoch_seconds_to_ist(-500)


def test_central_symbol_and_timeframe_mappings() -> None:
    """Verify single authoritative NIFTY/BANKNIFTY symbols and 8 FYERS resolutions."""
    assert get_instrument_provider_spec("NIFTY").provider_symbol == "NSE:NIFTY50-INDEX"
    assert get_instrument_provider_spec("BANKNIFTY").provider_symbol == "NSE:NIFTYBANK-INDEX"
    assert set(INSTRUMENT_PROVIDER_REGISTRY.keys()) == {"NIFTY", "BANKNIFTY"}

    with pytest.raises(HistoricalDataError):
        get_instrument_provider_spec("CRUDEOIL")

    expected_resolutions = {
        "1m": "1",
        "3m": "3",
        "5m": "5",
        "10m": "10",
        "15m": "15",
        "30m": "30",
        "60m": "60",
        "1d": "1D",
    }
    assert {k: v.fyers_resolution for k, v in TIMEFRAME_REGISTRY.items()} == expected_resolutions
    assert get_timeframe_spec("15 minutes").fyers_resolution == "15"
    assert get_timeframe_spec("1 day").fyers_resolution == "1D"

    with pytest.raises(HistoricalDataError):
        get_timeframe_spec("2m")


def test_data_source_labeling_contracts() -> None:
    fyers_lbl = classify_data_source("FYERS_HISTORICAL", "FYERS")
    assert fyers_lbl.ui_headline == "REAL HISTORICAL MARKET DATA"
    assert fyers_lbl.ui_source_line == "SOURCE: FYERS"
    assert fyers_lbl.ui_execution_line == "EXECUTION: SIMULATED PAPER TRADING"

    csv_lbl = classify_data_source("IMPORTED_CSV", "CSV")
    assert csv_lbl.ui_headline == "USER-UPLOADED HISTORICAL DATA"
    assert csv_lbl.ui_execution_line == "EXECUTION: SIMULATED PAPER TRADING"

    syn_lbl = classify_data_source("BUNDLED_CSV", "SYNTHETIC")
    assert syn_lbl.ui_headline == "SYNTHETIC DEMO DATA"
    assert syn_lbl.ui_execution_line == "EXECUTION: SIMULATED PAPER TRADING"


def test_21_point_validation_ohlc_zero_volume_missing_duplicates_and_partial_current() -> None:
    """Verify OHLC checks, zero-volume warning, missing bars, exact vs conflicting duplicates, and partial bar exclusion."""
    ref_now = datetime(2026, 2, 10, 9, 27, 0, tzinfo=IST)
    t1 = _ist_epoch(2026, 2, 10, 9, 15)  # Closed (ends 09:20 <= 09:27)
    t2 = _ist_epoch(2026, 2, 10, 9, 25)  # In-progress at 09:27 (ends 09:30 > 09:27)! Also missing 09:20!

    # 1. Exact duplicate removed + zero volume warning + partial current candle excluded
    raw = [
        [t1, "24250.35", "24265.80", "24241.10", "24260.05", 0],
        [t1, "24250.35", "24265.80", "24241.10", "24260.05", 0],  # Exact boundary duplicate
        [t2, "24260.05", "24270.00", "24255.00", "24268.00", 0],  # Incomplete current bar
    ]
    candles, qual = validate_and_normalize_candles(
        fyers_tuples_to_candidates(raw),
        instrument="NIFTY",
        timeframe="5m",
        source=DataSourceType.FYERS_HISTORICAL.value,
        reference_now=ref_now,
    )
    assert len(candles) == 1
    assert candles[0].open == Decimal("24250.35")
    assert candles[0].volume == 0  # Zero volume preserved truthfully!
    assert qual.exact_duplicate_removed_count == 1
    assert qual.partial_current_candle_excluded is True
    assert qual.zero_volume_candle_count == 1
    assert qual.quality_status == DataQualityStatus.VALID_WITH_WARNINGS
    codes = {i["check_code"] for i in qual.issues}
    assert "ZERO_OR_UNAVAILABLE_INDEX_VOLUME" in codes
    assert "PARTIAL_CURRENT_CANDLE_EXCLUDED" in codes

    # 2. Missing candle detection (09:15 followed by 09:25 after session close)
    after_close = datetime(2026, 2, 10, 16, 0, 0, tzinfo=IST)
    candles2, qual2 = validate_and_normalize_candles(
        fyers_tuples_to_candidates(
            [
                [t1, "24250.00", "24260.00", "24240.00", "24255.00", 1000],
                [t2, "24255.00", "24270.00", "24250.00", "24265.00", 1200],
            ]
        ),
        instrument="NIFTY",
        timeframe="5m",
        source=DataSourceType.FYERS_HISTORICAL.value,
        reference_now=after_close,
    )
    assert len(candles2) == 2  # Missing 09:20 is NOT fabricated!
    assert qual2.missing_candle_count == 1
    assert qual2.quality_status == DataQualityStatus.VALID_WITH_WARNINGS

    # 3. Conflicting duplicate timestamp -> INVALID
    _, qual_conflict = validate_and_normalize_candles(
        fyers_tuples_to_candidates(
            [
                [t1, "24250.00", "24260.00", "24240.00", "24255.00", 1000],
                [t1, "24250.00", "24299.00", "24240.00", "24255.00", 1000],
            ]
        ),
        instrument="NIFTY",
        timeframe="5m",
        source=DataSourceType.FYERS_HISTORICAL.value,
        reference_now=after_close,
    )
    assert qual_conflict.passed_validation is False
    assert qual_conflict.conflicting_duplicate_count == 1
    assert qual_conflict.quality_status == DataQualityStatus.INVALID

    # 4. Invalid OHLC relationship (High < Open) -> INVALID
    _, qual_bad_ohlc = validate_and_normalize_candles(
        fyers_tuples_to_candidates(
            [[t1, "24260.00", "24250.00", "24240.00", "24245.00", 1000]]
        ),
        instrument="NIFTY",
        timeframe="5m",
        source=DataSourceType.FYERS_HISTORICAL.value,
        reference_now=after_close,
    )
    assert qual_bad_ohlc.passed_validation is False
    assert qual_bad_ohlc.invalid_ohlc_count == 1
    assert qual_bad_ohlc.quality_status == DataQualityStatus.INVALID

    # 5. Saturday special Muhurat trading session -> preserved with WARNING, never deleted
    sat_epoch = _ist_epoch(2026, 2, 14, 18, 15)  # Saturday evening
    sat_bars, sat_qual = validate_and_normalize_candles(
        fyers_tuples_to_candidates(
            [[sat_epoch, "24300.00", "24320.00", "24295.00", "24315.00", 5000]]
        ),
        instrument="NIFTY",
        timeframe="5m",
        source=DataSourceType.FYERS_HISTORICAL.value,
        reference_now=datetime(2026, 2, 15, 10, 0, 0, tzinfo=IST),
    )
    assert len(sat_bars) == 1
    assert sat_qual.quality_status == DataQualityStatus.VALID_WITH_WARNINGS


def test_chunk_planning_merge_ordering_retries_auth_fail_fast_and_decimal_precision() -> None:
    """Verify date chunking, merge ordering, exact Decimal parsing, auth non-retry, and 429 bounded retry."""
    chunks_5m = plan_fyers_date_chunks(
        from_date=date(2026, 1, 1),
        to_date=date(2026, 6, 30),  # 181 days -> 3 chunks (90 + 90 + 1)
        timeframe="5m",
    )
    assert len(chunks_5m) == 3
    assert chunks_5m[0].from_date == date(2026, 1, 1)
    assert chunks_5m[0].day_span == 90
    assert chunks_5m[-1].to_date == date(2026, 6, 30)

    chunks_1d = plan_fyers_date_chunks(
        from_date=date(2026, 1, 1),
        to_date=date(2026, 12, 31),
        timeframe="1d",
    )
    assert len(chunks_1d) == 1

    with pytest.raises(HistoricalDataError) as exc_lim:
        plan_fyers_date_chunks(
            from_date=date(2025, 1, 1),
            to_date=date(2026, 6, 1),
            timeframe="5m",
            max_total_days=365,
        )
    assert exc_lim.value.code == "DATE_RANGE_LIMIT_EXCEEDED"

    # Auth failure (401 / code -16) NEVER retries and scrubs any secret token
    auth_calls = {"n": 0}
    secret_jwt = "eyJhbGciOiJIUzI1NiJ9.eyJzdWIiOiJ0ZXN0In0.SuperSecretSig"

    def _auth_fail_handler(request: httpx.Request) -> httpx.Response:
        auth_calls["n"] += 1
        return httpx.Response(
            401,
            json={"s": "error", "code": -16, "message": f"Could not authenticate token {secret_jwt}"},
        )

    cfg = AppSettings(
        FYERS_CLIENT_ID="DEMOAPP-100",
        FYERS_ACCESS_TOKEN=secret_jwt,
        FYERS_MAX_RETRIES=3,
    )
    prov_auth = FyersHistoricalDataProvider(
        settings=cfg,
        http_transport=httpx.MockTransport(_auth_fail_handler),
        sleep_fn=lambda _s: None,
    )
    with pytest.raises(FyersAuthenticationRequiredError) as auth_exc:
        prov_auth.fetch_historical_candles(
            instrument="NIFTY",
            timeframe="5m",
            from_date=date(2026, 1, 15),
            to_date=date(2026, 1, 15),
            reference_now=datetime(2026, 1, 20, 12, 0, tzinfo=IST),
        )
    assert auth_calls["n"] == 1  # Zero retries on auth failure!
    assert secret_jwt not in str(auth_exc.value)
    assert str(auth_exc.value) == FYERS_AUTH_RENEWAL_UI_MESSAGE

    # Rate limit (429) retries bounded times with exponential backoff, then succeeds & preserves exact Decimal
    rl_calls = {"n": 0}
    sleeps: list[float] = []
    bar_t1 = _ist_epoch(2026, 1, 15, 9, 20)
    bar_t0 = _ist_epoch(2026, 1, 15, 9, 15)

    def _rate_limit_then_ok(request: httpx.Request) -> httpx.Response:
        rl_calls["n"] += 1
        if rl_calls["n"] <= 2:
            return httpx.Response(429, json={"s": "error", "code": -429, "message": "Rate limit exceeded"})
        # Return out-of-order float literals to verify parse_float=Decimal + chronological sort
        body = (
            '{"s":"ok","candles":['
            f"[{bar_t1},24255.15,24270.35,24250.05,24268.95,45100],"
            f"[{bar_t0},24240.10,24258.45,24238.05,24255.15,52300]"
            "]}"
        )
        return httpx.Response(200, content=body.encode("utf-8"))

    prov_rl = FyersHistoricalDataProvider(
        settings=cfg,
        http_transport=httpx.MockTransport(_rate_limit_then_ok),
        sleep_fn=lambda s: sleeps.append(s),
    )
    res = prov_rl.fetch_historical_candles(
        instrument="NIFTY",
        timeframe="5m",
        from_date=date(2026, 1, 15),
        to_date=date(2026, 1, 15),
        reference_now=datetime(2026, 1, 20, 12, 0, tzinfo=IST),
    )
    assert rl_calls["n"] == 3
    assert sleeps == [0.5, 1.0]
    assert len(res.candles) == 2
    assert res.candles[0].timestamp < res.candles[1].timestamp
    assert res.candles[0].open == Decimal("24240.10")
    assert res.candles[1].close == Decimal("24268.95")

    # Exhausting retry limit raises sanitized HistoricalProviderUnavailableError
    exhaust_calls = {"n": 0}

    def _always_429(request: httpx.Request) -> httpx.Response:
        exhaust_calls["n"] += 1
        return httpx.Response(429, json={"s": "error", "code": -429, "message": "Rate limit"})

    prov_exhaust = FyersHistoricalDataProvider(
        settings=AppSettings(FYERS_CLIENT_ID="DEMOAPP-100", FYERS_ACCESS_TOKEN=secret_jwt, FYERS_MAX_RETRIES=2),
        http_transport=httpx.MockTransport(_always_429),
        sleep_fn=lambda _s: None,
    )
    with pytest.raises(HistoricalProviderUnavailableError) as exh_exc:
        prov_exhaust.fetch_historical_candles(
            instrument="NIFTY",
            timeframe="5m",
            from_date=date(2026, 1, 15),
            to_date=date(2026, 1, 15),
            reference_now=datetime(2026, 1, 20, 12, 0, tzinfo=IST),
        )
    assert exhaust_calls["n"] == 3  # 1 initial + 2 retries
    assert exh_exc.value.code == "FYERS_RATE_LIMITED"

    # Empty response ("no_data") raises FYERS_NO_DATA_RETURNED
    prov_empty = FyersHistoricalDataProvider(
        settings=cfg,
        http_transport=httpx.MockTransport(
            lambda _r: httpx.Response(200, json={"s": "no_data", "candles": []})
        ),
        sleep_fn=lambda _s: None,
    )
    with pytest.raises(HistoricalDataError) as nodata_exc:
        prov_empty.fetch_historical_candles(
            instrument="NIFTY",
            timeframe="5m",
            from_date=date(2026, 1, 15),
            to_date=date(2026, 1, 15),
            reference_now=datetime(2026, 1, 20, 12, 0, tzinfo=IST),
        )
    assert nodata_exc.value.code == "FYERS_NO_DATA_RETURNED"


def test_csv_export_filename_and_round_trip_reimport() -> None:
    """Verify FYERS -> normalize -> export CSV -> re-upload CSV -> validate -> identical candles."""
    t0 = _ist_epoch(2026, 1, 15, 9, 15)
    t1 = _ist_epoch(2026, 1, 15, 9, 20)
    cfg = AppSettings(FYERS_CLIENT_ID="DEMOAPP-100", FYERS_ACCESS_TOKEN="valid-demo-tok")

    def _mock_fyers(_req: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            content=json.dumps(
                {
                    "s": "ok",
                    "candles": [
                        [t0, 51800.25, 51845.50, 51790.00, 51830.75, 18500],
                        [t1, 51830.75, 51860.00, 51815.25, 51852.10, 21400],
                    ],
                }
            ).encode("utf-8"),
        )

    prov = FyersHistoricalDataProvider(
        settings=cfg,
        http_transport=httpx.MockTransport(_mock_fyers),
        sleep_fn=lambda _s: None,
    )
    fetched = prov.fetch_historical_candles(
        instrument="BANKNIFTY",
        timeframe="15m",
        from_date=date(2026, 1, 15),
        to_date=date(2026, 1, 15),
        reference_now=datetime(2026, 1, 20, 12, 0, tzinfo=IST),
    )

    fname = build_deterministic_csv_filename(
        instrument="BANKNIFTY",
        timeframe="15m",
        provider="FYERS",
        start_date=date(2026, 1, 1),
        end_date=date(2026, 10, 31),
    )
    assert fname == "BANKNIFTY_15MIN_FYERS_2026-01-01_2026-10-31.csv"

    exported_csv = render_normalized_csv(fetched.candles)
    lines = exported_csv.strip().split("\n")
    assert lines[0] == "timestamp,open,high,low,close,volume"
    assert "+05:30," in lines[1]

    # Round-trip re-import via CsvHistoricalDataProvider without manual edits
    reimported = CsvHistoricalDataProvider().parse_and_validate_csv(
        csv_text=exported_csv,
        instrument="BANKNIFTY",
        timeframe="15m",
        reference_now=datetime(2026, 1, 20, 12, 0, tzinfo=IST),
    )
    assert reimported.quality.passed_validation is True
    assert len(reimported.candles) == len(fetched.candles)
    assert compute_candles_fingerprint(reimported.candles) == compute_candles_fingerprint(
        fetched.candles
    )
    for orig, copy in zip(fetched.candles, reimported.candles, strict=True):
        assert orig.timestamp == copy.timestamp
        assert orig.open == copy.open
        assert orig.high == copy.high
        assert orig.low == copy.low
        assert orig.close == copy.close
        assert orig.volume == copy.volume


def test_credential_scrubber_and_dataset_immutability(migrated_db_url: str) -> None:
    scrubbed = sanitize_provider_error_text(
        "Failed auth header Bearer abc.def.ghi and DEMO-100:eyJhbGciOiJIUzI1NiJ9.eyJ1IjoxfQ.sig?access_token=topsecret",
        extra_secrets=("MY_RAW_SECRET",),
    )
    assert "eyJhbGciOiJIUzI1NiJ9" not in scrubbed
    assert "topsecret" not in scrubbed
    assert "[REDACTED" in scrubbed

    with unit_of_work() as session:
        up = HistoricalDatasetService.upload_csv_dataset(
            session,
            name="Immutable Test Dataset",
            instrument="NIFTY",
            timeframe="5m",
            csv_text=(
                "timestamp,open,high,low,close,volume\n"
                "2026-01-15T09:15:00+05:30,24200.00,24215.00,24195.00,24210.00,10000\n"
            ),
        )
        ds_code = up["dataset"]["dataset_id"]

    with pytest.raises(ImmutabilityViolationError):
        with unit_of_work() as session:
            row = session.scalar(
                select(HistoricalDatasetModel).where(
                    HistoricalDatasetModel.dataset_code == ds_code
                )
            )
            assert row is not None
            row.data_version_hash = "0" * 64
            session.flush()

    with pytest.raises(ImmutabilityViolationError):
        with unit_of_work() as session:
            HistoricalDatasetService.delete_dataset(session, "BUNDLED_NIFTY_5M_SAMPLE")
