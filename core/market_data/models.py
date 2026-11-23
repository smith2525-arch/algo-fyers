"""Authoritative normalized market-data models, instrument provider registry, and timeframe mappings.

Design Invariants:
- Provider-independent normalized candle & dataset representations (`Decimal` prices, IST timestamps).
- Single authoritative FYERS historical symbol mapping for `NIFTY` and `BANKNIFTY`.
- Single authoritative timeframe -> FYERS resolution & chunking specification.
- Strict separation of data-source labeling (`REAL HISTORICAL MARKET DATA`,
  `USER-UPLOADED HISTORICAL DATA`, `SYNTHETIC DEMO DATA`) from simulated execution
  (`EXECUTION: SIMULATED PAPER TRADING`).
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from decimal import Decimal
from enum import StrEnum
from typing import Any

from core.clock import ensure_ist, format_ist_iso
from core.exceptions import HistoricalDataError
from core.strategies.base import OHLCVBar


class DataSourceType(StrEnum):
    """Canonical historical dataset source classifications."""

    FYERS_HISTORICAL = "FYERS_HISTORICAL"
    IMPORTED_CSV = "IMPORTED_CSV"
    BUNDLED_CSV = "BUNDLED_CSV"


class HistoricalProviderName(StrEnum):
    """Canonical historical market-data provider identifiers."""

    FYERS = "FYERS"
    CSV = "CSV"
    SYNTHETIC = "SYNTHETIC"


class DataQualityStatus(StrEnum):
    """Canonical dataset validation & ingestion health states."""

    VALID = "VALID"
    VALID_WITH_WARNINGS = "VALID_WITH_WARNINGS"
    PARTIAL = "PARTIAL"
    INVALID = "INVALID"
    DOWNLOAD_FAILED = "DOWNLOAD_FAILED"
    AUTHENTICATION_REQUIRED = "AUTHENTICATION_REQUIRED"


@dataclass(frozen=True)
class DataSourceDisplayLabels:
    """Mandatory UI & Audit labels ensuring historical source & simulated execution are unmistakable."""

    category_code: str
    ui_headline: str
    ui_source_line: str | None
    ui_execution_line: str
    audit_provider: str
    audit_data_type: str
    audit_execution_type: str

    def to_dict(self) -> dict[str, Any]:
        return {
            "category_code": self.category_code,
            "ui_headline": self.ui_headline,
            "ui_source_line": self.ui_source_line,
            "ui_execution_line": self.ui_execution_line,
            "audit_provider": self.audit_provider,
            "audit_data_type": self.audit_data_type,
            "audit_execution_type": self.audit_execution_type,
        }


def classify_data_source(
    source_type: str | None, provider: str | None = None
) -> DataSourceDisplayLabels:
    """Return canonical UI & audit labeling for any historical dataset source."""
    norm_src = (source_type or "").strip().upper()
    norm_prov = (provider or "").strip().upper()

    if norm_src == DataSourceType.FYERS_HISTORICAL.value or norm_prov == HistoricalProviderName.FYERS.value:
        return DataSourceDisplayLabels(
            category_code="REAL_HISTORICAL_DATA",
            ui_headline="REAL HISTORICAL MARKET DATA",
            ui_source_line="SOURCE: FYERS",
            ui_execution_line="EXECUTION: SIMULATED PAPER TRADING",
            audit_provider="FYERS",
            audit_data_type="REAL HISTORICAL",
            audit_execution_type="SIMULATED PAPER",
        )

    if norm_src == DataSourceType.IMPORTED_CSV.value or norm_prov == HistoricalProviderName.CSV.value:
        return DataSourceDisplayLabels(
            category_code="USER_UPLOADED_CSV_DATA",
            ui_headline="USER-UPLOADED HISTORICAL DATA",
            ui_source_line=None,
            ui_execution_line="EXECUTION: SIMULATED PAPER TRADING",
            audit_provider="CSV",
            audit_data_type="USER UPLOADED HISTORICAL",
            audit_execution_type="SIMULATED PAPER",
        )

    return DataSourceDisplayLabels(
        category_code="SYNTHETIC_DEMO_DATA",
        ui_headline="SYNTHETIC DEMO DATA",
        ui_source_line=None,
        ui_execution_line="EXECUTION: SIMULATED PAPER TRADING",
        audit_provider="SYNTHETIC",
        audit_data_type="SYNTHETIC DEMO",
        audit_execution_type="SIMULATED PAPER",
    )


@dataclass(frozen=True)
class InstrumentProviderSpec:
    """Single authoritative instrument-to-provider mapping specification."""

    internal_symbol: str
    display_name: str
    data_provider: str
    provider_symbol: str
    exchange: str
    instrument_type: str


# Authoritative instrument mapping registry (used to seed & resolve InstrumentModel)
INSTRUMENT_PROVIDER_REGISTRY: dict[str, InstrumentProviderSpec] = {
    "NIFTY": InstrumentProviderSpec(
        internal_symbol="NIFTY",
        display_name="NIFTY 50 Index Paper Contract",
        data_provider=HistoricalProviderName.FYERS.value,
        provider_symbol="NSE:NIFTY50-INDEX",
        exchange="NSE",
        instrument_type="PAPER_INDEX_CONTRACT",
    ),
    "BANKNIFTY": InstrumentProviderSpec(
        internal_symbol="BANKNIFTY",
        display_name="NIFTY Bank Index Paper Contract",
        data_provider=HistoricalProviderName.FYERS.value,
        provider_symbol="NSE:NIFTYBANK-INDEX",
        exchange="NSE",
        instrument_type="PAPER_INDEX_CONTRACT",
    ),
}


def get_instrument_provider_spec(symbol: str) -> InstrumentProviderSpec:
    """Resolve authoritative provider symbol spec for a supported internal instrument code."""
    clean = (symbol or "").strip().upper()
    spec = INSTRUMENT_PROVIDER_REGISTRY.get(clean)
    if spec is None:
        raise HistoricalDataError(
            f"Unsupported instrument '{symbol}'. Supported instruments: {', '.join(INSTRUMENT_PROVIDER_REGISTRY)}.",
            code="INVALID_INSTRUMENT_SYMBOL",
        )
    return spec


@dataclass(frozen=True)
class TimeframeSpec:
    """Single authoritative timeframe specification and FYERS resolution mapping."""

    code: str
    display_name: str
    fyers_resolution: str
    csv_filename_tag: str
    bar_duration_minutes: int
    max_days_per_fyers_chunk: int
    is_intraday: bool

    def to_dict(self) -> dict[str, Any]:
        return {
            "code": self.code,
            "display_name": self.display_name,
            "fyers_resolution": self.fyers_resolution,
            "csv_filename_tag": self.csv_filename_tag,
            "bar_duration_minutes": self.bar_duration_minutes,
            "max_days_per_fyers_chunk": self.max_days_per_fyers_chunk,
            "is_intraday": self.is_intraday,
        }


# Official FYERS API v3 resolution limits:
# - Intraday (1..240 minutes): up to 100 calendar days per single request -> conservative 60–90 day chunks
# - Daily (1D): up to 366 calendar days per single request -> conservative 365 day chunks
TIMEFRAME_REGISTRY: dict[str, TimeframeSpec] = {
    "1m": TimeframeSpec(
        code="1m",
        display_name="1 minute",
        fyers_resolution="1",
        csv_filename_tag="1MIN",
        bar_duration_minutes=1,
        max_days_per_fyers_chunk=60,
        is_intraday=True,
    ),
    "3m": TimeframeSpec(
        code="3m",
        display_name="3 minutes",
        fyers_resolution="3",
        csv_filename_tag="3MIN",
        bar_duration_minutes=3,
        max_days_per_fyers_chunk=90,
        is_intraday=True,
    ),
    "5m": TimeframeSpec(
        code="5m",
        display_name="5 minutes",
        fyers_resolution="5",
        csv_filename_tag="5MIN",
        bar_duration_minutes=5,
        max_days_per_fyers_chunk=90,
        is_intraday=True,
    ),
    "10m": TimeframeSpec(
        code="10m",
        display_name="10 minutes",
        fyers_resolution="10",
        csv_filename_tag="10MIN",
        bar_duration_minutes=10,
        max_days_per_fyers_chunk=90,
        is_intraday=True,
    ),
    "15m": TimeframeSpec(
        code="15m",
        display_name="15 minutes",
        fyers_resolution="15",
        csv_filename_tag="15MIN",
        bar_duration_minutes=15,
        max_days_per_fyers_chunk=90,
        is_intraday=True,
    ),
    "30m": TimeframeSpec(
        code="30m",
        display_name="30 minutes",
        fyers_resolution="30",
        csv_filename_tag="30MIN",
        bar_duration_minutes=30,
        max_days_per_fyers_chunk=90,
        is_intraday=True,
    ),
    "60m": TimeframeSpec(
        code="60m",
        display_name="60 minutes",
        fyers_resolution="60",
        csv_filename_tag="60MIN",
        bar_duration_minutes=60,
        max_days_per_fyers_chunk=90,
        is_intraday=True,
    ),
    "1d": TimeframeSpec(
        code="1d",
        display_name="1 day",
        fyers_resolution="1D",
        csv_filename_tag="1DAY",
        bar_duration_minutes=1440,
        max_days_per_fyers_chunk=365,
        is_intraday=False,
    ),
}

# Friendly aliases accepted by API / UI / CSV import
_TIMEFRAME_ALIASES: dict[str, str] = {
    "1": "1m",
    "1m": "1m",
    "1min": "1m",
    "1 minute": "1m",
    "3": "3m",
    "3m": "3m",
    "3min": "3m",
    "3 minutes": "3m",
    "5": "5m",
    "5m": "5m",
    "5min": "5m",
    "5 minutes": "5m",
    "10": "10m",
    "10m": "10m",
    "10min": "10m",
    "10 minutes": "10m",
    "15": "15m",
    "15m": "15m",
    "15min": "15m",
    "15 minutes": "15m",
    "30": "30m",
    "30m": "30m",
    "30min": "30m",
    "30 minutes": "30m",
    "60": "60m",
    "60m": "60m",
    "60min": "60m",
    "1h": "60m",
    "60 minutes": "60m",
    "d": "1d",
    "1d": "1d",
    "1day": "1d",
    "1 day": "1d",
    "daily": "1d",
}


def get_timeframe_spec(timeframe: str) -> TimeframeSpec:
    """Resolve canonical TimeframeSpec or raise HistoricalDataError('INVALID_TIMEFRAME')."""
    key = _TIMEFRAME_ALIASES.get((timeframe or "").strip().lower())
    if key is None or key not in TIMEFRAME_REGISTRY:
        supported = ", ".join(TIMEFRAME_REGISTRY.keys())
        raise HistoricalDataError(
            f"Unsupported timeframe '{timeframe}'. Supported timeframes: {supported}.",
            code="INVALID_TIMEFRAME",
        )
    return TIMEFRAME_REGISTRY[key]


def list_supported_timeframes() -> list[TimeframeSpec]:
    """Return ordered list of all supported TimeframeSpecs."""
    return list(TIMEFRAME_REGISTRY.values())


@dataclass(frozen=True)
class NormalizedCandle:
    """Provider-independent normalized OHLCV candle with exact Decimal prices and IST timestamp."""

    timestamp: datetime
    open: Decimal
    high: Decimal
    low: Decimal
    close: Decimal
    volume: int
    instrument: str
    timeframe: str
    source: str
    provider_symbol: str | None = None
    retrieval_timestamp: datetime | None = None
    dataset_id: str | None = None

    def __post_init__(self) -> None:
        object.__setattr__(self, "timestamp", ensure_ist(self.timestamp))
        object.__setattr__(self, "instrument", self.instrument.strip().upper())

    def to_ohlcv_bar(self) -> OHLCVBar:
        return OHLCVBar(
            symbol=self.instrument,
            timeframe=self.timeframe,
            bar_timestamp=self.timestamp,
            open=self.open,
            high=self.high,
            low=self.low,
            close=self.close,
            volume=self.volume,
        )

    @staticmethod
    def _fmt_price(val: Decimal) -> str:
        # Preserve up to 4 fractional digits if sub-paisa ticks exist, otherwise canonical 2dp
        q4 = val.quantize(Decimal("0.0001"))
        q2 = val.quantize(Decimal("0.01"))
        return f"{q2:.2f}" if q4 == q2 else f"{q4:.4f}".rstrip("0").rstrip(".")

    def to_csv_row(self) -> dict[str, str]:
        """Serialize strictly into the 6 standard CSV columns (no DB IDs or provider secrets)."""
        ts_iso = ensure_ist(self.timestamp).isoformat(timespec="seconds")
        return {
            "timestamp": ts_iso,
            "open": self._fmt_price(self.open),
            "high": self._fmt_price(self.high),
            "low": self._fmt_price(self.low),
            "close": self._fmt_price(self.close),
            "volume": str(int(self.volume)),
        }


@dataclass(frozen=True)
class DatasetQualityResult:
    """Comprehensive 21-point data-quality validation result for a historical dataset."""

    quality_status: DataQualityStatus
    passed_validation: bool
    has_warnings: bool
    is_complete: bool
    total_rows_inspected: int
    valid_candle_count: int
    null_value_count: int
    timezone_issue_count: int
    out_of_order_count: int
    duplicate_timestamp_count: int
    exact_duplicate_removed_count: int
    conflicting_duplicate_count: int
    invalid_ohlc_count: int
    missing_candle_count: int
    unexpected_session_gap_count: int
    zero_volume_candle_count: int
    partial_current_candle_excluded: bool
    missing_intervals: list[dict[str, str]] = field(default_factory=list)
    first_timestamp_ist: str | None = None
    last_timestamp_ist: str | None = None
    requested_from_ist: str | None = None
    requested_to_ist: str | None = None
    checked_timezone: str = "Asia/Kolkata"
    issues: list[dict[str, Any]] = field(default_factory=list)

    def to_report_dict(self) -> dict[str, Any]:
        return {
            "quality_status": self.quality_status.value,
            "passed_validation": self.passed_validation,
            "has_warnings": self.has_warnings,
            "is_complete": self.is_complete,
            "total_rows_inspected": self.total_rows_inspected,
            "valid_candle_count": self.valid_candle_count,
            "null_value_count": self.null_value_count,
            "timezone_issue_count": self.timezone_issue_count,
            "out_of_order_count": self.out_of_order_count,
            "duplicate_timestamp_count": self.duplicate_timestamp_count,
            "exact_duplicate_removed_count": self.exact_duplicate_removed_count,
            "conflicting_duplicate_count": self.conflicting_duplicate_count,
            "invalid_ohlc_count": self.invalid_ohlc_count,
            "missing_candle_count": self.missing_candle_count,
            "unexpected_session_gap_count": self.unexpected_session_gap_count,
            "zero_volume_candle_count": self.zero_volume_candle_count,
            "partial_current_candle_excluded": self.partial_current_candle_excluded,
            "missing_intervals": list(self.missing_intervals),
            "first_timestamp_ist": self.first_timestamp_ist,
            "last_timestamp_ist": self.last_timestamp_ist,
            "requested_from_ist": self.requested_from_ist,
            "requested_to_ist": self.requested_to_ist,
            "checked_timezone": self.checked_timezone,
            "issues": list(self.issues[:100]),
        }


@dataclass(frozen=True)
class HistoricalDatasetMetadata:
    """Immutable metadata snapshot for a persisted or bundled historical OHLCV dataset."""

    dataset_id: str
    display_name: str
    instrument: str
    timeframe: str
    source_type: str
    provider: str
    provider_symbol: str | None
    quality_status: str
    is_complete: bool
    data_version_hash: str
    candle_count: int
    requested_from: datetime | None = None
    requested_to: datetime | None = None
    actual_from: datetime | None = None
    actual_to: datetime | None = None
    retrieved_at: datetime | None = None
    created_at: datetime | None = None
    created_by: str = "desk.operator"
    missing_candle_count: int = 0
    duplicate_candle_count: int = 0
    zero_volume_warning: bool = False
    referenced_backtests_count: int = 0
    referenced_replays_count: int = 0
    can_delete: bool = False
    delete_block_reason: str | None = None
    metadata: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        labels = classify_data_source(self.source_type, self.provider)
        tf_display = (
            TIMEFRAME_REGISTRY[self.timeframe].display_name
            if self.timeframe in TIMEFRAME_REGISTRY
            else self.timeframe
        )
        return {
            "dataset_id": self.dataset_id,
            "name": self.display_name,
            "display_name": self.display_name,
            "instrument": self.instrument,
            "timeframe": self.timeframe,
            "timeframe_display": tf_display,
            "source_type": self.source_type,
            "provider": self.provider,
            "provider_symbol": self.provider_symbol,
            "quality_status": self.quality_status,
            "is_complete": self.is_complete,
            "data_version_hash": self.data_version_hash,
            "row_count": self.candle_count,
            "candle_count": self.candle_count,
            "requested_from": format_ist_iso(self.requested_from) if self.requested_from else None,
            "requested_to": format_ist_iso(self.requested_to) if self.requested_to else None,
            "start_timestamp": format_ist_iso(self.actual_from) if self.actual_from else None,
            "end_timestamp": format_ist_iso(self.actual_to) if self.actual_to else None,
            "actual_from": format_ist_iso(self.actual_from) if self.actual_from else None,
            "actual_to": format_ist_iso(self.actual_to) if self.actual_to else None,
            "retrieved_at": format_ist_iso(self.retrieved_at) if self.retrieved_at else None,
            "created_at": format_ist_iso(self.created_at) if self.created_at else None,
            "created_by": self.created_by,
            "missing_candle_count": self.missing_candle_count,
            "duplicate_candle_count": self.duplicate_candle_count,
            "zero_volume_warning": self.zero_volume_warning,
            "referenced_backtests_count": self.referenced_backtests_count,
            "referenced_replays_count": self.referenced_replays_count,
            "can_delete": self.can_delete,
            "delete_block_reason": self.delete_block_reason,
            "labels": labels.to_dict(),
            "metadata": dict(self.metadata),
        }

