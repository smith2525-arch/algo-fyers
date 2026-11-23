"""Append-only operational audit trail query, login logging, and Correlation Trace endpoints."""

from __future__ import annotations

from typing import Any
from uuid import UUID

from fastapi import APIRouter, Query, status

from backend.schemas.api import (
    AuditEventCreateRequest,
    AuditEventOut,
    OperatorLoginAuditRequest,
)
from core.audit.service import AuditService
from core.audit.trace import CorrelationTraceService
from core.config import get_settings
from core.exceptions import ImmutabilityViolationError
from core.persistence.uow import unit_of_work

router = APIRouter(prefix="/api/v1/audit", tags=["Audit & Correlation Trace"])


@router.get("", response_model=list[AuditEventOut])
def list_audit_events(
    category: str | None = Query(default=None),
    event_type: str | None = Query(default=None),
    severity: str | None = Query(default=None),
    correlation_id: str | None = Query(default=None),
    strategy_id: str | None = Query(default=None),
    signal_id: str | None = Query(default=None),
    order_id: str | None = Query(default=None),
    search: str | None = Query(default=None),
    limit: int = Query(default=100, ge=1, le=500),
) -> list[AuditEventOut]:
    cfg = get_settings()
    with unit_of_work(settings=cfg) as session:
        events = AuditService.list_events(
            session,
            category=category,
            event_type=event_type,
            severity=severity,
            correlation_id=correlation_id,
            strategy_id=strategy_id,
            signal_id=signal_id,
            order_id=order_id,
            search=search,
            limit=limit,
        )
        return [AuditEventOut(**ev.__dict__) for ev in events]


@router.post("", response_model=AuditEventOut, status_code=status.HTTP_201_CREATED)
def create_audit_event(payload: AuditEventCreateRequest) -> AuditEventOut:
    cfg = get_settings()
    with unit_of_work(settings=cfg) as session:
        ev = AuditService.record(
            session,
            action=payload.action,
            event_type=payload.event_type,
            summary=payload.summary,
            actor=payload.actor,
            actor_type=payload.actor_type,
            category=payload.category,
            severity=payload.severity,
            entity_type=payload.entity_type,
            entity_id=payload.entity_id,
            strategy_id=payload.strategy_id,
            signal_id=payload.signal_id,
            order_id=payload.order_id,
            source=payload.source,
            details=payload.details,
        )
        return AuditEventOut(**ev.__dict__)


@router.post("/login", response_model=AuditEventOut, status_code=status.HTTP_201_CREATED)
def record_operator_login_event(payload: OperatorLoginAuditRequest) -> AuditEventOut:
    cfg = get_settings()
    with unit_of_work(settings=cfg) as session:
        ev = AuditService.record_operator_login(
            session,
            username=payload.username,
            source_ip=payload.source_ip,
            user_agent=payload.user_agent,
        )
        return AuditEventOut(**ev.__dict__)


@router.get("/traces")
def list_recent_traceable_correlations(
    limit: int = Query(default=30, ge=1, le=100),
) -> dict[str, Any]:
    cfg = get_settings()
    with unit_of_work(settings=cfg) as session:
        items = CorrelationTraceService.list_traceable_correlations(session, limit=limit)
        return {"count": len(items), "traces": items}


@router.get("/trace/{correlation_id}")
def get_trade_lifecycle_trace(correlation_id: str) -> dict[str, Any]:
    cfg = get_settings()
    with unit_of_work(settings=cfg) as session:
        return CorrelationTraceService.build_lifecycle_trace(session, correlation_id)


@router.put("/{event_id}")
@router.patch("/{event_id}")
@router.delete("/{event_id}")
def forbid_audit_mutation(event_id: UUID) -> dict[str, Any]:
    """Explicit safety lock: historical audit records can NEVER be edited or deleted."""
    raise ImmutabilityViolationError(
        f"Audit record '{event_id}' is append-only immutable and cannot be modified or deleted."
    )
