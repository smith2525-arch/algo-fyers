"""Independent Risk Engine, Global Kill Switch, Confirmed Flatten & Risk Decision Audit API."""

from __future__ import annotations

from typing import Any
from uuid import UUID

from fastapi import APIRouter, Query

from backend.schemas.api import (
    ConfirmedFlattenRequest,
    KillSwitchRequest,
    UpdateAccountRiskConfigRequest,
    UpdateStrategyRiskConfigRequest,
)
from core.config import get_settings
from core.persistence.uow import unit_of_work
from core.risk.service import RiskControlService

router = APIRouter(prefix="/api/v1/risk", tags=["Risk Engine & Safety Controls"])


@router.get("/overview")
def get_risk_center_overview() -> dict[str, Any]:
    cfg = get_settings()
    with unit_of_work(settings=cfg) as session:
        return RiskControlService.get_risk_center_overview(session, settings=cfg)


@router.get("/decisions")
def list_persisted_risk_decisions(
    decision: str = Query(default="ALL"),
    rule: str = Query(default="ALL"),
    limit: int = Query(default=100, ge=1, le=500),
) -> dict[str, Any]:
    cfg = get_settings()
    with unit_of_work(settings=cfg) as session:
        rows = RiskControlService.list_risk_decisions(
            session, decision_filter=decision, rule_filter=rule, limit=limit
        )
        return {"count": len(rows), "decisions": rows}


@router.put("/account-config")
def update_account_risk_configuration(
    payload: UpdateAccountRiskConfigRequest,
) -> dict[str, Any]:
    cfg = get_settings()
    updates = payload.model_dump(exclude_none=True, exclude={"actor"})
    with unit_of_work(settings=cfg) as session:
        return RiskControlService.update_account_risk_config(
            session, updates=updates, actor=payload.actor, settings=cfg
        )


@router.put("/strategy-config/{strategy_version_id}")
def update_strategy_risk_configuration(
    strategy_version_id: UUID,
    payload: UpdateStrategyRiskConfigRequest,
) -> dict[str, Any]:
    cfg = get_settings()
    updates = payload.model_dump(exclude_none=True, exclude={"actor"})
    with unit_of_work(settings=cfg) as session:
        return RiskControlService.upsert_strategy_risk_config(
            session,
            strategy_version_id=strategy_version_id,
            updates=updates,
            actor=payload.actor,
            settings=cfg,
        )


@router.post("/kill-switch/activate")
def activate_global_kill_switch(payload: KillSwitchRequest) -> dict[str, Any]:
    cfg = get_settings()
    with unit_of_work(settings=cfg) as session:
        return RiskControlService.activate_kill_switch(
            session, actor=payload.actor, reason=payload.reason, settings=cfg
        )


@router.post("/kill-switch/deactivate")
def deactivate_global_kill_switch(payload: KillSwitchRequest) -> dict[str, Any]:
    cfg = get_settings()
    with unit_of_work(settings=cfg) as session:
        return RiskControlService.deactivate_kill_switch(
            session, actor=payload.actor, reason=payload.reason, settings=cfg
        )


@router.post("/positions/flatten")
def flatten_paper_positions_with_confirmation(
    payload: ConfirmedFlattenRequest,
) -> dict[str, Any]:
    cfg = get_settings()
    with unit_of_work(settings=cfg) as session:
        return RiskControlService.flatten_paper_positions_confirmed(
            session,
            confirm_flatten=payload.confirm_flatten,
            instrument_symbol=payload.instrument_symbol,
            actor=payload.actor,
            settings=cfg,
        )
