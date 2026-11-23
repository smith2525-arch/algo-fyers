"""Paper Positions endpoints: execution-derived book, explicit exit/flatten, and reconciliation."""

from __future__ import annotations

from typing import Any
from uuid import UUID

from fastapi import APIRouter, Query
from sqlalchemy import desc, select

from backend.schemas.api import ExitPositionRequest, FlattenPositionsRequest
from core.config import get_settings
from core.domain.enums import PositionStatus
from core.exceptions import DirectPositionMutationForbiddenError
from core.paper_trading.service import PaperTradingService
from core.persistence.models import PaperPositionModel
from core.persistence.uow import unit_of_work
from core.positions.service import PositionService

router = APIRouter(prefix="/api/v1/positions", tags=["Paper Positions"])


@router.get("")
def list_paper_positions(
    status_filter: str = Query(default="ALL", alias="status"),
    instrument: str = Query(default="ALL"),
) -> dict[str, Any]:
    cfg = get_settings()
    with unit_of_work(settings=cfg) as session:
        stmt = select(PaperPositionModel).order_by(desc(PaperPositionModel.opened_at))
        if status_filter and status_filter.upper() in (
            PositionStatus.OPEN.value,
            PositionStatus.CLOSED.value,
        ):
            stmt = stmt.where(PaperPositionModel.status == status_filter.strip().upper())
        if instrument and instrument.upper() != "ALL":
            stmt = stmt.where(PaperPositionModel.instrument_symbol == instrument.strip().upper())

        rows = list(session.scalars(stmt).all())
        recon = PositionService.reconcile_positions_from_fills(
            session, heal_if_needed=False, settings=cfg
        )
        return {
            "simulation_banner": "SIMULATED PAPER TRADING",
            "execution_mode": cfg.EXECUTION_MODE,
            "positions": [PositionService.serialize_position(r) for r in rows],
            "reconciliation": recon.to_dict(),
        }


@router.put("/{position_id}")
@router.patch("/{position_id}")
def forbid_direct_position_mutation(position_id: UUID) -> dict[str, Any]:
    """Explicit guardrail: positions derive strictly from executions and can NEVER be directly edited."""
    raise DirectPositionMutationForbiddenError(
        f"Direct mutation of position '{position_id}' is forbidden. "
        "Positions derive strictly from simulated executions via OMS -> Paper Broker."
    )


@router.post("/{position_id}/exit")
def exit_open_paper_position(
    position_id: UUID, payload: ExitPositionRequest
) -> dict[str, Any]:
    cfg = get_settings()
    with unit_of_work(settings=cfg) as session:
        return PaperTradingService.exit_open_position(
            session,
            position_id,
            client_order_key=payload.client_order_key,
            actor=payload.actor,
            reason=payload.reason,
            settings=cfg,
        )


@router.post("/flatten-all")
def flatten_all_open_paper_positions(payload: FlattenPositionsRequest) -> dict[str, Any]:
    cfg = get_settings()
    with unit_of_work(settings=cfg) as session:
        return PaperTradingService.flatten_all_open_positions(
            session,
            instrument_symbol=payload.instrument_symbol,
            actor=payload.actor,
            settings=cfg,
        )


@router.post("/reconcile")
def reconcile_positions_endpoint() -> dict[str, Any]:
    cfg = get_settings()
    with unit_of_work(settings=cfg) as session:
        report = PositionService.reconcile_positions_from_fills(
            session, heal_if_needed=True, settings=cfg
        )
        return report.to_dict()
