"""Typed domain and infrastructure exceptions."""

from __future__ import annotations


class ControlCenterError(Exception):
    """Base exception for all Algo Trading Control Center errors."""

    code: str = "INTERNAL_ERROR"

    def __init__(self, message: str, *, code: str | None = None) -> None:
        super().__init__(message)
        if code is not None:
            self.code = code


class ConfigurationSafetyError(ControlCenterError):
    """Raised when an unsafe or unsupported execution configuration is supplied."""

    code = "CONFIGURATION_SAFETY_ERROR"


class InvalidStateTransitionError(ControlCenterError):
    """Raised when an entity attempts an illegal lifecycle transition."""

    code = "INVALID_STATE_TRANSITION"


class ImmutabilityViolationError(ControlCenterError):
    """Raised when code attempts to mutate or delete an immutable domain record."""

    code = "IMMUTABILITY_VIOLATION"


class EntityNotFoundError(ControlCenterError):
    """Raised when a requested domain record does not exist."""

    code = "ENTITY_NOT_FOUND"


class DuplicateEntityError(ControlCenterError):
    """Raised when an operation violates uniqueness or idempotency constraints."""

    code = "DUPLICATE_ENTITY"


class StrategyValidationError(ControlCenterError):
    """Raised when strategy parameters violate domain or session validation rules."""

    code = "STRATEGY_VALIDATION_ERROR"


class PaperTradingHaltedError(ControlCenterError):
    """Raised when new order generation is blocked by STOP PAPER TRADING."""

    code = "PAPER_TRADING_HALTED"


class StaleMarketDataError(ControlCenterError):
    """Raised when downstream paper order creation is refused due to STALE/STOPPED market data."""

    code = "STALE_MARKET_DATA"


class RiskRejectedError(ControlCenterError):
    """Raised when pre-trade risk evaluation blocks an order request."""

    code = "RISK_REJECTED"


class BrokerRejectedError(ControlCenterError):
    """Raised when the simulated Paper Broker rejects an order."""

    code = "BROKER_REJECTED"


class DirectPositionMutationForbiddenError(ControlCenterError):
    """Raised if any caller attempts to directly mutate positions outside execution fills."""

    code = "DIRECT_POSITION_MUTATION_FORBIDDEN"


class DatabaseUnavailableError(ControlCenterError):
    """Raised when PostgreSQL cannot be reached or fails health/session checks."""

    code = "DATABASE_UNAVAILABLE"


class HistoricalDataError(ControlCenterError):
    """Base error for historical market-data ingestion, validation, and export."""

    code = "HISTORICAL_DATA_ERROR"


class FyersAuthenticationRequiredError(HistoricalDataError):
    """Raised when FYERS historical-data token is missing, expired, or rejected."""

    code = "FYERS_AUTHENTICATION_REQUIRED"


class HistoricalProviderUnavailableError(HistoricalDataError):
    """Raised when an upstream historical data provider is disabled, rate-limited, or down."""

    code = "HISTORICAL_PROVIDER_UNAVAILABLE"


class DatasetInUseError(ImmutabilityViolationError):
    """Raised when attempting to delete a dataset referenced by backtests or replay sessions."""

    code = "DATASET_IN_USE"

