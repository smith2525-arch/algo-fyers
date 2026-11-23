"""OMS Paper Orders endpoints: idempotent submission, blotter listing, inspection & cancel."""

from __future__ import annotations

from typing import Any
from uuid import UUID

from fastapi import APIRouter, Query, Response, status

from backend.schemas.api import (
    CancelAllOrdersRequest,
    CancelOrderRequest,
    SubmitPaperOrderRequest,
)
from core.config import get_settings
from core.oms.service import OrderManagementService
from core.paper_trading.service import PaperTradingService
from core.persistence.uow import unit_of_work

router = APIRouter(prefix="/api/v1/orders", tags=["OMS Orders"])


@router.get("")
def list_paper_orders(
    status_filter: str = Query(default="ALL", alias="status"),
    instrument: str = Query(default="ALL"),
    limit: int = Query(default=100, ge=1, le=500),
) -> dict[str, Any]:
    cfg = get_settings()
    with unit_of_work(settings=cfg) as session:
        orders = OrderManagementService.list_orders(
            session,
            status_filter=status_filter,
            instrument_symbol=instrument,
            limit=limit,
        )
        return {
            "simulation_banner": "SIMULATED PAPER TRADING",
            "execution_mode": cfg.EXECUTION_MODE,
            "count": len(orders),
            "orders": orders,
        }


@router.get("/{order_id}")
def get_paper_order_detail(order_id: UUID) -> dict[str, Any]:
    cfg = get_settings()
    with unit_of_work(settings=cfg) as session:
        return OrderManagementService.get_order_detail(session, order_id)


@router.post("", status_code=status.HTTP_201_CREATED)
def submit_paper_order(payload: SubmitPaperOrderRequest, response: Response) -> dict[str, Any]:
    """Idempotently submit a simulated paper order (returns 200 with duplicate=True on repeat key)."""
    cfg = get_settings()
    with unit_of_work(settings=cfg) as session:
        order_data, is_dup = PaperTradingService.submit_manual_paper_order(
            session,
            client_order_key=payload.client_order_key,
            instrument_symbol=payload.instrument_symbol,
            side=payload.side.value,
            order_type=payload.order_type.value,
            quantity=payload.quantity,
            reference_price=payload.reference_price,
            limit_price=payload.limit_price,
            stop_price=payload.stop_price,
            stop_loss_price=payload.stop_loss_price,
            target_price=payload.target_price,
            execution_latency_ms=payload.execution_latency_ms,
            latency_bars=payload.latency_bars,
            slippage_bps=payload.slippage_bps,
            max_fill_ratio_per_bar=payload.max_fill_ratio_per_bar,
            simulate_rejection=payload.simulate_rejection,
            simulated_rejection_reason=payload.simulated_rejection_reason,
            valid_for_bars=payload.valid_for_bars,
            expires_at=payload.expires_at,
            require_fresh_market_data=payload.require_fresh_market_data,
            actor=payload.actor,
            settings=cfg,
        )
        if is_dup:
            response.status_code = status.HTTP_200_OK
        return {"order": order_data, "duplicate": is_dup}


@router.post("/{order_id}/cancel")
def cancel_paper_order(order_id: UUID, payload: CancelOrderRequest) -> dict[str, Any]:
    cfg = get_settings()
    with unit_of_work(settings=cfg) as session:
        order = OrderManagementService.cancel_order(
            session, order_id, actor=payload.actor, reason=payload.reason
        )
        return OrderManagementService.serialize_order(order, include_events=True)


@router.post("/cancel-all")
def cancel_all_working_paper_orders(payload: CancelAllOrdersRequest) -> dict[str, Any]:
    cfg = get_settings()
    with unit_of_work(settings=cfg) as session:
        cancelled = OrderManagementService.cancel_all_working_orders(
            session,
            instrument_symbol=payload.instrument_symbol,
            actor=payload.actor,
            reason=payload.reason,
        )
        return {
            "cancelled_count": len(cancelled),
            "orders": [OrderManagementService.serialize_order(o) for o in cancelled],
        }
