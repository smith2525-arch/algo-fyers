"""SQLAlchemy 2.0 ORM models for Phase 1, Phase 2A, and Phase 2B entities.

Tables:
- users
- paper_accounts
- instruments
- strategy_definitions
- strategy_versions (immutable)
- historical_datasets
- historical_candles
- backtest_runs (immutable)
- backtest_trades (immutable)
- replay_sessions
- signals
- audit_events (append-only)
- system_health_events (append-only)
"""

from __future__ import annotations

import uuid
from datetime import datetime
from decimal import Decimal
from typing import Any

from sqlalchemy import (
    JSON,
    BigInteger,
    Boolean,
    CheckConstraint,
    ForeignKey,
    Index,
    Integer,
    Numeric,
    String,
    Text,
    UniqueConstraint,
    Uuid,
    event,
    inspect,
)
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship

from core.clock import now_ist
from core.domain.enums import (
    AccountDesignation,
    ActorType,
    AuditCategory,
    AuditSeverity,
    OrderSource,
    OrderStatus,
    OrderType,
    PaperAccountStatus,
    PositionStatus,
    ReplaySessionStatus,
    ReplaySpeed,
    SignalStatus,
    StrategyInstanceStatus,
    StrategyVersionStatus,
    SystemComponent,
    SystemComponentStatus,
    UserRole,
)
from core.exceptions import ImmutabilityViolationError
from core.persistence.types import ISTDateTime


class Base(DeclarativeBase):
    """Declarative base for all ORM models."""


class UserModel(Base):
    """Control center operator / user identity."""

    __tablename__ = "users"

    id: Mapped[uuid.UUID] = mapped_column(Uuid(as_uuid=True), primary_key=True, default=uuid.uuid4)
    username: Mapped[str] = mapped_column(String(64), unique=True, nullable=False, index=True)
    email: Mapped[str] = mapped_column(String(255), unique=True, nullable=False)
    display_name: Mapped[str] = mapped_column(String(128), nullable=False)
    role: Mapped[str] = mapped_column(String(32), nullable=False, default=UserRole.OPERATOR.value)
    is_active: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    created_at: Mapped[datetime] = mapped_column(ISTDateTime(), nullable=False, default=now_ist)
    updated_at: Mapped[datetime] = mapped_column(
        ISTDateTime(), nullable=False, default=now_ist, onupdate=now_ist
    )

    paper_accounts: Mapped[list[PaperAccountModel]] = relationship(back_populates="owner")


class PaperAccountModel(Base):
    """Demonstration / Paper Trading capital book and operational halt state.

    IMPORTANT SAFETY RULE:
    `paper_trading_halted` backs STOP PAPER TRADING. When True, new order
    generation MUST be blocked while existing open positions remain untouched
    until an explicit separate flatten command is issued.
    """

    __tablename__ = "paper_accounts"
    __table_args__ = (
        CheckConstraint("initial_capital >= 0", name="ck_paper_accounts_initial_capital_nonneg"),
        CheckConstraint("is_demo_capital = true", name="ck_paper_accounts_demo_only_v1"),
    )

    id: Mapped[uuid.UUID] = mapped_column(Uuid(as_uuid=True), primary_key=True, default=uuid.uuid4)
    account_code: Mapped[str] = mapped_column(String(64), unique=True, nullable=False, index=True)
    account_name: Mapped[str] = mapped_column(String(128), nullable=False)
    owner_user_id: Mapped[uuid.UUID | None] = mapped_column(
        Uuid(as_uuid=True), ForeignKey("users.id", ondelete="SET NULL"), nullable=True
    )
    is_demo_capital: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    account_designation: Mapped[str] = mapped_column(
        String(32), nullable=False, default=AccountDesignation.DEMO_PAPER_ONLY.value
    )
    currency: Mapped[str] = mapped_column(String(8), nullable=False, default="INR")
    initial_capital: Mapped[Decimal] = mapped_column(
        Numeric(18, 4), nullable=False, default=Decimal("500000.0000")
    )
    available_capital: Mapped[Decimal] = mapped_column(
        Numeric(18, 4), nullable=False, default=Decimal("500000.0000")
    )
    realized_pnl_today: Mapped[Decimal] = mapped_column(
        Numeric(18, 4), nullable=False, default=Decimal("0.0000")
    )
    unrealized_pnl: Mapped[Decimal] = mapped_column(
        Numeric(18, 4), nullable=False, default=Decimal("0.0000")
    )
    paper_trading_halted: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    halt_reason: Mapped[str | None] = mapped_column(Text, nullable=True)
    halted_at: Mapped[datetime | None] = mapped_column(ISTDateTime(), nullable=True)
    halted_by: Mapped[str | None] = mapped_column(String(128), nullable=True)
    status: Mapped[str] = mapped_column(
        String(32), nullable=False, default=PaperAccountStatus.ACTIVE.value
    )
    version: Mapped[int] = mapped_column(Integer, nullable=False, default=1)
    created_at: Mapped[datetime] = mapped_column(ISTDateTime(), nullable=False, default=now_ist)
    updated_at: Mapped[datetime] = mapped_column(
        ISTDateTime(), nullable=False, default=now_ist, onupdate=now_ist
    )

    owner: Mapped[UserModel | None] = relationship(back_populates="paper_accounts")


class InstrumentModel(Base):
    """Configurable reference data for tradeable paper instruments (NIFTY, BANKNIFTY).

    Lot sizes, tick sizes, exchange codes, and IST session hours live here as
    configurable reference records rather than hard-coded trading constants.
    """

    __tablename__ = "instruments"
    __table_args__ = (
        CheckConstraint("lot_size >= 1", name="ck_instruments_lot_size_positive"),
        CheckConstraint("tick_size > 0", name="ck_instruments_tick_size_positive"),
    )

    id: Mapped[uuid.UUID] = mapped_column(Uuid(as_uuid=True), primary_key=True, default=uuid.uuid4)
    symbol: Mapped[str] = mapped_column(String(32), unique=True, nullable=False, index=True)
    display_name: Mapped[str] = mapped_column(String(128), nullable=False)
    exchange: Mapped[str] = mapped_column(String(32), nullable=False)
    segment: Mapped[str] = mapped_column(String(64), nullable=False)
    instrument_type: Mapped[str] = mapped_column(String(64), nullable=False)
    data_provider: Mapped[str] = mapped_column(String(32), nullable=False, default="FYERS")
    provider_symbol: Mapped[str] = mapped_column(String(64), nullable=False, default="")
    currency: Mapped[str] = mapped_column(String(8), nullable=False, default="INR")
    lot_size: Mapped[int] = mapped_column(Integer, nullable=False)
    tick_size: Mapped[Decimal] = mapped_column(Numeric(10, 4), nullable=False)
    session_open_ist: Mapped[str] = mapped_column(String(8), nullable=False, default="09:15:00")
    session_close_ist: Mapped[str] = mapped_column(String(8), nullable=False, default="15:30:00")
    metadata_json: Mapped[dict[str, Any]] = mapped_column(JSON, nullable=False, default=dict)
    is_active: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    created_at: Mapped[datetime] = mapped_column(ISTDateTime(), nullable=False, default=now_ist)
    updated_at: Mapped[datetime] = mapped_column(
        ISTDateTime(), nullable=False, default=now_ist, onupdate=now_ist
    )


class StrategyDefinitionModel(Base):
    """Strategy family metadata and parameter validation schema."""

    __tablename__ = "strategy_definitions"

    id: Mapped[uuid.UUID] = mapped_column(Uuid(as_uuid=True), primary_key=True, default=uuid.uuid4)
    code: Mapped[str] = mapped_column(String(64), unique=True, nullable=False, index=True)
    name: Mapped[str] = mapped_column(String(128), nullable=False)
    category: Mapped[str] = mapped_column(String(64), nullable=False)
    description: Mapped[str] = mapped_column(Text, nullable=False)
    parameter_schema_json: Mapped[dict[str, Any]] = mapped_column(JSON, nullable=False, default=dict)
    default_parameters_json: Mapped[dict[str, Any]] = mapped_column(
        JSON, nullable=False, default=dict
    )
    supported_symbols_json: Mapped[list[str]] = mapped_column(JSON, nullable=False, default=list)
    is_enabled: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    created_at: Mapped[datetime] = mapped_column(ISTDateTime(), nullable=False, default=now_ist)
    updated_at: Mapped[datetime] = mapped_column(
        ISTDateTime(), nullable=False, default=now_ist, onupdate=now_ist
    )

    versions: Mapped[list[StrategyVersionModel]] = relationship(
        back_populates="definition",
        order_by="StrategyVersionModel.version_number.desc()",
    )


class StrategyVersionModel(Base):
    """Immutable parameter snapshot for a StrategyDefinition.

    Parameter dictionaries, version labels, hashes, lineage, and version numbers
    MUST NEVER be mutated once created; only `status` may transition
    (e.g. DRAFT -> VALIDATED -> RETIRED).
    """

    __tablename__ = "strategy_versions"
    __table_args__ = (
        UniqueConstraint(
            "strategy_definition_id",
            "instrument_symbol",
            "version_number",
            name="uq_strategy_versions_def_inst_ver",
        ),
        UniqueConstraint("version_label", name="uq_strategy_versions_label"),
    )

    id: Mapped[uuid.UUID] = mapped_column(Uuid(as_uuid=True), primary_key=True, default=uuid.uuid4)
    strategy_definition_id: Mapped[uuid.UUID] = mapped_column(
        Uuid(as_uuid=True),
        ForeignKey("strategy_definitions.id", ondelete="RESTRICT"),
        nullable=False,
        index=True,
    )
    version_number: Mapped[int] = mapped_column(Integer, nullable=False)
    version_label: Mapped[str] = mapped_column(String(96), nullable=False, unique=True, index=True)
    instrument_symbol: Mapped[str] = mapped_column(String(32), nullable=False, index=True)
    timeframe: Mapped[str] = mapped_column(String(16), nullable=False, default="5m")
    cloned_from_version_id: Mapped[uuid.UUID | None] = mapped_column(
        Uuid(as_uuid=True),
        ForeignKey("strategy_versions.id", ondelete="RESTRICT"),
        nullable=True,
    )
    parameters_json: Mapped[dict[str, Any]] = mapped_column(JSON, nullable=False)
    parameter_hash: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    notes: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_by: Mapped[str] = mapped_column(String(128), nullable=False)
    status: Mapped[str] = mapped_column(
        String(32), nullable=False, default=StrategyVersionStatus.VALIDATED.value
    )
    created_at: Mapped[datetime] = mapped_column(ISTDateTime(), nullable=False, default=now_ist)

    definition: Mapped[StrategyDefinitionModel] = relationship(back_populates="versions")
    cloned_from: Mapped[StrategyVersionModel | None] = relationship(remote_side=[id])


class HistoricalDatasetModel(Base):
    """Persisted historical OHLCV dataset (FYERS real historical or uploaded CSV) for stateless execution."""

    __tablename__ = "historical_datasets"
    __table_args__ = (
        Index("ix_historical_datasets_inst_tf", "instrument_symbol", "timeframe"),
        Index("ix_historical_datasets_source_quality", "source_type", "quality_status"),
    )

    id: Mapped[uuid.UUID] = mapped_column(Uuid(as_uuid=True), primary_key=True, default=uuid.uuid4)
    dataset_code: Mapped[str] = mapped_column(String(96), unique=True, nullable=False, index=True)
    name: Mapped[str] = mapped_column(String(160), nullable=False)
    instrument_id: Mapped[uuid.UUID | None] = mapped_column(
        Uuid(as_uuid=True),
        ForeignKey("instruments.id", ondelete="RESTRICT"),
        nullable=True,
        index=True,
    )
    instrument_symbol: Mapped[str] = mapped_column(String(32), nullable=False, index=True)
    provider: Mapped[str] = mapped_column(String(32), nullable=False, default="CSV")
    provider_symbol: Mapped[str | None] = mapped_column(String(64), nullable=True)
    timeframe: Mapped[str] = mapped_column(String(16), nullable=False)
    source_type: Mapped[str] = mapped_column(String(32), nullable=False, default="IMPORTED_CSV")
    quality_status: Mapped[str] = mapped_column(String(32), nullable=False, default="VALID")
    is_complete: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    data_version_hash: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    row_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    requested_from: Mapped[datetime | None] = mapped_column(ISTDateTime(), nullable=True)
    requested_to: Mapped[datetime | None] = mapped_column(ISTDateTime(), nullable=True)
    start_timestamp: Mapped[datetime | None] = mapped_column(ISTDateTime(), nullable=True)
    end_timestamp: Mapped[datetime | None] = mapped_column(ISTDateTime(), nullable=True)
    retrieved_at: Mapped[datetime | None] = mapped_column(ISTDateTime(), nullable=True)
    raw_csv_text: Mapped[str] = mapped_column(Text, nullable=False, default="")
    data_quality_report_json: Mapped[dict[str, Any] | None] = mapped_column(JSON, nullable=True)
    metadata_json: Mapped[dict[str, Any] | None] = mapped_column(JSON, nullable=True)
    uploaded_by: Mapped[str] = mapped_column(String(128), nullable=False, default="desk.operator")
    created_at: Mapped[datetime] = mapped_column(ISTDateTime(), nullable=False, default=now_ist)

    candles: Mapped[list[HistoricalCandleModel]] = relationship(
        back_populates="dataset",
        cascade="all, delete-orphan",
        order_by="HistoricalCandleModel.bar_timestamp.asc()",
    )


class HistoricalCandleModel(Base):
    """Normalized IST OHLCV candle persisted for validated historical datasets."""

    __tablename__ = "historical_candles"
    __table_args__ = (
        UniqueConstraint(
            "dataset_id",
            "bar_timestamp",
            name="uq_historical_candles_dataset_ts",
        ),
        Index("ix_historical_candles_symbol_tf_ts", "instrument_symbol", "timeframe", "bar_timestamp"),
    )

    id: Mapped[uuid.UUID] = mapped_column(Uuid(as_uuid=True), primary_key=True, default=uuid.uuid4)
    dataset_id: Mapped[uuid.UUID] = mapped_column(
        Uuid(as_uuid=True),
        ForeignKey("historical_datasets.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    instrument_symbol: Mapped[str] = mapped_column(String(32), nullable=False)
    timeframe: Mapped[str] = mapped_column(String(16), nullable=False)
    bar_timestamp: Mapped[datetime] = mapped_column(ISTDateTime(), nullable=False)
    open: Mapped[Decimal] = mapped_column(Numeric(18, 4), nullable=False)
    high: Mapped[Decimal] = mapped_column(Numeric(18, 4), nullable=False)
    low: Mapped[Decimal] = mapped_column(Numeric(18, 4), nullable=False)
    close: Mapped[Decimal] = mapped_column(Numeric(18, 4), nullable=False)
    volume: Mapped[int] = mapped_column(BigInteger, nullable=False, default=0)

    dataset: Mapped[HistoricalDatasetModel] = relationship(back_populates="candles")


class HistoricalDownloadRunModel(Base):
    """Operational telemetry & sanitized status record for a historical data download run."""

    __tablename__ = "historical_download_runs"
    __table_args__ = (
        Index("ix_historical_download_runs_started_desc", "started_at"),
        Index("ix_historical_download_runs_status", "status"),
    )

    id: Mapped[uuid.UUID] = mapped_column(Uuid(as_uuid=True), primary_key=True, default=uuid.uuid4)
    run_code: Mapped[str] = mapped_column(String(64), unique=True, nullable=False, index=True)
    provider: Mapped[str] = mapped_column(String(32), nullable=False, default="FYERS")
    instrument_symbol: Mapped[str] = mapped_column(String(32), nullable=False, index=True)
    provider_symbol: Mapped[str] = mapped_column(String(64), nullable=False)
    timeframe: Mapped[str] = mapped_column(String(16), nullable=False)
    fyers_resolution: Mapped[str] = mapped_column(String(16), nullable=False)
    requested_from: Mapped[datetime] = mapped_column(ISTDateTime(), nullable=False)
    requested_to: Mapped[datetime] = mapped_column(ISTDateTime(), nullable=False)
    status: Mapped[str] = mapped_column(String(32), nullable=False, default="REQUESTED")
    quality_status: Mapped[str] = mapped_column(String(32), nullable=False, default="VALID")
    dataset_id: Mapped[uuid.UUID | None] = mapped_column(
        Uuid(as_uuid=True),
        ForeignKey("historical_datasets.id", ondelete="SET NULL"),
        nullable=True,
        index=True,
    )
    dataset_code: Mapped[str | None] = mapped_column(String(96), nullable=True)
    chunks_total: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    chunks_succeeded: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    chunks_failed: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    candles_fetched: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    candles_stored: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    error_code: Mapped[str | None] = mapped_column(String(64), nullable=True)
    error_message: Mapped[str | None] = mapped_column(Text, nullable=True)
    details_json: Mapped[dict[str, Any]] = mapped_column(JSON, nullable=False, default=dict)
    actor: Mapped[str] = mapped_column(String(128), nullable=False, default="desk.operator")
    correlation_id: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    started_at: Mapped[datetime] = mapped_column(ISTDateTime(), nullable=False, default=now_ist)
    completed_at: Mapped[datetime | None] = mapped_column(ISTDateTime(), nullable=True)


class BacktestRunModel(Base):
    """Immutable deterministic backtest execution record with full lineage & reproducibility."""

    __tablename__ = "backtest_runs"
    __table_args__ = (
        Index("ix_backtest_runs_started_at_desc", "started_at"),
        Index("ix_backtest_runs_strategy_status", "strategy_version_id", "status"),
    )

    id: Mapped[uuid.UUID] = mapped_column(Uuid(as_uuid=True), primary_key=True, default=uuid.uuid4)
    run_code: Mapped[str] = mapped_column(String(64), unique=True, nullable=False, index=True)
    strategy_version_id: Mapped[uuid.UUID] = mapped_column(
        Uuid(as_uuid=True),
        ForeignKey("strategy_versions.id", ondelete="RESTRICT"),
        nullable=False,
        index=True,
    )
    strategy_version_label: Mapped[str] = mapped_column(String(96), nullable=False)
    instrument_symbol: Mapped[str] = mapped_column(String(32), nullable=False, index=True)
    timeframe: Mapped[str] = mapped_column(String(16), nullable=False)
    dataset_id: Mapped[str] = mapped_column(String(128), nullable=False)
    dataset_name: Mapped[str] = mapped_column(String(160), nullable=False)
    dataset_source: Mapped[str] = mapped_column(String(32), nullable=False)
    data_version_hash: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    reproducibility_fingerprint: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    input_parameters_json: Mapped[dict[str, Any]] = mapped_column(JSON, nullable=False)
    execution_assumptions_json: Mapped[dict[str, Any]] = mapped_column(JSON, nullable=False)
    data_quality_report_json: Mapped[dict[str, Any]] = mapped_column(JSON, nullable=False)
    metrics_json: Mapped[dict[str, Any]] = mapped_column(JSON, nullable=False)
    equity_curve_json: Mapped[list[dict[str, Any]]] = mapped_column(JSON, nullable=False, default=list)
    drawdown_curve_json: Mapped[list[dict[str, Any]]] = mapped_column(
        JSON, nullable=False, default=list
    )
    monthly_returns_json: Mapped[list[dict[str, Any]]] = mapped_column(
        JSON, nullable=False, default=list
    )
    data_start_timestamp: Mapped[datetime | None] = mapped_column(ISTDateTime(), nullable=True)
    data_end_timestamp: Mapped[datetime | None] = mapped_column(ISTDateTime(), nullable=True)
    started_at: Mapped[datetime] = mapped_column(ISTDateTime(), nullable=False, default=now_ist)
    completed_at: Mapped[datetime] = mapped_column(ISTDateTime(), nullable=False, default=now_ist)
    status: Mapped[str] = mapped_column(String(32), nullable=False, index=True)
    engine_version: Mapped[str] = mapped_column(String(64), nullable=False)
    created_by: Mapped[str] = mapped_column(String(128), nullable=False, default="desk.operator")
    correlation_id: Mapped[str] = mapped_column(String(64), nullable=False, index=True)

    strategy_version: Mapped[StrategyVersionModel] = relationship()
    trades: Mapped[list[BacktestTradeModel]] = relationship(
        back_populates="backtest_run",
        cascade="all, delete-orphan",
        order_by="BacktestTradeModel.trade_number.asc()",
    )


class BacktestTradeModel(Base):
    """Immutable simulated trade ledger row belonging to a BacktestRunModel."""

    __tablename__ = "backtest_trades"
    __table_args__ = (
        UniqueConstraint(
            "backtest_run_id",
            "trade_number",
            name="uq_backtest_trades_run_num",
        ),
        Index("ix_backtest_trades_run_entry", "backtest_run_id", "entry_time"),
    )

    id: Mapped[uuid.UUID] = mapped_column(Uuid(as_uuid=True), primary_key=True, default=uuid.uuid4)
    backtest_run_id: Mapped[uuid.UUID] = mapped_column(
        Uuid(as_uuid=True),
        ForeignKey("backtest_runs.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    deterministic_trade_id: Mapped[uuid.UUID] = mapped_column(
        Uuid(as_uuid=True), nullable=False, index=True
    )
    trade_number: Mapped[int] = mapped_column(Integer, nullable=False)
    strategy_version_id: Mapped[uuid.UUID] = mapped_column(
        Uuid(as_uuid=True),
        ForeignKey("strategy_versions.id", ondelete="RESTRICT"),
        nullable=False,
    )
    strategy_version_label: Mapped[str] = mapped_column(String(96), nullable=False)
    instrument_symbol: Mapped[str] = mapped_column(String(32), nullable=False)
    direction: Mapped[str] = mapped_column(String(16), nullable=False)
    entry_signal_time: Mapped[datetime] = mapped_column(ISTDateTime(), nullable=False)
    entry_time: Mapped[datetime] = mapped_column(ISTDateTime(), nullable=False)
    raw_entry_price: Mapped[Decimal] = mapped_column(Numeric(18, 4), nullable=False)
    entry_price: Mapped[Decimal] = mapped_column(Numeric(18, 4), nullable=False)
    quantity: Mapped[int] = mapped_column(Integer, nullable=False)
    exit_time: Mapped[datetime] = mapped_column(ISTDateTime(), nullable=False)
    raw_exit_price: Mapped[Decimal] = mapped_column(Numeric(18, 4), nullable=False)
    exit_price: Mapped[Decimal] = mapped_column(Numeric(18, 4), nullable=False)
    exit_reason: Mapped[str] = mapped_column(String(32), nullable=False)
    gross_pnl: Mapped[Decimal] = mapped_column(Numeric(18, 4), nullable=False)
    charges: Mapped[Decimal] = mapped_column(Numeric(18, 4), nullable=False)
    slippage_cost: Mapped[Decimal] = mapped_column(Numeric(18, 4), nullable=False)
    net_pnl: Mapped[Decimal] = mapped_column(Numeric(18, 4), nullable=False)
    bars_held: Mapped[int] = mapped_column(Integer, nullable=False, default=1)

    backtest_run: Mapped[BacktestRunModel] = relationship(back_populates="trades")


class ReplaySessionModel(Base):
    """Persisted Market Replay session state enabling stateless Cloud Run control & recovery."""

    __tablename__ = "replay_sessions"
    __table_args__ = (
        Index("ix_replay_sessions_status_updated", "status", "updated_at"),
        Index("ix_replay_sessions_inst_status", "instrument_symbol", "status"),
    )

    id: Mapped[uuid.UUID] = mapped_column(Uuid(as_uuid=True), primary_key=True, default=uuid.uuid4)
    session_code: Mapped[str] = mapped_column(String(64), unique=True, nullable=False, index=True)
    instrument_symbol: Mapped[str] = mapped_column(String(32), nullable=False, index=True)
    timeframe: Mapped[str] = mapped_column(String(16), nullable=False, default="5m")
    strategy_version_id: Mapped[uuid.UUID] = mapped_column(
        Uuid(as_uuid=True),
        ForeignKey("strategy_versions.id", ondelete="RESTRICT"),
        nullable=False,
        index=True,
    )
    strategy_version_label: Mapped[str] = mapped_column(String(96), nullable=False)
    dataset_id: Mapped[str] = mapped_column(String(128), nullable=False)
    dataset_name: Mapped[str] = mapped_column(String(160), nullable=False)
    dataset_source: Mapped[str] = mapped_column(
        String(32), nullable=False, default="BUNDLED_CSV"
    )
    dataset_provider: Mapped[str] = mapped_column(
        String(32), nullable=False, default="SYNTHETIC"
    )
    data_version_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    strategy_instance_id: Mapped[uuid.UUID | None] = mapped_column(
        Uuid(as_uuid=True),
        ForeignKey("strategy_instances.id", ondelete="SET NULL"),
        nullable=True,
    )
    correlation_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    paper_trading_halted_snapshot: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=False
    )
    historical_session_date: Mapped[str] = mapped_column(
        String(32), nullable=False, default="ALL_SESSIONS"
    )
    warmup_bars_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    total_session_candles: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    candles_processed: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    signals_emitted: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    replay_speed: Mapped[str] = mapped_column(
        String(16), nullable=False, default=ReplaySpeed.X1.value
    )
    status: Mapped[str] = mapped_column(
        String(32), nullable=False, default=ReplaySessionStatus.CONFIGURED.value, index=True
    )
    stale_threshold_seconds: Mapped[int] = mapped_column(Integer, nullable=False, default=15)
    replay_clock_ist: Mapped[datetime | None] = mapped_column(ISTDateTime(), nullable=True)
    last_candle_received_at: Mapped[datetime | None] = mapped_column(ISTDateTime(), nullable=True)
    last_step_wall_clock: Mapped[datetime | None] = mapped_column(ISTDateTime(), nullable=True)
    fractional_bar_carry: Mapped[Decimal] = mapped_column(
        Numeric(12, 6), nullable=False, default=Decimal("0")
    )
    last_candle_json: Mapped[dict[str, Any] | None] = mapped_column(JSON, nullable=True)
    latest_evaluation_json: Mapped[dict[str, Any] | None] = mapped_column(JSON, nullable=True)
    latest_signal_json: Mapped[dict[str, Any] | None] = mapped_column(JSON, nullable=True)
    started_at: Mapped[datetime | None] = mapped_column(ISTDateTime(), nullable=True)
    paused_at: Mapped[datetime | None] = mapped_column(ISTDateTime(), nullable=True)
    stopped_at: Mapped[datetime | None] = mapped_column(ISTDateTime(), nullable=True)
    completed_at: Mapped[datetime | None] = mapped_column(ISTDateTime(), nullable=True)
    created_by: Mapped[str] = mapped_column(String(128), nullable=False, default="desk.operator")
    created_at: Mapped[datetime] = mapped_column(ISTDateTime(), nullable=False, default=now_ist)
    updated_at: Mapped[datetime] = mapped_column(
        ISTDateTime(), nullable=False, default=now_ist, onupdate=now_ist
    )

    strategy_version: Mapped[StrategyVersionModel] = relationship()
    signals: Mapped[list[ReplaySignalModel]] = relationship(
        back_populates="replay_session",
        order_by="ReplaySignalModel.emitted_at.desc()",
    )


class StrategyInstanceModel(Base):
    """Persisted Paper Trading strategy instance binding an immutable StrategyVersion to live/replay execution."""

    __tablename__ = "strategy_instances"
    __table_args__ = (
        Index("ix_strategy_instances_inst_status", "instrument_symbol", "status"),
    )

    id: Mapped[uuid.UUID] = mapped_column(Uuid(as_uuid=True), primary_key=True, default=uuid.uuid4)
    instance_code: Mapped[str] = mapped_column(String(64), unique=True, nullable=False, index=True)
    account_id: Mapped[uuid.UUID] = mapped_column(
        Uuid(as_uuid=True),
        ForeignKey("paper_accounts.id", ondelete="RESTRICT"),
        nullable=False,
        index=True,
    )
    strategy_version_id: Mapped[uuid.UUID] = mapped_column(
        Uuid(as_uuid=True),
        ForeignKey("strategy_versions.id", ondelete="RESTRICT"),
        nullable=False,
        index=True,
    )
    strategy_version_label: Mapped[str] = mapped_column(String(96), nullable=False)
    instrument_symbol: Mapped[str] = mapped_column(String(32), nullable=False, index=True)
    timeframe: Mapped[str] = mapped_column(String(16), nullable=False, default="5m")
    status: Mapped[str] = mapped_column(
        String(32), nullable=False, default=StrategyInstanceStatus.RUNNING.value, index=True
    )
    order_type: Mapped[str] = mapped_column(
        String(16), nullable=False, default=OrderType.MARKET.value
    )
    limit_offset_pts: Mapped[Decimal] = mapped_column(
        Numeric(18, 4), nullable=False, default=Decimal("0")
    )
    execution_latency_ms: Mapped[int] = mapped_column(Integer, nullable=False, default=120)
    latency_bars: Mapped[int] = mapped_column(Integer, nullable=False, default=1)
    slippage_bps: Mapped[Decimal] = mapped_column(
        Numeric(10, 4), nullable=False, default=Decimal("2.0")
    )
    partial_fill_enabled: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    max_fill_ratio_per_bar: Mapped[Decimal] = mapped_column(
        Numeric(6, 4), nullable=False, default=Decimal("1.0")
    )
    valid_for_bars: Mapped[int | None] = mapped_column(Integer, nullable=True)
    auto_execute_signals: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    signals_emitted: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    orders_created: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    created_by: Mapped[str] = mapped_column(String(128), nullable=False, default="desk.operator")
    started_at: Mapped[datetime | None] = mapped_column(ISTDateTime(), nullable=True)
    paused_at: Mapped[datetime | None] = mapped_column(ISTDateTime(), nullable=True)
    stopped_at: Mapped[datetime | None] = mapped_column(ISTDateTime(), nullable=True)
    created_at: Mapped[datetime] = mapped_column(ISTDateTime(), nullable=False, default=now_ist)
    updated_at: Mapped[datetime] = mapped_column(
        ISTDateTime(), nullable=False, default=now_ist, onupdate=now_ist
    )

    strategy_version: Mapped[StrategyVersionModel] = relationship()


class ReplaySignalModel(Base):
    """Persisted strategy signal emitted from normalized market events with deduplication."""

    __tablename__ = "signals"
    __table_args__ = (
        UniqueConstraint("deduplication_key", name="uq_signals_deduplication_key"),
        Index("ix_signals_session_bar", "replay_session_id", "bar_timestamp"),
    )

    id: Mapped[uuid.UUID] = mapped_column(Uuid(as_uuid=True), primary_key=True, default=uuid.uuid4)
    signal_code: Mapped[str] = mapped_column(String(64), unique=True, nullable=False, index=True)
    deduplication_key: Mapped[str] = mapped_column(String(160), unique=True, nullable=False)
    replay_session_id: Mapped[uuid.UUID | None] = mapped_column(
        Uuid(as_uuid=True),
        ForeignKey("replay_sessions.id", ondelete="RESTRICT"),
        nullable=True,
        index=True,
    )
    strategy_instance_id: Mapped[uuid.UUID | None] = mapped_column(
        Uuid(as_uuid=True),
        ForeignKey("strategy_instances.id", ondelete="RESTRICT"),
        nullable=True,
        index=True,
    )
    strategy_version_id: Mapped[uuid.UUID] = mapped_column(
        Uuid(as_uuid=True),
        ForeignKey("strategy_versions.id", ondelete="RESTRICT"),
        nullable=False,
        index=True,
    )
    strategy_version_label: Mapped[str] = mapped_column(String(96), nullable=False)
    instrument_symbol: Mapped[str] = mapped_column(String(32), nullable=False, index=True)
    timeframe: Mapped[str] = mapped_column(String(16), nullable=False)
    side: Mapped[str] = mapped_column(String(16), nullable=False)
    direction: Mapped[str] = mapped_column(String(32), nullable=False)
    exit_reason: Mapped[str | None] = mapped_column(String(32), nullable=True)
    bar_timestamp: Mapped[datetime] = mapped_column(ISTDateTime(), nullable=False)
    emitted_at: Mapped[datetime] = mapped_column(ISTDateTime(), nullable=False, default=now_ist)
    expires_at: Mapped[datetime] = mapped_column(ISTDateTime(), nullable=False)
    reference_price: Mapped[Decimal] = mapped_column(Numeric(18, 4), nullable=False)
    status: Mapped[str] = mapped_column(
        String(32), nullable=False, default=SignalStatus.GENERATED.value, index=True
    )
    rationale: Mapped[str] = mapped_column(Text, nullable=False)
    indicators_json: Mapped[dict[str, Any]] = mapped_column(JSON, nullable=False, default=dict)
    market_event_id: Mapped[str] = mapped_column(String(64), nullable=False)
    correlation_id: Mapped[str] = mapped_column(String(64), nullable=False, index=True)

    replay_session: Mapped[ReplaySessionModel | None] = relationship(back_populates="signals")
    strategy_instance: Mapped[StrategyInstanceModel | None] = relationship()


class PaperOrderModel(Base):
    """OMS paper order record enforcing strict state machine and idempotency."""

    __tablename__ = "paper_orders"
    __table_args__ = (
        UniqueConstraint("client_order_key", name="uq_paper_orders_client_order_key"),
        UniqueConstraint("signal_id", name="uq_paper_orders_signal_id"),
        CheckConstraint("quantity > 0", name="ck_paper_orders_qty_pos"),
        CheckConstraint("filled_quantity >= 0", name="ck_paper_orders_filled_nonneg"),
        CheckConstraint("leaves_quantity >= 0", name="ck_paper_orders_leaves_nonneg"),
        Index("ix_paper_orders_inst_status", "instrument_symbol", "status"),
        Index("ix_paper_orders_created_desc", "created_at"),
    )

    id: Mapped[uuid.UUID] = mapped_column(Uuid(as_uuid=True), primary_key=True, default=uuid.uuid4)
    order_code: Mapped[str] = mapped_column(String(64), unique=True, nullable=False, index=True)
    client_order_key: Mapped[str] = mapped_column(String(160), unique=True, nullable=False)
    correlation_id: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    account_id: Mapped[uuid.UUID] = mapped_column(
        Uuid(as_uuid=True),
        ForeignKey("paper_accounts.id", ondelete="RESTRICT"),
        nullable=False,
        index=True,
    )
    strategy_instance_id: Mapped[uuid.UUID | None] = mapped_column(
        Uuid(as_uuid=True),
        ForeignKey("strategy_instances.id", ondelete="RESTRICT"),
        nullable=True,
        index=True,
    )
    strategy_version_id: Mapped[uuid.UUID | None] = mapped_column(
        Uuid(as_uuid=True),
        ForeignKey("strategy_versions.id", ondelete="RESTRICT"),
        nullable=True,
        index=True,
    )
    strategy_version_label: Mapped[str | None] = mapped_column(String(96), nullable=True)
    signal_id: Mapped[uuid.UUID | None] = mapped_column(
        Uuid(as_uuid=True),
        ForeignKey("signals.id", ondelete="RESTRICT"),
        nullable=True,
        unique=True,
        index=True,
    )
    replay_session_id: Mapped[uuid.UUID | None] = mapped_column(
        Uuid(as_uuid=True),
        ForeignKey("replay_sessions.id", ondelete="RESTRICT"),
        nullable=True,
        index=True,
    )
    order_source: Mapped[str] = mapped_column(
        String(32), nullable=False, default=OrderSource.STRATEGY_SIGNAL.value
    )
    reduce_only: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    exit_reason: Mapped[str | None] = mapped_column(String(64), nullable=True)
    instrument_symbol: Mapped[str] = mapped_column(String(32), nullable=False, index=True)
    timeframe: Mapped[str] = mapped_column(String(16), nullable=False, default="5m")
    side: Mapped[str] = mapped_column(String(16), nullable=False)
    order_type: Mapped[str] = mapped_column(
        String(16), nullable=False, default=OrderType.MARKET.value
    )
    quantity: Mapped[int] = mapped_column(Integer, nullable=False)
    filled_quantity: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    leaves_quantity: Mapped[int] = mapped_column(Integer, nullable=False)
    requested_reference_price: Mapped[Decimal | None] = mapped_column(
        Numeric(18, 4), nullable=True
    )
    limit_price: Mapped[Decimal | None] = mapped_column(Numeric(18, 4), nullable=True)
    stop_price: Mapped[Decimal | None] = mapped_column(Numeric(18, 4), nullable=True)
    stop_triggered: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    stop_triggered_at: Mapped[datetime | None] = mapped_column(ISTDateTime(), nullable=True)
    average_fill_price: Mapped[Decimal | None] = mapped_column(Numeric(18, 4), nullable=True)
    total_slippage_cost: Mapped[Decimal] = mapped_column(
        Numeric(18, 4), nullable=False, default=Decimal("0")
    )
    total_charges: Mapped[Decimal] = mapped_column(
        Numeric(18, 4), nullable=False, default=Decimal("0")
    )
    execution_latency_ms: Mapped[int] = mapped_column(Integer, nullable=False, default=120)
    latency_bars: Mapped[int] = mapped_column(Integer, nullable=False, default=1)
    created_bar_timestamp: Mapped[datetime | None] = mapped_column(ISTDateTime(), nullable=True)
    bars_waited: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    slippage_bps: Mapped[Decimal] = mapped_column(
        Numeric(10, 4), nullable=False, default=Decimal("2.0")
    )
    charges_bps: Mapped[Decimal] = mapped_column(
        Numeric(10, 4), nullable=False, default=Decimal("1.5")
    )
    per_order_fee_inr: Mapped[Decimal] = mapped_column(
        Numeric(12, 4), nullable=False, default=Decimal("20.0")
    )
    max_fill_ratio_per_bar: Mapped[Decimal] = mapped_column(
        Numeric(6, 4), nullable=False, default=Decimal("1.0")
    )
    simulate_rejection: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    simulated_rejection_reason: Mapped[str | None] = mapped_column(String(255), nullable=True)
    valid_for_bars: Mapped[int | None] = mapped_column(Integer, nullable=True)
    expires_at: Mapped[datetime | None] = mapped_column(ISTDateTime(), nullable=True)
    target_stop_loss_price: Mapped[Decimal | None] = mapped_column(Numeric(18, 4), nullable=True)
    target_profit_price: Mapped[Decimal | None] = mapped_column(Numeric(18, 4), nullable=True)
    status: Mapped[str] = mapped_column(
        String(32), nullable=False, default=OrderStatus.CREATED.value, index=True
    )
    risk_check_passed: Mapped[bool | None] = mapped_column(Boolean, nullable=True)
    risk_check_details_json: Mapped[dict[str, Any]] = mapped_column(
        JSON, nullable=False, default=dict
    )
    rejection_reason: Mapped[str | None] = mapped_column(Text, nullable=True)
    cancel_reason: Mapped[str | None] = mapped_column(Text, nullable=True)
    submitted_at: Mapped[datetime | None] = mapped_column(ISTDateTime(), nullable=True)
    acknowledged_at: Mapped[datetime | None] = mapped_column(ISTDateTime(), nullable=True)
    completed_at: Mapped[datetime | None] = mapped_column(ISTDateTime(), nullable=True)
    created_by: Mapped[str] = mapped_column(String(128), nullable=False, default="desk.operator")
    created_at: Mapped[datetime] = mapped_column(ISTDateTime(), nullable=False, default=now_ist)
    updated_at: Mapped[datetime] = mapped_column(
        ISTDateTime(), nullable=False, default=now_ist, onupdate=now_ist
    )

    state_events: Mapped[list[OrderStateEventModel]] = relationship(
        back_populates="order",
        order_by="OrderStateEventModel.occurred_at.asc()",
    )
    fills: Mapped[list[PaperFillModel]] = relationship(
        back_populates="order",
        order_by="PaperFillModel.executed_at.asc()",
    )


class OrderStateEventModel(Base):
    """Append-only OMS state transition record."""

    __tablename__ = "order_state_events"
    __table_args__ = (
        Index("ix_order_state_events_order_time", "order_id", "occurred_at"),
    )

    id: Mapped[uuid.UUID] = mapped_column(Uuid(as_uuid=True), primary_key=True, default=uuid.uuid4)
    order_id: Mapped[uuid.UUID] = mapped_column(
        Uuid(as_uuid=True),
        ForeignKey("paper_orders.id", ondelete="RESTRICT"),
        nullable=False,
        index=True,
    )
    previous_status: Mapped[str | None] = mapped_column(String(32), nullable=True)
    new_status: Mapped[str] = mapped_column(String(32), nullable=False)
    occurred_at: Mapped[datetime] = mapped_column(ISTDateTime(), nullable=False, default=now_ist)
    actor: Mapped[str] = mapped_column(String(128), nullable=False)
    reason: Mapped[str] = mapped_column(Text, nullable=False)
    details_json: Mapped[dict[str, Any]] = mapped_column(JSON, nullable=False, default=dict)
    correlation_id: Mapped[str] = mapped_column(String(64), nullable=False, index=True)

    order: Mapped[PaperOrderModel] = relationship(back_populates="state_events")


class PaperFillModel(Base):
    """Immutable simulated execution fill generated by PaperBrokerService."""

    __tablename__ = "paper_fills"
    __table_args__ = (
        UniqueConstraint("fill_idempotency_key", name="uq_paper_fills_idempotency_key"),
        CheckConstraint("fill_quantity > 0", name="ck_paper_fills_qty_pos"),
        Index("ix_paper_fills_order_exec", "order_id", "executed_at"),
    )

    id: Mapped[uuid.UUID] = mapped_column(Uuid(as_uuid=True), primary_key=True, default=uuid.uuid4)
    fill_code: Mapped[str] = mapped_column(String(64), unique=True, nullable=False, index=True)
    fill_idempotency_key: Mapped[str] = mapped_column(String(160), unique=True, nullable=False)
    order_id: Mapped[uuid.UUID] = mapped_column(
        Uuid(as_uuid=True),
        ForeignKey("paper_orders.id", ondelete="RESTRICT"),
        nullable=False,
        index=True,
    )
    account_id: Mapped[uuid.UUID] = mapped_column(
        Uuid(as_uuid=True),
        ForeignKey("paper_accounts.id", ondelete="RESTRICT"),
        nullable=False,
        index=True,
    )
    strategy_instance_id: Mapped[uuid.UUID | None] = mapped_column(
        Uuid(as_uuid=True),
        ForeignKey("strategy_instances.id", ondelete="RESTRICT"),
        nullable=True,
        index=True,
    )
    strategy_version_id: Mapped[uuid.UUID | None] = mapped_column(
        Uuid(as_uuid=True),
        ForeignKey("strategy_versions.id", ondelete="RESTRICT"),
        nullable=True,
    )
    strategy_version_label: Mapped[str | None] = mapped_column(String(96), nullable=True)
    instrument_symbol: Mapped[str] = mapped_column(String(32), nullable=False, index=True)
    side: Mapped[str] = mapped_column(String(16), nullable=False)
    fill_quantity: Mapped[int] = mapped_column(Integer, nullable=False)
    raw_market_price: Mapped[Decimal] = mapped_column(Numeric(18, 4), nullable=False)
    fill_price: Mapped[Decimal] = mapped_column(Numeric(18, 4), nullable=False)
    slippage_cost: Mapped[Decimal] = mapped_column(
        Numeric(18, 4), nullable=False, default=Decimal("0")
    )
    charges: Mapped[Decimal] = mapped_column(
        Numeric(18, 4), nullable=False, default=Decimal("0")
    )
    simulated_latency_ms: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    bar_timestamp: Mapped[datetime] = mapped_column(ISTDateTime(), nullable=False)
    executed_at: Mapped[datetime] = mapped_column(ISTDateTime(), nullable=False, default=now_ist)
    market_event_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    liquidity_note: Mapped[str] = mapped_column(String(160), nullable=False, default="FULL_FILL")
    correlation_id: Mapped[str] = mapped_column(String(64), nullable=False, index=True)

    order: Mapped[PaperOrderModel] = relationship(back_populates="fills")


class PaperPositionModel(Base):
    """Derived paper trading position row calculated strictly from PaperFillModel executions."""

    __tablename__ = "paper_positions"
    __table_args__ = (
        CheckConstraint("quantity >= 0", name="ck_paper_positions_qty_nonneg"),
        Index("ix_paper_positions_book_status", "account_id", "instrument_symbol", "status"),
    )

    id: Mapped[uuid.UUID] = mapped_column(Uuid(as_uuid=True), primary_key=True, default=uuid.uuid4)
    position_code: Mapped[str] = mapped_column(String(64), unique=True, nullable=False, index=True)
    account_id: Mapped[uuid.UUID] = mapped_column(
        Uuid(as_uuid=True),
        ForeignKey("paper_accounts.id", ondelete="RESTRICT"),
        nullable=False,
        index=True,
    )
    strategy_instance_id: Mapped[uuid.UUID | None] = mapped_column(
        Uuid(as_uuid=True),
        ForeignKey("strategy_instances.id", ondelete="RESTRICT"),
        nullable=True,
        index=True,
    )
    strategy_version_id: Mapped[uuid.UUID | None] = mapped_column(
        Uuid(as_uuid=True),
        ForeignKey("strategy_versions.id", ondelete="RESTRICT"),
        nullable=True,
        index=True,
    )
    strategy_version_label: Mapped[str] = mapped_column(
        String(96), nullable=False, default="MANUAL_PAPER_DESK"
    )
    instrument_symbol: Mapped[str] = mapped_column(String(32), nullable=False, index=True)
    timeframe: Mapped[str] = mapped_column(String(16), nullable=False, default="5m")
    side: Mapped[str] = mapped_column(String(16), nullable=False)
    quantity: Mapped[int] = mapped_column(Integer, nullable=False)
    opened_quantity: Mapped[int] = mapped_column(Integer, nullable=False)
    closed_quantity: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    average_entry_price: Mapped[Decimal] = mapped_column(Numeric(18, 4), nullable=False)
    average_exit_price: Mapped[Decimal | None] = mapped_column(Numeric(18, 4), nullable=True)
    last_price: Mapped[Decimal] = mapped_column(Numeric(18, 4), nullable=False)
    highest_price_since_entry: Mapped[Decimal] = mapped_column(Numeric(18, 4), nullable=False)
    lowest_price_since_entry: Mapped[Decimal] = mapped_column(Numeric(18, 4), nullable=False)
    bars_held: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    realized_pnl: Mapped[Decimal] = mapped_column(
        Numeric(18, 4), nullable=False, default=Decimal("0")
    )
    unrealized_pnl: Mapped[Decimal] = mapped_column(
        Numeric(18, 4), nullable=False, default=Decimal("0")
    )
    unallocated_entry_charges: Mapped[Decimal] = mapped_column(
        Numeric(18, 4), nullable=False, default=Decimal("0")
    )
    total_charges: Mapped[Decimal] = mapped_column(
        Numeric(18, 4), nullable=False, default=Decimal("0")
    )
    stop_loss_price: Mapped[Decimal | None] = mapped_column(Numeric(18, 4), nullable=True)
    target_price: Mapped[Decimal | None] = mapped_column(Numeric(18, 4), nullable=True)
    status: Mapped[str] = mapped_column(
        String(32), nullable=False, default=PositionStatus.OPEN.value, index=True
    )
    opened_at: Mapped[datetime] = mapped_column(ISTDateTime(), nullable=False, default=now_ist)
    last_mark_at: Mapped[datetime] = mapped_column(ISTDateTime(), nullable=False, default=now_ist)
    closed_at: Mapped[datetime | None] = mapped_column(ISTDateTime(), nullable=True)
    exit_reason: Mapped[str | None] = mapped_column(String(64), nullable=True)
    updated_at: Mapped[datetime] = mapped_column(
        ISTDateTime(), nullable=False, default=now_ist, onupdate=now_ist
    )


class PositionEventModel(Base):
    """Append-only position mutation ledger guaranteeing fill-to-position idempotency."""

    __tablename__ = "position_events"
    __table_args__ = (
        UniqueConstraint("event_idempotency_key", name="uq_position_events_idempotency_key"),
        UniqueConstraint("fill_id", name="uq_position_events_fill_id"),
        Index("ix_position_events_pos_time", "position_id", "occurred_at"),
    )

    id: Mapped[uuid.UUID] = mapped_column(Uuid(as_uuid=True), primary_key=True, default=uuid.uuid4)
    event_idempotency_key: Mapped[str] = mapped_column(String(160), unique=True, nullable=False)
    fill_id: Mapped[uuid.UUID] = mapped_column(
        Uuid(as_uuid=True),
        ForeignKey("paper_fills.id", ondelete="RESTRICT"),
        nullable=False,
        unique=True,
        index=True,
    )
    position_id: Mapped[uuid.UUID] = mapped_column(
        Uuid(as_uuid=True),
        ForeignKey("paper_positions.id", ondelete="RESTRICT"),
        nullable=False,
        index=True,
    )
    event_type: Mapped[str] = mapped_column(String(32), nullable=False)
    delta_quantity: Mapped[int] = mapped_column(Integer, nullable=False)
    resulting_quantity: Mapped[int] = mapped_column(Integer, nullable=False)
    fill_price: Mapped[Decimal] = mapped_column(Numeric(18, 4), nullable=False)
    realized_pnl_delta: Mapped[Decimal] = mapped_column(
        Numeric(18, 4), nullable=False, default=Decimal("0")
    )
    charges_delta: Mapped[Decimal] = mapped_column(
        Numeric(18, 4), nullable=False, default=Decimal("0")
    )
    occurred_at: Mapped[datetime] = mapped_column(ISTDateTime(), nullable=False, default=now_ist)
    correlation_id: Mapped[str] = mapped_column(String(64), nullable=False, index=True)


class AccountRiskConfigModel(Base):
    """Persisted configurable account & operational paper-risk settings (DEMO DEFAULTS ONLY)."""

    __tablename__ = "account_risk_configs"
    __table_args__ = (
        UniqueConstraint("account_id", name="uq_account_risk_configs_account_id"),
    )

    id: Mapped[uuid.UUID] = mapped_column(Uuid(as_uuid=True), primary_key=True, default=uuid.uuid4)
    account_id: Mapped[uuid.UUID] = mapped_column(
        Uuid(as_uuid=True),
        ForeignKey("paper_accounts.id", ondelete="RESTRICT"),
        nullable=False,
        unique=True,
        index=True,
    )
    is_demo_settings: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    # Account-level limits
    max_daily_loss_inr: Mapped[Decimal] = mapped_column(
        Numeric(18, 4), nullable=False, default=Decimal("10000.00")
    )
    max_open_positions: Mapped[int] = mapped_column(Integer, nullable=False, default=4)
    max_gross_exposure_inr: Mapped[Decimal] = mapped_column(
        Numeric(18, 4), nullable=False, default=Decimal("6000000.00")
    )
    max_order_value_inr: Mapped[Decimal] = mapped_column(
        Numeric(18, 4), nullable=False, default=Decimal("3000000.00")
    )
    max_trades_per_day: Mapped[int] = mapped_column(Integer, nullable=False, default=15)
    max_consecutive_losses: Mapped[int] = mapped_column(Integer, nullable=False, default=3)
    trading_session_enabled: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    session_start_ist: Mapped[str] = mapped_column(String(16), nullable=False, default="09:15:00")
    session_end_ist: Mapped[str] = mapped_column(String(16), nullable=False, default="15:30:00")
    # Global Kill Switch
    global_kill_switch_active: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=False, index=True
    )
    kill_switch_reason: Mapped[str | None] = mapped_column(Text, nullable=True)
    kill_switch_activated_at: Mapped[datetime | None] = mapped_column(
        ISTDateTime(), nullable=True
    )
    kill_switch_activated_by: Mapped[str | None] = mapped_column(String(128), nullable=True)
    # Operational Risk Gates
    market_data_stale_threshold_seconds: Mapped[int] = mapped_column(
        Integer, nullable=False, default=10
    )
    max_signal_age_seconds: Mapped[int] = mapped_column(Integer, nullable=False, default=15)
    enforce_system_health_gate: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=True
    )
    simulated_system_degraded: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=False
    )
    enforce_position_reconciliation_gate: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=True
    )
    # Default Strategy-Level Limits (applied unless overridden per StrategyVersion)
    default_max_quantity: Mapped[int] = mapped_column(Integer, nullable=False, default=300)
    default_max_risk_per_trade_inr: Mapped[Decimal] = mapped_column(
        Numeric(18, 4), nullable=False, default=Decimal("2500.00")
    )
    default_strategy_daily_loss_inr: Mapped[Decimal] = mapped_column(
        Numeric(18, 4), nullable=False, default=Decimal("6000.00")
    )
    default_max_strategy_drawdown_inr: Mapped[Decimal] = mapped_column(
        Numeric(18, 4), nullable=False, default=Decimal("8000.00")
    )
    default_max_trades_per_strategy_day: Mapped[int] = mapped_column(
        Integer, nullable=False, default=5
    )
    default_cooldown_seconds_after_loss: Mapped[int] = mapped_column(
        Integer, nullable=False, default=300
    )
    default_max_holding_duration_seconds: Mapped[int] = mapped_column(
        Integer, nullable=False, default=14400
    )
    default_allowed_instruments_json: Mapped[list[str]] = mapped_column(
        JSON, nullable=False, default=lambda: ["NIFTY", "BANKNIFTY"]
    )
    default_allowed_order_types_json: Mapped[list[str]] = mapped_column(
        JSON, nullable=False, default=lambda: ["MARKET", "LIMIT", "STOP"]
    )
    updated_by: Mapped[str] = mapped_column(String(128), nullable=False, default="system.seed")
    created_at: Mapped[datetime] = mapped_column(ISTDateTime(), nullable=False, default=now_ist)
    updated_at: Mapped[datetime] = mapped_column(
        ISTDateTime(), nullable=False, default=now_ist, onupdate=now_ist
    )


class StrategyRiskConfigModel(Base):
    """Persisted configurable strategy-level paper-risk rules per immutable StrategyVersion."""

    __tablename__ = "strategy_risk_configs"
    __table_args__ = (
        UniqueConstraint(
            "account_id", "strategy_version_id", name="uq_strategy_risk_configs_acct_ver"
        ),
    )

    id: Mapped[uuid.UUID] = mapped_column(Uuid(as_uuid=True), primary_key=True, default=uuid.uuid4)
    account_id: Mapped[uuid.UUID] = mapped_column(
        Uuid(as_uuid=True),
        ForeignKey("paper_accounts.id", ondelete="RESTRICT"),
        nullable=False,
        index=True,
    )
    strategy_version_id: Mapped[uuid.UUID] = mapped_column(
        Uuid(as_uuid=True),
        ForeignKey("strategy_versions.id", ondelete="RESTRICT"),
        nullable=False,
        index=True,
    )
    strategy_version_label: Mapped[str] = mapped_column(String(96), nullable=False)
    max_quantity: Mapped[int] = mapped_column(Integer, nullable=False, default=300)
    max_risk_per_trade_inr: Mapped[Decimal] = mapped_column(
        Numeric(18, 4), nullable=False, default=Decimal("2500.00")
    )
    strategy_daily_loss_inr: Mapped[Decimal] = mapped_column(
        Numeric(18, 4), nullable=False, default=Decimal("6000.00")
    )
    max_strategy_drawdown_inr: Mapped[Decimal] = mapped_column(
        Numeric(18, 4), nullable=False, default=Decimal("8000.00")
    )
    max_trades_per_day: Mapped[int] = mapped_column(Integer, nullable=False, default=5)
    cooldown_seconds_after_loss: Mapped[int] = mapped_column(
        Integer, nullable=False, default=300
    )
    max_holding_duration_seconds: Mapped[int] = mapped_column(
        Integer, nullable=False, default=14400
    )
    allowed_instruments_json: Mapped[list[str]] = mapped_column(
        JSON, nullable=False, default=lambda: ["NIFTY", "BANKNIFTY"]
    )
    allowed_order_types_json: Mapped[list[str]] = mapped_column(
        JSON, nullable=False, default=lambda: ["MARKET", "LIMIT", "STOP"]
    )
    is_enabled: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    updated_by: Mapped[str] = mapped_column(String(128), nullable=False, default="system.seed")
    created_at: Mapped[datetime] = mapped_column(ISTDateTime(), nullable=False, default=now_ist)
    updated_at: Mapped[datetime] = mapped_column(
        ISTDateTime(), nullable=False, default=now_ist, onupdate=now_ist
    )


class RiskDecisionModel(Base):
    """Immutable persisted pre-trade risk decision generated for every attempted order."""

    __tablename__ = "risk_decisions"
    __table_args__ = (
        Index("ix_risk_decisions_evaluated_desc", "evaluated_at"),
        Index("ix_risk_decisions_decision_rule", "decision", "primary_rule"),
    )

    id: Mapped[uuid.UUID] = mapped_column(Uuid(as_uuid=True), primary_key=True, default=uuid.uuid4)
    decision_code: Mapped[str] = mapped_column(
        String(64), unique=True, nullable=False, index=True
    )
    decision: Mapped[str] = mapped_column(String(16), nullable=False, index=True)
    primary_rule: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    current_value: Mapped[str] = mapped_column(String(128), nullable=False)
    limit_value: Mapped[str] = mapped_column(String(128), nullable=False)
    reason: Mapped[str] = mapped_column(Text, nullable=False)
    account_id: Mapped[uuid.UUID] = mapped_column(
        Uuid(as_uuid=True),
        ForeignKey("paper_accounts.id", ondelete="RESTRICT"),
        nullable=False,
        index=True,
    )
    order_id: Mapped[uuid.UUID | None] = mapped_column(
        Uuid(as_uuid=True),
        ForeignKey("paper_orders.id", ondelete="RESTRICT"),
        nullable=True,
        index=True,
    )
    client_order_key: Mapped[str | None] = mapped_column(String(160), nullable=True, index=True)
    signal_id: Mapped[uuid.UUID | None] = mapped_column(
        Uuid(as_uuid=True),
        ForeignKey("signals.id", ondelete="RESTRICT"),
        nullable=True,
        index=True,
    )
    strategy_instance_id: Mapped[uuid.UUID | None] = mapped_column(
        Uuid(as_uuid=True),
        ForeignKey("strategy_instances.id", ondelete="RESTRICT"),
        nullable=True,
    )
    strategy_version_id: Mapped[uuid.UUID | None] = mapped_column(
        Uuid(as_uuid=True),
        ForeignKey("strategy_versions.id", ondelete="RESTRICT"),
        nullable=True,
    )
    strategy_label: Mapped[str] = mapped_column(
        String(96), nullable=False, default="MANUAL_PAPER_DESK"
    )
    instrument_symbol: Mapped[str] = mapped_column(String(32), nullable=False, index=True)
    side: Mapped[str] = mapped_column(String(16), nullable=False)
    order_type: Mapped[str] = mapped_column(String(16), nullable=False)
    proposed_quantity: Mapped[int] = mapped_column(Integer, nullable=False)
    proposed_price: Mapped[Decimal | None] = mapped_column(Numeric(18, 4), nullable=True)
    proposed_notional_inr: Mapped[Decimal] = mapped_column(
        Numeric(18, 4), nullable=False, default=Decimal("0")
    )
    checks_json: Mapped[list[dict[str, Any]]] = mapped_column(JSON, nullable=False, default=list)
    correlation_id: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    evaluated_at: Mapped[datetime] = mapped_column(
        ISTDateTime(), nullable=False, default=now_ist, index=True
    )


class AuditEventModel(Base):
    """Append-only operational audit record traceable via correlation_id."""

    __tablename__ = "audit_events"
    __table_args__ = (
        Index("ix_audit_events_occurred_at_desc", "occurred_at"),
        Index("ix_audit_events_cat_sev", "category", "severity"),
        Index("ix_audit_events_entity", "entity_type", "entity_id"),
    )

    id: Mapped[uuid.UUID] = mapped_column(Uuid(as_uuid=True), primary_key=True, default=uuid.uuid4)
    occurred_at: Mapped[datetime] = mapped_column(ISTDateTime(), nullable=False, default=now_ist)
    correlation_id: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    actor: Mapped[str] = mapped_column(String(128), nullable=False)
    actor_type: Mapped[str] = mapped_column(
        String(32), nullable=False, default=ActorType.SYSTEM.value
    )
    category: Mapped[str] = mapped_column(
        String(32), nullable=False, default=AuditCategory.SYSTEM.value
    )
    action: Mapped[str] = mapped_column(String(128), nullable=False, index=True)
    event_type: Mapped[str | None] = mapped_column(String(128), nullable=True, index=True)
    severity: Mapped[str] = mapped_column(
        String(32), nullable=False, default=AuditSeverity.INFO.value
    )
    entity_type: Mapped[str | None] = mapped_column(String(64), nullable=True)
    entity_id: Mapped[str | None] = mapped_column(String(128), nullable=True)
    strategy_id: Mapped[str | None] = mapped_column(String(64), nullable=True, index=True)
    signal_id: Mapped[str | None] = mapped_column(String(64), nullable=True, index=True)
    order_id: Mapped[str | None] = mapped_column(String(64), nullable=True, index=True)
    source: Mapped[str | None] = mapped_column(String(64), nullable=True, default="core.service")
    summary: Mapped[str] = mapped_column(Text, nullable=False)
    details_json: Mapped[dict[str, Any]] = mapped_column(JSON, nullable=False, default=dict)


class ReconciliationRunModel(Base):
    """Append-only persisted deterministic reconciliation run & explicit repair change log."""

    __tablename__ = "reconciliation_runs"
    __table_args__ = (
        Index("ix_reconciliation_runs_completed_desc", "completed_at"),
    )

    id: Mapped[uuid.UUID] = mapped_column(Uuid(as_uuid=True), primary_key=True, default=uuid.uuid4)
    run_code: Mapped[str] = mapped_column(String(64), unique=True, nullable=False, index=True)
    account_id: Mapped[uuid.UUID | None] = mapped_column(
        Uuid(as_uuid=True),
        ForeignKey("paper_accounts.id", ondelete="RESTRICT"),
        nullable=True,
        index=True,
    )
    status: Mapped[str] = mapped_column(String(32), nullable=False, index=True)
    repair_applied: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    fills_checked: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    open_orders_checked: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    positions_checked: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    discrepancies_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    repairs_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    checks_json: Mapped[dict[str, Any]] = mapped_column(JSON, nullable=False, default=dict)
    discrepancies_json: Mapped[list[dict[str, Any]]] = mapped_column(
        JSON, nullable=False, default=list
    )
    repairs_json: Mapped[list[dict[str, Any]]] = mapped_column(JSON, nullable=False, default=list)
    actor: Mapped[str] = mapped_column(
        String(128), nullable=False, default="system.reconciliation"
    )
    correlation_id: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    started_at: Mapped[datetime] = mapped_column(ISTDateTime(), nullable=False, default=now_ist)
    completed_at: Mapped[datetime] = mapped_column(
        ISTDateTime(), nullable=False, default=now_ist, index=True
    )


class SystemHealthEventModel(Base):
    """Append-only subsystem health transition / probe history."""

    __tablename__ = "system_health_events"
    __table_args__ = (
        Index("ix_system_health_events_comp_time", "component", "recorded_at"),
    )

    id: Mapped[uuid.UUID] = mapped_column(Uuid(as_uuid=True), primary_key=True, default=uuid.uuid4)
    recorded_at: Mapped[datetime] = mapped_column(
        ISTDateTime(), nullable=False, default=now_ist, index=True
    )
    component: Mapped[str] = mapped_column(
        String(32), nullable=False, default=SystemComponent.DATABASE.value
    )
    status: Mapped[str] = mapped_column(
        String(32), nullable=False, default=SystemComponentStatus.HEALTHY.value
    )
    latency_ms: Mapped[Decimal | None] = mapped_column(Numeric(10, 2), nullable=True)
    message: Mapped[str] = mapped_column(Text, nullable=False)
    details_json: Mapped[dict[str, Any]] = mapped_column(JSON, nullable=False, default=dict)
    correlation_id: Mapped[str | None] = mapped_column(String(64), nullable=True, index=True)


# --- Domain Immutability Guards at ORM Layer ---

_IMMUTABLE_STRATEGY_VERSION_FIELDS = (
    "strategy_definition_id",
    "version_number",
    "version_label",
    "instrument_symbol",
    "timeframe",
    "cloned_from_version_id",
    "parameters_json",
    "parameter_hash",
    "created_by",
    "created_at",
)


@event.listens_for(StrategyVersionModel, "before_update")
def _prevent_strategy_version_parameter_mutation(
    mapper: Any, connection: Any, target: StrategyVersionModel
) -> None:
    state = inspect(target)
    for attr_name in _IMMUTABLE_STRATEGY_VERSION_FIELDS:
        hist = state.attrs[attr_name].history
        if hist.has_changes():
            raise ImmutabilityViolationError(
                f"StrategyVersion.{attr_name} is immutable. Create a new strategy version instead."
            )


@event.listens_for(StrategyVersionModel, "before_delete")
def _prevent_strategy_version_delete(
    mapper: Any, connection: Any, target: StrategyVersionModel
) -> None:
    raise ImmutabilityViolationError(
        "StrategyVersion records are immutable and cannot be deleted; transition status to RETIRED."
    )


@event.listens_for(AuditEventModel, "before_update")
@event.listens_for(AuditEventModel, "before_delete")
def _prevent_audit_event_mutation(
    mapper: Any, connection: Any, target: AuditEventModel
) -> None:
    raise ImmutabilityViolationError("AuditEvent records are append-only and cannot be modified or deleted.")


@event.listens_for(BacktestRunModel, "before_update")
@event.listens_for(BacktestRunModel, "before_delete")
def _prevent_backtest_run_mutation(
    mapper: Any, connection: Any, target: BacktestRunModel
) -> None:
    raise ImmutabilityViolationError(
        "BacktestRun records are immutable historical artifacts and cannot be modified or deleted."
    )


@event.listens_for(BacktestTradeModel, "before_update")
@event.listens_for(BacktestTradeModel, "before_delete")
def _prevent_backtest_trade_mutation(
    mapper: Any, connection: Any, target: BacktestTradeModel
) -> None:
    raise ImmutabilityViolationError(
        "BacktestTrade ledger records are immutable and cannot be modified or deleted."
    )


@event.listens_for(PaperFillModel, "before_update")
@event.listens_for(PaperFillModel, "before_delete")
def _prevent_paper_fill_mutation(
    mapper: Any, connection: Any, target: PaperFillModel
) -> None:
    raise ImmutabilityViolationError(
        "PaperFill execution records are immutable and cannot be modified or deleted."
    )


@event.listens_for(OrderStateEventModel, "before_update")
@event.listens_for(OrderStateEventModel, "before_delete")
def _prevent_order_state_event_mutation(
    mapper: Any, connection: Any, target: OrderStateEventModel
) -> None:
    raise ImmutabilityViolationError(
        "OrderStateEvent records are append-only and cannot be modified or deleted."
    )


@event.listens_for(PositionEventModel, "before_update")
@event.listens_for(PositionEventModel, "before_delete")
def _prevent_position_event_mutation(
    mapper: Any, connection: Any, target: PositionEventModel
) -> None:
    raise ImmutabilityViolationError(
        "PositionEvent records are append-only and cannot be modified or deleted."
    )


@event.listens_for(RiskDecisionModel, "before_update")
@event.listens_for(RiskDecisionModel, "before_delete")
def _prevent_risk_decision_mutation(
    mapper: Any, connection: Any, target: RiskDecisionModel
) -> None:
    raise ImmutabilityViolationError(
        "RiskDecision records are append-only audit artifacts and cannot be modified or deleted."
    )


@event.listens_for(ReconciliationRunModel, "before_update")
@event.listens_for(ReconciliationRunModel, "before_delete")
def _prevent_reconciliation_run_mutation(
    mapper: Any, connection: Any, target: ReconciliationRunModel
) -> None:
    raise ImmutabilityViolationError(
        "ReconciliationRun records are append-only operational records and cannot be modified or deleted."
    )


_IMMUTABLE_HISTORICAL_DATASET_FIELDS: tuple[str, ...] = (
    "dataset_code",
    "instrument_symbol",
    "provider",
    "provider_symbol",
    "timeframe",
    "source_type",
    "data_version_hash",
    "row_count",
    "start_timestamp",
    "end_timestamp",
)


@event.listens_for(HistoricalDatasetModel, "before_update")
def _prevent_historical_dataset_identity_mutation(
    mapper: Any, connection: Any, target: HistoricalDatasetModel
) -> None:
    state = inspect(target)
    for attr_name in _IMMUTABLE_HISTORICAL_DATASET_FIELDS:
        hist = state.attrs[attr_name].history
        if hist.has_changes():
            raise ImmutabilityViolationError(
                f"HistoricalDataset.{attr_name} is immutable. Create a new dataset version instead."
            )

