"""Health endpoints for Cloud Run probes and the System Health console screen."""

from __future__ import annotations

from fastapi import APIRouter, Response, status

from backend.schemas.api import (
    ComponentHealthOut,
    HealthEventOut,
    LivenessOut,
    SystemHealthOut,
)
from core.clock import now_ist
from core.config import get_settings
from core.domain.enums import SystemComponentStatus
from core.health.service import HealthService
from core.persistence.uow import unit_of_work

router = APIRouter(tags=["Health"])


def _serialize_component(comp: object) -> ComponentHealthOut:
    from core.domain.entities import ComponentHealthSnapshot

    assert isinstance(comp, ComponentHealthSnapshot)
    return ComponentHealthOut(
        component=comp.component,
        status=comp.status,
        message=comp.message,
        checked_at=comp.checked_at,
        latency_ms=comp.latency_ms,
        details=comp.details,
    )


@router.get("/health", response_model=SystemHealthOut)
def root_health_summary(response: Response) -> SystemHealthOut:
    """Top-level operational health endpoint verifying API and PostgreSQL state."""
    report = HealthService.evaluate_system_health(persist_probe=False)
    if report["database"].status != SystemComponentStatus.HEALTHY:
        response.status_code = status.HTTP_503_SERVICE_UNAVAILABLE
    return SystemHealthOut(
        overall_status=report["overall_status"],
        checked_at=report["checked_at"],
        timezone=report["timezone"],
        execution_mode=report["execution_mode"],
        paper_trading_halted=report["paper_trading_halted"],
        halt_reason=report["halt_reason"],
        database=_serialize_component(report["database"]),
        components=[_serialize_component(c) for c in report["components"]],
        operational_components=[
            _serialize_component(c) for c in report.get("operational_components", [])
        ],
        recent_events=[],
    )


@router.get("/health/live", response_model=LivenessOut)
def liveness_probe() -> LivenessOut:
    """Stateless Cloud Run liveness probe (never blocks on cold Neon wake-ups)."""
    cfg = get_settings()
    return LivenessOut(
        status="ALIVE",
        service=cfg.APP_NAME,
        version=cfg.APP_VERSION,
        execution_mode=cfg.EXECUTION_MODE,
        timezone=cfg.TIMEZONE,
        timestamp_ist=now_ist(),
    )


@router.get("/health/ready", response_model=SystemHealthOut)
def readiness_probe(response: Response) -> SystemHealthOut:
    """Cloud Run readiness probe requiring active PostgreSQL connectivity."""
    return root_health_summary(response)


@router.get("/api/v1/health", response_model=SystemHealthOut)
def full_system_health() -> SystemHealthOut:
    """Detailed health diagnostics plus persisted `system_health_events` history."""
    cfg = get_settings()
    report = HealthService.evaluate_system_health(settings=cfg, persist_probe=False)
    events: list[HealthEventOut] = []
    if report["database"].status == SystemComponentStatus.HEALTHY:
        try:
            with unit_of_work(settings=cfg) as session:
                raw_events = HealthService.list_recent_health_events(session, limit=50)
                events = [HealthEventOut(**item) for item in raw_events]
        except Exception:  # noqa: BLE001
            events = []

    return SystemHealthOut(
        overall_status=report["overall_status"],
        checked_at=report["checked_at"],
        timezone=report["timezone"],
        execution_mode=report["execution_mode"],
        paper_trading_halted=report["paper_trading_halted"],
        halt_reason=report["halt_reason"],
        database=_serialize_component(report["database"]),
        components=[_serialize_component(c) for c in report["components"]],
        operational_components=[
            _serialize_component(c) for c in report.get("operational_components", [])
        ],
        recent_events=events,
    )


@router.post("/api/v1/health/probe", response_model=SystemHealthOut)
def record_diagnostic_probe() -> SystemHealthOut:
    """Execute an active PostgreSQL health check and persist a `system_health_events` record."""
    cfg = get_settings()
    report = HealthService.evaluate_system_health(settings=cfg, persist_probe=True)
    events: list[HealthEventOut] = []
    if report["database"].status == SystemComponentStatus.HEALTHY:
        with unit_of_work(settings=cfg) as session:
            raw_events = HealthService.list_recent_health_events(session, limit=50)
            events = [HealthEventOut(**item) for item in raw_events]

    return SystemHealthOut(
        overall_status=report["overall_status"],
        checked_at=report["checked_at"],
        timezone=report["timezone"],
        execution_mode=report["execution_mode"],
        paper_trading_halted=report["paper_trading_halted"],
        halt_reason=report["halt_reason"],
        database=_serialize_component(report["database"]),
        components=[_serialize_component(c) for c in report["components"]],
        operational_components=[
            _serialize_component(c) for c in report.get("operational_components", [])
        ],
        recent_events=events,
    )
