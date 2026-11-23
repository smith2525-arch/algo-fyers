"""Configurable instrument reference data endpoints (NIFTY, BANKNIFTY)."""

from __future__ import annotations

from fastapi import APIRouter

from backend.schemas.api import InstrumentOut, InstrumentUpdateRequest
from core.config import get_settings
from core.controls.service import ControlService
from core.persistence.uow import unit_of_work

router = APIRouter(prefix="/api/v1/instruments", tags=["Instruments"])


@router.get("", response_model=list[InstrumentOut])
def list_instruments() -> list[InstrumentOut]:
    cfg = get_settings()
    with unit_of_work(settings=cfg) as session:
        items = ControlService.list_instruments(session)
        return [InstrumentOut(**inst.__dict__) for inst in items]


@router.put("/{symbol}", response_model=InstrumentOut)
def update_instrument(symbol: str, payload: InstrumentUpdateRequest) -> InstrumentOut:
    cfg = get_settings()
    with unit_of_work(settings=cfg) as session:
        updated = ControlService.update_instrument_reference(
            session,
            symbol=symbol,
            lot_size=payload.lot_size,
            tick_size=payload.tick_size,
            session_open_ist=payload.session_open_ist,
            session_close_ist=payload.session_close_ist,
            is_active=payload.is_active,
            actor=payload.actor,
        )
        return InstrumentOut(**updated.__dict__)
