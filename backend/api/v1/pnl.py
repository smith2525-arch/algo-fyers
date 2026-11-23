"""Paper-Trading P&L Analytics API endpoint (`SIMULATED PAPER-TRADING RESULTS`)."""

from __future__ import annotations

from datetime import date
from typing import Any

from fastapi import APIRouter, Query

from core.analytics.pnl_service import PnlAnalyticsService
from core.config import get_settings
from core.persistence.uow import unit_of_work

router = APIRouter(prefix="/api/v1/pnl", tags=["P&L Analytics"])


@router.get("/analytics")
def get_paper_pnl_analytics(
    start_date: date | None = Query(default=None),
    end_date: date | None = Query(default=None),
    strategy: str = Query(default="ALL"),
    instrument: str = Query(default="ALL"),
    direction: str = Query(default="ALL"),
    exit_reason: str = Query(default="ALL"),
    outcome: str = Query(default="ALL"),
) -> dict[str, Any]:
    cfg = get_settings()
    with unit_of_work(settings=cfg) as session:
        return PnlAnalyticsService.compute_pnl_analytics(
            session,
            start_date=start_date,
            end_date=end_date,
            strategy=strategy,
            instrument=instrument,
            direction=direction,
            exit_reason=exit_reason,
            outcome=outcome,
            settings=cfg,
        )
