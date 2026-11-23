"""Framework-independent domain snapshots and value objects."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from decimal import Decimal
from typing import Any
from uuid import UUID

from core.domain.enums import (
    AccountDesignation,
    ActorType,
    AuditCategory,
    AuditSeverity,
    PaperAccountStatus,
    StrategyVersionStatus,
    SystemComponent,
    SystemComponentStatus,
    UserRole,
)


@dataclass(frozen=True)
class UserEntity:
    id: UUID
    username: str
    email: str
    display_name: str
    role: UserRole
    is_active: bool
    created_at: datetime
    updated_at: datetime


@dataclass(frozen=True)
class PaperAccountEntity:
    id: UUID
    account_code: str
    account_name: str
    owner_user_id: UUID | None
    is_demo_capital: bool
    account_designation: AccountDesignation
    currency: str
    initial_capital: Decimal
    available_capital: Decimal
    realized_pnl_today: Decimal
    unrealized_pnl: Decimal
    paper_trading_halted: bool
    halt_reason: str | None
    halted_at: datetime | None
    halted_by: str | None
    status: PaperAccountStatus
    version: int
    created_at: datetime
    updated_at: datetime


@dataclass(frozen=True)
class InstrumentEntity:
    id: UUID
    symbol: str
    display_name: str
    exchange: str
    segment: str
    instrument_type: str
    currency: str
    lot_size: int
    tick_size: Decimal
    session_open_ist: str
    session_close_ist: str
    metadata: dict[str, Any]
    is_active: bool
    created_at: datetime
    updated_at: datetime
    data_provider: str = "FYERS"
    provider_symbol: str = ""


@dataclass(frozen=True)
class StrategyDefinitionEntity:
    id: UUID
    code: str
    name: str
    category: str
    description: str
    parameter_schema: dict[str, Any]
    default_parameters: dict[str, Any]
    supported_symbols: list[str]
    is_enabled: bool
    created_at: datetime
    updated_at: datetime


@dataclass(frozen=True)
class StrategyVersionEntity:
    id: UUID
    strategy_definition_id: UUID
    strategy_code: str
    version_number: int
    version_label: str
    instrument_symbol: str
    timeframe: str
    parameters: dict[str, Any]
    parameter_hash: str
    notes: str | None
    created_by: str
    status: StrategyVersionStatus
    created_at: datetime
    cloned_from_version_id: UUID | None = None
    cloned_from_label: str | None = None


@dataclass(frozen=True)
class AuditEventEntity:
    id: UUID
    occurred_at: datetime
    occurred_at_utc: str
    correlation_id: str
    actor: str
    actor_type: ActorType
    category: AuditCategory
    action: str
    event_type: str
    severity: AuditSeverity
    entity_type: str | None
    entity_id: str | None
    strategy_id: str | None
    signal_id: str | None
    order_id: str | None
    source: str
    summary: str
    details: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class ComponentHealthSnapshot:
    component: SystemComponent
    status: SystemComponentStatus
    message: str
    checked_at: datetime
    latency_ms: Decimal | None = None
    details: dict[str, Any] = field(default_factory=dict)
