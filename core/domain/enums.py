"""Canonical lifecycle enums and state-machine transition guards."""

from __future__ import annotations

from enum import StrEnum
from typing import TypeVar

from core.exceptions import InvalidStateTransitionError

StateT = TypeVar("StateT", bound=StrEnum)


class ExecutionMode(StrEnum):
    """Locked execution mode for V1 (real-money execution excluded)."""

    PAPER_ONLY = "PAPER_ONLY"


class MarketMode(StrEnum):
    """Supported market feed modes."""

    HISTORICAL_AND_REPLAY = "HISTORICAL_AND_REPLAY"
    BACKTEST = "BACKTEST"
    MARKET_REPLAY = "MARKET_REPLAY"


class SystemComponent(StrEnum):
    """Monitored subsystems displayed in Dashboard and System Health."""

    API_SERVER = "API_SERVER"
    DATABASE = "DATABASE"
    MARKET_REPLAY = "MARKET_REPLAY"
    MARKET_DATA = "MARKET_DATA"
    STRATEGY_ENGINE = "STRATEGY_ENGINE"
    RISK_ENGINE = "RISK_ENGINE"
    PAPER_BROKER = "PAPER_BROKER"
    POSITION_ENGINE = "POSITION_ENGINE"
    LAST_RECONCILIATION = "LAST_RECONCILIATION"


class SystemComponentStatus(StrEnum):
    """Canonical subsystem operational health values."""

    HEALTHY = "HEALTHY"
    DEGRADED = "DEGRADED"
    STALE = "STALE"
    BLOCKED = "BLOCKED"
    STOPPED = "STOPPED"


class UserRole(StrEnum):
    """Control-center operator roles."""

    ADMIN = "ADMIN"
    OPERATOR = "OPERATOR"
    RISK_OFFICER = "RISK_OFFICER"
    VIEWER = "VIEWER"


class AccountDesignation(StrEnum):
    """Explicit capital classification ensuring simulated funds are unmistakable."""

    DEMO_PAPER_ONLY = "DEMO_PAPER_ONLY"


class PaperAccountStatus(StrEnum):
    """Lifecycle states of a paper trading account."""

    ACTIVE = "ACTIVE"
    HALTED = "HALTED"
    ARCHIVED = "ARCHIVED"


class StrategyVersionStatus(StrEnum):
    """Lifecycle states of an immutable strategy version."""

    DRAFT = "DRAFT"
    VALIDATED = "VALIDATED"
    ACTIVE = "ACTIVE"
    RETIRED = "RETIRED"


class AuditCategory(StrEnum):
    """Audit trail event categories."""

    SYSTEM = "SYSTEM"
    CONTROL = "CONTROL"
    RISK = "RISK"
    STRATEGY = "STRATEGY"
    BACKTEST = "BACKTEST"
    MARKET_DATA = "MARKET_DATA"
    OMS = "OMS"
    EXECUTION = "EXECUTION"
    POSITION = "POSITION"
    RECONCILIATION = "RECONCILIATION"
    ACCOUNT = "ACCOUNT"
    INSTRUMENT = "INSTRUMENT"
    CONFIG = "CONFIG"
    HEALTH = "HEALTH"


class AuditSeverity(StrEnum):
    """Audit event operational severity."""

    INFO = "INFO"
    WARNING = "WARNING"
    CRITICAL = "CRITICAL"


class ActorType(StrEnum):
    """Originator category for an audited command or system transition."""

    SYSTEM = "SYSTEM"
    OPERATOR = "OPERATOR"
    API = "API"


class Side(StrEnum):
    """Order / signal directional side."""

    BUY = "BUY"
    SELL = "SELL"


class SignalDirection(StrEnum):
    """Explicit strategy signal intent (strategies emit signals only, never fills)."""

    ENTRY_LONG = "ENTRY_LONG"
    EXIT_LONG = "EXIT_LONG"
    ENTRY_SHORT = "ENTRY_SHORT"
    EXIT_SHORT = "EXIT_SHORT"


class ExitReason(StrEnum):
    """Configurable strategy exit trigger classification."""

    STOP_LOSS = "STOP_LOSS"
    TRAILING_STOP = "TRAILING_STOP"
    PROFIT_TARGET = "PROFIT_TARGET"
    OPPOSITE_SIGNAL = "OPPOSITE_SIGNAL"
    MAX_HOLDING_PERIOD = "MAX_HOLDING_PERIOD"
    EOD_SQUARE_OFF = "EOD_SQUARE_OFF"
    END_OF_BACKTEST = "END_OF_BACKTEST"
    OPERATOR_EXIT = "OPERATOR_EXIT"
    OPERATOR_FLATTEN_ALL = "OPERATOR_FLATTEN_ALL"


class TradeDirection(StrEnum):
    """Explicit trade ledger position direction."""

    LONG = "LONG"
    SHORT = "SHORT"


class BacktestStatus(StrEnum):
    """Canonical backtest run completion outcomes."""

    COMPLETED = "COMPLETED"
    COMPLETED_WITH_WARNINGS = "COMPLETED_WITH_WARNINGS"
    FAILED_VALIDATION = "FAILED_VALIDATION"
    INSUFFICIENT_DATA = "INSUFFICIENT_DATA"


class ExitTargetUnit(StrEnum):
    """Unit convention for stop-loss, profit-target, and trailing-stop distances."""

    POINTS = "POINTS"
    PERCENT = "PERCENT"


class PositionSizingMode(StrEnum):
    """Configurable position sizing method stored on immutable strategy versions."""

    FIXED_LOTS = "FIXED_LOTS"
    FIXED_QUANTITY = "FIXED_QUANTITY"
    RISK_PER_TRADE_PCT = "RISK_PER_TRADE_PCT"


class BarTimeframe(StrEnum):
    """Supported OHLCV bar intervals."""

    M1 = "1m"
    M3 = "3m"
    M5 = "5m"
    M10 = "10m"
    M15 = "15m"
    M30 = "30m"
    M60 = "60m"
    D1 = "1d"


class OrderType(StrEnum):
    """Supported simulated paper order pricing types."""

    MARKET = "MARKET"
    LIMIT = "LIMIT"
    STOP = "STOP"


class OrderSource(StrEnum):
    """Originator classification for an OMS paper order."""

    STRATEGY_SIGNAL = "STRATEGY_SIGNAL"
    OPERATOR_MANUAL = "OPERATOR_MANUAL"
    POSITION_EXIT = "POSITION_EXIT"


class SignalStatus(StrEnum):
    """Strategy signal lifecycle states."""

    GENERATED = "GENERATED"
    EXPIRED = "EXPIRED"
    DUPLICATE_SUPPRESSED = "DUPLICATE_SUPPRESSED"
    RISK_APPROVED = "RISK_APPROVED"
    RISK_REJECTED = "RISK_REJECTED"
    ORDER_CREATED = "ORDER_CREATED"


class OrderStatus(StrEnum):
    """Canonical OMS order lifecycle states (active progression + terminal alternatives)."""

    CREATED = "CREATED"
    RISK_APPROVED = "RISK_APPROVED"
    SUBMITTED = "SUBMITTED"
    ACKNOWLEDGED = "ACKNOWLEDGED"
    PARTIALLY_FILLED = "PARTIALLY_FILLED"
    FILLED = "FILLED"
    RISK_REJECTED = "RISK_REJECTED"
    REJECTED = "REJECTED"
    CANCELLED = "CANCELLED"
    EXPIRED = "EXPIRED"
    FAILED = "FAILED"


WORKING_ORDER_STATUSES: frozenset[OrderStatus] = frozenset(
    {
        OrderStatus.CREATED,
        OrderStatus.RISK_APPROVED,
        OrderStatus.SUBMITTED,
        OrderStatus.ACKNOWLEDGED,
        OrderStatus.PARTIALLY_FILLED,
    }
)

TERMINAL_ORDER_STATUSES: frozenset[OrderStatus] = frozenset(
    {
        OrderStatus.FILLED,
        OrderStatus.RISK_REJECTED,
        OrderStatus.REJECTED,
        OrderStatus.CANCELLED,
        OrderStatus.EXPIRED,
        OrderStatus.FAILED,
    }
)


class StrategyInstanceStatus(StrEnum):
    """Paper trading strategy runner instance lifecycle states."""

    RUNNING = "RUNNING"
    PAUSED = "PAUSED"
    STOPPED = "STOPPED"


class PositionStatus(StrEnum):
    """Derived paper position lifecycle states."""

    OPEN = "OPEN"
    CLOSED = "CLOSED"


class RiskDecisionOutcome(StrEnum):
    """Outcome of an independent pre-trade risk evaluation."""

    APPROVED = "APPROVED"
    REJECTED = "REJECTED"


class RiskRuleScope(StrEnum):
    """Classification of independent risk engine rules."""

    OPERATIONAL = "OPERATIONAL"
    STRATEGY = "STRATEGY"
    ACCOUNT = "ACCOUNT"


class ReconciliationStatus(StrEnum):
    """Outcome states of a deterministic reconciliation run."""

    COMPLETED = "COMPLETED"
    DISCREPANCY_DETECTED = "DISCREPANCY_DETECTED"
    REPAIRED = "REPAIRED"
    FAILED = "FAILED"


class AuditEventType(StrEnum):
    """Canonical structured operational audit event types."""

    LOGIN = "LOGIN"
    STRATEGY_CREATED = "STRATEGY_CREATED"
    STRATEGY_VERSION_CREATED = "STRATEGY_VERSION_CREATED"
    BACKTEST_STARTED = "BACKTEST_STARTED"
    BACKTEST_COMPLETED = "BACKTEST_COMPLETED"
    REPLAY_STARTED = "REPLAY_STARTED"
    REPLAY_PAUSED = "REPLAY_PAUSED"
    STRATEGY_STARTED = "STRATEGY_STARTED"
    STRATEGY_PAUSED = "STRATEGY_PAUSED"
    STRATEGY_STOPPED = "STRATEGY_STOPPED"
    SIGNAL_GENERATED = "SIGNAL_GENERATED"
    RISK_APPROVED = "RISK_APPROVED"
    RISK_REJECTED = "RISK_REJECTED"
    ORDER_CREATED = "ORDER_CREATED"
    ORDER_SUBMITTED = "ORDER_SUBMITTED"
    ORDER_FILLED = "ORDER_FILLED"
    ORDER_REJECTED = "ORDER_REJECTED"
    ORDER_CANCELLED = "ORDER_CANCELLED"
    POSITION_OPENED = "POSITION_OPENED"
    POSITION_CLOSED = "POSITION_CLOSED"
    STOP_TRIGGERED = "STOP_TRIGGERED"
    TARGET_TRIGGERED = "TARGET_TRIGGERED"
    KILL_SWITCH_ACTIVATED = "KILL_SWITCH_ACTIVATED"
    KILL_SWITCH_DEACTIVATED = "KILL_SWITCH_DEACTIVATED"
    MARKET_DATA_STALE = "MARKET_DATA_STALE"
    MARKET_DATA_RECOVERED = "MARKET_DATA_RECOVERED"
    RECONCILIATION_COMPLETED = "RECONCILIATION_COMPLETED"
    RECONCILIATION_FAILED = "RECONCILIATION_FAILED"
    HISTORICAL_DOWNLOAD_REQUESTED = "HISTORICAL_DOWNLOAD_REQUESTED"
    HISTORICAL_DOWNLOAD_STARTED = "HISTORICAL_DOWNLOAD_STARTED"
    HISTORICAL_DOWNLOAD_CHUNK_COMPLETED = "HISTORICAL_DOWNLOAD_CHUNK_COMPLETED"
    HISTORICAL_DOWNLOAD_COMPLETED = "HISTORICAL_DOWNLOAD_COMPLETED"
    HISTORICAL_DOWNLOAD_PARTIAL = "HISTORICAL_DOWNLOAD_PARTIAL"
    HISTORICAL_DOWNLOAD_FAILED = "HISTORICAL_DOWNLOAD_FAILED"
    HISTORICAL_DATA_AUTHENTICATION_REQUIRED = "HISTORICAL_DATA_AUTHENTICATION_REQUIRED"
    HISTORICAL_DATASET_VALIDATED = "HISTORICAL_DATASET_VALIDATED"
    HISTORICAL_DATASET_SELECTED_FOR_BACKTEST = "HISTORICAL_DATASET_SELECTED_FOR_BACKTEST"
    HISTORICAL_DATASET_SELECTED_FOR_REPLAY = "HISTORICAL_DATASET_SELECTED_FOR_REPLAY"
    HISTORICAL_DATASET_CSV_EXPORTED = "HISTORICAL_DATASET_CSV_EXPORTED"
    REAL_DATA_REPLAY_STARTED = "REAL_DATA_REPLAY_STARTED"
    REAL_DATA_REPLAY_PAUSED = "REAL_DATA_REPLAY_PAUSED"
    REAL_DATA_REPLAY_COMPLETED = "REAL_DATA_REPLAY_COMPLETED"


class ReplaySessionStatus(StrEnum):
    """Market replay & paper trading session lifecycle states."""

    CONFIGURED = "CONFIGURED"
    RUNNING = "RUNNING"
    PAUSED = "PAUSED"
    PAUSED_RECOVERY = "PAUSED_RECOVERY"
    COMPLETED = "COMPLETED"
    STOPPED = "STOPPED"


class ReplaySpeed(StrEnum):
    """Supported deterministic market replay speed multipliers."""

    X1 = "1x"
    X5 = "5x"
    X10 = "10x"
    X25 = "25x"
    X50 = "50x"


REPLAY_SPEED_MULTIPLIERS: dict[ReplaySpeed, int] = {
    ReplaySpeed.X1: 1,
    ReplaySpeed.X5: 5,
    ReplaySpeed.X10: 10,
    ReplaySpeed.X25: 25,
    ReplaySpeed.X50: 50,
}


class MarketDataFreshnessStatus(StrEnum):
    """Canonical market-data freshness states gating downstream paper order execution."""

    HEALTHY = "HEALTHY"
    STALE = "STALE"
    STOPPED = "STOPPED"


class MarketFeedSource(StrEnum):
    """Originator classification for normalized MarketEvent streams."""

    HISTORICAL_REPLAY = "HISTORICAL_REPLAY"
    FYERS_WEBSOCKET = "FYERS_WEBSOCKET"
    OTHER_BROKER_FEED = "OTHER_BROKER_FEED"


class MarketEventType(StrEnum):
    """Normalized market stream event classifications."""

    SESSION_OPEN = "SESSION_OPEN"
    CANDLE = "CANDLE"
    SESSION_CLOSE = "SESSION_CLOSE"
    HEARTBEAT = "HEARTBEAT"


class StrategyEvaluationState(StrEnum):
    """Explainable strategy evaluation status displayed on the Market Replay desk."""

    STANDBY = "STANDBY"
    WARMING_UP = "WARMING_UP"
    OUTSIDE_TRADING_WINDOW = "OUTSIDE_TRADING_WINDOW"
    DAILY_LIMIT_REACHED = "DAILY_LIMIT_REACHED"
    EVALUATING_NO_SIGNAL = "EVALUATING_NO_SIGNAL"
    SIGNAL_GENERATED = "SIGNAL_GENERATED"
    HALTED_BY_OPERATOR = "HALTED_BY_OPERATOR"


PAPER_ACCOUNT_TRANSITIONS: dict[PaperAccountStatus, frozenset[PaperAccountStatus]] = {
    PaperAccountStatus.ACTIVE: frozenset({PaperAccountStatus.HALTED, PaperAccountStatus.ARCHIVED}),
    PaperAccountStatus.HALTED: frozenset({PaperAccountStatus.ACTIVE, PaperAccountStatus.ARCHIVED}),
    PaperAccountStatus.ARCHIVED: frozenset(),
}

STRATEGY_VERSION_TRANSITIONS: dict[StrategyVersionStatus, frozenset[StrategyVersionStatus]] = {
    StrategyVersionStatus.DRAFT: frozenset(
        {StrategyVersionStatus.VALIDATED, StrategyVersionStatus.RETIRED}
    ),
    StrategyVersionStatus.VALIDATED: frozenset(
        {StrategyVersionStatus.ACTIVE, StrategyVersionStatus.RETIRED}
    ),
    StrategyVersionStatus.ACTIVE: frozenset(
        {StrategyVersionStatus.VALIDATED, StrategyVersionStatus.RETIRED}
    ),
    StrategyVersionStatus.RETIRED: frozenset(),
}

SIGNAL_TRANSITIONS: dict[SignalStatus, frozenset[SignalStatus]] = {
    SignalStatus.GENERATED: frozenset(
        {
            SignalStatus.EXPIRED,
            SignalStatus.DUPLICATE_SUPPRESSED,
            SignalStatus.RISK_APPROVED,
            SignalStatus.RISK_REJECTED,
        }
    ),
    SignalStatus.RISK_APPROVED: frozenset({SignalStatus.ORDER_CREATED, SignalStatus.EXPIRED}),
    SignalStatus.EXPIRED: frozenset(),
    SignalStatus.DUPLICATE_SUPPRESSED: frozenset(),
    SignalStatus.RISK_REJECTED: frozenset(),
    SignalStatus.ORDER_CREATED: frozenset(),
}

ORDER_TRANSITIONS: dict[OrderStatus, frozenset[OrderStatus]] = {
    OrderStatus.CREATED: frozenset(
        {
            OrderStatus.RISK_APPROVED,
            OrderStatus.RISK_REJECTED,
            OrderStatus.CANCELLED,
            OrderStatus.EXPIRED,
            OrderStatus.FAILED,
        }
    ),
    OrderStatus.RISK_APPROVED: frozenset(
        {
            OrderStatus.SUBMITTED,
            OrderStatus.CANCELLED,
            OrderStatus.EXPIRED,
            OrderStatus.FAILED,
        }
    ),
    OrderStatus.SUBMITTED: frozenset(
        {
            OrderStatus.ACKNOWLEDGED,
            OrderStatus.REJECTED,
            OrderStatus.CANCELLED,
            OrderStatus.EXPIRED,
            OrderStatus.FAILED,
        }
    ),
    OrderStatus.ACKNOWLEDGED: frozenset(
        {
            OrderStatus.PARTIALLY_FILLED,
            OrderStatus.FILLED,
            OrderStatus.REJECTED,
            OrderStatus.CANCELLED,
            OrderStatus.EXPIRED,
            OrderStatus.FAILED,
        }
    ),
    OrderStatus.PARTIALLY_FILLED: frozenset(
        {
            OrderStatus.PARTIALLY_FILLED,
            OrderStatus.FILLED,
            OrderStatus.CANCELLED,
            OrderStatus.EXPIRED,
            OrderStatus.FAILED,
        }
    ),
    OrderStatus.RISK_REJECTED: frozenset(),
    OrderStatus.REJECTED: frozenset(),
    OrderStatus.FILLED: frozenset(),
    OrderStatus.CANCELLED: frozenset(),
    OrderStatus.EXPIRED: frozenset(),
    OrderStatus.FAILED: frozenset(),
}

STRATEGY_INSTANCE_TRANSITIONS: dict[StrategyInstanceStatus, frozenset[StrategyInstanceStatus]] = {
    StrategyInstanceStatus.RUNNING: frozenset(
        {StrategyInstanceStatus.PAUSED, StrategyInstanceStatus.STOPPED}
    ),
    StrategyInstanceStatus.PAUSED: frozenset(
        {StrategyInstanceStatus.RUNNING, StrategyInstanceStatus.STOPPED}
    ),
    StrategyInstanceStatus.STOPPED: frozenset(),
}

POSITION_TRANSITIONS: dict[PositionStatus, frozenset[PositionStatus]] = {
    PositionStatus.OPEN: frozenset({PositionStatus.OPEN, PositionStatus.CLOSED}),
    PositionStatus.CLOSED: frozenset(),
}

REPLAY_SESSION_TRANSITIONS: dict[ReplaySessionStatus, frozenset[ReplaySessionStatus]] = {
    ReplaySessionStatus.CONFIGURED: frozenset(
        {ReplaySessionStatus.RUNNING, ReplaySessionStatus.STOPPED}
    ),
    ReplaySessionStatus.RUNNING: frozenset(
        {
            ReplaySessionStatus.PAUSED,
            ReplaySessionStatus.PAUSED_RECOVERY,
            ReplaySessionStatus.COMPLETED,
            ReplaySessionStatus.STOPPED,
        }
    ),
    ReplaySessionStatus.PAUSED: frozenset(
        {ReplaySessionStatus.RUNNING, ReplaySessionStatus.STOPPED}
    ),
    ReplaySessionStatus.PAUSED_RECOVERY: frozenset(
        {ReplaySessionStatus.RUNNING, ReplaySessionStatus.STOPPED}
    ),
    ReplaySessionStatus.COMPLETED: frozenset(),
    ReplaySessionStatus.STOPPED: frozenset(),
}


def validate_state_transition(
    entity_name: str,
    current: StateT,
    target: StateT,
    allowed_map: dict[StateT, frozenset[StateT]],
) -> StateT:
    """Validate that transitioning `current -> target` is explicitly allowed."""
    allowed = allowed_map.get(current, frozenset())
    if target not in allowed:
        raise InvalidStateTransitionError(
            f"Illegal {entity_name} transition: {current.value} -> {target.value}"
        )
    return target
