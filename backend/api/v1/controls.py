"""Paper account inspection and STOP PAPER TRADING operational control routes."""

from __future__ import annotations

from fastapi import APIRouter

from backend.schemas.api import PaperAccountOut, PaperTradingControlRequest
from core.config import get_settings
from core.controls.service import ControlService
from core.persistence.uow import unit_of_work

router = APIRouter(prefix="/api/v1", tags=["Controls & Accounts"])


@router.get("/accounts/primary", response_model=PaperAccountOut)
def get_primary_account() -> PaperAccountOut:
    cfg = get_settings()
    with unit_of_work(settings=cfg) as session:
        acct = ControlService.get_primary_paper_account(session, cfg)
        return PaperAccountOut(**acct.__dict__)


@router.post("/controls/paper-trading/stop", response_model=PaperAccountOut)
def stop_paper_trading(payload: PaperTradingControlRequest) -> PaperAccountOut:
    """Engage STOP PAPER TRADING.

    Stops generation of NEW orders immediately and persists across restarts.
    Does NOT flatten open positions (position flattening is a separate command).
    """
    cfg = get_settings()
    with unit_of_work(settings=cfg) as session:
        acct = ControlService.stop_paper_trading(
            session,
            actor=payload.actor,
            reason=payload.reason,
            settings=cfg,
        )
        return PaperAccountOut(**acct.__dict__)


@router.post("/controls/paper-trading/resume", response_model=PaperAccountOut)
def resume_paper_trading(payload: PaperTradingControlRequest) -> PaperAccountOut:
    """Disengage STOP PAPER TRADING and re-enable the pre-trade new-order gate."""
    cfg = get_settings()
    with unit_of_work(settings=cfg) as session:
        acct = ControlService.resume_paper_trading(
            session,
            actor=payload.actor,
            reason=payload.reason,
            settings=cfg,
        )
        return PaperAccountOut(**acct.__dict__)
