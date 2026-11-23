"""Deterministic Reconciliation API endpoints (`verify-only` vs `recorded repair`)."""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Query

from backend.schemas.api import RunReconciliationRequest
from core.config import get_settings
from core.persistence.uow import unit_of_work
from core.reconciliation.service import ReconciliationService

router = APIRouter(prefix="/api/v1/reconciliation", tags=["Deterministic Reconciliation"])


@router.get("/runs")
def list_reconciliation_runs(
    limit: int = Query(default=30, ge=1, le=200),
) -> dict[str, Any]:
    cfg = get_settings()
    with unit_of_work(settings=cfg) as session:
        runs = ReconciliationService.list_runs(session, limit=limit)
        return {"count": len(runs), "runs": runs}


@router.post("/run")
def execute_deterministic_reconciliation(
    payload: RunReconciliationRequest,
) -> dict[str, Any]:
    cfg = get_settings()
    with unit_of_work(settings=cfg) as session:
        return ReconciliationService.run_reconciliation(
            session,
            apply_repair=payload.apply_repair,
            actor=payload.actor,
            settings=cfg,
        )
