"""Operational desk controls, foundation reference queries, and STOP PAPER TRADING."""

from __future__ import annotations

from decimal import Decimal
from typing import Any
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.orm import Session

from core.audit.service import AuditService
from core.clock import ensure_ist, now_ist
from core.config import AppSettings, get_settings
from core.domain.entities import (
    InstrumentEntity,
    PaperAccountEntity,
    StrategyDefinitionEntity,
    StrategyVersionEntity,
)
from core.domain.enums import (
    AccountDesignation,
    ActorType,
    AuditCategory,
    AuditSeverity,
    PAPER_ACCOUNT_TRANSITIONS,
    PaperAccountStatus,
    SystemComponent,
    SystemComponentStatus,
    validate_state_transition,
)
from core.exceptions import (
    ControlCenterError,
    EntityNotFoundError,
    PaperTradingHaltedError,
)
from core.health.service import HealthService
from core.persistence.models import (
    AccountRiskConfigModel,
    InstrumentModel,
    PaperAccountModel,
)


def _account_to_entity(row: PaperAccountModel) -> PaperAccountEntity:
    return PaperAccountEntity(
        id=row.id,
        account_code=row.account_code,
        account_name=row.account_name,
        owner_user_id=row.owner_user_id,
        is_demo_capital=row.is_demo_capital,
        account_designation=AccountDesignation(row.account_designation),
        currency=row.currency,
        initial_capital=Decimal(row.initial_capital),
        available_capital=Decimal(row.available_capital),
        realized_pnl_today=Decimal(row.realized_pnl_today),
        unrealized_pnl=Decimal(row.unrealized_pnl),
        paper_trading_halted=bool(row.paper_trading_halted),
        halt_reason=row.halt_reason,
        halted_at=ensure_ist(row.halted_at) if row.halted_at else None,
        halted_by=row.halted_by,
        status=PaperAccountStatus(row.status),
        version=row.version,
        created_at=ensure_ist(row.created_at),
        updated_at=ensure_ist(row.updated_at),
    )


def _instrument_to_entity(row: InstrumentModel) -> InstrumentEntity:
    from core.market_data.models import INSTRUMENT_PROVIDER_REGISTRY

    spec = INSTRUMENT_PROVIDER_REGISTRY.get(row.symbol.upper())
    prov = (row.data_provider or "").strip() or (spec.data_provider if spec else "FYERS")
    prov_sym = (row.provider_symbol or "").strip() or (spec.provider_symbol if spec else "")
    return InstrumentEntity(
        id=row.id,
        symbol=row.symbol,
        display_name=row.display_name,
        exchange=row.exchange,
        segment=row.segment,
        instrument_type=row.instrument_type,
        currency=row.currency,
        lot_size=row.lot_size,
        tick_size=Decimal(row.tick_size),
        session_open_ist=row.session_open_ist,
        session_close_ist=row.session_close_ist,
        metadata=dict(row.metadata_json or {}),
        is_active=bool(row.is_active),
        created_at=ensure_ist(row.created_at),
        updated_at=ensure_ist(row.updated_at),
        data_provider=prov,
        provider_symbol=prov_sym,
    )


class ControlService:
    """Manages paper account halt controls, configurable instruments, and immutable strategy versions."""

    @staticmethod
    def get_primary_paper_account(
        session: Session, settings: AppSettings | None = None
    ) -> PaperAccountEntity:
        cfg = settings or get_settings()
        row = session.scalar(
            select(PaperAccountModel).where(
                PaperAccountModel.account_code == cfg.DEFAULT_PAPER_ACCOUNT_CODE
            )
        )
        if row is None:
            row = session.scalar(select(PaperAccountModel).order_by(PaperAccountModel.created_at))
        if row is None:
            raise EntityNotFoundError("No demonstration paper account is initialized in PostgreSQL.")
        return _account_to_entity(row)

    @staticmethod
    def assert_new_orders_permitted(account: PaperAccountEntity) -> None:
        """Gate used by Pre-Trade Risk & OMS before creating any NEW paper order."""
        if account.paper_trading_halted or account.status == PaperAccountStatus.HALTED:
            raise PaperTradingHaltedError(
                f"STOP PAPER TRADING is active ({account.halt_reason or 'Operator halt'}). "
                "New order generation is blocked; open positions remain untouched."
            )

    @staticmethod
    def stop_paper_trading(
        session: Session,
        *,
        actor: str,
        reason: str = "Operator engaged STOP PAPER TRADING from Control Center",
        settings: AppSettings | None = None,
    ) -> PaperAccountEntity:
        """Engage STOP PAPER TRADING idempotently.

        CRITICAL SAFETY INVARIANT:
        This halts generation of NEW orders only. It MUST NOT flatten or alter
        any open positions. Position flattening is a separate explicit command.
        """
        cfg = settings or get_settings()
        stmt = (
            select(PaperAccountModel)
            .where(PaperAccountModel.account_code == cfg.DEFAULT_PAPER_ACCOUNT_CODE)
            .with_for_update()
        )
        row = session.scalar(stmt)
        if row is None:
            raise EntityNotFoundError("Primary paper account not found.")

        if row.paper_trading_halted and row.status == PaperAccountStatus.HALTED.value:
            # Idempotent repeat call: return existing halted state cleanly
            return _account_to_entity(row)

        current_status = PaperAccountStatus(row.status)
        target_status = validate_state_transition(
            "PaperAccount",
            current_status,
            PaperAccountStatus.HALTED,
            PAPER_ACCOUNT_TRANSITIONS,
        )

        ts = now_ist()
        clean_reason = reason.strip() or "Operator engaged STOP PAPER TRADING"
        row.paper_trading_halted = True
        row.halt_reason = clean_reason
        row.halted_at = ts
        row.halted_by = actor
        row.status = target_status.value
        row.version += 1
        row.updated_at = ts

        risk_cfg = session.scalar(
            select(AccountRiskConfigModel).where(AccountRiskConfigModel.account_id == row.id)
        )
        if risk_cfg is not None:
            risk_cfg.global_kill_switch_active = True
            risk_cfg.kill_switch_reason = clean_reason
            risk_cfg.kill_switch_activated_at = ts
            risk_cfg.kill_switch_activated_by = actor
            risk_cfg.updated_by = actor
            risk_cfg.updated_at = ts

        session.flush()

        AuditService.record(
            session,
            action="PAPER_TRADING_STOPPED",
            summary=(
                f"STOP PAPER TRADING engaged by {actor}: new order generation blocked; "
                "open positions NOT flattened."
            ),
            actor=actor,
            actor_type=ActorType.OPERATOR,
            category=AuditCategory.CONTROL,
            severity=AuditSeverity.WARNING,
            entity_type="paper_accounts",
            entity_id=row.account_code,
            details={
                "reason": clean_reason,
                "blocks_new_orders": True,
                "auto_flatten_positions": False,
                "account_version": row.version,
            },
        )
        HealthService.record_health_event(
            session,
            component=SystemComponent.RISK_ENGINE,
            status=SystemComponentStatus.BLOCKED,
            message=f"STOP PAPER TRADING engaged by {actor}: {clean_reason}",
            details={"auto_flatten_positions": False, "account_code": row.account_code},
        )
        return _account_to_entity(row)

    @staticmethod
    def resume_paper_trading(
        session: Session,
        *,
        actor: str,
        reason: str = "Operator resumed paper trading",
        settings: AppSettings | None = None,
    ) -> PaperAccountEntity:
        """Disengage STOP PAPER TRADING idempotently so new paper orders may be evaluated again."""
        cfg = settings or get_settings()
        stmt = (
            select(PaperAccountModel)
            .where(PaperAccountModel.account_code == cfg.DEFAULT_PAPER_ACCOUNT_CODE)
            .with_for_update()
        )
        row = session.scalar(stmt)
        if row is None:
            raise EntityNotFoundError("Primary paper account not found.")

        if not row.paper_trading_halted and row.status == PaperAccountStatus.ACTIVE.value:
            return _account_to_entity(row)

        current_status = PaperAccountStatus(row.status)
        target_status = validate_state_transition(
            "PaperAccount",
            current_status,
            PaperAccountStatus.ACTIVE,
            PAPER_ACCOUNT_TRANSITIONS,
        )

        ts = now_ist()
        clean_reason = reason.strip() or "Operator resumed paper trading"
        row.paper_trading_halted = False
        row.halt_reason = None
        row.halted_at = None
        row.halted_by = None
        row.status = target_status.value
        row.version += 1
        row.updated_at = ts

        risk_cfg = session.scalar(
            select(AccountRiskConfigModel).where(AccountRiskConfigModel.account_id == row.id)
        )
        if risk_cfg is not None:
            risk_cfg.global_kill_switch_active = False
            risk_cfg.kill_switch_reason = None
            risk_cfg.kill_switch_activated_at = None
            risk_cfg.kill_switch_activated_by = None
            risk_cfg.updated_by = actor
            risk_cfg.updated_at = ts
        session.flush()

        AuditService.record(
            session,
            action="PAPER_TRADING_RESUMED",
            summary=f"Paper trading resumed by {actor}: new order gate re-enabled.",
            actor=actor,
            actor_type=ActorType.OPERATOR,
            category=AuditCategory.CONTROL,
            severity=AuditSeverity.INFO,
            entity_type="paper_accounts",
            entity_id=row.account_code,
            details={"reason": clean_reason, "account_version": row.version},
        )
        HealthService.record_health_event(
            session,
            component=SystemComponent.RISK_ENGINE,
            status=SystemComponentStatus.HEALTHY,
            message=f"Paper trading resumed by {actor}",
            details={"account_code": row.account_code},
        )
        return _account_to_entity(row)

    @staticmethod
    def list_instruments(session: Session) -> list[InstrumentEntity]:
        rows = session.scalars(select(InstrumentModel).order_by(InstrumentModel.symbol)).all()
        return [_instrument_to_entity(row) for row in rows]

    @staticmethod
    def update_instrument_reference(
        session: Session,
        *,
        symbol: str,
        lot_size: int,
        tick_size: Decimal,
        session_open_ist: str,
        session_close_ist: str,
        is_active: bool,
        actor: str,
    ) -> InstrumentEntity:
        """Update configurable reference data for an instrument with full before/after audit."""
        clean_symbol = symbol.strip().upper()
        row = session.scalar(
            select(InstrumentModel).where(InstrumentModel.symbol == clean_symbol).with_for_update()
        )
        if row is None:
            raise EntityNotFoundError(f"Instrument {clean_symbol!r} not found.")
        if lot_size < 1:
            raise ControlCenterError("lot_size must be >= 1.")
        if tick_size <= Decimal("0"):
            raise ControlCenterError("tick_size must be > 0.")

        before = {
            "lot_size": row.lot_size,
            "tick_size": str(row.tick_size),
            "session_open_ist": row.session_open_ist,
            "session_close_ist": row.session_close_ist,
            "is_active": row.is_active,
        }
        row.lot_size = lot_size
        row.tick_size = tick_size.quantize(Decimal("0.0001"))
        row.session_open_ist = session_open_ist.strip()
        row.session_close_ist = session_close_ist.strip()
        row.is_active = is_active
        row.updated_at = now_ist()
        session.flush()

        after = {
            "lot_size": row.lot_size,
            "tick_size": str(row.tick_size),
            "session_open_ist": row.session_open_ist,
            "session_close_ist": row.session_close_ist,
            "is_active": row.is_active,
        }
        AuditService.record(
            session,
            action="INSTRUMENT_REFERENCE_UPDATED",
            summary=f"Updated configurable reference spec for {clean_symbol} (lot_size={lot_size}, tick={tick_size}).",
            actor=actor,
            actor_type=ActorType.OPERATOR,
            category=AuditCategory.INSTRUMENT,
            severity=AuditSeverity.INFO,
            entity_type="instruments",
            entity_id=clean_symbol,
            details={"before": before, "after": after},
        )
        return _instrument_to_entity(row)

    @staticmethod
    def list_strategies_with_versions(
        session: Session,
        *,
        instrument: str | None = None,
    ) -> list[tuple[StrategyDefinitionEntity, list[StrategyVersionEntity]]]:
        from core.strategies.registry import StrategyRegistryService

        return StrategyRegistryService.list_definitions_with_versions(
            session, instrument=instrument
        )

    @staticmethod
    def create_strategy_version(
        session: Session,
        *,
        strategy_code: str,
        parameters: dict[str, Any],
        notes: str | None,
        actor: str,
    ) -> StrategyVersionEntity:
        """Create a new immutable StrategyVersion snapshot without mutating prior versions."""
        from core.strategies.registry import StrategyRegistryService

        return StrategyRegistryService.create_version(
            session,
            strategy_code=strategy_code,
            raw_parameters=parameters,
            notes=notes,
            actor=actor,
        )

    @staticmethod
    def get_version_by_id(session: Session, version_id: UUID) -> StrategyVersionEntity:
        from core.strategies.registry import StrategyRegistryService

        return StrategyRegistryService.get_version(session, version_id)
