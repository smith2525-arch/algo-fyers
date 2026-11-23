"""End-to-End Trade Lifecycle Correlation Trace View Service (`core/audit/trace.py`).

Reconstructs one trade's complete visual & verifiable lifecycle from a Correlation ID:
    Market Event
        ↓
    Signal
        ↓
    Risk Decision
        ↓
    Order
        ↓
    Execution
        ↓
    Position
        ↓
    Exit
        ↓
    Final P&L
"""

from __future__ import annotations

import uuid
from decimal import Decimal
from typing import Any

from sqlalchemy import asc, desc, select
from sqlalchemy.orm import Session

from core.audit.service import AuditService, _format_utc_iso
from core.clock import format_ist_iso
from core.oms.service import OrderManagementService
from core.persistence.models import (
    AuditEventModel,
    PaperFillModel,
    PaperOrderModel,
    PaperPositionModel,
    PositionEventModel,
    ReplaySessionModel,
    ReplaySignalModel,
    RiskDecisionModel,
)
from core.positions.service import PositionService
from core.risk.service import RiskControlService

_Q2 = Decimal("0.01")


class CorrelationTraceService:
    """Builds consulting-grade end-to-end trade lifecycle traces by Correlation ID."""

    @staticmethod
    def list_traceable_correlations(
        session: Session, *, limit: int = 30
    ) -> list[dict[str, Any]]:
        """List recent trade/order/signal correlation IDs for 1-click Trace View inspection."""
        orders = list(
            session.scalars(
                select(PaperOrderModel)
                .order_by(desc(PaperOrderModel.created_at))
                .limit(max(1, min(limit, 100)))
            ).all()
        )
        seen: set[str] = set()
        items: list[dict[str, Any]] = []
        for o in orders:
            if o.correlation_id in seen:
                continue
            seen.add(o.correlation_id)
            items.append(
                {
                    "correlation_id": o.correlation_id,
                    "order_code": o.order_code,
                    "instrument_symbol": o.instrument_symbol,
                    "side": o.side,
                    "quantity": int(o.quantity),
                    "order_status": o.status,
                    "strategy": o.strategy_version_label or "MANUAL_PAPER_DESK",
                    "created_at": format_ist_iso(o.created_at),
                }
            )

        if len(items) < limit:
            sigs = list(
                session.scalars(
                    select(ReplaySignalModel)
                    .order_by(desc(ReplaySignalModel.emitted_at))
                    .limit(limit)
                ).all()
            )
            for s in sigs:
                if s.correlation_id in seen:
                    continue
                seen.add(s.correlation_id)
                items.append(
                    {
                        "correlation_id": s.correlation_id,
                        "order_code": s.signal_code,
                        "instrument_symbol": s.instrument_symbol,
                        "side": s.side,
                        "quantity": 0,
                        "order_status": f"SIGNAL_{s.status}",
                        "strategy": s.strategy_version_label,
                        "created_at": format_ist_iso(s.emitted_at),
                    }
                )
        return items

    @staticmethod
    def build_lifecycle_trace(
        session: Session, correlation_id: str
    ) -> dict[str, Any]:
        """Assemble the 8-stage trade lifecycle trace for `correlation_id`."""
        clean_cid = correlation_id.strip()

        # 1. Direct correlation matches
        signals = list(
            session.scalars(
                select(ReplaySignalModel)
                .where(ReplaySignalModel.correlation_id == clean_cid)
                .order_by(asc(ReplaySignalModel.emitted_at))
            ).all()
        )
        risk_decisions = list(
            session.scalars(
                select(RiskDecisionModel)
                .where(RiskDecisionModel.correlation_id == clean_cid)
                .order_by(asc(RiskDecisionModel.evaluated_at))
            ).all()
        )
        orders = list(
            session.scalars(
                select(PaperOrderModel)
                .where(PaperOrderModel.correlation_id == clean_cid)
                .order_by(asc(PaperOrderModel.created_at))
            ).all()
        )
        fills = list(
            session.scalars(
                select(PaperFillModel)
                .where(PaperFillModel.correlation_id == clean_cid)
                .order_by(asc(PaperFillModel.executed_at))
            ).all()
        )

        # Cross-link signal <-> order <-> risk_decision if linked by FK across ticks
        if signals and not orders:
            orders = list(
                session.scalars(
                    select(PaperOrderModel).where(
                        PaperOrderModel.signal_id.in_([s.id for s in signals])
                    )
                ).all()
            )
        if orders and not signals:
            sig_ids = [o.signal_id for o in orders if o.signal_id is not None]
            if sig_ids:
                signals = list(
                    session.scalars(
                        select(ReplaySignalModel).where(ReplaySignalModel.id.in_(sig_ids))
                    ).all()
                )
        if orders and not fills:
            fills = list(
                session.scalars(
                    select(PaperFillModel)
                    .where(PaperFillModel.order_id.in_([o.id for o in orders]))
                    .order_by(asc(PaperFillModel.executed_at))
                ).all()
            )
        if orders and not risk_decisions:
            risk_decisions = list(
                session.scalars(
                    select(RiskDecisionModel)
                    .where(RiskDecisionModel.order_id.in_([o.id for o in orders]))
                    .order_by(asc(RiskDecisionModel.evaluated_at))
                ).all()
            )

        # Locate Position & PositionEvents touched by these fills (including round-trip exit!)
        fill_ids = [f.id for f in fills]
        pos_events: list[PositionEventModel] = []
        if fill_ids:
            pos_events = list(
                session.scalars(
                    select(PositionEventModel)
                    .where(PositionEventModel.fill_id.in_(fill_ids))
                    .order_by(asc(PositionEventModel.occurred_at))
                ).all()
            )

        position: PaperPositionModel | None = None
        if pos_events:
            position = session.get(PaperPositionModel, pos_events[0].position_id)

        # If this correlation_id was an EXIT order/fill, also resolve the entry chain on `position`
        entry_order: PaperOrderModel | None = orders[0] if orders else None
        exit_order: PaperOrderModel | None = None
        exit_fills: list[PaperFillModel] = []
        exit_signal: ReplaySignalModel | None = None

        if position is not None:
            all_pos_events = list(
                session.scalars(
                    select(PositionEventModel)
                    .where(PositionEventModel.position_id == position.id)
                    .order_by(asc(PositionEventModel.occurred_at))
                ).all()
            )
            opening_evts = [
                pe for pe in all_pos_events if pe.event_type in ("OPENED", "SCALED_IN")
            ]
            closing_evts = [
                pe
                for pe in all_pos_events
                if pe.event_type in ("PARTIALLY_CLOSED", "CLOSED", "REVERSED_OPEN")
            ]

            # If caller searched by exit correlation_id, backfill entry artefacts from opening_evts
            if entry_order is not None and entry_order.reduce_only and opening_evts:
                exit_order = entry_order
                exit_fills = list(fills)
                open_fill = session.get(PaperFillModel, opening_evts[0].fill_id)
                if open_fill is not None:
                    fills = [open_fill]
                    entry_order = session.get(PaperOrderModel, open_fill.order_id)
                    if entry_order is not None:
                        orders = [entry_order]
                        if entry_order.signal_id is not None:
                            s_open = session.get(ReplaySignalModel, entry_order.signal_id)
                            if s_open is not None:
                                signals = [s_open]
                        rd_open = list(
                            session.scalars(
                                select(RiskDecisionModel).where(
                                    RiskDecisionModel.order_id == entry_order.id
                                )
                            ).all()
                        )
                        if rd_open:
                            risk_decisions = rd_open
            elif closing_evts:
                c_fill_ids = [ce.fill_id for ce in closing_evts]
                exit_fills = list(
                    session.scalars(
                        select(PaperFillModel)
                        .where(PaperFillModel.id.in_(c_fill_ids))
                        .order_by(asc(PaperFillModel.executed_at))
                    ).all()
                )
                if exit_fills:
                    exit_order = session.get(PaperOrderModel, exit_fills[-1].order_id)
                    if exit_order is not None and exit_order.signal_id is not None:
                        exit_signal = session.get(ReplaySignalModel, exit_order.signal_id)

        # Collect related correlation IDs so the chronological audit timeline shows the whole story
        related_cids: set[str] = {clean_cid}
        for s in signals:
            related_cids.add(s.correlation_id)
        for o in orders:
            related_cids.add(o.correlation_id)
        if exit_order is not None:
            related_cids.add(exit_order.correlation_id)

        audit_rows = list(
            session.scalars(
                select(AuditEventModel)
                .where(AuditEventModel.correlation_id.in_(list(related_cids)))
                .order_by(asc(AuditEventModel.occurred_at))
            ).all()
        )
        audit_timeline = [
            {
                "id": str(ev.id),
                "occurred_at": format_ist_iso(ev.occurred_at),
                "occurred_at_utc": _format_utc_iso(ev.occurred_at),
                "event_type": ev.event_type or ev.action,
                "action": ev.action,
                "category": ev.category,
                "severity": ev.severity,
                "actor": ev.actor,
                "source": ev.source or ev.actor,
                "entity_type": ev.entity_type,
                "entity_id": ev.entity_id,
                "strategy_id": ev.strategy_id,
                "signal_id": ev.signal_id,
                "order_id": ev.order_id,
                "correlation_id": ev.correlation_id,
                "summary": ev.summary,
                "details": ev.details_json or {},
            }
            for ev in audit_rows
        ]

        found_any = bool(signals or risk_decisions or orders or fills or audit_timeline)

        # Build Stage 1: Market Event
        primary_sig = signals[0] if signals else None
        primary_ord = entry_order
        market_event_stage: dict[str, Any]
        if primary_sig is not None:
            rpl = (
                session.get(ReplaySessionModel, primary_sig.replay_session_id)
                if primary_sig.replay_session_id
                else None
            )
            market_event_stage = {
                "stage": "1. Market Event",
                "present": True,
                "status": "CAPTURED",
                "headline": f"{primary_sig.instrument_symbol} ({primary_sig.timeframe}) @ {Decimal(str(primary_sig.reference_price)):,.2f}",
                "timestamp_ist": format_ist_iso(primary_sig.bar_timestamp),
                "timestamp_utc": _format_utc_iso(primary_sig.bar_timestamp),
                "details": {
                    "market_event_id": primary_sig.market_event_id,
                    "symbol": primary_sig.instrument_symbol,
                    "timeframe": primary_sig.timeframe,
                    "bar_close_price": str(Decimal(str(primary_sig.reference_price)).quantize(_Q2)),
                    "replay_session": rpl.session_code if rpl else "DIRECT_STREAM",
                    "indicators": primary_sig.indicators_json or {},
                },
            }
        elif primary_ord is not None:
            ts_bar = primary_ord.created_bar_timestamp or primary_ord.created_at
            market_event_stage = {
                "stage": "1. Market Event",
                "present": True,
                "status": "CAPTURED",
                "headline": f"{primary_ord.instrument_symbol} ({primary_ord.timeframe}) Quote Snapshot",
                "timestamp_ist": format_ist_iso(ts_bar),
                "timestamp_utc": _format_utc_iso(ts_bar),
                "details": {
                    "symbol": primary_ord.instrument_symbol,
                    "timeframe": primary_ord.timeframe,
                    "reference_price": (
                        str(Decimal(str(primary_ord.requested_reference_price)).quantize(_Q2))
                        if primary_ord.requested_reference_price is not None
                        else "—"
                    ),
                    "order_source": primary_ord.order_source,
                },
            }
        else:
            market_event_stage = {
                "stage": "1. Market Event",
                "present": False,
                "status": "N/A",
                "headline": "Non-market operational event",
                "timestamp_ist": None,
                "timestamp_utc": None,
                "details": {},
            }

        # Stage 2: Signal
        if primary_sig is not None:
            signal_stage = {
                "stage": "2. Signal",
                "present": True,
                "status": primary_sig.status,
                "headline": f"{primary_sig.signal_code} • {primary_sig.direction} ({primary_sig.side})",
                "timestamp_ist": format_ist_iso(primary_sig.emitted_at),
                "timestamp_utc": _format_utc_iso(primary_sig.emitted_at),
                "details": {
                    "signal_id": str(primary_sig.id),
                    "signal_code": primary_sig.signal_code,
                    "strategy_version": primary_sig.strategy_version_label,
                    "direction": primary_sig.direction,
                    "reference_price": str(Decimal(str(primary_sig.reference_price)).quantize(_Q2)),
                    "reason": primary_sig.rationale,
                },
            }
        elif primary_ord is not None:
            signal_stage = {
                "stage": "2. Signal",
                "present": False,
                "status": "MANUAL_TICKET",
                "headline": f"Direct {primary_ord.order_source} (no strategy signal)",
                "timestamp_ist": format_ist_iso(primary_ord.created_at),
                "timestamp_utc": _format_utc_iso(primary_ord.created_at),
                "details": {"order_source": primary_ord.order_source},
            }
        else:
            signal_stage = {
                "stage": "2. Signal",
                "present": False,
                "status": "NONE",
                "headline": "No signal associated",
                "timestamp_ist": None,
                "timestamp_utc": None,
                "details": {},
            }

        # Stage 3: Risk Decision
        primary_rd = risk_decisions[0] if risk_decisions else None
        if primary_rd is not None:
            rd_dict = RiskControlService.serialize_risk_decision(primary_rd)
            risk_stage = {
                "stage": "3. Risk Decision",
                "present": True,
                "status": primary_rd.decision,
                "headline": f"{primary_rd.decision} • Rule: {primary_rd.primary_rule} ({primary_rd.current_value} / {primary_rd.limit_value})",
                "timestamp_ist": format_ist_iso(primary_rd.evaluated_at),
                "timestamp_utc": _format_utc_iso(primary_rd.evaluated_at),
                "details": rd_dict,
            }
        else:
            risk_stage = {
                "stage": "3. Risk Decision",
                "present": False,
                "status": "NOT_REACHED",
                "headline": "No pre-trade risk evaluation recorded",
                "timestamp_ist": None,
                "timestamp_utc": None,
                "details": {},
            }

        # Stage 4: Order
        if primary_ord is not None:
            ord_dict = OrderManagementService.serialize_order(primary_ord, include_events=True)
            order_stage = {
                "stage": "4. Order",
                "present": True,
                "status": primary_ord.status,
                "headline": f"{primary_ord.order_code} • {primary_ord.side} {primary_ord.quantity} {primary_ord.instrument_symbol} ({primary_ord.order_type})",
                "timestamp_ist": format_ist_iso(primary_ord.created_at),
                "timestamp_utc": _format_utc_iso(primary_ord.created_at),
                "details": ord_dict,
            }
        else:
            order_stage = {
                "stage": "4. Order",
                "present": False,
                "status": "NOT_CREATED",
                "headline": "Order not created",
                "timestamp_ist": None,
                "timestamp_utc": None,
                "details": {},
            }

        # Stage 5: Execution
        if fills:
            tot_fill_qty = sum(int(f.fill_quantity) for f in fills)
            w_px = (
                sum(Decimal(str(f.fill_price)) * Decimal(int(f.fill_quantity)) for f in fills)
                / Decimal(tot_fill_qty)
            ).quantize(_Q2)
            execution_stage = {
                "stage": "5. Execution",
                "present": True,
                "status": "FILLED" if (primary_ord and primary_ord.leaves_quantity == 0) else "PARTIALLY_FILLED",
                "headline": f"{len(fills)} Fill Slice(s) • {tot_fill_qty} qty @ Avg {w_px:,.2f}",
                "timestamp_ist": format_ist_iso(fills[-1].executed_at),
                "timestamp_utc": _format_utc_iso(fills[-1].executed_at),
                "details": {
                    "fills": [OrderManagementService.serialize_fill(f) for f in fills],
                },
            }
        else:
            execution_stage = {
                "stage": "5. Execution",
                "present": False,
                "status": primary_ord.status if primary_ord else "NO_EXECUTION",
                "headline": (
                    f"Blocked / Unfilled ({primary_ord.status})"
                    if primary_ord
                    else "No simulated fills executed"
                ),
                "timestamp_ist": None,
                "timestamp_utc": None,
                "details": {},
            }

        # Stage 6: Position
        if position is not None:
            pos_dict = PositionService.serialize_position(position)
            position_stage = {
                "stage": "6. Position",
                "present": True,
                "status": position.status,
                "headline": f"{position.position_code} • {position.side} {position.opened_quantity} {position.instrument_symbol} @ {Decimal(str(position.average_entry_price)):,.2f}",
                "timestamp_ist": format_ist_iso(position.opened_at),
                "timestamp_utc": _format_utc_iso(position.opened_at),
                "details": pos_dict,
            }
        else:
            position_stage = {
                "stage": "6. Position",
                "present": False,
                "status": "FLAT",
                "headline": "No position opened",
                "timestamp_ist": None,
                "timestamp_utc": None,
                "details": {},
            }

        # Stage 7: Exit
        if position is not None and position.status == "CLOSED":
            exit_px_str = (
                f"{Decimal(str(position.average_exit_price)):,.2f}"
                if position.average_exit_price is not None
                else "—"
            )
            exit_stage = {
                "stage": "7. Exit",
                "present": True,
                "status": "CLOSED",
                "headline": f"{position.exit_reason or 'EXECUTION_CLOSE'} • Closed {position.closed_quantity} @ {exit_px_str}",
                "timestamp_ist": format_ist_iso(position.closed_at)
                if position.closed_at
                else None,
                "timestamp_utc": _format_utc_iso(position.closed_at)
                if position.closed_at
                else None,
                "details": {
                    "exit_reason": position.exit_reason,
                    "exit_order_code": exit_order.order_code if exit_order else None,
                    "exit_signal_code": exit_signal.signal_code if exit_signal else None,
                    "exit_fills": [OrderManagementService.serialize_fill(ef) for ef in exit_fills],
                },
            }
        elif position is not None:
            exit_stage = {
                "stage": "7. Exit",
                "present": False,
                "status": "OPEN_ACTIVE",
                "headline": f"Position still OPEN (Mark {Decimal(str(position.last_price)):,.2f})",
                "timestamp_ist": format_ist_iso(position.last_mark_at),
                "timestamp_utc": _format_utc_iso(position.last_mark_at),
                "details": {
                    "stop_loss_price": str(position.stop_loss_price)
                    if position.stop_loss_price
                    else None,
                    "target_price": str(position.target_price) if position.target_price else None,
                },
            }
        else:
            exit_stage = {
                "stage": "7. Exit",
                "present": False,
                "status": "N/A",
                "headline": "No position to exit",
                "timestamp_ist": None,
                "timestamp_utc": None,
                "details": {},
            }

        # Stage 8: Final P&L
        if position is not None:
            real_pnl = Decimal(str(position.realized_pnl)).quantize(_Q2)
            unreal_pnl = Decimal(str(position.unrealized_pnl)).quantize(_Q2)
            charges_tot = Decimal(str(position.total_charges)).quantize(_Q2)
            gross_pnl = (real_pnl + charges_tot + unreal_pnl).quantize(_Q2)
            net_pnl = (real_pnl + unreal_pnl).quantize(_Q2)
            all_trade_fills = fills + exit_fills
            slip_tot = sum(
                (Decimal(str(f.slippage_cost)) for f in all_trade_fills), Decimal("0.00")
            ).quantize(_Q2)
            final_pnl_stage = {
                "stage": "8. Final P&L",
                "present": True,
                "status": "REALIZED" if position.status == "CLOSED" else "MARK_TO_MARKET",
                "headline": f"Net ₹{net_pnl:+,.2f} (Gross ₹{gross_pnl:+,.2f} | Charges ₹{charges_tot:,.2f})",
                "timestamp_ist": format_ist_iso(position.closed_at or position.last_mark_at),
                "timestamp_utc": _format_utc_iso(position.closed_at or position.last_mark_at),
                "details": {
                    "gross_pnl": str(gross_pnl),
                    "slippage_cost": str(slip_tot),
                    "charges": str(charges_tot),
                    "realized_pnl": str(real_pnl),
                    "unrealized_pnl": str(unreal_pnl),
                    "net_pnl": str(net_pnl),
                },
            }
        else:
            final_pnl_stage = {
                "stage": "8. Final P&L",
                "present": False,
                "status": "ZERO_IMPACT",
                "headline": "₹0.00 (Order did not open a position)",
                "timestamp_ist": None,
                "timestamp_utc": None,
                "details": {
                    "gross_pnl": "0.00",
                    "slippage_cost": "0.00",
                    "charges": "0.00",
                    "realized_pnl": "0.00",
                    "unrealized_pnl": "0.00",
                    "net_pnl": "0.00",
                },
            }

        stages = [
            market_event_stage,
            signal_stage,
            risk_stage,
            order_stage,
            execution_stage,
            position_stage,
            exit_stage,
            final_pnl_stage,
        ]

        return {
            "found": found_any,
            "correlation_id": clean_cid,
            "related_correlation_ids": sorted(list(related_cids)),
            "simulation_banner": "SIMULATED PAPER-TRADING RESULTS",
            "stages": stages,
            "audit_timeline": audit_timeline,
        }
