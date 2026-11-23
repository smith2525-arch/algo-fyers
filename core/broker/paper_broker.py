"""Simulated Paper Broker matching engine (`PAPER_ONLY` — zero external broker routing).

==================================================================================
DOCUMENTED SIMULATED FILL MODEL (PHASE 3B)
==================================================================================
1. STRICT NON-CLOSE-FILL & EXECUTION LATENCY DISCIPLINE:
   - Orders generated from a completed candle `t` default to `latency_bars = 1`
     (`created_bar_timestamp = t`). They NEVER fill at candle `t` close; matching
     only begins on subsequent normalized `CandleEvent`s (`t + latency_bars`).
   - Every acknowledged order records `execution_latency_ms` (`acknowledged_at`).

2. ORDER PRICING TYPES:
   - `MARKET`:
     Fills on the first eligible `CandleEvent` at `candle.open` adjusted adversely
     by `slippage_bps` (`BUY` higher, `SELL` lower). (When `latency_bars == 0` with
     `immediate_match_allowed=True` for an operator immediate exit/market ticket,
     fills against the current live replay price + adverse slippage.)
   - `LIMIT` (Strict Trade-Through Fill Model):
     * `BUY LIMIT` at `L`:
       - If `candle.open <= L`, fills at `candle.open` (gap price improvement).
       - Else if `candle.low < L` (simulated market traded strictly through `L`),
         fills at `L` (limit price cap guaranteed).
       - If `candle.low >= L`, order remains working (`ACKNOWLEDGED` / `PARTIALLY_FILLED`).
     * `SELL LIMIT` at `L`:
       - If `candle.open >= L`, fills at `candle.open` (gap price improvement).
       - Else if `candle.high > L` (simulated market traded strictly through `L`),
         fills at `L`.
       - If `candle.high <= L`, order remains working.
   - `STOP` (Stop-Trigger Market Model):
     * `BUY STOP` at `S`:
       - Requires `candle.high >= S` to trigger (`stop_triggered = True`).
       - Fills at `max(candle.open, S)` plus adverse `BUY` slippage.
       - While `candle.high < S`, order does NOT trigger or fill.
     * `SELL STOP` at `S`:
       - Requires `candle.low <= S` to trigger (`stop_triggered = True`).
       - Fills at `min(candle.open, S)` minus adverse `SELL` slippage.
       - While `candle.low > S`, order does NOT trigger or fill.

3. PARTIAL FILLS, REJECTIONS, CANCELLATIONS & EXPIRATIONS:
   - `max_fill_ratio_per_bar < 1.0` slices fills across multiple market events
     (`ACKNOWLEDGED -> PARTIALLY_FILLED -> FILLED`), updating positions incrementally.
   - `simulate_rejection = True` transitions `SUBMITTED -> REJECTED`.
   - `expires_at` or `valid_for_bars` transitions unfilled working orders to `EXPIRED`.
==================================================================================
"""

from __future__ import annotations

import math
import uuid
from datetime import datetime, timedelta
from decimal import Decimal
from typing import Any

from sqlalchemy import asc, func, select
from sqlalchemy.orm import Session

from core.audit.service import AuditService
from core.clock import ensure_ist, now_ist
from core.config import AppSettings, get_settings
from core.domain.enums import (
    ActorType,
    AuditCategory,
    AuditSeverity,
    OrderStatus,
    OrderType,
    Side,
)
from core.market_data.events import CandleEvent
from core.oms.service import OrderManagementService
from core.persistence.models import PaperFillModel, PaperOrderModel
from core.positions.service import PositionService

_BPS = Decimal("10000")
_Q4 = Decimal("0.0001")


def _apply_adverse_slippage(raw_price: Decimal, side: Side, slippage_bps: Decimal) -> Decimal:
    rate = slippage_bps / _BPS
    if side == Side.BUY:
        return (raw_price * (Decimal("1") + rate)).quantize(_Q4)
    return (raw_price * (Decimal("1") - rate)).quantize(_Q4)


def _compute_slice_charges(
    *,
    fill_price: Decimal,
    fill_quantity: int,
    charges_bps: Decimal,
    per_order_fee_inr: Decimal,
    is_first_fill_for_order: bool,
) -> Decimal:
    turnover = fill_price * Decimal(fill_quantity)
    variable_fee = turnover * (charges_bps / _BPS)
    flat_fee = per_order_fee_inr if is_first_fill_for_order else Decimal("0")
    return (variable_fee + flat_fee).quantize(_Q4)


class PaperBrokerService:
    """Simulated Paper Broker engine processing OMS orders and normalized `CandleEvent`s."""

    @staticmethod
    def fill_model_documentation() -> dict[str, str]:
        return {
            "mode": "SIMULATED_PAPER_BROKER_ONLY",
            "market_order": "Executes on next eligible bar open (latency_bars >= 1) + adverse slippage_bps.",
            "limit_order": "Requires strict trade-through (BUY: low < limit, SELL: high > limit; gap opens receive price improvement).",
            "stop_order": "Triggers only when market reaches stop_price (BUY: high >= stop, SELL: low <= stop), then fills with adverse slippage.",
            "partial_fills": "Controlled via max_fill_ratio_per_bar in (0, 1]; slices execute across sequential market events.",
        }

    @staticmethod
    def submit_order_to_paper_broker(
        session: Session,
        order: PaperOrderModel,
        *,
        current_candle: CandleEvent | None = None,
        immediate_match_allowed: bool = False,
        settings: AppSettings | None = None,
    ) -> PaperOrderModel:
        """Route a `RISK_APPROVED` OMS order into the simulated Paper Broker."""
        cfg = settings or get_settings()
        if order.status != OrderStatus.RISK_APPROVED.value:
            return order

        submit_ts = now_ist()
        OrderManagementService.transition_order_status(
            session,
            order,
            OrderStatus.SUBMITTED,
            actor="oms.router",
            reason="Dispatched RISK_APPROVED order to Simulated Paper Broker.",
            occurred_at=submit_ts,
        )

        ack_ts = submit_ts + timedelta(milliseconds=max(0, int(order.execution_latency_ms)))

        # Simulated Broker Rejection path
        if order.simulate_rejection:
            rej_reason = (
                order.simulated_rejection_reason
                or "Simulated Paper Broker rejection (price band / exchange throttle test)."
            )
            OrderManagementService.transition_order_status(
                session,
                order,
                OrderStatus.REJECTED,
                actor="paper.broker",
                reason=rej_reason,
                details={"simulated_latency_ms": int(order.execution_latency_ms)},
                occurred_at=ack_ts,
            )
            return order

        # Acknowledge order into Paper Broker working book
        OrderManagementService.transition_order_status(
            session,
            order,
            OrderStatus.ACKNOWLEDGED,
            actor="paper.broker",
            reason=f"Acknowledged by Simulated Paper Broker (latency {order.execution_latency_ms}ms).",
            details={"simulated_latency_ms": int(order.execution_latency_ms)},
            occurred_at=ack_ts,
        )

        # Immediate match ONLY when latency_bars == 0 and caller explicitly allows immediate quote match
        if (
            immediate_match_allowed
            and int(order.latency_bars) == 0
            and current_candle is not None
        ):
            PaperBrokerService._evaluate_single_order_against_candle(
                session,
                order,
                current_candle,
                use_close_as_spot=True,
                settings=cfg,
            )

        return order

    @staticmethod
    def record_execution_fill(
        session: Session,
        *,
        order: PaperOrderModel,
        fill_quantity: int,
        raw_market_price: Decimal,
        fill_price: Decimal,
        bar_timestamp: datetime,
        fill_idempotency_key: str,
        market_event_id: str | None = None,
        liquidity_note: str = "FULL_FILL",
        executed_at: datetime | None = None,
        settings: AppSettings | None = None,
    ) -> tuple[PaperFillModel, bool]:
        """Idempotently record a simulated `PaperFillModel`, update order, and derive position.

        Returns `(fill, is_duplicate)`. Repeated calls with the same `fill_idempotency_key`
        never double-fill the order or double-apply position quantities.
        """
        cfg = settings or get_settings()
        clean_idem = fill_idempotency_key.strip()
        existing_fill = session.scalar(
            select(PaperFillModel).where(PaperFillModel.fill_idempotency_key == clean_idem)
        )
        if existing_fill is not None:
            # Ensure position event also exists idempotently
            PositionService.apply_fill_to_position(
                session, existing_fill, order=order, settings=cfg
            )
            return existing_fill, True

        if order.status not in (
            OrderStatus.ACKNOWLEDGED.value,
            OrderStatus.PARTIALLY_FILLED.value,
        ):
            raise ValueError(
                f"Cannot apply fill to order '{order.order_code}' in status '{order.status}'."
            )

        qty = min(int(fill_quantity), int(order.leaves_quantity))
        if qty <= 0:
            raise ValueError(f"Fill quantity must be > 0 (got {qty}).")

        raw_px = Decimal(str(raw_market_price)).quantize(_Q4)
        exec_px = Decimal(str(fill_price)).quantize(_Q4)
        bar_ts = ensure_ist(bar_timestamp)
        exec_ts = ensure_ist(executed_at) if executed_at is not None else now_ist()

        prior_fills_count = int(
            session.scalar(
                select(func.count())
                .select_from(PaperFillModel)
                .where(PaperFillModel.order_id == order.id)
            )
            or 0
        )
        slip_per_unit = abs(exec_px - raw_px)
        slip_cost = (slip_per_unit * Decimal(qty)).quantize(_Q4)
        charges = _compute_slice_charges(
            fill_price=exec_px,
            fill_quantity=qty,
            charges_bps=Decimal(str(order.charges_bps)),
            per_order_fee_inr=Decimal(str(order.per_order_fee_inr)),
            is_first_fill_for_order=(prior_fills_count == 0),
        )

        fill_code = f"FIL-{order.instrument_symbol}-{exec_ts.strftime('%H%M%S')}-{uuid.uuid4().hex[:5].upper()}"
        fill = PaperFillModel(
            fill_code=fill_code,
            fill_idempotency_key=clean_idem,
            order_id=order.id,
            account_id=order.account_id,
            strategy_instance_id=order.strategy_instance_id,
            strategy_version_id=order.strategy_version_id,
            strategy_version_label=order.strategy_version_label,
            instrument_symbol=order.instrument_symbol,
            side=order.side,
            fill_quantity=qty,
            raw_market_price=raw_px,
            fill_price=exec_px,
            slippage_cost=slip_cost,
            charges=charges,
            simulated_latency_ms=int(order.execution_latency_ms),
            bar_timestamp=bar_ts,
            executed_at=exec_ts,
            market_event_id=market_event_id,
            liquidity_note=liquidity_note,
            correlation_id=order.correlation_id,
        )
        session.add(fill)
        session.flush()

        # Update cumulative order fill quantities & weighted average fill price
        prev_filled = int(order.filled_quantity)
        new_filled = prev_filled + qty
        new_leaves = max(0, int(order.quantity) - new_filled)
        prev_avg = (
            Decimal(str(order.average_fill_price))
            if order.average_fill_price is not None
            else Decimal("0")
        )
        new_avg = (
            (prev_avg * Decimal(prev_filled) + exec_px * Decimal(qty)) / Decimal(new_filled)
        ).quantize(_Q4)

        order.filled_quantity = new_filled
        order.leaves_quantity = new_leaves
        order.average_fill_price = new_avg
        order.total_slippage_cost = (
            Decimal(str(order.total_slippage_cost)) + slip_cost
        ).quantize(_Q4)
        order.total_charges = (Decimal(str(order.total_charges)) + charges).quantize(_Q4)

        target_status = (
            OrderStatus.FILLED if new_leaves == 0 else OrderStatus.PARTIALLY_FILLED
        )
        OrderManagementService.transition_order_status(
            session,
            order,
            target_status,
            actor="paper.broker",
            reason=(
                f"Executed {qty} @ {exec_px:.2f} ({liquidity_note}; "
                f"filled {new_filled}/{order.quantity})."
            ),
            details={
                "fill_code": fill_code,
                "fill_quantity": qty,
                "fill_price": str(exec_px),
                "raw_market_price": str(raw_px),
                "leaves_quantity": new_leaves,
            },
            occurred_at=exec_ts,
        )

        AuditService.record(
            session,
            action="PAPER_FILL_EXECUTED",
            summary=(
                f"Simulated fill {fill_code}: {order.side} {qty} {order.instrument_symbol} "
                f"@ {exec_px:.2f} (order {order.order_code})."
            ),
            actor="paper.broker",
            actor_type=ActorType.SYSTEM,
            category=AuditCategory.EXECUTION,
            severity=AuditSeverity.INFO,
            entity_type="paper_fill",
            entity_id=str(fill.id),
            details={
                "fill_code": fill_code,
                "order_code": order.order_code,
                "fill_quantity": qty,
                "fill_price": str(exec_px),
                "slippage_cost": str(slip_cost),
                "charges": str(charges),
            },
        )

        # Derive position strictly from execution fill
        PositionService.apply_fill_to_position(session, fill, order=order, settings=cfg)
        return fill, False

    @staticmethod
    def process_candle_event(
        session: Session,
        candle: CandleEvent,
        *,
        settings: AppSettings | None = None,
    ) -> list[PaperFillModel]:
        """Match working orders for `candle.symbol` and mark open positions to market."""
        cfg = settings or get_settings()
        clean_sym = candle.symbol.strip().upper()

        working_orders = list(
            session.scalars(
                select(PaperOrderModel)
                .where(
                    PaperOrderModel.instrument_symbol == clean_sym,
                    PaperOrderModel.status.in_(
                        [OrderStatus.ACKNOWLEDGED.value, OrderStatus.PARTIALLY_FILLED.value]
                    ),
                )
                .order_by(asc(PaperOrderModel.created_at), asc(PaperOrderModel.id))
            ).all()
        )

        emitted_fills: list[PaperFillModel] = []
        for order in working_orders:
            fill = PaperBrokerService._evaluate_single_order_against_candle(
                session,
                order,
                candle,
                use_close_as_spot=False,
                settings=cfg,
            )
            if fill is not None:
                emitted_fills.append(fill)

        # Mark all open positions on `candle.symbol` to the completed candle's close
        PositionService.mark_positions_to_market(
            session,
            symbol=clean_sym,
            mark_price=candle.close,
            bar_high=candle.high,
            bar_low=candle.low,
            bar_timestamp=candle.exchange_timestamp,
            increment_bars_held=True,
            settings=cfg,
        )
        return emitted_fills

    @staticmethod
    def _evaluate_single_order_against_candle(
        session: Session,
        order: PaperOrderModel,
        candle: CandleEvent,
        *,
        use_close_as_spot: bool = False,
        settings: AppSettings | None = None,
    ) -> PaperFillModel | None:
        cfg = settings or get_settings()
        bar_ts = ensure_ist(candle.exchange_timestamp)

        # 1. Timestamp-based Expiration Check
        if order.expires_at is not None and bar_ts >= ensure_ist(order.expires_at):
            OrderManagementService.transition_order_status(
                session,
                order,
                OrderStatus.EXPIRED,
                actor="paper.broker",
                reason=f"Order expired at {ensure_ist(order.expires_at).isoformat()} (candle {bar_ts.isoformat()}).",
                occurred_at=now_ist(),
            )
            return None

        # 2. Execution Latency Bar Gate (Never fill at signal candle close when latency_bars >= 1)
        if not use_close_as_spot:
            if order.created_bar_timestamp is not None:
                created_bar_ts = ensure_ist(order.created_bar_timestamp)
                if bar_ts <= created_bar_ts and int(order.latency_bars) >= 1:
                    return None
                if bar_ts > created_bar_ts:
                    order.bars_waited = int(order.bars_waited) + 1
            else:
                order.bars_waited = int(order.bars_waited) + 1

            # Bar-based Time-In-Force Expiration Check
            if (
                order.valid_for_bars is not None
                and int(order.bars_waited) > int(order.valid_for_bars)
            ):
                OrderManagementService.transition_order_status(
                    session,
                    order,
                    OrderStatus.EXPIRED,
                    actor="paper.broker",
                    reason=f"Order exceeded valid_for_bars={order.valid_for_bars} without full execution.",
                    occurred_at=now_ist(),
                )
                return None

            if int(order.bars_waited) < int(order.latency_bars):
                session.flush()
                return None

        # 3. Evaluate Documented Fill Model (MARKET | LIMIT | STOP)
        side = Side(order.side)
        ord_type = OrderType(order.order_type)
        slip_bps = Decimal(str(order.slippage_bps))

        c_open = Decimal(str(candle.open))
        c_high = Decimal(str(candle.high))
        c_low = Decimal(str(candle.low))
        c_close = Decimal(str(candle.close))
        spot_px = c_close if use_close_as_spot else c_open

        raw_px: Decimal | None = None
        exec_px: Decimal | None = None
        note = "FULL_FILL"

        if ord_type == OrderType.MARKET:
            raw_px = spot_px
            exec_px = _apply_adverse_slippage(raw_px, side, slip_bps)
            note = "MARKET_IMMEDIATE" if use_close_as_spot else "MARKET_NEXT_OPEN"

        elif ord_type == OrderType.LIMIT:
            lim_px = Decimal(str(order.limit_price))
            if side == Side.BUY:
                if spot_px <= lim_px:
                    raw_px = spot_px
                    exec_px = min(lim_px, _apply_adverse_slippage(raw_px, side, slip_bps))
                    note = "LIMIT_PRICE_IMPROVED"
                elif (not use_close_as_spot) and c_low < lim_px:
                    raw_px = lim_px
                    exec_px = lim_px
                    note = "LIMIT_TRADED_THROUGH"
            else:  # SELL LIMIT
                if spot_px >= lim_px:
                    raw_px = spot_px
                    exec_px = max(lim_px, _apply_adverse_slippage(raw_px, side, slip_bps))
                    note = "LIMIT_PRICE_IMPROVED"
                elif (not use_close_as_spot) and c_high > lim_px:
                    raw_px = lim_px
                    exec_px = lim_px
                    note = "LIMIT_TRADED_THROUGH"

        elif ord_type == OrderType.STOP:
            stp_px = Decimal(str(order.stop_price))
            was_triggered = bool(order.stop_triggered)
            if side == Side.BUY:
                triggered = (spot_px >= stp_px) if use_close_as_spot else (c_high >= stp_px)
                if triggered:
                    order.stop_triggered = True
                    order.stop_triggered_at = order.stop_triggered_at or bar_ts
                    raw_px = max(spot_px, stp_px)
                    exec_px = _apply_adverse_slippage(raw_px, side, slip_bps)
                    note = "STOP_TRIGGERED_BUY"
            else:  # SELL STOP
                triggered = (spot_px <= stp_px) if use_close_as_spot else (c_low <= stp_px)
                if triggered:
                    order.stop_triggered = True
                    order.stop_triggered_at = order.stop_triggered_at or bar_ts
                    raw_px = min(spot_px, stp_px)
                    exec_px = _apply_adverse_slippage(raw_px, side, slip_bps)
                    note = "STOP_TRIGGERED_SELL"
            if order.stop_triggered and not was_triggered:
                AuditService.record(
                    session,
                    action="STOP_TRIGGERED",
                    event_type="STOP_TRIGGERED",
                    summary=f"STOP order {order.order_code} triggered at {stp_px:.2f} on {order.instrument_symbol}.",
                    actor="paper.broker",
                    actor_type=ActorType.SYSTEM,
                    category=AuditCategory.EXECUTION,
                    severity=AuditSeverity.INFO,
                    entity_type="paper_order",
                    entity_id=str(order.id),
                    order_id=str(order.id),
                    source="paper.broker",
                    details={"stop_price": str(stp_px), "side": side.value},
                    correlation_id=order.correlation_id,
                )

        if raw_px is None or exec_px is None:
            # Check if valid_for_bars was exact-1-bar TIF that just completed without fill
            if (
                (not use_close_as_spot)
                and order.valid_for_bars is not None
                and int(order.bars_waited) >= int(order.valid_for_bars)
            ):
                OrderManagementService.transition_order_status(
                    session,
                    order,
                    OrderStatus.EXPIRED,
                    actor="paper.broker",
                    reason=f"Order expired at end of {order.valid_for_bars}-bar TIF window without fill.",
                    occurred_at=now_ist(),
                )
            else:
                session.flush()
            return None

        # 4. Determine Slice Quantity (Supports Configurable Partial Fills)
        ratio = Decimal(str(order.max_fill_ratio_per_bar))
        if ratio < Decimal("1"):
            slice_cap = max(1, int(math.ceil(float(Decimal(order.quantity) * ratio))))
            fill_qty = min(int(order.leaves_quantity), slice_cap)
            if fill_qty < int(order.leaves_quantity):
                note = f"PARTIAL_{note}"
        else:
            fill_qty = int(order.leaves_quantity)

        slice_number = int(order.filled_quantity) + qty_marker(fill_qty)
        idem_key = f"fill:{order.id}:{candle.event_id}:{slice_number}"
        fill, _ = PaperBrokerService.record_execution_fill(
            session,
            order=order,
            fill_quantity=fill_qty,
            raw_market_price=raw_px,
            fill_price=exec_px,
            bar_timestamp=bar_ts,
            fill_idempotency_key=idem_key,
            market_event_id=str(candle.event_id),
            liquidity_note=note,
            settings=cfg,
        )
        return fill


def qty_marker(fill_qty: int) -> int:
    return int(fill_qty)
