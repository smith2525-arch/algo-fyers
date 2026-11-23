"""Paper Trading workspace, strategy instances, signal dispatch, and recovery endpoints."""

from __future__ import annotations

from typing import Any
from uuid import UUID

from fastapi import APIRouter, status

from backend.schemas.api import (
    StartStrategyInstanceRequest,
    StrategyInstanceControlRequest,
)
from core.config import get_settings
from core.market_data.service import MarketReplayService
from core.paper_trading.service import PaperTradingService
from core.persistence.models import ReplaySignalModel
from core.persistence.uow import unit_of_work

router = APIRouter(prefix="/api/v1/paper-trading", tags=["Paper Trading"])


@router.get("/workspace")
def get_paper_trading_workspace(advance_due: bool = False) -> dict[str, Any]:
    cfg = get_settings()
    with unit_of_work(settings=cfg) as session:
        if advance_due:
            MarketReplayService.advance_running_sessions_by_wall_clock(session, settings=cfg)
        return PaperTradingService.get_paper_trading_workspace(session, settings=cfg)


@router.post("/instances", status_code=status.HTTP_201_CREATED)
def start_strategy_instance(payload: StartStrategyInstanceRequest) -> dict[str, Any]:
    cfg = get_settings()
    with unit_of_work(settings=cfg) as session:
        return PaperTradingService.start_strategy_instance(
            session,
            strategy_version_id=payload.strategy_version_id,
            order_type=payload.order_type.value,
            limit_offset_pts=payload.limit_offset_pts,
            execution_latency_ms=payload.execution_latency_ms,
            latency_bars=payload.latency_bars,
            slippage_bps=payload.slippage_bps,
            partial_fill_enabled=payload.partial_fill_enabled,
            max_fill_ratio_per_bar=payload.max_fill_ratio_per_bar,
            valid_for_bars=payload.valid_for_bars,
            auto_execute_signals=payload.auto_execute_signals,
            actor=payload.actor,
            settings=cfg,
        )


@router.post("/instances/{instance_id}/pause")
def pause_strategy_instance(
    instance_id: UUID, payload: StrategyInstanceControlRequest
) -> dict[str, Any]:
    cfg = get_settings()
    with unit_of_work(settings=cfg) as session:
        return PaperTradingService.pause_strategy_instance(
            session, instance_id, actor=payload.actor
        )


@router.post("/instances/{instance_id}/resume")
def resume_strategy_instance(
    instance_id: UUID, payload: StrategyInstanceControlRequest
) -> dict[str, Any]:
    cfg = get_settings()
    with unit_of_work(settings=cfg) as session:
        return PaperTradingService.resume_strategy_instance(
            session, instance_id, actor=payload.actor
        )


@router.post("/instances/{instance_id}/stop")
def stop_strategy_instance(
    instance_id: UUID, payload: StrategyInstanceControlRequest
) -> dict[str, Any]:
    cfg = get_settings()
    with unit_of_work(settings=cfg) as session:
        return PaperTradingService.stop_strategy_instance(
            session, instance_id, actor=payload.actor
        )


@router.post("/signals/{signal_id}/route")
def route_signal_to_paper_order(
    signal_id: UUID, payload: StrategyInstanceControlRequest
) -> dict[str, Any]:
    """Idempotently route an existing persisted Signal through Risk -> OMS -> Paper Broker."""
    from core.exceptions import EntityNotFoundError
    from core.oms.service import OrderManagementService

    cfg = get_settings()
    with unit_of_work(settings=cfg) as session:
        sig = session.get(ReplaySignalModel, signal_id)
        if sig is None:
            raise EntityNotFoundError(f"Signal '{signal_id}' not found.")
        order, is_dup = PaperTradingService.route_signal_to_paper_execution(
            session, sig, actor=payload.actor, settings=cfg
        )
        return {
            "order": OrderManagementService.serialize_order(order, include_events=True),
            "duplicate": is_dup,
        }


@router.post("/reconcile")
def run_paper_recovery_and_reconciliation() -> dict[str, Any]:
    cfg = get_settings()
    with unit_of_work(settings=cfg) as session:
        return PaperTradingService.recover_and_reconcile_on_startup(session, settings=cfg)
