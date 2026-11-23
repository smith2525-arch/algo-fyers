"""Transactional append-only Audit Event service with structured correlation & trace fields."""

from __future__ import annotations

import logging
from datetime import timezone
from typing import Any

from sqlalchemy import Select, desc, select
from sqlalchemy.orm import Session

from core.clock import ensure_ist, now_ist
from core.domain.entities import AuditEventEntity
from core.domain.enums import ActorType, AuditCategory, AuditEventType, AuditSeverity
from core.logging import get_correlation_id
from core.persistence.models import AuditEventModel

logger = logging.getLogger(__name__)

_ACTION_TO_CANONICAL_EVENT_TYPE: dict[str, str] = {
    "USER_LOGIN": AuditEventType.LOGIN.value,
    "LOGIN": AuditEventType.LOGIN.value,
    "STRATEGY_CREATED": AuditEventType.STRATEGY_CREATED.value,
    "FOUNDATION_REFERENCE_DATA_SEEDED": AuditEventType.STRATEGY_CREATED.value,
    "STRATEGY_VERSION_CREATED": AuditEventType.STRATEGY_VERSION_CREATED.value,
    "STRATEGY_VERSION_CLONED": AuditEventType.STRATEGY_VERSION_CREATED.value,
    "BACKTEST_STARTED": AuditEventType.BACKTEST_STARTED.value,
    "BACKTEST_EXECUTED": AuditEventType.BACKTEST_COMPLETED.value,
    "BACKTEST_COMPLETED": AuditEventType.BACKTEST_COMPLETED.value,
    "REPLAY_SESSION_STARTED": AuditEventType.REPLAY_STARTED.value,
    "REPLAY_STARTED": AuditEventType.REPLAY_STARTED.value,
    "REPLAY_SESSION_PAUSED": AuditEventType.REPLAY_PAUSED.value,
    "REPLAY_PAUSED_ON_RECOVERY": AuditEventType.REPLAY_PAUSED.value,
    "REPLAY_PAUSED": AuditEventType.REPLAY_PAUSED.value,
    "STRATEGY_INSTANCE_STARTED": AuditEventType.STRATEGY_STARTED.value,
    "STRATEGY_INSTANCE_RUNNING": AuditEventType.STRATEGY_STARTED.value,
    "STRATEGY_STARTED": AuditEventType.STRATEGY_STARTED.value,
    "STRATEGY_INSTANCE_PAUSED": AuditEventType.STRATEGY_PAUSED.value,
    "STRATEGY_PAUSED": AuditEventType.STRATEGY_PAUSED.value,
    "STRATEGY_INSTANCE_STOPPED": AuditEventType.STRATEGY_STOPPED.value,
    "STRATEGY_STOPPED": AuditEventType.STRATEGY_STOPPED.value,
    "STRATEGY_SIGNAL_GENERATED": AuditEventType.SIGNAL_GENERATED.value,
    "SIGNAL_GENERATED": AuditEventType.SIGNAL_GENERATED.value,
    "RISK_DECISION_APPROVED": AuditEventType.RISK_APPROVED.value,
    "RISK_APPROVED": AuditEventType.RISK_APPROVED.value,
    "RISK_DECISION_REJECTED": AuditEventType.RISK_REJECTED.value,
    "RISK_REJECTED": AuditEventType.RISK_REJECTED.value,
    "ORDER_CREATED": AuditEventType.ORDER_CREATED.value,
    "ORDER_STATE_CREATED": AuditEventType.ORDER_CREATED.value,
    "ORDER_STATE_SUBMITTED": AuditEventType.ORDER_SUBMITTED.value,
    "ORDER_SUBMITTED": AuditEventType.ORDER_SUBMITTED.value,
    "ORDER_STATE_FILLED": AuditEventType.ORDER_FILLED.value,
    "ORDER_FILLED": AuditEventType.ORDER_FILLED.value,
    "ORDER_STATE_REJECTED": AuditEventType.ORDER_REJECTED.value,
    "ORDER_STATE_RISK_REJECTED": AuditEventType.ORDER_REJECTED.value,
    "ORDER_REJECTED": AuditEventType.ORDER_REJECTED.value,
    "ORDER_STATE_CANCELLED": AuditEventType.ORDER_CANCELLED.value,
    "ORDER_CANCELLED": AuditEventType.ORDER_CANCELLED.value,
    "PAPER_POSITION_OPENED": AuditEventType.POSITION_OPENED.value,
    "POSITION_OPENED": AuditEventType.POSITION_OPENED.value,
    "PAPER_POSITION_CLOSED": AuditEventType.POSITION_CLOSED.value,
    "POSITION_CLOSED": AuditEventType.POSITION_CLOSED.value,
    "STOP_TRIGGERED": AuditEventType.STOP_TRIGGERED.value,
    "TARGET_TRIGGERED": AuditEventType.TARGET_TRIGGERED.value,
    "GLOBAL_KILL_SWITCH_ACTIVATED": AuditEventType.KILL_SWITCH_ACTIVATED.value,
    "PAPER_TRADING_STOPPED": AuditEventType.KILL_SWITCH_ACTIVATED.value,
    "KILL_SWITCH_ACTIVATED": AuditEventType.KILL_SWITCH_ACTIVATED.value,
    "GLOBAL_KILL_SWITCH_DEACTIVATED": AuditEventType.KILL_SWITCH_DEACTIVATED.value,
    "PAPER_TRADING_RESUMED": AuditEventType.KILL_SWITCH_DEACTIVATED.value,
    "KILL_SWITCH_DEACTIVATED": AuditEventType.KILL_SWITCH_DEACTIVATED.value,
    "MARKET_DATA_STALE": AuditEventType.MARKET_DATA_STALE.value,
    "MARKET_DATA_RECOVERED": AuditEventType.MARKET_DATA_RECOVERED.value,
    "RECONCILIATION_COMPLETED": AuditEventType.RECONCILIATION_COMPLETED.value,
    "RECONCILIATION_FAILED": AuditEventType.RECONCILIATION_FAILED.value,
}


def _format_utc_iso(dt_val: Any) -> str:
    ist_dt = ensure_ist(dt_val)
    utc_dt = ist_dt.astimezone(timezone.utc)
    return utc_dt.strftime("%Y-%m-%dT%H:%M:%S.%f")[:-3] + "Z"


def _to_entity(row: AuditEventModel) -> AuditEventEntity:
    ist_dt = ensure_ist(row.occurred_at)
    det = dict(row.details_json or {})
    ev_type = (
        row.event_type
        or _ACTION_TO_CANONICAL_EVENT_TYPE.get(row.action)
        or row.action
    )
    return AuditEventEntity(
        id=row.id,
        occurred_at=ist_dt,
        occurred_at_utc=_format_utc_iso(ist_dt),
        correlation_id=row.correlation_id,
        actor=row.actor,
        actor_type=ActorType(row.actor_type),
        category=AuditCategory(row.category),
        action=row.action,
        event_type=ev_type,
        severity=AuditSeverity(row.severity),
        entity_type=row.entity_type,
        entity_id=row.entity_id,
        strategy_id=row.strategy_id,
        signal_id=row.signal_id,
        order_id=row.order_id,
        source=row.source or row.actor or "core.service",
        summary=row.summary,
        details=det,
    )


class AuditService:
    """Writes and queries append-only operational audit events (never edits or deletes)."""

    @staticmethod
    def record(
        session: Session,
        *,
        action: str,
        summary: str,
        actor: str = "system",
        actor_type: ActorType = ActorType.SYSTEM,
        category: AuditCategory = AuditCategory.SYSTEM,
        severity: AuditSeverity = AuditSeverity.INFO,
        entity_type: str | None = None,
        entity_id: str | None = None,
        event_type: str | None = None,
        strategy_id: str | None = None,
        signal_id: str | None = None,
        order_id: str | None = None,
        source: str | None = None,
        details: dict[str, Any] | None = None,
        correlation_id: str | None = None,
    ) -> AuditEventEntity:
        """Stage an immutable AuditEvent within the caller's active database session."""
        cid = (correlation_id or "").strip() or get_correlation_id()
        det = dict(details or {})
        clean_action = action.strip().upper()
        resolved_event_type = (
            (event_type or "").strip().upper()
            or _ACTION_TO_CANONICAL_EVENT_TYPE.get(clean_action)
            or clean_action
        )

        eff_strategy = (
            strategy_id
            or (str(det["strategy_version_id"]) if det.get("strategy_version_id") else None)
            or (str(det["strategy_version_label"]) if det.get("strategy_version_label") else None)
            or (str(det["strategy"]) if det.get("strategy") else None)
        )
        eff_signal = (
            signal_id
            or (str(det["signal_id"]) if det.get("signal_id") else None)
            or (str(entity_id) if entity_type == "signal" and entity_id else None)
        )
        eff_order = (
            order_id
            or (str(det["order_id"]) if det.get("order_id") else None)
            or (str(entity_id) if entity_type == "paper_order" and entity_id else None)
        )
        eff_source = (source or "").strip() or actor

        event_row = AuditEventModel(
            occurred_at=now_ist(),
            correlation_id=cid,
            actor=actor,
            actor_type=actor_type.value,
            category=category.value,
            action=clean_action,
            event_type=resolved_event_type,
            severity=severity.value,
            entity_type=entity_type,
            entity_id=entity_id,
            strategy_id=eff_strategy[:64] if eff_strategy else None,
            signal_id=eff_signal[:64] if eff_signal else None,
            order_id=eff_order[:64] if eff_order else None,
            source=eff_source[:64],
            summary=summary,
            details_json=det,
        )
        session.add(event_row)
        session.flush()
        logger.info(
            "Audit event recorded",
            extra={
                "audit_action": clean_action,
                "audit_event_type": resolved_event_type,
                "audit_category": category.value,
                "audit_severity": severity.value,
                "entity_type": entity_type,
                "entity_id": entity_id,
            },
        )
        return _to_entity(event_row)

    @staticmethod
    def record_operator_login(
        session: Session,
        *,
        username: str,
        source_ip: str = "127.0.0.1",
        user_agent: str = "StreamlitControlConsole",
    ) -> AuditEventEntity:
        return AuditService.record(
            session,
            action=AuditEventType.LOGIN.value,
            event_type=AuditEventType.LOGIN.value,
            summary=f"Operator '{username}' authenticated into Algo Trading Control Center session.",
            actor=username,
            actor_type=ActorType.OPERATOR,
            category=AuditCategory.CONTROL,
            severity=AuditSeverity.INFO,
            entity_type="user_session",
            entity_id=username,
            source="auth.session",
            details={"source_ip": source_ip, "user_agent": user_agent},
        )

    @staticmethod
    def list_events(
        session: Session,
        *,
        category: str | None = None,
        event_type: str | None = None,
        severity: str | None = None,
        correlation_id: str | None = None,
        strategy_id: str | None = None,
        signal_id: str | None = None,
        order_id: str | None = None,
        search: str | None = None,
        limit: int = 100,
    ) -> list[AuditEventEntity]:
        """Return newest-first audit events matching optional filters."""
        safe_limit = max(1, min(limit, 500))
        stmt: Select[tuple[AuditEventModel]] = select(AuditEventModel).order_by(
            desc(AuditEventModel.occurred_at)
        )
        if category and category.upper() != "ALL":
            stmt = stmt.where(AuditEventModel.category == category.strip().upper())
        if event_type and event_type.upper() != "ALL":
            et = event_type.strip().upper()
            stmt = stmt.where(
                (AuditEventModel.event_type == et) | (AuditEventModel.action == et)
            )
        if severity and severity.upper() != "ALL":
            stmt = stmt.where(AuditEventModel.severity == severity.strip().upper())
        if correlation_id:
            stmt = stmt.where(AuditEventModel.correlation_id == correlation_id.strip())
        if strategy_id:
            stmt = stmt.where(AuditEventModel.strategy_id == strategy_id.strip())
        if signal_id:
            stmt = stmt.where(AuditEventModel.signal_id == signal_id.strip())
        if order_id:
            stmt = stmt.where(AuditEventModel.order_id == order_id.strip())
        if search:
            pattern = f"%{search.strip()}%"
            stmt = stmt.where(
                AuditEventModel.summary.ilike(pattern)
                | AuditEventModel.action.ilike(pattern)
                | AuditEventModel.actor.ilike(pattern)
                | AuditEventModel.event_type.ilike(pattern)
            )
        stmt = stmt.limit(safe_limit)
        rows = session.scalars(stmt).all()
        return [_to_entity(row) for row in rows]
