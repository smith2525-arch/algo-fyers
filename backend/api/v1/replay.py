"""Market Replay API endpoints: session catalog, controls, speed, stepping, and freshness."""

from __future__ import annotations

from uuid import UUID

from fastapi import APIRouter, Query, status

from backend.schemas.api import (
    MarketDataFreshnessOut,
    ReplayCatalogOut,
    ReplayControlRequest,
    ReplaySpeedRequest,
    ReplayStartRequest,
    ReplayStateOut,
    ReplayStepRequest,
)
from core.config import get_settings
from core.market_data.service import MarketReplayService
from core.persistence.uow import unit_of_work

router = APIRouter(prefix="/api/v1/replay", tags=["Market Replay"])


@router.get("/catalog", response_model=ReplayCatalogOut)
def get_replay_catalog(
    instrument: str = Query(default="NIFTY", description="NIFTY or BANKNIFTY"),
) -> ReplayCatalogOut:
    cfg = get_settings()
    with unit_of_work(settings=cfg) as session:
        data = MarketReplayService.get_replay_catalog(session, instrument=instrument)
        return ReplayCatalogOut(**data)


@router.get("/state", response_model=ReplayStateOut)
def get_replay_state(
    session_id: UUID | None = Query(default=None),
    advance_due: bool = Query(
        default=False,
        description="When True, opportunistically advances due RUNNING bars for serverless hosts.",
    ),
) -> ReplayStateOut:
    cfg = get_settings()
    with unit_of_work(settings=cfg) as session:
        if advance_due:
            MarketReplayService.advance_running_sessions_by_wall_clock(session, settings=cfg)
        state = MarketReplayService.get_replay_state(
            session, session_id=session_id, settings=cfg
        )
        return ReplayStateOut(**state)


@router.get("/freshness", response_model=MarketDataFreshnessOut)
def get_market_data_freshness() -> MarketDataFreshnessOut:
    cfg = get_settings()
    with unit_of_work(settings=cfg) as session:
        _, snap = MarketReplayService.get_current_freshness(session, settings=cfg)
        return MarketDataFreshnessOut(**snap.to_dict())


@router.post("/freshness/check-order-gate", response_model=MarketDataFreshnessOut)
def check_market_data_new_order_gate() -> MarketDataFreshnessOut:
    """Verify whether downstream pre-trade risk/OMS may accept NEW executable paper orders."""
    cfg = get_settings()
    with unit_of_work(settings=cfg) as session:
        snap = MarketReplayService.assert_new_paper_orders_allowed_by_market_data(
            session, settings=cfg
        )
        return MarketDataFreshnessOut(**snap.to_dict())


@router.post("/start", response_model=ReplayStateOut, status_code=status.HTTP_201_CREATED)
def start_replay_session(payload: ReplayStartRequest) -> ReplayStateOut:
    cfg = get_settings()
    with unit_of_work(settings=cfg) as session:
        state = MarketReplayService.start_session(
            session,
            instrument=payload.instrument,
            strategy_version_id=payload.strategy_version_id,
            dataset_id=payload.dataset_id,
            historical_session_date=payload.historical_session_date,
            replay_speed=payload.replay_speed.value,
            actor=payload.actor,
            settings=cfg,
            initial_step_bars=payload.initial_step_bars,
        )
        return ReplayStateOut(**state)


@router.post("/{session_id}/pause", response_model=ReplayStateOut)
def pause_replay_session(
    session_id: UUID, payload: ReplayControlRequest
) -> ReplayStateOut:
    cfg = get_settings()
    with unit_of_work(settings=cfg) as session:
        state = MarketReplayService.pause_session(
            session, session_id, actor=payload.actor, settings=cfg
        )
        return ReplayStateOut(**state)


@router.post("/{session_id}/resume", response_model=ReplayStateOut)
def resume_replay_session(
    session_id: UUID, payload: ReplayControlRequest
) -> ReplayStateOut:
    cfg = get_settings()
    with unit_of_work(settings=cfg) as session:
        state = MarketReplayService.resume_session(
            session, session_id, actor=payload.actor, settings=cfg
        )
        return ReplayStateOut(**state)


@router.post("/{session_id}/stop", response_model=ReplayStateOut)
def stop_replay_session(
    session_id: UUID, payload: ReplayControlRequest
) -> ReplayStateOut:
    cfg = get_settings()
    with unit_of_work(settings=cfg) as session:
        state = MarketReplayService.stop_session(
            session, session_id, actor=payload.actor, settings=cfg
        )
        return ReplayStateOut(**state)


@router.post("/{session_id}/reset", response_model=ReplayStateOut)
def reset_replay_session(
    session_id: UUID, payload: ReplayControlRequest
) -> ReplayStateOut:
    cfg = get_settings()
    with unit_of_work(settings=cfg) as session:
        state = MarketReplayService.reset_session(
            session, session_id, actor=payload.actor, settings=cfg
        )
        return ReplayStateOut(**state)


@router.post("/{session_id}/speed", response_model=ReplayStateOut)
def update_replay_speed(
    session_id: UUID, payload: ReplaySpeedRequest
) -> ReplayStateOut:
    cfg = get_settings()
    with unit_of_work(settings=cfg) as session:
        state = MarketReplayService.update_speed(
            session,
            session_id,
            replay_speed=payload.replay_speed.value,
            actor=payload.actor,
            settings=cfg,
        )
        return ReplayStateOut(**state)


@router.post("/{session_id}/step", response_model=ReplayStateOut)
def step_replay_session(
    session_id: UUID, payload: ReplayStepRequest
) -> ReplayStateOut:
    cfg = get_settings()
    with unit_of_work(settings=cfg) as session:
        state = MarketReplayService.step_session(
            session,
            session_id,
            bars=payload.bars,
            actor=payload.actor,
            settings=cfg,
        )
        return ReplayStateOut(**state)
