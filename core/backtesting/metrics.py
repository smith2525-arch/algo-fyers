"""Institutional backtest performance, risk, drawdown, and distribution metrics.

Mathematical Discipline:
Where any metric is mathematically undefined (e.g. Win Rate with 0 trades, Profit
Factor with 0 losing trades, Average Loser with 0 losers), this module returns
`None` so the API and UI display `"N/A"` rather than Infinity or fabricated values.
"""

from __future__ import annotations

from collections import defaultdict
from decimal import Decimal
from typing import Any

from core.backtesting.models import BacktestTradeRecord


def _q2(val: Decimal) -> str:
    return str(val.quantize(Decimal("0.01")))


def compute_backtest_analytics(
    *,
    initial_capital: Decimal,
    trades: list[BacktestTradeRecord],
    raw_equity_points: list[dict[str, Any]],
    total_evaluated_bars: int,
    bars_in_position: int,
) -> tuple[dict[str, Any], list[dict[str, Any]], list[dict[str, Any]], list[dict[str, Any]]]:
    """Compute all 14 summary metrics, equity curve, drawdown curve, and monthly returns."""
    total_trades = len(trades)
    net_pnl = sum((t.net_pnl for t in trades), Decimal("0"))
    gross_pnl = sum((t.gross_pnl for t in trades), Decimal("0"))
    total_charges = sum((t.charges for t in trades), Decimal("0"))
    total_slippage = sum((t.slippage_cost for t in trades), Decimal("0"))

    return_pct: str | None = (
        _q2((net_pnl / initial_capital) * Decimal("100")) if initial_capital > 0 else None
    )

    # Equity & Peak-to-Trough Drawdown Curves (computed over 100% of bars for exact metrics)
    full_equity_curve: list[dict[str, Any]] = []
    full_drawdown_curve: list[dict[str, Any]] = []
    peak_equity = initial_capital
    max_dd_inr = Decimal("0")
    max_dd_pct = Decimal("0")
    peak_idx = 0
    max_dd_idx = 0

    for idx, pt in enumerate(raw_equity_points):
        eq = Decimal(str(pt["equity"]))
        if eq > peak_equity:
            peak_equity = eq
            peak_idx = idx
        dd_inr = peak_equity - eq  # >= 0 magnitude of drawdown
        dd_pct = (dd_inr / peak_equity * Decimal("100")) if peak_equity > 0 else Decimal("0")
        if dd_inr > max_dd_inr:
            max_dd_inr = dd_inr
            max_dd_idx = idx
        if dd_pct > max_dd_pct:
            max_dd_pct = dd_pct

        ts_str = str(pt["timestamp"])
        full_equity_curve.append(
            {
                "timestamp": ts_str,
                "equity": _q2(eq),
                "cash": _q2(Decimal(str(pt["cash"]))),
                "unrealized_pnl": _q2(Decimal(str(pt["unrealized_pnl"]))),
            }
        )
        full_drawdown_curve.append(
            {
                "timestamp": ts_str,
                "drawdown_inr": _q2(-dd_inr if dd_inr > 0 else Decimal("0")),
                "drawdown_pct": _q2(-dd_pct if dd_pct > 0 else Decimal("0")),
            }
        )

    # Bound stored/transmitted curve length for multi-month/1-year intraday datasets (>1,500 bars)
    # while preserving exact endpoints, peak, and maximum-drawdown trough indices.
    max_curve_points = 1500
    n_pts = len(full_equity_curve)
    if n_pts <= max_curve_points:
        equity_curve = full_equity_curve
        drawdown_curve = full_drawdown_curve
    else:
        stride = max(1, n_pts // max_curve_points)
        keep_indices = set(range(0, n_pts, stride))
        keep_indices.update({0, n_pts - 1, peak_idx, max_dd_idx})
        ordered_idx = sorted(keep_indices)
        equity_curve = [full_equity_curve[i] for i in ordered_idx]
        drawdown_curve = [full_drawdown_curve[i] for i in ordered_idx]

    winners = [t.net_pnl for t in trades if t.net_pnl > 0]
    losers = [t.net_pnl for t in trades if t.net_pnl < 0]

    win_rate: str | None = None
    expectancy: str | None = None
    if total_trades > 0:
        win_rate = _q2((Decimal(len(winners)) / Decimal(total_trades)) * Decimal("100"))
        expectancy = _q2(net_pnl / Decimal(total_trades))

    gross_wins = sum(winners, Decimal("0"))
    gross_losses_abs = abs(sum(losers, Decimal("0")))

    # Profit Factor is strictly undefined if there are 0 trades or 0 gross losses
    profit_factor: str | None = None
    if total_trades > 0 and gross_losses_abs > Decimal("0"):
        profit_factor = _q2(gross_wins / gross_losses_abs)

    avg_winner: str | None = _q2(gross_wins / Decimal(len(winners))) if winners else None
    avg_loser: str | None = (
        _q2(sum(losers, Decimal("0")) / Decimal(len(losers))) if losers else None
    )
    largest_winner: str | None = _q2(max(winners)) if winners else None
    largest_loser: str | None = _q2(min(losers)) if losers else None

    # Consecutive win / loss streaks
    max_consec_wins = 0
    max_consec_losses = 0
    cur_wins = 0
    cur_losses = 0
    for t in trades:
        if t.net_pnl > 0:
            cur_wins += 1
            cur_losses = 0
            if cur_wins > max_consec_wins:
                max_consec_wins = cur_wins
        elif t.net_pnl < 0:
            cur_losses += 1
            cur_wins = 0
            if cur_losses > max_consec_losses:
                max_consec_losses = cur_losses
        else:
            cur_wins = 0
            cur_losses = 0

    exposure_time_pct: str | None = None
    if total_evaluated_bars > 0:
        exposure_time_pct = _q2(
            (Decimal(bars_in_position) / Decimal(total_evaluated_bars)) * Decimal("100")
        )

    # Monthly Returns aggregation by exit timestamp month (IST YYYY-MM)
    monthly_pnl: dict[str, Decimal] = defaultdict(lambda: Decimal("0"))
    monthly_counts: dict[str, int] = defaultdict(int)
    for t in trades:
        ym = t.exit_time.strftime("%Y-%m")
        monthly_pnl[ym] += t.net_pnl
        monthly_counts[ym] += 1

    monthly_returns: list[dict[str, Any]] = []
    running_base = initial_capital
    for ym in sorted(monthly_pnl.keys()):
        m_pnl = monthly_pnl[ym]
        m_ret = (m_pnl / running_base * Decimal("100")) if running_base > 0 else Decimal("0")
        monthly_returns.append(
            {
                "month": ym,
                "net_pnl": _q2(m_pnl),
                "return_pct": _q2(m_ret),
                "trade_count": monthly_counts[ym],
            }
        )
        running_base += m_pnl

    ending_equity = initial_capital + net_pnl

    metrics: dict[str, Any] = {
        "initial_capital": _q2(initial_capital),
        "ending_equity": _q2(ending_equity),
        "gross_pnl": _q2(gross_pnl),
        "total_charges": _q2(total_charges),
        "total_slippage_cost": _q2(total_slippage),
        "net_pnl": _q2(net_pnl),
        "return_pct": return_pct,
        "max_drawdown_inr": _q2(max_dd_inr),
        "max_drawdown_pct": _q2(max_dd_pct),
        "win_rate_pct": win_rate,
        "profit_factor": profit_factor,
        "expectancy_inr": expectancy,
        "total_trades": total_trades,
        "winning_trades": len(winners),
        "losing_trades": len(losers),
        "average_winner_inr": avg_winner,
        "average_loser_inr": avg_loser,
        "largest_winner_inr": largest_winner,
        "largest_loser_inr": largest_loser,
        "max_consecutive_wins": max_consec_wins,
        "max_consecutive_losses": max_consec_losses,
        "exposure_time_pct": exposure_time_pct,
    }
    return metrics, equity_curve, drawdown_curve, monthly_returns

