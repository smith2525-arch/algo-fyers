"""Paper-Trading P&L Analytics Service (`core/analytics/pnl_service.py`).

Computes truth-based simulated paper-trading P&L attribution, filters, and chart series:
- Gross P&L, Charges, Slippage Cost, Net P&L, Realized P&L, Unrealized P&L
- Filtering: Date, Strategy, Instrument, Direction, Exit Reason, Winning/Losing trades
- Charts: Daily P&L, Cumulative P&L, Strategy Contribution, Instrument Contribution, Drawdown
- Explicitly labeled: `SIMULATED PAPER-TRADING RESULTS`
"""

from __future__ import annotations

from collections import defaultdict
from datetime import date
from decimal import Decimal
from typing import Any

from sqlalchemy import asc, select
from sqlalchemy.orm import Session

from core.clock import ensure_ist, format_ist_iso
from core.config import AppSettings, get_settings
from core.domain.enums import PositionStatus
from core.persistence.models import (
    PaperAccountModel,
    PaperFillModel,
    PaperPositionModel,
    PositionEventModel,
)

_Q2 = Decimal("0.01")
_Q4 = Decimal("0.0001")


class PnlAnalyticsService:
    """Computes filtered P&L metrics, trade ledger, and 5 analytical chart series."""

    @staticmethod
    def compute_pnl_analytics(
        session: Session,
        *,
        start_date: date | None = None,
        end_date: date | None = None,
        strategy: str = "ALL",
        instrument: str = "ALL",
        direction: str = "ALL",
        exit_reason: str = "ALL",
        outcome: str = "ALL",
        settings: AppSettings | None = None,
    ) -> dict[str, Any]:
        cfg = settings or get_settings()
        acct = session.scalar(
            select(PaperAccountModel).where(
                PaperAccountModel.account_code == cfg.DEFAULT_PAPER_ACCOUNT_CODE
            )
        )
        if acct is None:
            acct = session.scalar(
                select(PaperAccountModel).order_by(PaperAccountModel.created_at.asc()).limit(1)
            )

        positions = list(
            session.scalars(
                select(PaperPositionModel).order_by(
                    asc(PaperPositionModel.opened_at), asc(PaperPositionModel.id)
                )
            ).all()
        )
        pos_events = list(session.scalars(select(PositionEventModel)).all())
        fills = {
            f.id: f
            for f in session.scalars(select(PaperFillModel)).all()
        }

        slippage_by_pos: dict[Any, Decimal] = defaultdict(lambda: Decimal("0.0000"))
        corr_by_pos: dict[Any, str] = {}
        for pe in pos_events:
            corr_by_pos.setdefault(pe.position_id, pe.correlation_id)
            f_row = fills.get(pe.fill_id)
            if f_row is not None:
                slippage_by_pos[pe.position_id] += Decimal(str(f_row.slippage_cost))

        # Collect distinct filter option values across all persisted positions
        all_strategies = sorted(
            {p.strategy_version_label for p in positions if p.strategy_version_label}
        )
        all_instruments = sorted(
            {p.instrument_symbol for p in positions if p.instrument_symbol}
            | {"NIFTY", "BANKNIFTY"}
        )
        all_exit_reasons = sorted(
            {p.exit_reason for p in positions if p.exit_reason}
        )

        clean_strat = (strategy or "ALL").strip()
        clean_inst = (instrument or "ALL").strip().upper()
        clean_dir = (direction or "ALL").strip().upper()
        clean_exit = (exit_reason or "ALL").strip().upper()
        clean_outcome = (outcome or "ALL").strip().upper()

        filtered_rows: list[dict[str, Any]] = []
        tot_gross = Decimal("0.0000")
        tot_charges = Decimal("0.0000")
        tot_slippage = Decimal("0.0000")
        tot_realized = Decimal("0.0000")
        tot_unrealized = Decimal("0.0000")

        daily_bucket: dict[str, Decimal] = defaultdict(lambda: Decimal("0.0000"))
        strat_bucket: dict[str, Decimal] = defaultdict(lambda: Decimal("0.0000"))
        inst_bucket: dict[str, Decimal] = defaultdict(lambda: Decimal("0.0000"))

        timeline_events: list[tuple[Any, str, Decimal]] = []
        winners_cnt = 0
        losers_cnt = 0

        for pos in positions:
            ref_dt = ensure_ist(pos.closed_at or pos.last_mark_at or pos.opened_at)
            ref_date = ref_dt.date()

            if start_date is not None and ref_date < start_date:
                continue
            if end_date is not None and ref_date > end_date:
                continue
            if clean_strat.upper() != "ALL" and pos.strategy_version_label != clean_strat:
                continue
            if clean_inst != "ALL" and pos.instrument_symbol.upper() != clean_inst:
                continue
            if clean_dir != "ALL" and pos.side.upper() != clean_dir:
                continue
            pos_exit_str = (pos.exit_reason or ("OPEN" if pos.status == "OPEN" else "EXECUTION_CLOSE")).upper()
            if clean_exit != "ALL" and pos_exit_str != clean_exit:
                continue

            real_pnl = Decimal(str(pos.realized_pnl)).quantize(_Q4)
            unreal_pnl = (
                Decimal(str(pos.unrealized_pnl)).quantize(_Q4)
                if pos.status == PositionStatus.OPEN.value
                else Decimal("0.0000")
            )
            charges = Decimal(str(pos.total_charges)).quantize(_Q4)
            slip = slippage_by_pos[pos.id].quantize(_Q4)
            net_pnl = (real_pnl + unreal_pnl).quantize(_Q4)
            gross_pnl = (net_pnl + charges).quantize(_Q4)

            if clean_outcome in ("WINNERS", "WINNING") and net_pnl <= Decimal("0"):
                continue
            if clean_outcome in ("LOSERS", "LOSING") and net_pnl >= Decimal("0"):
                continue

            if net_pnl > Decimal("0"):
                winners_cnt += 1
            elif net_pnl < Decimal("0"):
                losers_cnt += 1

            tot_gross += gross_pnl
            tot_charges += charges
            tot_slippage += slip
            tot_realized += real_pnl
            tot_unrealized += unreal_pnl

            day_key = ref_date.isoformat()
            daily_bucket[day_key] += net_pnl
            strat_bucket[pos.strategy_version_label] += net_pnl
            inst_bucket[pos.instrument_symbol] += net_pnl
            timeline_events.append((ref_dt, pos.position_code, net_pnl))

            filtered_rows.append(
                {
                    "position_id": str(pos.id),
                    "position_code": pos.position_code,
                    "correlation_id": corr_by_pos.get(pos.id),
                    "strategy": pos.strategy_version_label,
                    "instrument": pos.instrument_symbol,
                    "direction": pos.side,
                    "status": pos.status,
                    "quantity": int(pos.opened_quantity),
                    "open_quantity": int(pos.quantity),
                    "average_entry_price": str(Decimal(str(pos.average_entry_price)).quantize(_Q2)),
                    "exit_or_mark_price": str(
                        Decimal(
                            str(
                                pos.average_exit_price
                                if (pos.status == "CLOSED" and pos.average_exit_price is not None)
                                else pos.last_price
                            )
                        ).quantize(_Q2)
                    ),
                    "exit_reason": pos_exit_str,
                    "gross_pnl": str(gross_pnl.quantize(_Q2)),
                    "charges": str(charges.quantize(_Q2)),
                    "slippage_cost": str(slip.quantize(_Q2)),
                    "realized_pnl": str(real_pnl.quantize(_Q2)),
                    "unrealized_pnl": str(unreal_pnl.quantize(_Q2)),
                    "net_pnl": str(net_pnl.quantize(_Q2)),
                    "opened_at": format_ist_iso(pos.opened_at),
                    "timestamp": format_ist_iso(ref_dt),
                    "date": day_key,
                }
            )

        # Build the 5 analytical chart series deterministically
        daily_pnl_series = [
            {"date": d, "net_pnl": str(daily_bucket[d].quantize(_Q2))}
            for d in sorted(daily_bucket.keys())
        ]

        timeline_events.sort(key=lambda t: t[0])
        cumulative_series: list[dict[str, Any]] = []
        drawdown_series: list[dict[str, Any]] = []
        running_cum = Decimal("0.0000")
        peak_cum = Decimal("0.0000")
        max_dd = Decimal("0.0000")

        for ts_dt, p_code, delta_pnl in timeline_events:
            running_cum += delta_pnl
            if running_cum > peak_cum:
                peak_cum = running_cum
            dd = running_cum - peak_cum  # <= 0 underwater curve
            if abs(dd) > max_dd:
                max_dd = abs(dd)
            ts_iso = format_ist_iso(ts_dt)
            cumulative_series.append(
                {
                    "timestamp": ts_iso,
                    "position_code": p_code,
                    "cumulative_pnl": str(running_cum.quantize(_Q2)),
                }
            )
            drawdown_series.append(
                {
                    "timestamp": ts_iso,
                    "position_code": p_code,
                    "drawdown_inr": str(dd.quantize(_Q2)),
                }
            )

        strategy_contribution = [
            {"strategy": s, "net_pnl": str(strat_bucket[s].quantize(_Q2))}
            for s in sorted(strat_bucket.keys())
        ]
        instrument_contribution = [
            {"instrument": inst, "net_pnl": str(inst_bucket[inst].quantize(_Q2))}
            for inst in sorted(inst_bucket.keys())
        ]

        decided_trades = winners_cnt + losers_cnt
        win_rate = (
            str((Decimal(winners_cnt) / Decimal(decided_trades) * Decimal("100")).quantize(_Q2))
            if decided_trades > 0
            else None
        )

        return {
            "simulation_disclaimer": "SIMULATED PAPER-TRADING RESULTS",
            "execution_mode": cfg.EXECUTION_MODE,
            "account": {
                "account_code": acct.account_code if acct else "PAPER-DESK-001",
                "initial_capital": str(
                    Decimal(str(acct.initial_capital)).quantize(_Q2)
                    if acct
                    else cfg.DEFAULT_DEMO_CAPITAL_INR
                ),
                "available_capital": str(
                    Decimal(str(acct.available_capital)).quantize(_Q2)
                    if acct
                    else cfg.DEFAULT_DEMO_CAPITAL_INR
                ),
            },
            "summary": {
                "gross_pnl": str(tot_gross.quantize(_Q2)),
                "charges": str(tot_charges.quantize(_Q2)),
                "slippage_cost": str(tot_slippage.quantize(_Q2)),
                "realized_pnl": str(tot_realized.quantize(_Q2)),
                "unrealized_pnl": str(tot_unrealized.quantize(_Q2)),
                "net_pnl": str((tot_realized + tot_unrealized).quantize(_Q2)),
                "max_drawdown_inr": str(max_dd.quantize(_Q2)),
                "matched_positions": len(filtered_rows),
                "winning_count": winners_cnt,
                "losing_count": losers_cnt,
                "win_rate_pct": win_rate,
            },
            "filter_options": {
                "strategies": all_strategies,
                "instruments": all_instruments,
                "directions": ["LONG", "SHORT"],
                "exit_reasons": all_exit_reasons,
                "outcomes": ["ALL", "WINNERS", "LOSERS"],
            },
            "charts": {
                "daily_pnl": daily_pnl_series,
                "cumulative_pnl": cumulative_series,
                "strategy_contribution": strategy_contribution,
                "instrument_contribution": instrument_contribution,
                "drawdown": drawdown_series,
            },
            "trades": list(reversed(filtered_rows)),
        }
