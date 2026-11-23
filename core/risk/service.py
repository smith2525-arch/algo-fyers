"""Risk Configuration, Persisted Risk Decision Audit, Global Kill Switch & Confirmed Flatten Service."""

from __future__ import annotations

import uuid
from decimal import Decimal
from typing import Any

from sqlalchemy import desc, select
from sqlalchemy.orm import Session

from core.audit.service import AuditService
from core.clock import format_ist_iso, now_ist, parse_hms_time
from core.config import AppSettings, get_settings
from core.controls.service import ControlService
from core.domain.enums import (
    ActorType,
    AuditCategory,
    AuditSeverity,
    PositionStatus,
    RiskDecisionOutcome,
)
from core.exceptions import ControlCenterError, EntityNotFoundError
from core.logging import get_correlation_id
from core.market_data.freshness import evaluate_market_data_freshness
from core.persistence.models import (
    AccountRiskConfigModel,
    PaperAccountModel,
    PaperPositionModel,
    ReplaySessionModel,
    RiskDecisionModel,
    StrategyRiskConfigModel,
    StrategyVersionModel,
)
from core.positions.service import PositionService
from core.risk.evaluator import PreTradeRiskEvaluator, RiskDecision

_Q2 = Decimal("0.01")
_Q4 = Decimal("0.0001")


class RiskControlService:
    """Manages configurable paper-risk rules, immutable risk decisions, kill switch & confirmed flatten."""

    @staticmethod
    def get_primary_account(
        session: Session, settings: AppSettings | None = None
    ) -> PaperAccountModel:
        cfg = settings or get_settings()
        acct = session.scalar(
            select(PaperAccountModel).where(
                PaperAccountModel.account_code == cfg.DEFAULT_PAPER_ACCOUNT_CODE
            )
        )
        if acct is None:
            acct = session.scalar(
                select(PaperAccountModel).order_by(PaperAccountModel.created_at.asc()).limit(1)
            )
        if acct is None:
            raise EntityNotFoundError("Primary demonstration paper account not found.")
        return acct

    @staticmethod
    def get_account_risk_config(
        session: Session, account_id: uuid.UUID
    ) -> AccountRiskConfigModel:
        row = session.scalar(
            select(AccountRiskConfigModel).where(AccountRiskConfigModel.account_id == account_id)
        )
        if row is None:
            raise EntityNotFoundError(
                "Persisted AccountRiskConfig not found (fail-closed state)."
            )
        return row

    @staticmethod
    def record_risk_decision(
        session: Session,
        *,
        decision: RiskDecision | None = None,
        outcome: RiskDecisionOutcome | None = None,
        primary_rule: str | None = None,
        current_value: str | None = None,
        limit_value: str | None = None,
        reason: str | None = None,
        checks: list[dict[str, Any]] | None = None,
        account_id: uuid.UUID,
        order_id: uuid.UUID | None = None,
        client_order_key: str | None = None,
        signal_id: uuid.UUID | None = None,
        strategy_instance_id: uuid.UUID | None = None,
        strategy_version_id: uuid.UUID | None = None,
        strategy_label: str = "MANUAL_PAPER_DESK",
        instrument_symbol: str,
        side: str,
        order_type: str,
        proposed_quantity: int,
        proposed_price: Decimal | None = None,
        proposed_notional_inr: Decimal = Decimal("0"),
        actor: str = "risk.engine",
    ) -> RiskDecisionModel:
        """Persist an immutable `RiskDecisionModel` row and append-only `AuditEventModel`."""
        ts_now = now_ist()
        corr_id = get_correlation_id()

        eff_outcome = decision.decision if decision is not None else (outcome or RiskDecisionOutcome.REJECTED)
        eff_rule = decision.primary_rule if decision is not None else (primary_rule or "UNKNOWN_RULE")
        eff_curr = decision.current_value if decision is not None else (current_value or "N/A")
        eff_lim = decision.limit_value if decision is not None else (limit_value or "N/A")
        eff_reason = decision.reason if decision is not None else (reason or "Risk check evaluated.")
        eff_checks = decision.checks if decision is not None else (checks or [])
        eff_notional = (
            decision.estimated_notional_inr
            if decision is not None
            else Decimal(str(proposed_notional_inr))
        )

        dec_code = f"RSK-{ts_now.strftime('%H%M%S')}-{uuid.uuid4().hex[:6].upper()}"
        row = RiskDecisionModel(
            decision_code=dec_code,
            decision=eff_outcome.value,
            primary_rule=eff_rule,
            current_value=eff_curr,
            limit_value=eff_lim,
            reason=eff_reason,
            account_id=account_id,
            order_id=order_id,
            client_order_key=client_order_key,
            signal_id=signal_id,
            strategy_instance_id=strategy_instance_id,
            strategy_version_id=strategy_version_id,
            strategy_label=strategy_label or "MANUAL_PAPER_DESK",
            instrument_symbol=instrument_symbol.strip().upper(),
            side=side.strip().upper(),
            order_type=order_type.strip().upper(),
            proposed_quantity=int(proposed_quantity),
            proposed_price=proposed_price,
            proposed_notional_inr=eff_notional.quantize(_Q4),
            checks_json=eff_checks,
            correlation_id=corr_id,
            evaluated_at=ts_now,
        )
        session.add(row)
        session.flush()

        AuditService.record(
            session,
            action=f"RISK_DECISION_{eff_outcome.value}",
            summary=(
                f"[{eff_outcome.value}] Rule={eff_rule} ({eff_curr} vs {eff_lim}) on "
                f"{side} {proposed_quantity} {instrument_symbol} ({strategy_label or 'MANUAL_PAPER_DESK'}): {eff_reason}"
            ),
            actor=actor,
            actor_type=ActorType.SYSTEM,
            category=AuditCategory.RISK,
            severity=(
                AuditSeverity.INFO
                if eff_outcome == RiskDecisionOutcome.APPROVED
                else AuditSeverity.WARNING
            ),
            entity_type="risk_decision",
            entity_id=str(row.id),
            details={
                "decision_code": dec_code,
                "decision": eff_outcome.value,
                "rule": eff_rule,
                "current_value": eff_curr,
                "limit_value": eff_lim,
                "strategy": strategy_label or "MANUAL_PAPER_DESK",
                "signal_id": str(signal_id) if signal_id else None,
                "order_id": str(order_id) if order_id else None,
                "client_order_key": client_order_key,
                "reason": eff_reason,
            },
        )
        return row

    # -------------------------------------------------------------------------
    # Global Kill Switch & Explicit Confirmed Flatten
    # -------------------------------------------------------------------------

    @staticmethod
    def activate_kill_switch(
        session: Session,
        *,
        actor: str = "risk.officer",
        reason: str = "Global Kill Switch activated from Risk Center",
        settings: AppSettings | None = None,
    ) -> dict[str, Any]:
        """Activate persistent Global Kill Switch without closing open positions."""
        cfg = settings or get_settings()
        acct_entity = ControlService.stop_paper_trading(
            session, actor=actor, reason=reason, settings=cfg
        )
        acct = RiskControlService.get_primary_account(session, cfg)
        risk_cfg = RiskControlService.get_account_risk_config(session, acct.id)

        ts_now = now_ist()
        risk_cfg.global_kill_switch_active = True
        risk_cfg.kill_switch_reason = reason.strip()
        risk_cfg.kill_switch_activated_at = risk_cfg.kill_switch_activated_at or ts_now
        risk_cfg.kill_switch_activated_by = actor
        risk_cfg.updated_by = actor
        risk_cfg.updated_at = ts_now
        session.flush()

        AuditService.record(
            session,
            action="GLOBAL_KILL_SWITCH_ACTIVATED",
            summary=(
                f"Global Kill Switch activated by {actor}: NEW paper orders immediately blocked; "
                "open positions preserved (auto_flatten_positions=False)."
            ),
            actor=actor,
            actor_type=ActorType.OPERATOR,
            category=AuditCategory.RISK,
            severity=AuditSeverity.CRITICAL,
            entity_type="account_risk_config",
            entity_id=str(risk_cfg.id),
            details={
                "account_code": acct_entity.account_code,
                "reason": reason.strip(),
                "auto_flatten_positions": False,
            },
        )
        return RiskControlService.serialize_account_risk_config(risk_cfg)

    @staticmethod
    def deactivate_kill_switch(
        session: Session,
        *,
        actor: str = "risk.officer",
        reason: str = "Global Kill Switch deactivated after desk safety verification",
        settings: AppSettings | None = None,
    ) -> dict[str, Any]:
        """Deactivate persistent Global Kill Switch to resume pre-trade evaluation."""
        cfg = settings or get_settings()
        acct_entity = ControlService.resume_paper_trading(
            session, actor=actor, reason=reason, settings=cfg
        )
        acct = RiskControlService.get_primary_account(session, cfg)
        risk_cfg = RiskControlService.get_account_risk_config(session, acct.id)

        ts_now = now_ist()
        risk_cfg.global_kill_switch_active = False
        risk_cfg.kill_switch_reason = None
        risk_cfg.kill_switch_activated_at = None
        risk_cfg.kill_switch_activated_by = None
        risk_cfg.updated_by = actor
        risk_cfg.updated_at = ts_now
        session.flush()

        AuditService.record(
            session,
            action="GLOBAL_KILL_SWITCH_DEACTIVATED",
            summary=f"Global Kill Switch deactivated by {actor}: {reason.strip()}",
            actor=actor,
            actor_type=ActorType.OPERATOR,
            category=AuditCategory.RISK,
            severity=AuditSeverity.INFO,
            entity_type="account_risk_config",
            entity_id=str(risk_cfg.id),
            details={"account_code": acct_entity.account_code, "reason": reason.strip()},
        )
        return RiskControlService.serialize_account_risk_config(risk_cfg)

    @staticmethod
    def flatten_paper_positions_confirmed(
        session: Session,
        *,
        confirm_flatten: bool,
        instrument_symbol: str | None = None,
        actor: str = "risk.officer",
        settings: AppSettings | None = None,
    ) -> dict[str, Any]:
        """Separate explicit-confirmation FLATTEN PAPER POSITIONS operation."""
        if not confirm_flatten:
            raise ControlCenterError(
                "Explicit confirmation (confirm_flatten=True) is required before flattening open paper positions.",
                code="FLATTEN_CONFIRMATION_REQUIRED",
            )
        from core.paper_trading.service import PaperTradingService

        return PaperTradingService.flatten_all_open_positions(
            session,
            instrument_symbol=instrument_symbol,
            actor=actor,
            settings=settings,
        )

    # -------------------------------------------------------------------------
    # Configurable Account & Strategy Risk Rules CRUD
    # -------------------------------------------------------------------------

    @staticmethod
    def update_account_risk_config(
        session: Session,
        *,
        updates: dict[str, Any],
        actor: str = "risk.officer",
        settings: AppSettings | None = None,
    ) -> dict[str, Any]:
        cfg = settings or get_settings()
        acct = RiskControlService.get_primary_account(session, cfg)
        row = RiskControlService.get_account_risk_config(session, acct.id)

        if "session_start_ist" in updates or "session_end_ist" in updates:
            s_start = str(updates.get("session_start_ist", row.session_start_ist))
            s_end = str(updates.get("session_end_ist", row.session_end_ist))
            if parse_hms_time(s_start) >= parse_hms_time(s_end):
                raise ControlCenterError(
                    "session_start_ist must be strictly earlier than session_end_ist.",
                    code="INVALID_RISK_SESSION_WINDOW",
                )
            row.session_start_ist = s_start
            row.session_end_ist = s_end

        decimal_fields = (
            "max_daily_loss_inr",
            "max_gross_exposure_inr",
            "max_order_value_inr",
            "default_max_risk_per_trade_inr",
            "default_strategy_daily_loss_inr",
            "default_max_strategy_drawdown_inr",
        )
        for df in decimal_fields:
            if df in updates and updates[df] is not None:
                val = Decimal(str(updates[df]))
                if val <= Decimal("0"):
                    raise ControlCenterError(f"{df} must be > 0.", code="INVALID_RISK_LIMIT")
                setattr(row, df, val.quantize(_Q4))

        int_fields = (
            "max_open_positions",
            "max_trades_per_day",
            "max_consecutive_losses",
            "market_data_stale_threshold_seconds",
            "max_signal_age_seconds",
            "default_max_quantity",
            "default_max_trades_per_strategy_day",
            "default_cooldown_seconds_after_loss",
            "default_max_holding_duration_seconds",
        )
        for inf in int_fields:
            if inf in updates and updates[inf] is not None:
                ival = int(updates[inf])
                min_v = 0 if inf == "default_cooldown_seconds_after_loss" else 1
                if ival < min_v:
                    raise ControlCenterError(
                        f"{inf} must be >= {min_v}.", code="INVALID_RISK_LIMIT"
                    )
                setattr(row, inf, ival)

        bool_fields = (
            "trading_session_enabled",
            "enforce_system_health_gate",
            "simulated_system_degraded",
            "enforce_position_reconciliation_gate",
        )
        for bf in bool_fields:
            if bf in updates and updates[bf] is not None:
                setattr(row, bf, bool(updates[bf]))

        if "default_allowed_instruments" in updates and updates["default_allowed_instruments"] is not None:
            row.default_allowed_instruments_json = [
                str(x).strip().upper() for x in updates["default_allowed_instruments"] if str(x).strip()
            ]
        if "default_allowed_order_types" in updates and updates["default_allowed_order_types"] is not None:
            row.default_allowed_order_types_json = [
                str(x).strip().upper() for x in updates["default_allowed_order_types"] if str(x).strip()
            ]

        # Also propagate stale threshold update to any active/configured ReplaySessionModel
        if "market_data_stale_threshold_seconds" in updates and updates["market_data_stale_threshold_seconds"] is not None:
            latest_rpl = session.scalar(
                select(ReplaySessionModel).order_by(desc(ReplaySessionModel.created_at)).limit(1)
            )
            if latest_rpl is not None:
                latest_rpl.stale_threshold_seconds = int(row.market_data_stale_threshold_seconds)

        row.updated_by = actor
        row.updated_at = now_ist()
        session.flush()

        AuditService.record(
            session,
            action="ACCOUNT_RISK_CONFIG_UPDATED",
            summary=f"Updated configurable account & operational paper-risk limits by {actor}.",
            actor=actor,
            actor_type=ActorType.OPERATOR,
            category=AuditCategory.RISK,
            severity=AuditSeverity.INFO,
            entity_type="account_risk_config",
            entity_id=str(row.id),
            details={"updated_fields": sorted(list(updates.keys()))},
        )
        return RiskControlService.serialize_account_risk_config(row)

    @staticmethod
    def upsert_strategy_risk_config(
        session: Session,
        *,
        strategy_version_id: uuid.UUID,
        updates: dict[str, Any],
        actor: str = "risk.officer",
        settings: AppSettings | None = None,
    ) -> dict[str, Any]:
        cfg = settings or get_settings()
        acct = RiskControlService.get_primary_account(session, cfg)
        sv = session.get(StrategyVersionModel, strategy_version_id)
        if sv is None:
            raise EntityNotFoundError(f"StrategyVersion '{strategy_version_id}' not found.")

        row = session.scalar(
            select(StrategyRiskConfigModel).where(
                StrategyRiskConfigModel.account_id == acct.id,
                StrategyRiskConfigModel.strategy_version_id == sv.id,
            )
        )
        if row is None:
            row = StrategyRiskConfigModel(
                account_id=acct.id,
                strategy_version_id=sv.id,
                strategy_version_label=sv.version_label,
                allowed_instruments_json=[sv.instrument_symbol],
                allowed_order_types_json=["MARKET", "LIMIT", "STOP"],
                updated_by=actor,
            )
            session.add(row)
            session.flush()

        for df in ("max_risk_per_trade_inr", "strategy_daily_loss_inr", "max_strategy_drawdown_inr"):
            if df in updates and updates[df] is not None:
                val = Decimal(str(updates[df]))
                if val <= Decimal("0"):
                    raise ControlCenterError(f"{df} must be > 0.", code="INVALID_RISK_LIMIT")
                setattr(row, df, val.quantize(_Q4))

        for inf in (
            "max_quantity",
            "max_trades_per_day",
            "cooldown_seconds_after_loss",
            "max_holding_duration_seconds",
        ):
            if inf in updates and updates[inf] is not None:
                ival = int(updates[inf])
                min_v = 0 if inf == "cooldown_seconds_after_loss" else 1
                if ival < min_v:
                    raise ControlCenterError(
                        f"{inf} must be >= {min_v}.", code="INVALID_RISK_LIMIT"
                    )
                setattr(row, inf, ival)

        if "allowed_instruments" in updates and updates["allowed_instruments"] is not None:
            row.allowed_instruments_json = [
                str(x).strip().upper() for x in updates["allowed_instruments"] if str(x).strip()
            ]
        if "allowed_order_types" in updates and updates["allowed_order_types"] is not None:
            row.allowed_order_types_json = [
                str(x).strip().upper() for x in updates["allowed_order_types"] if str(x).strip()
            ]
        if "is_enabled" in updates and updates["is_enabled"] is not None:
            row.is_enabled = bool(updates["is_enabled"])

        row.updated_by = actor
        row.updated_at = now_ist()
        session.flush()

        AuditService.record(
            session,
            action="STRATEGY_RISK_CONFIG_UPDATED",
            summary=f"Updated strategy paper-risk limits for {sv.version_label} by {actor}.",
            actor=actor,
            actor_type=ActorType.OPERATOR,
            category=AuditCategory.RISK,
            severity=AuditSeverity.INFO,
            entity_type="strategy_risk_config",
            entity_id=str(row.id),
            details={
                "strategy_version_label": sv.version_label,
                "updated_fields": sorted(list(updates.keys())),
            },
        )
        return RiskControlService.serialize_strategy_risk_config(row)

    # -------------------------------------------------------------------------
    # Serialization & Risk Center Overview
    # -------------------------------------------------------------------------

    @staticmethod
    def serialize_account_risk_config(row: AccountRiskConfigModel) -> dict[str, Any]:
        return {
            "id": str(row.id),
            "account_id": str(row.account_id),
            "is_demo_settings": bool(row.is_demo_settings),
            "settings_disclaimer": "DEMONSTRATION PAPER-RISK SETTINGS ONLY — NOT TRADING RECOMMENDATIONS",
            "max_daily_loss_inr": str(Decimal(str(row.max_daily_loss_inr)).quantize(_Q2)),
            "max_open_positions": int(row.max_open_positions),
            "max_gross_exposure_inr": str(Decimal(str(row.max_gross_exposure_inr)).quantize(_Q2)),
            "max_order_value_inr": str(Decimal(str(row.max_order_value_inr)).quantize(_Q2)),
            "max_trades_per_day": int(row.max_trades_per_day),
            "max_consecutive_losses": int(row.max_consecutive_losses),
            "trading_session_enabled": bool(row.trading_session_enabled),
            "session_start_ist": row.session_start_ist,
            "session_end_ist": row.session_end_ist,
            "global_kill_switch_active": bool(row.global_kill_switch_active),
            "kill_switch_reason": row.kill_switch_reason,
            "kill_switch_activated_at": format_ist_iso(row.kill_switch_activated_at)
            if row.kill_switch_activated_at
            else None,
            "kill_switch_activated_by": row.kill_switch_activated_by,
            "market_data_stale_threshold_seconds": int(row.market_data_stale_threshold_seconds),
            "max_signal_age_seconds": int(row.max_signal_age_seconds),
            "enforce_system_health_gate": bool(row.enforce_system_health_gate),
            "simulated_system_degraded": bool(row.simulated_system_degraded),
            "enforce_position_reconciliation_gate": bool(
                row.enforce_position_reconciliation_gate
            ),
            "default_max_quantity": int(row.default_max_quantity),
            "default_max_risk_per_trade_inr": str(
                Decimal(str(row.default_max_risk_per_trade_inr)).quantize(_Q2)
            ),
            "default_strategy_daily_loss_inr": str(
                Decimal(str(row.default_strategy_daily_loss_inr)).quantize(_Q2)
            ),
            "default_max_strategy_drawdown_inr": str(
                Decimal(str(row.default_max_strategy_drawdown_inr)).quantize(_Q2)
            ),
            "default_max_trades_per_strategy_day": int(row.default_max_trades_per_strategy_day),
            "default_cooldown_seconds_after_loss": int(
                row.default_cooldown_seconds_after_loss
            ),
            "default_max_holding_duration_seconds": int(
                row.default_max_holding_duration_seconds
            ),
            "default_allowed_instruments": list(row.default_allowed_instruments_json or []),
            "default_allowed_order_types": list(row.default_allowed_order_types_json or []),
            "updated_by": row.updated_by,
            "updated_at": format_ist_iso(row.updated_at),
        }

    @staticmethod
    def serialize_strategy_risk_config(row: StrategyRiskConfigModel) -> dict[str, Any]:
        return {
            "id": str(row.id),
            "account_id": str(row.account_id),
            "strategy_version_id": str(row.strategy_version_id),
            "strategy_version_label": row.strategy_version_label,
            "max_quantity": int(row.max_quantity),
            "max_risk_per_trade_inr": str(
                Decimal(str(row.max_risk_per_trade_inr)).quantize(_Q2)
            ),
            "strategy_daily_loss_inr": str(
                Decimal(str(row.strategy_daily_loss_inr)).quantize(_Q2)
            ),
            "max_strategy_drawdown_inr": str(
                Decimal(str(row.max_strategy_drawdown_inr)).quantize(_Q2)
            ),
            "max_trades_per_day": int(row.max_trades_per_day),
            "cooldown_seconds_after_loss": int(row.cooldown_seconds_after_loss),
            "max_holding_duration_seconds": int(row.max_holding_duration_seconds),
            "allowed_instruments": list(row.allowed_instruments_json or []),
            "allowed_order_types": list(row.allowed_order_types_json or []),
            "is_enabled": bool(row.is_enabled),
            "updated_by": row.updated_by,
            "updated_at": format_ist_iso(row.updated_at),
        }

    @staticmethod
    def serialize_risk_decision(row: RiskDecisionModel) -> dict[str, Any]:
        return {
            "id": str(row.id),
            "decision_code": row.decision_code,
            "decision": row.decision,
            "rule": row.primary_rule,
            "primary_rule": row.primary_rule,
            "current_value": row.current_value,
            "limit_value": row.limit_value,
            "reason": row.reason,
            "strategy": row.strategy_label,
            "strategy_version_id": str(row.strategy_version_id)
            if row.strategy_version_id
            else None,
            "signal_id": str(row.signal_id) if row.signal_id else None,
            "order_id": str(row.order_id) if row.order_id else None,
            "client_order_key": row.client_order_key,
            "instrument_symbol": row.instrument_symbol,
            "side": row.side,
            "order_type": row.order_type,
            "proposed_quantity": int(row.proposed_quantity),
            "proposed_price": (
                str(Decimal(str(row.proposed_price)).quantize(_Q2))
                if row.proposed_price is not None
                else None
            ),
            "proposed_notional_inr": str(
                Decimal(str(row.proposed_notional_inr)).quantize(_Q2)
            ),
            "checks": list(row.checks_json or []),
            "correlation_id": row.correlation_id,
            "timestamp": format_ist_iso(row.evaluated_at),
            "evaluated_at": format_ist_iso(row.evaluated_at),
        }

    @staticmethod
    def list_risk_decisions(
        session: Session,
        *,
        decision_filter: str = "ALL",
        rule_filter: str | None = None,
        limit: int = 100,
    ) -> list[dict[str, Any]]:
        stmt = select(RiskDecisionModel).order_by(desc(RiskDecisionModel.evaluated_at))
        if decision_filter and decision_filter.upper() in ("APPROVED", "REJECTED"):
            stmt = stmt.where(RiskDecisionModel.decision == decision_filter.strip().upper())
        if rule_filter and rule_filter.strip() and rule_filter.upper() != "ALL":
            stmt = stmt.where(RiskDecisionModel.primary_rule == rule_filter.strip().upper())
        rows = list(session.scalars(stmt.limit(max(1, min(limit, 500)))).all())
        return [RiskControlService.serialize_risk_decision(r) for r in rows]

    @staticmethod
    def get_risk_center_overview(
        session: Session, *, settings: AppSettings | None = None
    ) -> dict[str, Any]:
        cfg = settings or get_settings()
        acct = RiskControlService.get_primary_account(session, cfg)
        risk_cfg = RiskControlService.get_account_risk_config(session, acct.id)

        # Ensure every StrategyVersion has a StrategyRiskConfig row visible in Risk Center
        all_svs = list(
            session.scalars(
                select(StrategyVersionModel).order_by(
                    StrategyVersionModel.instrument_symbol.asc(),
                    StrategyVersionModel.version_number.desc(),
                )
            ).all()
        )
        strat_configs: list[StrategyRiskConfigModel] = []
        for sv in all_svs:
            sr = session.scalar(
                select(StrategyRiskConfigModel).where(
                    StrategyRiskConfigModel.account_id == acct.id,
                    StrategyRiskConfigModel.strategy_version_id == sv.id,
                )
            )
            if sr is None:
                sr = StrategyRiskConfigModel(
                    account_id=acct.id,
                    strategy_version_id=sv.id,
                    strategy_version_label=sv.version_label,
                    max_quantity=int(risk_cfg.default_max_quantity),
                    max_risk_per_trade_inr=Decimal(str(risk_cfg.default_max_risk_per_trade_inr)),
                    strategy_daily_loss_inr=Decimal(
                        str(risk_cfg.default_strategy_daily_loss_inr)
                    ),
                    max_strategy_drawdown_inr=Decimal(
                        str(risk_cfg.default_max_strategy_drawdown_inr)
                    ),
                    max_trades_per_day=int(risk_cfg.default_max_trades_per_strategy_day),
                    cooldown_seconds_after_loss=int(
                        risk_cfg.default_cooldown_seconds_after_loss
                    ),
                    max_holding_duration_seconds=int(
                        risk_cfg.default_max_holding_duration_seconds
                    ),
                    allowed_instruments_json=[sv.instrument_symbol],
                    allowed_order_types_json=["MARKET", "LIMIT", "STOP"],
                    is_enabled=True,
                    updated_by="system.sync",
                )
                session.add(sr)
                session.flush()
            strat_configs.append(sr)

        latest_rpl = session.scalar(
            select(ReplaySessionModel).order_by(desc(ReplaySessionModel.created_at)).limit(1)
        )
        fresh = evaluate_market_data_freshness(
            session_status=latest_rpl.status if latest_rpl else None,
            last_candle_received_at=latest_rpl.last_candle_received_at if latest_rpl else None,
            last_exchange_timestamp=latest_rpl.replay_clock_ist if latest_rpl else None,
            stale_threshold_seconds=int(risk_cfg.market_data_stale_threshold_seconds),
        )
        recon = PositionService.reconcile_positions_from_fills(
            session, account_id=acct.id, heal_if_needed=False, settings=cfg
        )

        open_positions = list(
            session.scalars(
                select(PaperPositionModel).where(
                    PaperPositionModel.account_id == acct.id,
                    PaperPositionModel.status == PositionStatus.OPEN.value,
                )
            ).all()
        )
        gross_exposure = sum(
            (Decimal(str(p.last_price)) * Decimal(int(p.quantity)) for p in open_positions),
            Decimal("0.0000"),
        )
        realized = Decimal(str(acct.realized_pnl_today))
        unrealized = Decimal(str(acct.unrealized_pnl))
        comb = realized + min(Decimal("0"), unrealized)
        daily_loss_used = abs(comb) if comb < Decimal("0") else Decimal("0.00")
        consec_losses = PreTradeRiskEvaluator._count_consecutive_account_losses(session, acct.id)
        decisions = RiskControlService.list_risk_decisions(session, limit=80)

        kill_active = bool(risk_cfg.global_kill_switch_active or acct.paper_trading_halted)

        return {
            "simulation_banner": "SIMULATED PAPER TRADING",
            "settings_disclaimer": "DEMONSTRATION PAPER-RISK SETTINGS ONLY — CONFIGURABLE LIMITS, NOT TRADING RECOMMENDATIONS",
            "account": {
                "id": str(acct.id),
                "account_code": acct.account_code,
                "account_designation": acct.account_designation,
                "paper_capital_inr": str(Decimal(str(acct.initial_capital)).quantize(_Q2)),
                "available_capital_inr": str(Decimal(str(acct.available_capital)).quantize(_Q2)),
                "realized_pnl_today_inr": str(realized.quantize(_Q2)),
                "unrealized_pnl_inr": str(unrealized.quantize(_Q2)),
                "paper_trading_halted": kill_active,
                "version": int(acct.version),
            },
            "utilization": {
                "daily_loss_used_inr": str(daily_loss_used.quantize(_Q2)),
                "open_positions_count": len(open_positions),
                "gross_exposure_inr": str(gross_exposure.quantize(_Q2)),
                "consecutive_losses": consec_losses,
                "market_data_freshness": fresh.to_dict(),
                "reconciliation": recon.to_dict(),
            },
            "account_risk_config": RiskControlService.serialize_account_risk_config(risk_cfg),
            "strategy_risk_configs": [
                RiskControlService.serialize_strategy_risk_config(sr) for sr in strat_configs
            ],
            "recent_decisions": decisions,
        }
