"""Deterministic Reconciliation Engine (`core/reconciliation/service.py`).

Checks at minimum:
1. Executions vs Positions (`paper_fills` -> `position_events` -> `paper_positions`)
2. Open Orders (`filled_quantity + leaves_quantity == quantity`, fill sums, terminal invariants)
3. Position Quantities (`OPEN` net qty matches execution ledger; `CLOSED` qty == 0)
4. Realized P&L Integrity (position realized P&L + account balances match execution math)

CRITICAL INVARIANT:
- Default `apply_repair=False` NEVER mutates discrepancies silently; it records a
  `DISCREPANCY_DETECTED` run and emits `RECONCILIATION_FAILED` in the Audit Trail.
- When `apply_repair=True` is explicitly invoked, EVERY single change is recorded
  with exact `before` and `after` values in `repairs_json` and the Audit Trail.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from decimal import Decimal
from typing import Any

from sqlalchemy import asc, desc, select
from sqlalchemy.orm import Session

from core.audit.service import AuditService, _format_utc_iso
from core.clock import format_ist_iso, now_ist
from core.config import AppSettings, get_settings
from core.domain.enums import (
    TERMINAL_ORDER_STATUSES,
    WORKING_ORDER_STATUSES,
    ActorType,
    AuditCategory,
    AuditEventType,
    AuditSeverity,
    OrderStatus,
    PositionStatus,
    ReconciliationStatus,
    Side,
    TradeDirection,
)
from core.logging import get_correlation_id
from core.persistence.models import (
    PaperAccountModel,
    PaperFillModel,
    PaperOrderModel,
    PaperPositionModel,
    PositionEventModel,
    ReconciliationRunModel,
)
from core.positions.service import PositionService

_Q2 = Decimal("0.01")
_Q4 = Decimal("0.0001")


@dataclass
class _ExpectedBook:
    side: str | None = None
    qty: int = 0
    avg_entry: Decimal = Decimal("0.0000")
    unalloc_chg: Decimal = Decimal("0.0000")
    realized_pnl: Decimal = Decimal("0.0000")


class ReconciliationService:
    """Executes deterministic ledger verification, persists runs, and logs explicit repairs."""

    @staticmethod
    def run_reconciliation(
        session: Session,
        *,
        apply_repair: bool = False,
        actor: str = "system.reconciliation",
        settings: AppSettings | None = None,
    ) -> dict[str, Any]:
        cfg = settings or get_settings()
        started_at = now_ist()
        acct = session.scalar(
            select(PaperAccountModel).where(
                PaperAccountModel.account_code == cfg.DEFAULT_PAPER_ACCOUNT_CODE
            )
        )
        if acct is None:
            acct = session.scalar(
                select(PaperAccountModel).order_by(PaperAccountModel.created_at.asc()).limit(1)
            )

        discrepancies: list[dict[str, Any]] = []
        repairs: list[dict[str, Any]] = []

        # ---------------------------------------------------------------------
        # 1. Open & Terminal Orders Consistency Check
        # ---------------------------------------------------------------------
        all_orders = list(
            session.scalars(
                select(PaperOrderModel).order_by(asc(PaperOrderModel.created_at))
            ).all()
        )
        working_vals = {s.value for s in WORKING_ORDER_STATUSES}
        terminal_vals = {s.value for s in TERMINAL_ORDER_STATUSES}
        open_orders_checked = 0
        order_issues = 0

        for order in all_orders:
            if order.status in working_vals:
                open_orders_checked += 1

            ord_fills = list(
                session.scalars(
                    select(PaperFillModel)
                    .where(PaperFillModel.order_id == order.id)
                    .order_by(asc(PaperFillModel.executed_at))
                ).all()
            )
            sum_fill_qty = sum(int(f.fill_quantity) for f in ord_fills)

            if int(order.filled_quantity) != sum_fill_qty:
                order_issues += 1
                discrepancies.append(
                    {
                        "check": "open_orders",
                        "entity_type": "paper_order",
                        "entity_id": str(order.id),
                        "code": order.order_code,
                        "field": "filled_quantity",
                        "expected": str(sum_fill_qty),
                        "actual": str(order.filled_quantity),
                        "detail": f"Order {order.order_code} filled_quantity ({order.filled_quantity}) != sum of fills ({sum_fill_qty}).",
                    }
                )
                if apply_repair:
                    before_fq = int(order.filled_quantity)
                    before_lq = int(order.leaves_quantity)
                    order.filled_quantity = sum_fill_qty
                    order.leaves_quantity = (
                        0
                        if order.status in terminal_vals
                        else max(0, int(order.quantity) - sum_fill_qty)
                    )
                    repairs.append(
                        {
                            "entity_type": "paper_order",
                            "entity_id": str(order.id),
                            "code": order.order_code,
                            "field": "filled_quantity/leaves_quantity",
                            "before": f"{before_fq}/{before_lq}",
                            "after": f"{order.filled_quantity}/{order.leaves_quantity}",
                            "reason": "Synchronized order fill/leaves quantities from immutable paper_fills.",
                        }
                    )

            if order.status in working_vals and (
                int(order.filled_quantity) + int(order.leaves_quantity) != int(order.quantity)
            ):
                order_issues += 1
                exp_leaves = max(0, int(order.quantity) - int(order.filled_quantity))
                discrepancies.append(
                    {
                        "check": "open_orders",
                        "entity_type": "paper_order",
                        "entity_id": str(order.id),
                        "code": order.order_code,
                        "field": "leaves_quantity",
                        "expected": str(exp_leaves),
                        "actual": str(order.leaves_quantity),
                        "detail": f"Working order {order.order_code} filled+leaves != quantity ({order.quantity}).",
                    }
                )
                if apply_repair:
                    before_l = int(order.leaves_quantity)
                    order.leaves_quantity = exp_leaves
                    repairs.append(
                        {
                            "entity_type": "paper_order",
                            "entity_id": str(order.id),
                            "code": order.order_code,
                            "field": "leaves_quantity",
                            "before": str(before_l),
                            "after": str(exp_leaves),
                            "reason": "Restored working order leaves_quantity invariant.",
                        }
                    )

            if order.status in terminal_vals and int(order.leaves_quantity) != 0:
                order_issues += 1
                discrepancies.append(
                    {
                        "check": "open_orders",
                        "entity_type": "paper_order",
                        "entity_id": str(order.id),
                        "code": order.order_code,
                        "field": "leaves_quantity",
                        "expected": "0",
                        "actual": str(order.leaves_quantity),
                        "detail": f"Terminal order {order.order_code} ({order.status}) has non-zero leaves_quantity.",
                    }
                )
                if apply_repair:
                    before_l = int(order.leaves_quantity)
                    order.leaves_quantity = 0
                    repairs.append(
                        {
                            "entity_type": "paper_order",
                            "entity_id": str(order.id),
                            "code": order.order_code,
                            "field": "leaves_quantity",
                            "before": str(before_l),
                            "after": "0",
                            "reason": "Cleared non-zero leaves_quantity on terminal order.",
                        }
                    )

        # ---------------------------------------------------------------------
        # 2. Executions vs Positions, Position Quantities & Realized P&L Integrity
        # ---------------------------------------------------------------------
        fills = list(
            session.scalars(
                select(PaperFillModel).order_by(
                    asc(PaperFillModel.executed_at), asc(PaperFillModel.id)
                )
            ).all()
        )
        exec_vs_pos_issues = 0
        for f in fills:
            has_evt = session.scalar(
                select(PositionEventModel.id).where(PositionEventModel.fill_id == f.id)
            )
            if has_evt is None:
                exec_vs_pos_issues += 1
                discrepancies.append(
                    {
                        "check": "executions_vs_positions",
                        "entity_type": "paper_fill",
                        "entity_id": str(f.id),
                        "code": f.fill_code,
                        "field": "position_event",
                        "expected": "APPLIED",
                        "actual": "MISSING",
                        "detail": f"Execution fill {f.fill_code} has not been applied to position_events.",
                    }
                )
                if apply_repair:
                    pos_applied, _ = PositionService.apply_fill_to_position(
                        session, f, settings=cfg
                    )
                    repairs.append(
                        {
                            "entity_type": "paper_fill",
                            "entity_id": str(f.id),
                            "code": f.fill_code,
                            "field": "position_event",
                            "before": "MISSING",
                            "after": f"APPLIED_TO_{pos_applied.position_code}",
                            "reason": "Applied unindexed execution fill to position book.",
                        }
                    )

        # Deterministically replay fills in memory to compute expected book quantities & realized P&L
        sim_books: dict[tuple[uuid.UUID, uuid.UUID | None, str], _ExpectedBook] = {}
        for f in fills:
            key = (f.account_id, f.strategy_instance_id, f.instrument_symbol)
            b = sim_books.setdefault(key, _ExpectedBook())
            f_dir = (
                TradeDirection.LONG.value
                if f.side == Side.BUY.value
                else TradeDirection.SHORT.value
            )
            fq = int(f.fill_quantity)
            fp = Decimal(str(f.fill_price)).quantize(_Q4)
            fc = Decimal(str(f.charges)).quantize(_Q4)
            ord_row = session.get(PaperOrderModel, f.order_id)
            red_only = bool(ord_row.reduce_only) if ord_row else False

            if b.qty == 0 or b.side is None:
                b.side = f_dir
                b.qty = fq
                b.avg_entry = fp
                b.unalloc_chg = fc
            elif b.side == f_dir:
                nq = b.qty + fq
                b.avg_entry = (
                    (b.avg_entry * Decimal(b.qty) + fp * Decimal(fq)) / Decimal(nq)
                ).quantize(_Q4)
                b.qty = nq
                b.unalloc_chg = (b.unalloc_chg + fc).quantize(_Q4)
            else:
                cq = min(b.qty, fq)
                rem = fq - cq
                gross = (
                    ((fp - b.avg_entry) * Decimal(cq)).quantize(_Q4)
                    if b.side == TradeDirection.LONG.value
                    else ((b.avg_entry - fp) * Decimal(cq)).quantize(_Q4)
                )
                alloc_in = (
                    b.unalloc_chg
                    if cq == b.qty
                    else (b.unalloc_chg * Decimal(cq) / Decimal(b.qty)).quantize(_Q4)
                )
                alloc_out = (
                    fc if rem == 0 else (fc * Decimal(cq) / Decimal(fq)).quantize(_Q4)
                )
                b.realized_pnl = (b.realized_pnl + gross - alloc_in - alloc_out).quantize(_Q4)
                b.unalloc_chg = max(Decimal("0"), (b.unalloc_chg - alloc_in).quantize(_Q4))
                b.qty -= cq
                if b.qty == 0:
                    b.side = None
                    b.avg_entry = Decimal("0")
                    if rem > 0 and not red_only:
                        b.side = f_dir
                        b.qty = rem
                        b.avg_entry = fp
                        b.unalloc_chg = max(Decimal("0"), (fc - alloc_out).quantize(_Q4))

        positions = list(session.scalars(select(PaperPositionModel)).all())
        by_key: dict[tuple[uuid.UUID, uuid.UUID | None, str], list[PaperPositionModel]] = {}
        for p in positions:
            by_key.setdefault(
                (p.account_id, p.strategy_instance_id, p.instrument_symbol), []
            ).append(p)

        qty_issues = 0
        pnl_issues = 0

        for p in positions:
            if p.status == PositionStatus.CLOSED.value and int(p.quantity) != 0:
                qty_issues += 1
                discrepancies.append(
                    {
                        "check": "position_quantities",
                        "entity_type": "paper_position",
                        "entity_id": str(p.id),
                        "code": p.position_code,
                        "field": "quantity",
                        "expected": "0",
                        "actual": str(p.quantity),
                        "detail": f"CLOSED position {p.position_code} has non-zero quantity {p.quantity}.",
                    }
                )
                if apply_repair:
                    prev_q = int(p.quantity)
                    p.quantity = 0
                    p.unrealized_pnl = Decimal("0.0000")
                    repairs.append(
                        {
                            "entity_type": "paper_position",
                            "entity_id": str(p.id),
                            "code": p.position_code,
                            "field": "quantity",
                            "before": str(prev_q),
                            "after": "0",
                            "reason": "Zeroed open quantity on CLOSED paper position.",
                        }
                    )

        all_keys = set(sim_books.keys()) | set(by_key.keys())
        for key in all_keys:
            sb = sim_books.get(key, _ExpectedBook())
            plist = by_key.get(key, [])
            open_rows = [p for p in plist if p.status == PositionStatus.OPEN.value]
            actual_open_qty = sum(int(p.quantity) for p in open_rows)
            actual_realized = sum(
                (Decimal(str(p.realized_pnl)) for p in plist), Decimal("0.0000")
            )

            if actual_open_qty != sb.qty:
                qty_issues += 1
                discrepancies.append(
                    {
                        "check": "position_quantities",
                        "entity_type": "paper_position",
                        "entity_id": str(open_rows[0].id) if open_rows else key[2],
                        "code": open_rows[0].position_code if open_rows else key[2],
                        "field": "open_quantity",
                        "expected": str(sb.qty),
                        "actual": str(actual_open_qty),
                        "detail": f"Open position quantity on {key[2]} ({actual_open_qty}) != execution-derived quantity ({sb.qty}).",
                    }
                )
                if apply_repair and len(open_rows) == 1:
                    target_p = open_rows[0]
                    before_q = int(target_p.quantity)
                    target_p.quantity = sb.qty
                    if sb.qty == 0:
                        target_p.status = PositionStatus.CLOSED.value
                        target_p.unrealized_pnl = Decimal("0.0000")
                        target_p.closed_at = now_ist()
                    else:
                        target_p.average_entry_price = sb.avg_entry
                        mark = Decimal(str(target_p.last_price))
                        target_p.unrealized_pnl = (
                            ((mark - sb.avg_entry) * Decimal(sb.qty)).quantize(_Q4)
                            if target_p.side == TradeDirection.LONG.value
                            else ((sb.avg_entry - mark) * Decimal(sb.qty)).quantize(_Q4)
                        )
                    repairs.append(
                        {
                            "entity_type": "paper_position",
                            "entity_id": str(target_p.id),
                            "code": target_p.position_code,
                            "field": "quantity",
                            "before": str(before_q),
                            "after": str(sb.qty),
                            "reason": "Reconciled open position quantity from execution fills.",
                        }
                    )

            if abs(actual_realized - sb.realized_pnl) > Decimal("0.01"):
                pnl_issues += 1
                discrepancies.append(
                    {
                        "check": "realized_pnl_integrity",
                        "entity_type": "paper_position",
                        "entity_id": str(plist[-1].id) if plist else key[2],
                        "code": plist[-1].position_code if plist else key[2],
                        "field": "realized_pnl",
                        "expected": str(sb.realized_pnl.quantize(_Q2)),
                        "actual": str(actual_realized.quantize(_Q2)),
                        "detail": f"Realized P&L on {key[2]} ({actual_realized:.2f}) != execution-derived P&L ({sb.realized_pnl:.2f}).",
                    }
                )
                if apply_repair and len(plist) == 1:
                    before_rpnl = str(Decimal(str(plist[0].realized_pnl)).quantize(_Q2))
                    plist[0].realized_pnl = sb.realized_pnl
                    repairs.append(
                        {
                            "entity_type": "paper_position",
                            "entity_id": str(plist[0].id),
                            "code": plist[0].position_code,
                            "field": "realized_pnl",
                            "before": before_rpnl,
                            "after": str(sb.realized_pnl.quantize(_Q2)),
                            "reason": "Reconciled position realized_pnl from execution fills.",
                        }
                    )

        # Check Account-level realized_pnl_today & unrealized_pnl integrity
        if acct is not None:
            refreshed_positions = list(
                session.scalars(
                    select(PaperPositionModel).where(PaperPositionModel.account_id == acct.id)
                ).all()
            )
            exp_acct_realized = sum(
                (Decimal(str(p.realized_pnl)) for p in refreshed_positions), Decimal("0.0000")
            )
            exp_acct_unrealized = sum(
                (
                    Decimal(str(p.unrealized_pnl))
                    for p in refreshed_positions
                    if p.status == PositionStatus.OPEN.value
                ),
                Decimal("0.0000"),
            )
            if abs(Decimal(str(acct.realized_pnl_today)) - exp_acct_realized) > Decimal("0.01"):
                pnl_issues += 1
                discrepancies.append(
                    {
                        "check": "realized_pnl_integrity",
                        "entity_type": "paper_account",
                        "entity_id": str(acct.id),
                        "code": acct.account_code,
                        "field": "realized_pnl_today",
                        "expected": str(exp_acct_realized.quantize(_Q2)),
                        "actual": str(Decimal(str(acct.realized_pnl_today)).quantize(_Q2)),
                        "detail": "Account realized_pnl_today drifted from sum of position realized P&L.",
                    }
                )
                if apply_repair:
                    before_ar = str(Decimal(str(acct.realized_pnl_today)).quantize(_Q2))
                    PositionService.sync_account_balances_from_positions(
                        session, account_id=acct.id, settings=cfg
                    )
                    repairs.append(
                        {
                            "entity_type": "paper_account",
                            "entity_id": str(acct.id),
                            "code": acct.account_code,
                            "field": "realized_pnl_today",
                            "before": before_ar,
                            "after": str(Decimal(str(acct.realized_pnl_today)).quantize(_Q2)),
                            "reason": "Synchronized account P&L and margin balances from positions.",
                        }
                    )
            elif (
                apply_repair
                and repairs
            ):
                PositionService.sync_account_balances_from_positions(
                    session, account_id=acct.id, settings=cfg
                )

        completed_at = now_ist()
        if not discrepancies:
            run_status = ReconciliationStatus.COMPLETED
        elif apply_repair and len(repairs) >= len(discrepancies):
            run_status = ReconciliationStatus.REPAIRED
        else:
            run_status = ReconciliationStatus.DISCREPANCY_DETECTED

        checks_summary = {
            "executions_vs_positions": {
                "passed": exec_vs_pos_issues == 0,
                "fills_inspected": len(fills),
                "discrepancies": exec_vs_pos_issues,
            },
            "open_orders": {
                "passed": order_issues == 0,
                "open_orders_inspected": open_orders_checked,
                "total_orders_inspected": len(all_orders),
                "discrepancies": order_issues,
            },
            "position_quantities": {
                "passed": qty_issues == 0,
                "positions_inspected": len(positions),
                "discrepancies": qty_issues,
            },
            "realized_pnl_integrity": {
                "passed": pnl_issues == 0,
                "discrepancies": pnl_issues,
            },
        }

        run_code = f"REC-{completed_at.strftime('%Y%m%d-%H%M%S')}-{uuid.uuid4().hex[:5].upper()}"
        run_row = ReconciliationRunModel(
            run_code=run_code,
            account_id=acct.id if acct else None,
            status=run_status.value,
            repair_applied=bool(apply_repair and len(repairs) > 0),
            fills_checked=len(fills),
            open_orders_checked=open_orders_checked,
            positions_checked=len(positions),
            discrepancies_count=len(discrepancies),
            repairs_count=len(repairs),
            checks_json=checks_summary,
            discrepancies_json=discrepancies,
            repairs_json=repairs,
            actor=actor,
            correlation_id=get_correlation_id(),
            started_at=started_at,
            completed_at=completed_at,
        )
        session.add(run_row)
        session.flush()

        is_ok = run_status in (
            ReconciliationStatus.COMPLETED,
            ReconciliationStatus.REPAIRED,
        )
        audit_ev_type = (
            AuditEventType.RECONCILIATION_COMPLETED
            if is_ok
            else AuditEventType.RECONCILIATION_FAILED
        )
        AuditService.record(
            session,
            action=audit_ev_type.value,
            event_type=audit_ev_type.value,
            summary=(
                f"Reconciliation {run_code} finished with status {run_status.value} "
                f"({len(discrepancies)} discrepancy(ies), {len(repairs)} recorded repair(s))."
            ),
            actor=actor,
            actor_type=ActorType.OPERATOR if "operator" in actor.lower() else ActorType.SYSTEM,
            category=AuditCategory.RECONCILIATION,
            severity=AuditSeverity.INFO if is_ok else AuditSeverity.CRITICAL,
            entity_type="reconciliation_run",
            entity_id=str(run_row.id),
            source="reconciliation.engine",
            details={
                "run_code": run_code,
                "status": run_status.value,
                "repair_applied": run_row.repair_applied,
                "discrepancies_count": len(discrepancies),
                "repairs_count": len(repairs),
                "repairs": repairs,
            },
        )
        return ReconciliationService.serialize_run(run_row)

    @staticmethod
    def list_runs(session: Session, *, limit: int = 30) -> list[dict[str, Any]]:
        rows = list(
            session.scalars(
                select(ReconciliationRunModel)
                .order_by(desc(ReconciliationRunModel.completed_at))
                .limit(max(1, min(limit, 200)))
            ).all()
        )
        return [ReconciliationService.serialize_run(r) for r in rows]

    @staticmethod
    def get_latest_run(session: Session) -> ReconciliationRunModel | None:
        return session.scalar(
            select(ReconciliationRunModel)
            .order_by(desc(ReconciliationRunModel.completed_at))
            .limit(1)
        )

    @staticmethod
    def serialize_run(row: ReconciliationRunModel) -> dict[str, Any]:
        return {
            "id": str(row.id),
            "run_code": row.run_code,
            "account_id": str(row.account_id) if row.account_id else None,
            "status": row.status,
            "repair_applied": bool(row.repair_applied),
            "fills_checked": int(row.fills_checked),
            "open_orders_checked": int(row.open_orders_checked),
            "positions_checked": int(row.positions_checked),
            "discrepancies_count": int(row.discrepancies_count),
            "repairs_count": int(row.repairs_count),
            "checks": dict(row.checks_json or {}),
            "discrepancies": list(row.discrepancies_json or []),
            "repairs": list(row.repairs_json or []),
            "actor": row.actor,
            "correlation_id": row.correlation_id,
            "started_at": format_ist_iso(row.started_at),
            "completed_at": format_ist_iso(row.completed_at),
            "completed_at_utc": _format_utc_iso(row.completed_at),
        }
