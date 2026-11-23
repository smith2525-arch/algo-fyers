"""Strict Pydantic DTOs with guaranteed Asia/Kolkata (+05:30) timestamp serialization."""

from __future__ import annotations

from datetime import date, datetime
from decimal import Decimal
from typing import Annotated, Any
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, PlainSerializer

from core.clock import format_ist_iso
from core.domain.enums import (
    AccountDesignation,
    ActorType,
    AuditCategory,
    AuditSeverity,
    BacktestStatus,
    ExitReason,
    MarketDataFreshnessStatus,
    OrderType,
    PaperAccountStatus,
    PositionSizingMode,
    ReplaySessionStatus,
    ReplaySpeed,
    Side,
    StrategyVersionStatus,
    SystemComponent,
    SystemComponentStatus,
    TradeDirection,
)

ISTTimestamp = Annotated[
    datetime,
    PlainSerializer(lambda dt: format_ist_iso(dt), return_type=str, when_used="json"),
]


class LivenessOut(BaseModel):
    status: str = "ALIVE"
    service: str
    version: str
    execution_mode: str
    timezone: str
    timestamp_ist: ISTTimestamp


class ComponentHealthOut(BaseModel):
    component: SystemComponent
    status: SystemComponentStatus
    message: str
    checked_at: ISTTimestamp
    latency_ms: Decimal | None = None
    details: dict[str, Any] = Field(default_factory=dict)


class HealthEventOut(BaseModel):
    id: str
    recorded_at: ISTTimestamp
    component: str
    status: str
    latency_ms: Decimal | None = None
    message: str
    details: dict[str, Any] = Field(default_factory=dict)
    correlation_id: str | None = None


class SystemHealthOut(BaseModel):
    overall_status: SystemComponentStatus
    checked_at: ISTTimestamp
    timezone: str
    execution_mode: str
    paper_trading_halted: bool
    halt_reason: str | None = None
    database: ComponentHealthOut
    components: list[ComponentHealthOut]
    operational_components: list[ComponentHealthOut] = Field(default_factory=list)
    recent_events: list[HealthEventOut] = Field(default_factory=list)


class PaperAccountOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: UUID
    account_code: str
    account_name: str
    is_demo_capital: bool
    account_designation: AccountDesignation
    currency: str
    initial_capital: Decimal
    available_capital: Decimal
    realized_pnl_today: Decimal
    unrealized_pnl: Decimal
    paper_trading_halted: bool
    halt_reason: str | None = None
    halted_at: ISTTimestamp | None = None
    halted_by: str | None = None
    status: PaperAccountStatus
    version: int
    created_at: ISTTimestamp
    updated_at: ISTTimestamp


class PaperTradingControlRequest(BaseModel):
    actor: str = Field(default="desk.operator", min_length=2, max_length=128)
    reason: str = Field(
        default="Operator action via Algo Trading Control Center",
        min_length=3,
        max_length=500,
    )


class InstrumentOut(BaseModel):
    id: UUID
    symbol: str
    display_name: str
    exchange: str
    segment: str
    instrument_type: str
    data_provider: str = "FYERS"
    provider_symbol: str = ""
    currency: str
    lot_size: int
    tick_size: Decimal
    session_open_ist: str
    session_close_ist: str
    metadata: dict[str, Any]
    is_active: bool
    created_at: ISTTimestamp
    updated_at: ISTTimestamp


class InstrumentUpdateRequest(BaseModel):
    lot_size: int = Field(..., ge=1, le=10000)
    tick_size: Decimal = Field(..., gt=Decimal("0"), le=Decimal("100"))
    session_open_ist: str = Field(default="09:15:00", pattern=r"^\d{2}:\d{2}(:\d{2})?$")
    session_close_ist: str = Field(default="15:30:00", pattern=r"^\d{2}:\d{2}(:\d{2})?$")
    is_active: bool = True
    actor: str = Field(default="desk.operator", min_length=2, max_length=128)


class StrategyVersionOut(BaseModel):
    id: UUID
    strategy_definition_id: UUID
    strategy_code: str
    version_number: int
    version_label: str
    instrument_symbol: str
    timeframe: str
    parameters: dict[str, Any]
    parameter_hash: str
    notes: str | None = None
    created_by: str
    status: StrategyVersionStatus
    created_at: ISTTimestamp
    cloned_from_version_id: UUID | None = None
    cloned_from_label: str | None = None


class StrategyDefinitionOut(BaseModel):
    id: UUID
    code: str
    name: str
    category: str
    description: str
    parameter_schema: dict[str, Any]
    default_parameters: dict[str, Any]
    supported_symbols: list[str]
    is_enabled: bool
    created_at: ISTTimestamp
    updated_at: ISTTimestamp
    versions: list[StrategyVersionOut] = Field(default_factory=list)


class CreateStrategyVersionRequest(BaseModel):
    parameters: dict[str, Any]
    notes: str | None = Field(default=None, max_length=500)
    actor: str = Field(default="desk.operator", min_length=2, max_length=128)


class CloneStrategyVersionRequest(BaseModel):
    parameter_overrides: dict[str, Any] = Field(default_factory=dict)
    notes: str | None = Field(default=None, max_length=500)
    actor: str = Field(default="desk.operator", min_length=2, max_length=128)


class ValidateStrategyConfigRequest(BaseModel):
    parameters: dict[str, Any]


class ValidateStrategyConfigOut(BaseModel):
    valid: bool
    canonical_parameters: dict[str, Any]
    parameter_hash: str


class StrategyDiffRowOut(BaseModel):
    parameter: str
    group: str
    label: str
    left_value: Any
    right_value: Any
    changed: bool


class StrategyVersionComparisonOut(BaseModel):
    left_version: StrategyVersionOut
    right_version: StrategyVersionOut
    identical_hash: bool
    changed_count: int
    unchanged_count: int
    rows: list[StrategyDiffRowOut]


class AuditEventOut(BaseModel):
    id: UUID
    occurred_at: ISTTimestamp
    occurred_at_utc: str | None = None
    correlation_id: str
    actor: str
    actor_type: ActorType
    category: AuditCategory
    action: str
    event_type: str | None = None
    severity: AuditSeverity
    entity_type: str | None = None
    entity_id: str | None = None
    strategy_id: str | None = None
    signal_id: str | None = None
    order_id: str | None = None
    source: str | None = None
    summary: str
    details: dict[str, Any] = Field(default_factory=dict)


class AuditEventCreateRequest(BaseModel):
    action: str = Field(..., min_length=3, max_length=128)
    summary: str = Field(..., min_length=3, max_length=1000)
    actor: str = Field(default="desk.operator", min_length=2, max_length=128)
    actor_type: ActorType = ActorType.OPERATOR
    category: AuditCategory = AuditCategory.CONTROL
    severity: AuditSeverity = AuditSeverity.INFO
    entity_type: str | None = Field(default=None, max_length=64)
    entity_id: str | None = Field(default=None, max_length=128)
    event_type: str | None = Field(default=None, max_length=128)
    strategy_id: str | None = Field(default=None, max_length=64)
    signal_id: str | None = Field(default=None, max_length=64)
    order_id: str | None = Field(default=None, max_length=64)
    source: str | None = Field(default=None, max_length=64)
    details: dict[str, Any] = Field(default_factory=dict)


class OperatorLoginAuditRequest(BaseModel):
    username: str = Field(default="desk.operator", min_length=2, max_length=128)
    source_ip: str = Field(default="127.0.0.1", max_length=64)
    user_agent: str = Field(default="StreamlitControlConsole", max_length=255)


class RunReconciliationRequest(BaseModel):
    apply_repair: bool = False
    actor: str = Field(default="desk.operator", min_length=2, max_length=128)


class DashboardKPIsOut(BaseModel):
    paper_capital: Decimal
    available_capital: Decimal
    todays_realized_pnl: Decimal
    unrealized_pnl: Decimal
    open_positions: int
    active_strategies: int
    pending_orders: int
    currency: str = "INR"
    is_demo_capital: bool = True
    account_designation: str = "DEMO_PAPER_ONLY"


class RiskUtilizationSnapshotOut(BaseModel):
    paper_trading_halted: bool
    new_orders_gate: str
    capital_deployed_pct: Decimal
    daily_loss_used_inr: Decimal
    open_positions_count: int
    auto_flatten_on_stop: bool = False


class DashboardOverviewOut(BaseModel):
    generated_at: ISTTimestamp
    execution_mode: str
    market_mode: str
    timezone: str
    database_connected: bool
    account: PaperAccountOut | None = None
    kpis: DashboardKPIsOut
    system_status_strip: list[ComponentHealthOut]
    running_strategies: list[dict[str, Any]] = Field(default_factory=list)
    open_positions: list[dict[str, Any]] = Field(default_factory=list)
    recent_orders: list[dict[str, Any]] = Field(default_factory=list)
    risk_utilization: RiskUtilizationSnapshotOut
    recent_alerts: list[AuditEventOut] = Field(default_factory=list)
    daily_pnl_series: list[dict[str, Any]] = Field(default_factory=list)


class RuntimeSettingsOut(BaseModel):
    app_name: str
    app_env: str
    app_version: str
    execution_mode: str
    market_mode: str
    timezone: str
    masked_database_url: str
    db_pool_size: int
    db_max_overflow: int
    db_pool_recycle_seconds: int
    api_base_url: str
    alembic_version: str | None = None
    database_reachable: bool
    fyers_historical_data_enabled: bool = True
    fyers_token_configured: bool = False
    fyers_auth_mode: str = "UNCONFIGURED"
    timestamp_ist: ISTTimestamp


class DatasetDescriptorOut(BaseModel):
    dataset_id: str
    name: str
    instrument: str
    timeframe: str
    source_type: str
    provider: str = "SYNTHETIC"
    provider_symbol: str | None = None
    quality_status: str = "VALID"
    is_complete: bool = True
    data_version_hash: str
    row_count: int
    start_timestamp: ISTTimestamp | None = None
    end_timestamp: ISTTimestamp | None = None
    retrieved_at: ISTTimestamp | None = None
    labels: dict[str, Any] = Field(default_factory=dict)


class ImportCsvDatasetRequest(BaseModel):
    name: str = Field(..., min_length=2, max_length=160)
    instrument: str = Field(..., min_length=2, max_length=32)
    timeframe: str = Field(default="5m", min_length=2, max_length=16)
    csv_text: str = Field(..., min_length=10)
    actor: str = Field(default="desk.operator", min_length=2, max_length=128)


class ImportCsvDatasetOut(BaseModel):
    dataset: DatasetDescriptorOut
    data_quality_report: dict[str, Any]


class RunBacktestRequest(BaseModel):
    strategy_version_id: UUID
    dataset_id: str = Field(..., min_length=2, max_length=128)
    start_date: date | None = None
    end_date: date | None = None
    initial_capital: Decimal = Field(default=Decimal("500000.00"), gt=Decimal("0"))
    slippage_bps: Decimal | None = Field(default=None, ge=Decimal("0"), le=Decimal("250"))
    estimated_charges_bps: Decimal | None = Field(
        default=None, ge=Decimal("0"), le=Decimal("250")
    )
    per_order_fee_inr: Decimal | None = Field(default=None, ge=Decimal("0"), le=Decimal("5000"))
    position_sizing_mode: PositionSizingMode | None = None
    position_size_value: Decimal | None = Field(default=None, gt=Decimal("0"))
    execution_delay_bars: int = Field(default=1, ge=1, le=10)
    allow_partial_dataset: bool = False
    actor: str = Field(default="desk.operator", min_length=2, max_length=128)


class BacktestTradeOut(BaseModel):
    trade_id: UUID
    trade_number: int
    strategy_version_id: UUID
    strategy_version_label: str
    instrument: str
    direction: TradeDirection
    entry_signal_time: ISTTimestamp
    entry_time: ISTTimestamp
    raw_entry_price: Decimal
    entry_price: Decimal
    quantity: int
    exit_time: ISTTimestamp
    raw_exit_price: Decimal
    exit_price: Decimal
    exit_reason: ExitReason
    gross_pnl: Decimal
    charges: Decimal
    slippage_cost: Decimal
    net_pnl: Decimal
    bars_held: int


class BacktestRunSummaryOut(BaseModel):
    id: UUID
    run_code: str
    simulation_disclaimer: str = "SIMULATED BACKTEST RESULTS"
    strategy_version_id: UUID
    strategy_version_label: str
    instrument_symbol: str
    timeframe: str
    dataset_id: str
    dataset_name: str
    dataset_source: str
    dataset_labels: dict[str, Any] = Field(default_factory=dict)
    data_version_hash: str
    reproducibility_fingerprint: str
    status: BacktestStatus
    engine_version: str
    total_trades: int
    net_pnl: str | None = None
    return_pct: str | None = None
    max_drawdown_pct: str | None = None
    win_rate_pct: str | None = None
    profit_factor: str | None = None
    data_start_timestamp: ISTTimestamp | None = None
    data_end_timestamp: ISTTimestamp | None = None
    started_at: ISTTimestamp
    completed_at: ISTTimestamp
    created_by: str
    correlation_id: str


class BacktestRunDetailOut(BacktestRunSummaryOut):
    input_parameters: dict[str, Any]
    execution_assumptions: dict[str, Any]
    data_quality_report: dict[str, Any]
    metrics: dict[str, Any]
    equity_curve: list[dict[str, Any]] = Field(default_factory=list)
    drawdown_curve: list[dict[str, Any]] = Field(default_factory=list)
    monthly_returns: list[dict[str, Any]] = Field(default_factory=list)
    trades: list[BacktestTradeOut] = Field(default_factory=list)
    chart_bars: list[dict[str, Any]] = Field(default_factory=list)


class ReplayStartRequest(BaseModel):
    instrument: str = Field(..., min_length=2, max_length=32)
    strategy_version_id: UUID
    dataset_id: str = Field(..., min_length=2, max_length=128)
    historical_session_date: str = Field(default="ALL_SESSIONS", min_length=2, max_length=32)
    replay_speed: ReplaySpeed = ReplaySpeed.X1
    initial_step_bars: int = Field(default=1, ge=0, le=20)
    actor: str = Field(default="desk.operator", min_length=2, max_length=128)


class ReplayControlRequest(BaseModel):
    actor: str = Field(default="desk.operator", min_length=2, max_length=128)


class ReplaySpeedRequest(BaseModel):
    replay_speed: ReplaySpeed
    actor: str = Field(default="desk.operator", min_length=2, max_length=128)


class ReplayStepRequest(BaseModel):
    bars: int = Field(default=1, ge=1, le=200)
    actor: str = Field(default="desk.operator", min_length=2, max_length=128)


class HistoricalDownloadRequest(BaseModel):
    instrument: str = Field(default="NIFTY", min_length=2, max_length=32)
    timeframe: str = Field(default="5m", min_length=1, max_length=16)
    from_date: date
    to_date: date
    display_name: str | None = Field(default=None, max_length=160)
    force_refresh: bool = False
    allow_partial_current_candle: bool = False
    actor: str = Field(default="desk.operator", min_length=2, max_length=128)


class HistoricalUploadRequest(BaseModel):
    name: str = Field(..., min_length=2, max_length=160)
    instrument: str = Field(..., min_length=2, max_length=32)
    timeframe: str = Field(default="5m", min_length=1, max_length=16)
    csv_text: str = Field(..., min_length=10)
    actor: str = Field(default="desk.operator", min_length=2, max_length=128)


class MarketDataFreshnessOut(BaseModel):
    status: MarketDataFreshnessStatus
    market_data_age_seconds: str | None = None
    stale_threshold_seconds: int
    last_candle_received_at: str | None = None
    last_exchange_timestamp: str | None = None
    new_orders_permitted: bool
    reason: str


class ReplayCatalogOut(BaseModel):
    instrument: str
    supported_instruments: list[str]
    supported_speeds: list[str]
    strategy_versions: list[dict[str, Any]]
    datasets: list[dict[str, Any]]


class ReplayStateOut(BaseModel):
    has_session: bool
    session: dict[str, Any] | None = None
    freshness: MarketDataFreshnessOut
    last_candle: dict[str, Any] | None = None
    strategy_evaluation: dict[str, Any] = Field(default_factory=dict)
    latest_signal: dict[str, Any] | None = None
    recent_signals: list[dict[str, Any]] = Field(default_factory=list)
    chart_candles: list[dict[str, Any]] = Field(default_factory=list)


class StartStrategyInstanceRequest(BaseModel):
    strategy_version_id: UUID
    order_type: OrderType = OrderType.MARKET
    limit_offset_pts: Decimal = Field(default=Decimal("0"), ge=Decimal("0"), le=Decimal("2000"))
    execution_latency_ms: int = Field(default=120, ge=0, le=30000)
    latency_bars: int = Field(default=1, ge=1, le=20)
    slippage_bps: Decimal | None = Field(default=None, ge=Decimal("0"), le=Decimal("250"))
    partial_fill_enabled: bool = False
    max_fill_ratio_per_bar: Decimal = Field(
        default=Decimal("1.0"), gt=Decimal("0"), le=Decimal("1.0")
    )
    valid_for_bars: int | None = Field(default=None, ge=1, le=500)
    auto_execute_signals: bool = True
    actor: str = Field(default="desk.operator", min_length=2, max_length=128)


class StrategyInstanceControlRequest(BaseModel):
    actor: str = Field(default="desk.operator", min_length=2, max_length=128)


class SubmitPaperOrderRequest(BaseModel):
    client_order_key: str = Field(..., min_length=3, max_length=160)
    instrument_symbol: str = Field(..., min_length=2, max_length=32)
    side: Side
    order_type: OrderType = OrderType.MARKET
    quantity: int = Field(..., ge=1, le=50000)
    reference_price: Decimal | None = Field(default=None, gt=Decimal("0"))
    limit_price: Decimal | None = Field(default=None, gt=Decimal("0"))
    stop_price: Decimal | None = Field(default=None, gt=Decimal("0"))
    stop_loss_price: Decimal | None = Field(default=None, gt=Decimal("0"))
    target_price: Decimal | None = Field(default=None, gt=Decimal("0"))
    execution_latency_ms: int = Field(default=120, ge=0, le=30000)
    latency_bars: int = Field(default=1, ge=0, le=20)
    slippage_bps: Decimal | None = Field(default=None, ge=Decimal("0"), le=Decimal("250"))
    max_fill_ratio_per_bar: Decimal = Field(
        default=Decimal("1.0"), gt=Decimal("0"), le=Decimal("1.0")
    )
    simulate_rejection: bool = False
    simulated_rejection_reason: str | None = Field(default=None, max_length=255)
    valid_for_bars: int | None = Field(default=None, ge=1, le=500)
    expires_at: datetime | None = None
    require_fresh_market_data: bool = True
    actor: str = Field(default="desk.operator", min_length=2, max_length=128)


class CancelOrderRequest(BaseModel):
    actor: str = Field(default="desk.operator", min_length=2, max_length=128)
    reason: str = Field(default="Cancelled by desk operator", min_length=2, max_length=255)


class CancelAllOrdersRequest(BaseModel):
    instrument_symbol: str | None = None
    actor: str = Field(default="desk.operator", min_length=2, max_length=128)
    reason: str = Field(
        default="Operator cancelled all working paper orders", min_length=2, max_length=255
    )


class ExitPositionRequest(BaseModel):
    client_order_key: str | None = Field(default=None, max_length=160)
    actor: str = Field(default="desk.operator", min_length=2, max_length=128)
    reason: str = Field(default="OPERATOR_EXIT", min_length=2, max_length=64)


class FlattenPositionsRequest(BaseModel):
    instrument_symbol: str | None = None
    actor: str = Field(default="desk.operator", min_length=2, max_length=128)


class KillSwitchRequest(BaseModel):
    actor: str = Field(default="risk.officer", min_length=2, max_length=128)
    reason: str = Field(
        default="Global Kill Switch toggled from Risk Center", min_length=3, max_length=500
    )


class ConfirmedFlattenRequest(BaseModel):
    confirm_flatten: bool = False
    instrument_symbol: str | None = None
    actor: str = Field(default="risk.officer", min_length=2, max_length=128)


class UpdateAccountRiskConfigRequest(BaseModel):
    max_daily_loss_inr: Decimal | None = None
    max_open_positions: int | None = None
    max_gross_exposure_inr: Decimal | None = None
    max_order_value_inr: Decimal | None = None
    max_trades_per_day: int | None = None
    max_consecutive_losses: int | None = None
    trading_session_enabled: bool | None = None
    session_start_ist: str | None = None
    session_end_ist: str | None = None
    market_data_stale_threshold_seconds: int | None = None
    max_signal_age_seconds: int | None = None
    enforce_system_health_gate: bool | None = None
    simulated_system_degraded: bool | None = None
    enforce_position_reconciliation_gate: bool | None = None
    default_max_quantity: int | None = None
    default_max_risk_per_trade_inr: Decimal | None = None
    default_strategy_daily_loss_inr: Decimal | None = None
    default_max_strategy_drawdown_inr: Decimal | None = None
    default_max_trades_per_strategy_day: int | None = None
    default_cooldown_seconds_after_loss: int | None = None
    default_max_holding_duration_seconds: int | None = None
    default_allowed_instruments: list[str] | None = None
    default_allowed_order_types: list[str] | None = None
    actor: str = Field(default="risk.officer", min_length=2, max_length=128)


class UpdateStrategyRiskConfigRequest(BaseModel):
    max_quantity: int | None = None
    max_risk_per_trade_inr: Decimal | None = None
    strategy_daily_loss_inr: Decimal | None = None
    max_strategy_drawdown_inr: Decimal | None = None
    max_trades_per_day: int | None = None
    cooldown_seconds_after_loss: int | None = None
    max_holding_duration_seconds: int | None = None
    allowed_instruments: list[str] | None = None
    allowed_order_types: list[str] | None = None
    is_enabled: bool | None = None
    actor: str = Field(default="risk.officer", min_length=2, max_length=128)

