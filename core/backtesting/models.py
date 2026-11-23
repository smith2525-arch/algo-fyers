"""Domain value objects for historical data validation, backtest execution, and trade ledgers."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, datetime
from decimal import Decimal
from typing import Any
from uuid import UUID

from core.domain.enums import (
    BacktestStatus,
    ExitReason,
    PositionSizingMode,
    TradeDirection,
)
from core.strategies.base import OHLCVBar


@dataclass(frozen=True)
class RawCandleRow:
    """Unvalidated raw OHLCV row loaded by a HistoricalDataAdapter."""

    row_index: int
    timestamp_raw: Any
    open_raw: Any
    high_raw: Any
    low_raw: Any
    close_raw: Any
    volume_raw: Any = 0


@dataclass(frozen=True)
class DatasetDescriptor:
    """Metadata identifying a historical OHLCV dataset and its content hash."""

    dataset_id: str
    name: str
    instrument: str
    timeframe: str
    source_type: str  # FYERS_HISTORICAL | IMPORTED_CSV | BUNDLED_CSV
    data_version_hash: str
    row_count: int
    start_timestamp: datetime | None = None
    end_timestamp: datetime | None = None
    provider: str = "SYNTHETIC"
    provider_symbol: str | None = None
    quality_status: str = "VALID"
    is_complete: bool = True
    retrieved_at: datetime | None = None


@dataclass(frozen=True)
class RawCandleDataset:
    """Container returned by a HistoricalDataAdapter prior to validation."""

    descriptor: DatasetDescriptor
    rows: list[RawCandleRow]


@dataclass(frozen=True)
class DataQualityIssue:
    """Single validation error or warning recorded in a DataQualityReport."""

    severity: str  # ERROR | WARNING
    check_code: str
    message: str
    row_index: int | None = None
    timestamp: str | None = None


@dataclass(frozen=True)
class DataQualityReport:
    """Structured pre-backtest OHLCV quality assessment."""

    passed_validation: bool
    has_warnings: bool
    total_rows_inspected: int
    valid_candle_count: int
    null_value_count: int
    timezone_issue_count: int
    out_of_order_count: int
    duplicate_timestamp_count: int
    invalid_ohlc_count: int
    missing_candle_count: int
    unexpected_session_gap_count: int
    first_timestamp_ist: str | None
    last_timestamp_ist: str
    checked_timezone: str = "Asia/Kolkata"
    issues: list[DataQualityIssue] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {
            "passed_validation": self.passed_validation,
            "has_warnings": self.has_warnings,
            "total_rows_inspected": self.total_rows_inspected,
            "valid_candle_count": self.valid_candle_count,
            "null_value_count": self.null_value_count,
            "timezone_issue_count": self.timezone_issue_count,
            "out_of_order_count": self.out_of_order_count,
            "duplicate_timestamp_count": self.duplicate_timestamp_count,
            "invalid_ohlc_count": self.invalid_ohlc_count,
            "missing_candle_count": self.missing_candle_count,
            "unexpected_session_gap_count": self.unexpected_session_gap_count,
            "first_timestamp_ist": self.first_timestamp_ist,
            "last_timestamp_ist": self.last_timestamp_ist or None,
            "checked_timezone": self.checked_timezone,
            "issues": [
                {
                    "severity": i.severity,
                    "check_code": i.check_code,
                    "message": i.message,
                    "row_index": i.row_index,
                    "timestamp": i.timestamp,
                }
                for i in self.issues[:100]
            ],
        }


@dataclass(frozen=True)
class BacktestExecutionInputs:
    """Explicit runtime inputs & execution assumptions for a deterministic backtest."""

    strategy_version_id: UUID
    strategy_version_label: str
    instrument: str
    timeframe: str
    start_date: date | None
    end_date: date | None
    initial_capital: Decimal
    lot_size: int
    slippage_bps: Decimal
    estimated_charges_bps: Decimal
    per_order_fee_inr: Decimal
    position_sizing_mode: PositionSizingMode
    position_size_value: Decimal
    execution_delay_bars: int = 1


@dataclass(frozen=True)
class BacktestTradeRecord:
    """Explicit simulated trade ledger record with full cost attribution."""

    trade_id: UUID
    trade_number: int
    strategy_version_id: UUID
    strategy_version_label: str
    instrument: str
    direction: TradeDirection
    entry_signal_time: datetime
    entry_time: datetime
    raw_entry_price: Decimal
    entry_price: Decimal
    quantity: int
    exit_time: datetime
    raw_exit_price: Decimal
    exit_price: Decimal
    exit_reason: ExitReason
    gross_pnl: Decimal
    charges: Decimal
    slippage_cost: Decimal
    net_pnl: Decimal
    bars_held: int

    def to_dict(self) -> dict[str, Any]:
        from core.clock import format_ist_iso

        return {
            "trade_id": str(self.trade_id),
            "trade_number": self.trade_number,
            "strategy_version_id": str(self.strategy_version_id),
            "strategy_version_label": self.strategy_version_label,
            "instrument": self.instrument,
            "direction": self.direction.value,
            "entry_signal_time": format_ist_iso(self.entry_signal_time),
            "entry_time": format_ist_iso(self.entry_time),
            "raw_entry_price": str(self.raw_entry_price),
            "entry_price": str(self.entry_price),
            "quantity": self.quantity,
            "exit_time": format_ist_iso(self.exit_time),
            "raw_exit_price": str(self.raw_exit_price),
            "exit_price": str(self.exit_price),
            "exit_reason": self.exit_reason.value,
            "gross_pnl": str(self.gross_pnl),
            "charges": str(self.charges),
            "slippage_cost": str(self.slippage_cost),
            "net_pnl": str(self.net_pnl),
            "bars_held": self.bars_held,
        }


@dataclass(frozen=True)
class BacktestEngineOutput:
    """Complete deterministic output produced by BacktestEngine."""

    status: BacktestStatus
    engine_version: str
    data_quality_report: DataQualityReport
    validated_bars: list[OHLCVBar]
    trades: list[BacktestTradeRecord]
    metrics: dict[str, Any]
    equity_curve: list[dict[str, Any]]
    drawdown_curve: list[dict[str, Any]]
    monthly_returns: list[dict[str, Any]]
    unfilled_end_signals: int = 0
