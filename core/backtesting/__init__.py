"""Deterministic Backtesting subsystem (Data Adapters, Validator, Engine, Metrics, Service)."""

from core.backtesting.data_adapter import (
    BundledCsvDataAdapter,
    CompositeHistoricalDataAdapter,
    DatabaseCsvDataAdapter,
    HistoricalDataAdapter,
    compute_canonical_csv_sha256,
    parse_csv_text_to_raw_rows,
)
from core.backtesting.engine import BACKTEST_ENGINE_VERSION, BacktestEngine
from core.backtesting.metrics import compute_backtest_analytics
from core.backtesting.models import (
    BacktestEngineOutput,
    BacktestExecutionInputs,
    BacktestTradeRecord,
    DataQualityIssue,
    DataQualityReport,
    DatasetDescriptor,
    RawCandleDataset,
    RawCandleRow,
)
from core.backtesting.validator import validate_ohlcv_rows
from core.backtesting.service import BacktestService

__all__ = [
    "BACKTEST_ENGINE_VERSION",
    "BacktestEngine",
    "BacktestEngineOutput",
    "BacktestExecutionInputs",
    "BacktestService",
    "BacktestTradeRecord",
    "BundledCsvDataAdapter",
    "CompositeHistoricalDataAdapter",
    "DataQualityIssue",
    "DataQualityReport",
    "DatabaseCsvDataAdapter",
    "DatasetDescriptor",
    "HistoricalDataAdapter",
    "RawCandleDataset",
    "RawCandleRow",
    "compute_backtest_analytics",
    "compute_canonical_csv_sha256",
    "parse_csv_text_to_raw_rows",
    "validate_ohlcv_rows",
]
