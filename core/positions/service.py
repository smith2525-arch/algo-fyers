"""Execution-derived Position & P&L Service with idempotent fill processing and reconciliation.

CRITICAL ARCHITECTURAL INVARIANT:
Positions are NEVER directly edited by UI or strategy code. Every position state
change derives strictly from persisted `PaperFillModel` executions via `PositionService`.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal
from typing import Any

from sqlalchemy import asc, desc, select
from sqlalchemy.orm import Session

from core.audit.service import AuditService
from core.clock import ensure_ist, format_ist_iso, now_ist
from core.config import AppSettings, get_settings
from core.domain.enums import (
    ActorType,
    AuditCategory,
    AuditSeverity,
    PositionStatus,
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
)
from core.strategies.base import OpenPositionSnapshot

_Q4 = Decimal("0.0001")
_Q2 = Decimal("0.01")


def _format_holding_duration(seconds: int) -> str:
    secs = max(0, int(seconds))
    hours, rem = divmod(secs, 3600)
    mins, s = divmod(rem, 60)
    if hours > 0:
        return f"{hours}h {mins:02d}m"
    if mins > 0:
        return f"{mins}m {s:02d}s"
    return f"{s}s"


def _compute_unrealized_pnl(side: str, avg_entry: Decimal, mark_price: Decimal, qty: int) -> Decimal:
    if qty <= 0:
        return Decimal("0.0000")
    if side == TradeDirection.LONG.value:
        return ((mark_price - avg_entry) * Decimal(qty)).quantize(_Q4)
    return ((avg_entry - mark_price) * Decimal(qty)).quantize(_Q4)


@dataclass(frozen=True)
class PositionReconciliationReport:
    """Verification report comparing execution-ledger-derived state vs persisted positions."""

    reconciled: bool
    fills_inspected: int
    open_positions_count: int
    closed_positions_count: int
    discrepancies_found: int
    healed_discrepancies: int
    total_realized_pnl: Decimal
    total_unrealized_pnl: Decimal
    details: list[str]

    def to_dict(self) -> dict[str, Any]:
        return {
            "reconciled": self.reconciled,
            "fills_inspected": self.fills_inspected,
            "open_positions_count": self.open_positions_count,
            "closed_positions_count": self.closed_positions_count,
            "discrepancies_found": self.discrepancies_found,
            "healed_discrepancies": self.healed_discrepancies,
            "total_realized_pnl": str(self.total_realized_pnl.quantize(_Q2)),
            "total_unrealized_pnl": str(self.total_unrealized_pnl.quantize(_Q2)),
            "details": self.details,
        }


class PositionService:
    """Derives, marks-to-market, and reconciles `PaperPositionModel` rows from `PaperFillModel`s."""

    @staticmethod
    def _find_open_position(
        session: Session,
        *,
        account_id: uuid.UUID,
        strategy_instance_id: uuid.UUID | None,
        instrument_symbol: str,
    ) -> PaperPositionModel | None:
        stmt = select(PaperPositionModel).where(
            PaperPositionModel.account_id == account_id,
            PaperPositionModel.instrument_symbol == instrument_symbol.strip().upper(),
            PaperPositionModel.status == PositionStatus.OPEN.value,
        )
        if strategy_instance_id is None:
            stmt = stmt.where(PaperPositionModel.strategy_instance_id.is_(None))
        else:
            stmt = stmt.where(PaperPositionModel.strategy_instance_id == strategy_instance_id)
        return session.scalar(stmt.order_by(desc(PaperPositionModel.opened_at)).limit(1))

    @staticmethod
    def apply_fill_to_position(
        session: Session,
        fill: PaperFillModel,
        *,
        order: PaperOrderModel | None = None,
        event_idempotency_key: str | None = None,
        settings: AppSettings | None = None,
    ) -> tuple[PaperPositionModel, bool]:
        """Idempotently apply a persisted `PaperFillModel` to the position book.

        Returns `(position, was_duplicate)`. If this `fill.id` or `event_idempotency_key`
        was already applied, returns the existing position without mutating quantities or P&L.
        """
        cfg = settings or get_settings()
        idem_key = (event_idempotency_key or f"posevt:{fill.id}").strip()

        existing_evt = session.scalar(
            select(PositionEventModel).where(
                (PositionEventModel.fill_id == fill.id)
                | (PositionEventModel.event_idempotency_key == idem_key)
            )
        )
        if existing_evt is not None:
            pos = session.get(PaperPositionModel, existing_evt.position_id)
            assert pos is not None
            return pos, True

        parent_order = order or session.get(PaperOrderModel, fill.order_id)
        fill_side = Side(fill.side)
        desired_dir = (
            TradeDirection.LONG.value if fill_side == Side.BUY else TradeDirection.SHORT.value
        )
        fill_qty = int(fill.fill_quantity)
        fill_px = Decimal(str(fill.fill_price)).quantize(_Q4)
        fill_charges = Decimal(str(fill.charges)).quantize(_Q4)
        bar_ts = ensure_ist(fill.bar_timestamp)
        ts_now = now_ist()

        strat_label = (
            fill.strategy_version_label
            or (parent_order.strategy_version_label if parent_order else None)
            or "MANUAL_PAPER_DESK"
        )
        tf = parent_order.timeframe if parent_order else "5m"
        stop_px = parent_order.target_stop_loss_price if parent_order else None
        target_px = parent_order.target_profit_price if parent_order else None
        exit_rsn = parent_order.exit_reason if parent_order else None
        reduce_only = bool(parent_order.reduce_only) if parent_order else False

        open_pos = PositionService._find_open_position(
            session,
            account_id=fill.account_id,
            strategy_instance_id=fill.strategy_instance_id,
            instrument_symbol=fill.instrument_symbol,
        )

        # Case 1: Flat book -> Open new position
        if open_pos is None:
            pos_code = (
                f"POS-{fill.instrument_symbol}-{bar_ts.strftime('%H%M%S')}-"
                f"{uuid.uuid4().hex[:5].upper()}"
            )
            new_pos = PaperPositionModel(
                position_code=pos_code,
                account_id=fill.account_id,
                strategy_instance_id=fill.strategy_instance_id,
                strategy_version_id=fill.strategy_version_id,
                strategy_version_label=strat_label,
                instrument_symbol=fill.instrument_symbol,
                timeframe=tf,
                side=desired_dir,
                quantity=fill_qty,
                opened_quantity=fill_qty,
                closed_quantity=0,
                average_entry_price=fill_px,
                average_exit_price=None,
                last_price=fill_px,
                highest_price_since_entry=fill_px,
                lowest_price_since_entry=fill_px,
                bars_held=0,
                realized_pnl=Decimal("0.0000"),
                unrealized_pnl=Decimal("0.0000"),
                unallocated_entry_charges=fill_charges,
                total_charges=fill_charges,
                stop_loss_price=stop_px,
                target_price=target_px,
                status=PositionStatus.OPEN.value,
                opened_at=bar_ts,
                last_mark_at=bar_ts,
                closed_at=None,
                exit_reason=None,
                updated_at=ts_now,
            )
            session.add(new_pos)
            session.flush()

            evt = PositionEventModel(
                event_idempotency_key=idem_key,
                fill_id=fill.id,
                position_id=new_pos.id,
                event_type="OPENED",
                delta_quantity=fill_qty,
                resulting_quantity=fill_qty,
                fill_price=fill_px,
                realized_pnl_delta=Decimal("0.0000"),
                charges_delta=fill_charges,
                occurred_at=ts_now,
                correlation_id=get_correlation_id(),
            )
            session.add(evt)
            session.flush()
            PositionService.sync_account_balances_from_positions(
                session, account_id=fill.account_id, settings=cfg
            )
            AuditService.record(
                session,
                action="PAPER_POSITION_OPENED",
                summary=(
                    f"Opened {new_pos.side} paper position {new_pos.position_code} on "
                    f"{new_pos.instrument_symbol} ({fill_qty} @ {fill_px:.2f})."
                ),
                actor="position.engine",
                actor_type=ActorType.SYSTEM,
                category=AuditCategory.POSITION,
                severity=AuditSeverity.INFO,
                entity_type="paper_position",
                entity_id=str(new_pos.id),
                details={
                    "position_code": new_pos.position_code,
                    "fill_code": fill.fill_code,
                    "side": new_pos.side,
                    "quantity": fill_qty,
                    "average_entry_price": str(fill_px),
                },
            )
            return new_pos, False

        # Case 2: Same direction -> Scale in weighted average entry
        if open_pos.side == desired_dir:
            old_qty = int(open_pos.quantity)
            new_qty = old_qty + fill_qty
            old_avg = Decimal(str(open_pos.average_entry_price))
            new_avg = (
                (old_avg * Decimal(old_qty) + fill_px * Decimal(fill_qty)) / Decimal(new_qty)
            ).quantize(_Q4)

            open_pos.quantity = new_qty
            open_pos.opened_quantity = int(open_pos.opened_quantity) + fill_qty
            open_pos.average_entry_price = new_avg
            open_pos.last_price = fill_px
            open_pos.highest_price_since_entry = max(
                Decimal(str(open_pos.highest_price_since_entry)), fill_px
            )
            open_pos.lowest_price_since_entry = min(
                Decimal(str(open_pos.lowest_price_since_entry)), fill_px
            )
            open_pos.unallocated_entry_charges = (
                Decimal(str(open_pos.unallocated_entry_charges)) + fill_charges
            ).quantize(_Q4)
            open_pos.total_charges = (
                Decimal(str(open_pos.total_charges)) + fill_charges
            ).quantize(_Q4)
            if stop_px is not None:
                open_pos.stop_loss_price = stop_px
            if target_px is not None:
                open_pos.target_price = target_px
            open_pos.unrealized_pnl = _compute_unrealized_pnl(
                open_pos.side, new_avg, fill_px, new_qty
            )
            open_pos.last_mark_at = bar_ts
            open_pos.updated_at = ts_now

            evt = PositionEventModel(
                event_idempotency_key=idem_key,
                fill_id=fill.id,
                position_id=open_pos.id,
                event_type="SCALED_IN",
                delta_quantity=fill_qty,
                resulting_quantity=new_qty,
                fill_price=fill_px,
                realized_pnl_delta=Decimal("0.0000"),
                charges_delta=fill_charges,
                occurred_at=ts_now,
                correlation_id=get_correlation_id(),
            )
            session.add(evt)
            session.flush()
            PositionService.sync_account_balances_from_positions(
                session, account_id=fill.account_id, settings=cfg
            )
            return open_pos, False

        # Case 3: Opposite direction -> Close (partial or full) and optionally reverse
        prev_open_qty = int(open_pos.quantity)
        close_qty = min(prev_open_qty, fill_qty)
        remainder_qty = fill_qty - close_qty
        avg_entry = Decimal(str(open_pos.average_entry_price))

        if open_pos.side == TradeDirection.LONG.value:
            gross_pnl_delta = ((fill_px - avg_entry) * Decimal(close_qty)).quantize(_Q4)
        else:
            gross_pnl_delta = ((avg_entry - fill_px) * Decimal(close_qty)).quantize(_Q4)

        unalloc_entry_chg = Decimal(str(open_pos.unallocated_entry_charges))
        if close_qty == prev_open_qty:
            alloc_entry_chg = unalloc_entry_chg
        else:
            alloc_entry_chg = (
                unalloc_entry_chg * Decimal(close_qty) / Decimal(prev_open_qty)
            ).quantize(_Q4)

        alloc_exit_chg = (
            fill_charges
            if remainder_qty == 0
            else (fill_charges * Decimal(close_qty) / Decimal(fill_qty)).quantize(_Q4)
        )
        net_realized_delta = (gross_pnl_delta - alloc_entry_chg - alloc_exit_chg).quantize(_Q4)

        prev_closed_qty = int(open_pos.closed_quantity)
        new_closed_qty = prev_closed_qty + close_qty
        prev_avg_exit = (
            Decimal(str(open_pos.average_exit_price))
            if open_pos.average_exit_price is not None
            else Decimal("0")
        )
        new_avg_exit = (
            (prev_avg_exit * Decimal(prev_closed_qty) + fill_px * Decimal(close_qty))
            / Decimal(new_closed_qty)
        ).quantize(_Q4)

        new_open_qty = prev_open_qty - close_qty
        open_pos.quantity = new_open_qty
        open_pos.closed_quantity = new_closed_qty
        open_pos.average_exit_price = new_avg_exit
        open_pos.last_price = fill_px
        open_pos.unallocated_entry_charges = max(
            Decimal("0.0000"), (unalloc_entry_chg - alloc_entry_chg).quantize(_Q4)
        )
        open_pos.total_charges = (
            Decimal(str(open_pos.total_charges)) + alloc_exit_chg
        ).quantize(_Q4)
        open_pos.realized_pnl = (
            Decimal(str(open_pos.realized_pnl)) + net_realized_delta
        ).quantize(_Q4)
        open_pos.last_mark_at = bar_ts
        open_pos.updated_at = ts_now

        if new_open_qty == 0:
            open_pos.status = PositionStatus.CLOSED.value
            open_pos.unrealized_pnl = Decimal("0.0000")
            open_pos.closed_at = bar_ts
            open_pos.exit_reason = exit_rsn or "EXECUTION_CLOSE"
            evt_type = "CLOSED"
        else:
            open_pos.unrealized_pnl = _compute_unrealized_pnl(
                open_pos.side, avg_entry, fill_px, new_open_qty
            )
            evt_type = "PARTIALLY_CLOSED"

        target_pos_for_return = open_pos

        # If fill exceeded open_pos and is NOT reduce_only, open reversed position for remainder
        if remainder_qty > 0 and not reduce_only:
            rem_chg = max(Decimal("0.0000"), (fill_charges - alloc_exit_chg).quantize(_Q4))
            rev_code = (
                f"POS-{fill.instrument_symbol}-{bar_ts.strftime('%H%M%S')}-"
                f"{uuid.uuid4().hex[:5].upper()}"
            )
            rev_pos = PaperPositionModel(
                position_code=rev_code,
                account_id=fill.account_id,
                strategy_instance_id=fill.strategy_instance_id,
                strategy_version_id=fill.strategy_version_id,
                strategy_version_label=strat_label,
                instrument_symbol=fill.instrument_symbol,
                timeframe=tf,
                side=desired_dir,
                quantity=remainder_qty,
                opened_quantity=remainder_qty,
                closed_quantity=0,
                average_entry_price=fill_px,
                average_exit_price=None,
                last_price=fill_px,
                highest_price_since_entry=fill_px,
                lowest_price_since_entry=fill_px,
                bars_held=0,
                realized_pnl=Decimal("0.0000"),
                unrealized_pnl=Decimal("0.0000"),
                unallocated_entry_charges=rem_chg,
                total_charges=rem_chg,
                stop_loss_price=stop_px,
                target_price=target_px,
                status=PositionStatus.OPEN.value,
                opened_at=bar_ts,
                last_mark_at=bar_ts,
                closed_at=None,
                exit_reason=None,
                updated_at=ts_now,
            )
            session.add(rev_pos)
            session.flush()
            target_pos_for_return = rev_pos
            evt_type = "REVERSED_OPEN"

        evt = PositionEventModel(
            event_idempotency_key=idem_key,
            fill_id=fill.id,
            position_id=open_pos.id,
            event_type=evt_type,
            delta_quantity=-close_qty,
            resulting_quantity=new_open_qty,
            fill_price=fill_px,
            realized_pnl_delta=net_realized_delta,
            charges_delta=alloc_exit_chg,
            occurred_at=ts_now,
            correlation_id=get_correlation_id(),
        )
        session.add(evt)
        session.flush()

        PositionService.sync_account_balances_from_positions(
            session, account_id=fill.account_id, settings=cfg
        )

        AuditService.record(
            session,
            action=f"PAPER_POSITION_{evt_type}",
            summary=(
                f"Position {open_pos.position_code} {evt_type.lower()} ({close_qty} @ {fill_px:.2f}, "
                f"realized Δ ₹{net_realized_delta:,.2f})."
            ),
            actor="position.engine",
            actor_type=ActorType.SYSTEM,
            category=AuditCategory.POSITION,
            severity=AuditSeverity.INFO,
            entity_type="paper_position",
            entity_id=str(open_pos.id),
            details={
                "position_code": open_pos.position_code,
                "fill_code": fill.fill_code,
                "closed_quantity": close_qty,
                "remaining_quantity": new_open_qty,
                "realized_pnl_delta": str(net_realized_delta),
                "total_realized_pnl": str(open_pos.realized_pnl),
            },
        )
        if exit_rsn in ("STOP_LOSS", "TRAILING_STOP"):
            AuditService.record(
                session,
                action="STOP_TRIGGERED",
                event_type="STOP_TRIGGERED",
                summary=f"Position {open_pos.position_code} exited via {exit_rsn} @ {fill_px:.2f}.",
                actor="position.engine",
                actor_type=ActorType.SYSTEM,
                category=AuditCategory.POSITION,
                severity=AuditSeverity.WARNING,
                entity_type="paper_position",
                entity_id=str(open_pos.id),
                order_id=str(fill.order_id),
                source="position.engine",
                details={"exit_reason": exit_rsn, "fill_price": str(fill_px)},
            )
        elif exit_rsn == "PROFIT_TARGET":
            AuditService.record(
                session,
                action="TARGET_TRIGGERED",
                event_type="TARGET_TRIGGERED",
                summary=f"Position {open_pos.position_code} exited via PROFIT_TARGET @ {fill_px:.2f}.",
                actor="position.engine",
                actor_type=ActorType.SYSTEM,
                category=AuditCategory.POSITION,
                severity=AuditSeverity.INFO,
                entity_type="paper_position",
                entity_id=str(open_pos.id),
                order_id=str(fill.order_id),
                source="position.engine",
                details={"exit_reason": exit_rsn, "fill_price": str(fill_px)},
            )
        return target_pos_for_return, False

    @staticmethod
    def mark_positions_to_market(
        session: Session,
        *,
        symbol: str,
        mark_price: Decimal,
        bar_high: Decimal | None = None,
        bar_low: Decimal | None = None,
        bar_timestamp: datetime | None = None,
        increment_bars_held: bool = True,
        settings: AppSettings | None = None,
    ) -> int:
        """Update last_price, peak/trough, bars_held, and unrealized P&L on OPEN positions."""
        cfg = settings or get_settings()
        clean_sym = symbol.strip().upper()
        mark_px = Decimal(str(mark_price)).quantize(_Q4)
        hi_px = Decimal(str(bar_high)).quantize(_Q4) if bar_high is not None else mark_px
        lo_px = Decimal(str(bar_low)).quantize(_Q4) if bar_low is not None else mark_px
        mark_ts = ensure_ist(bar_timestamp) if bar_timestamp is not None else now_ist()
        ts_now = now_ist()

        open_positions = list(
            session.scalars(
                select(PaperPositionModel).where(
                    PaperPositionModel.instrument_symbol == clean_sym,
                    PaperPositionModel.status == PositionStatus.OPEN.value,
                )
            ).all()
        )
        touched_accounts: set[uuid.UUID] = set()
        for pos in open_positions:
            pos.last_price = mark_px
            pos.highest_price_since_entry = max(
                Decimal(str(pos.highest_price_since_entry)), hi_px, mark_px
            )
            pos.lowest_price_since_entry = min(
                Decimal(str(pos.lowest_price_since_entry)), lo_px, mark_px
            )
            if increment_bars_held and mark_ts > ensure_ist(pos.opened_at):
                pos.bars_held = int(pos.bars_held) + 1
            pos.unrealized_pnl = _compute_unrealized_pnl(
                pos.side, Decimal(str(pos.average_entry_price)), mark_px, int(pos.quantity)
            )
            pos.last_mark_at = mark_ts
            pos.updated_at = ts_now
            touched_accounts.add(pos.account_id)

        if open_positions:
            session.flush()
            for acct_id in touched_accounts:
                PositionService.sync_account_balances_from_positions(
                    session, account_id=acct_id, settings=cfg
                )
        return len(open_positions)

    @staticmethod
    def sync_account_balances_from_positions(
        session: Session,
        *,
        account_id: uuid.UUID,
        settings: AppSettings | None = None,
    ) -> None:
        """Synchronize PaperAccountModel realized_pnl, unrealized_pnl, and available_capital."""
        cfg = settings or get_settings()
        acct = session.get(PaperAccountModel, account_id)
        if acct is None:
            return

        all_positions = list(
            session.scalars(
                select(PaperPositionModel).where(PaperPositionModel.account_id == account_id)
            ).all()
        )
        total_realized = sum(
            (Decimal(str(p.realized_pnl)) for p in all_positions), Decimal("0.0000")
        )
        total_unrealized = sum(
            (
                Decimal(str(p.unrealized_pnl))
                for p in all_positions
                if p.status == PositionStatus.OPEN.value
            ),
            Decimal("0.0000"),
        )
        deployed_margin = sum(
            (
                Decimal(str(p.average_entry_price))
                * Decimal(int(p.quantity))
                * cfg.PAPER_FUTURES_MARGIN_RATE
                for p in all_positions
                if p.status == PositionStatus.OPEN.value
            ),
            Decimal("0.0000"),
        )
        init_cap = Decimal(str(acct.initial_capital))
        avail = max(Decimal("0.0000"), (init_cap + total_realized - deployed_margin).quantize(_Q4))

        acct.realized_pnl_today = total_realized.quantize(_Q4)
        acct.unrealized_pnl = total_unrealized.quantize(_Q4)
        acct.available_capital = avail
        acct.updated_at = now_ist()
        session.flush()

    @staticmethod
    def get_open_position_snapshot_for_strategy(
        session: Session,
        *,
        account_id: uuid.UUID,
        strategy_instance_id: uuid.UUID,
        symbol: str,
    ) -> OpenPositionSnapshot | None:
        """Build a read-only OpenPositionSnapshot from the execution-derived position book."""
        pos = PositionService._find_open_position(
            session,
            account_id=account_id,
            strategy_instance_id=strategy_instance_id,
            instrument_symbol=symbol,
        )
        if pos is None or pos.quantity <= 0:
            return None
        entry_side = Side.BUY if pos.side == TradeDirection.LONG.value else Side.SELL
        return OpenPositionSnapshot(
            symbol=pos.instrument_symbol,
            side=entry_side,
            entry_price=Decimal(str(pos.average_entry_price)),
            entry_bar_timestamp=ensure_ist(pos.opened_at),
            bars_held=int(pos.bars_held),
            highest_price_since_entry=Decimal(str(pos.highest_price_since_entry)),
            lowest_price_since_entry=Decimal(str(pos.lowest_price_since_entry)),
        )

    @staticmethod
    def reconcile_positions_from_fills(
        session: Session,
        *,
        account_id: uuid.UUID | None = None,
        heal_if_needed: bool = True,
        settings: AppSettings | None = None,
    ) -> PositionReconciliationReport:
        """Verify (and optionally heal) positions strictly from immutable `PaperFillModel` rows."""
        cfg = settings or get_settings()
        fill_stmt = select(PaperFillModel).order_by(
            asc(PaperFillModel.executed_at), asc(PaperFillModel.id)
        )
        if account_id is not None:
            fill_stmt = fill_stmt.where(PaperFillModel.account_id == account_id)
        fills = list(session.scalars(fill_stmt).all())

        # Ensure every persisted fill has been applied via PositionEventModel
        missing_applied = 0
        for f in fills:
            has_evt = session.scalar(
                select(PositionEventModel.id).where(PositionEventModel.fill_id == f.id)
            )
            if has_evt is None and heal_if_needed:
                PositionService.apply_fill_to_position(session, f, settings=cfg)
                missing_applied += 1

        # Simulate expected book state per (account_id, strategy_instance_id, symbol)
        @dataclass
        class _SimBook:
            side: str | None = None
            qty: int = 0
            avg_entry: Decimal = Decimal("0")
            unalloc_chg: Decimal = Decimal("0")
            realized_pnl: Decimal = Decimal("0")

        sim_books: dict[tuple[uuid.UUID, uuid.UUID | None, str], _SimBook] = {}
        for f in fills:
            key = (f.account_id, f.strategy_instance_id, f.instrument_symbol)
            b = sim_books.setdefault(key, _SimBook())
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
                    fc
                    if rem == 0
                    else (fc * Decimal(cq) / Decimal(fq)).quantize(_Q4)
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

        pos_stmt = select(PaperPositionModel)
        if account_id is not None:
            pos_stmt = pos_stmt.where(PaperPositionModel.account_id == account_id)
        positions = list(session.scalars(pos_stmt).all())

        by_key: dict[tuple[uuid.UUID, uuid.UUID | None, str], list[PaperPositionModel]] = {}
        for p in positions:
            by_key.setdefault(
                (p.account_id, p.strategy_instance_id, p.instrument_symbol), []
            ).append(p)

        discrepancies = missing_applied
        healed = missing_applied
        details: list[str] = []
        if missing_applied:
            details.append(f"Applied {missing_applied} unindexed execution fill(s) to positions.")

        all_keys = set(sim_books.keys()) | set(by_key.keys())
        for key in all_keys:
            sb = sim_books.get(key, _SimBook())
            plist = by_key.get(key, [])
            open_rows = [p for p in plist if p.status == PositionStatus.OPEN.value]
            actual_open_qty = sum(int(p.quantity) for p in open_rows)
            actual_realized = sum(
                (Decimal(str(p.realized_pnl)) for p in plist), Decimal("0.0000")
            )

            if actual_open_qty != sb.qty:
                discrepancies += 1
                details.append(
                    f"Open qty mismatch on {key[2]}: expected {sb.qty}, found {actual_open_qty}."
                )
                if heal_if_needed and len(open_rows) == 1:
                    open_rows[0].quantity = sb.qty
                    if sb.qty == 0:
                        open_rows[0].status = PositionStatus.CLOSED.value
                        open_rows[0].unrealized_pnl = Decimal("0.0000")
                        open_rows[0].closed_at = now_ist()
                    else:
                        open_rows[0].average_entry_price = sb.avg_entry
                        open_rows[0].unrealized_pnl = _compute_unrealized_pnl(
                            open_rows[0].side,
                            sb.avg_entry,
                            Decimal(str(open_rows[0].last_price)),
                            sb.qty,
                        )
                    healed += 1

            if abs(actual_realized - sb.realized_pnl) > Decimal("0.01"):
                discrepancies += 1
                details.append(
                    f"Realized P&L drift on {key[2]}: expected {sb.realized_pnl:.2f}, found {actual_realized:.2f}."
                )

        # Refresh account balances
        acct_ids = {p.account_id for p in positions} | {f.account_id for f in fills}
        for aid in acct_ids:
            PositionService.sync_account_balances_from_positions(
                session, account_id=aid, settings=cfg
            )

        open_cnt = sum(1 for p in positions if p.status == PositionStatus.OPEN.value)
        closed_cnt = sum(1 for p in positions if p.status == PositionStatus.CLOSED.value)
        tot_realized = sum(
            (Decimal(str(p.realized_pnl)) for p in positions), Decimal("0.0000")
        )
        tot_unrealized = sum(
            (
                Decimal(str(p.unrealized_pnl))
                for p in positions
                if p.status == PositionStatus.OPEN.value
            ),
            Decimal("0.0000"),
        )
        is_clean = discrepancies == healed
        if not details:
            details.append("All open/closed positions and P&L match execution fills 100%.")

        return PositionReconciliationReport(
            reconciled=is_clean,
            fills_inspected=len(fills),
            open_positions_count=open_cnt,
            closed_positions_count=closed_cnt,
            discrepancies_found=discrepancies,
            healed_discrepancies=healed,
            total_realized_pnl=tot_realized,
            total_unrealized_pnl=tot_unrealized,
            details=details,
        )

    @staticmethod
    def serialize_position(
        pos: PaperPositionModel,
        *,
        reference_time: datetime | None = None,
    ) -> dict[str, Any]:
        opened_ist = ensure_ist(pos.opened_at)
        if pos.status == PositionStatus.CLOSED.value and pos.closed_at is not None:
            end_ist = ensure_ist(pos.closed_at)
        elif reference_time is not None:
            end_ist = ensure_ist(reference_time)
        else:
            end_ist = ensure_ist(pos.last_mark_at)

        dur_sec = max(0, int((end_ist - opened_ist).total_seconds()))
        return {
            "id": str(pos.id),
            "position_code": pos.position_code,
            "account_id": str(pos.account_id),
            "strategy_instance_id": str(pos.strategy_instance_id)
            if pos.strategy_instance_id
            else None,
            "strategy_version_id": str(pos.strategy_version_id)
            if pos.strategy_version_id
            else None,
            "strategy_version_label": pos.strategy_version_label,
            "instrument_symbol": pos.instrument_symbol,
            "timeframe": pos.timeframe,
            "side": pos.side,
            "quantity": int(pos.quantity),
            "opened_quantity": int(pos.opened_quantity),
            "closed_quantity": int(pos.closed_quantity),
            "average_entry_price": str(Decimal(str(pos.average_entry_price)).quantize(_Q2)),
            "average_exit_price": (
                str(Decimal(str(pos.average_exit_price)).quantize(_Q2))
                if pos.average_exit_price is not None
                else None
            ),
            "last_price": str(Decimal(str(pos.last_price)).quantize(_Q2)),
            "realized_pnl": str(Decimal(str(pos.realized_pnl)).quantize(_Q2)),
            "unrealized_pnl": str(Decimal(str(pos.unrealized_pnl)).quantize(_Q2)),
            "total_charges": str(Decimal(str(pos.total_charges)).quantize(_Q2)),
            "stop_loss_price": (
                str(Decimal(str(pos.stop_loss_price)).quantize(_Q2))
                if pos.stop_loss_price is not None
                else None
            ),
            "target_price": (
                str(Decimal(str(pos.target_price)).quantize(_Q2))
                if pos.target_price is not None
                else None
            ),
            "bars_held": int(pos.bars_held),
            "holding_duration_seconds": dur_sec,
            "holding_duration_display": _format_holding_duration(dur_sec),
            "status": pos.status,
            "exit_reason": pos.exit_reason,
            "opened_at": format_ist_iso(opened_ist),
            "last_mark_at": format_ist_iso(pos.last_mark_at),
            "closed_at": format_ist_iso(pos.closed_at) if pos.closed_at else None,
            "updated_at": format_ist_iso(pos.updated_at),
        }
