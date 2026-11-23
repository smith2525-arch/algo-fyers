"""Screen 3: Backtests — Deterministic Historical Simulation & Attribution Console.

Architectural Rule:
This Streamlit view contains ZERO indicator calculations, signal generation, or P&L
accounting logic. All backtest execution, data validation, and series generation
occur inside `core.backtesting` via the FastAPI backend.

Mandatory Safety & Engineering Discipline:
- Every result prominently displays `SIMULATED BACKTEST RESULTS`.
- Mathematically undefined metrics (`None`) render strictly as `N/A`.
- No wording implies guaranteed or future strategy profitability.
"""

from __future__ import annotations

import html
from datetime import date, datetime
from decimal import Decimal
from typing import Any

import plotly.graph_objects as go
import streamlit as st

from frontend.api_client import ControlCenterApiClient
from frontend.components.ui import (
    badge_html,
    render_api_offline_banner,
    render_data_source_banner,
    render_empty_blotter,
    render_screen_header,
)
from frontend.formatting import fmt_inr, fmt_ist, pnl_css_class, to_decimal

SIZING_MODES = ["FIXED_LOTS", "FIXED_QUANTITY", "RISK_PER_TRADE_PCT"]


def _fmt_metric_inr(val: Any, *, signed: bool = False) -> str:
    if val is None or val == "":
        return "N/A"
    return fmt_inr(val, signed=signed)


def _fmt_metric_pct(val: Any, *, signed: bool = False) -> str:
    if val is None or val == "":
        return "N/A"
    dec = to_decimal(val)
    prefix = "+" if (signed and dec > 0) else ""
    return f"{prefix}{dec:.2f}%"


def _fmt_metric_num(val: Any) -> str:
    if val is None or val == "":
        return "N/A"
    return str(val)


def _metric_color_class(val: Any) -> str:
    if val is None or val == "":
        return "num-flat"
    return pnl_css_class(val)


def _plotly_dark_layout(title: str, height: int = 310, yaxis_title: str = "") -> dict[str, Any]:
    return {
        "title": {
            "text": f"{title}  •  SIMULATED BACKTEST RESULTS",
            "font": {"size": 12, "color": "#CBD5E1", "family": "Consolas, monospace"},
            "x": 0.01,
        },
        "height": height,
        "margin": {"l": 52, "r": 20, "t": 36, "b": 34},
        "paper_bgcolor": "#101827",
        "plot_bgcolor": "#0A0F1A",
        "font": {"color": "#94A3B8", "size": 11, "family": "Consolas, monospace"},
        "xaxis": {
            "gridcolor": "#1E293B",
            "linecolor": "#24324B",
            "zerolinecolor": "#1E293B",
        },
        "yaxis": {
            "title": yaxis_title,
            "gridcolor": "#1E293B",
            "linecolor": "#24324B",
            "zerolinecolor": "#334155",
        },
        "legend": {
            "orientation": "h",
            "yanchor": "bottom",
            "y": 1.01,
            "xanchor": "right",
            "x": 1.0,
            "bgcolor": "rgba(0,0,0,0)",
            "font": {"size": 10, "color": "#CBD5E1"},
        },
    }


def _parse_iso_date(ts_str: str | None) -> date | None:
    if not ts_str:
        return None
    try:
        return datetime.fromisoformat(ts_str).date()
    except ValueError:
        return None


def _render_simulation_result_banner(detail: dict[str, Any]) -> None:
    run_code = html.escape(str(detail.get("run_code", "—")))
    ver_label = html.escape(str(detail.get("strategy_version_label", "—")))
    status_val = str(detail.get("status", "COMPLETED"))
    engine_ver = html.escape(str(detail.get("engine_version", "—")))
    fp = html.escape(str(detail.get("reproducibility_fingerprint", ""))[:16])
    data_hash = html.escape(str(detail.get("data_version_hash", ""))[:16])

    st.markdown(
        f"""
        <div style="background:linear-gradient(90deg, #1E1B4B 0%, #172554 55%, #0F172A 100%);
                    border:1px solid #F59E0B; border-left:5px solid #F59E0B; border-radius:3px;
                    padding:0.48rem 0.85rem; margin-bottom:0.60rem; display:flex;
                    justify-content:space-between; align-items:center; flex-wrap:wrap; gap:0.5rem;">
            <div>
                <span class="mono" style="font-size:0.84rem; font-weight:800; color:#FBBF24; letter-spacing:0.08em;">
                    SIMULATED BACKTEST RESULTS
                </span>
                <span style="font-size:0.74rem; color:#E2E8F0; margin-left:0.65rem;">
                    Historical bar-by-bar simulation with modeled execution delay, adverse slippage, and charges.
                    Past simulated results do NOT indicate future performance.
                </span>
            </div>
            <div class="mono" style="font-size:0.71rem; color:#CBD5E1;">
                RUN: <strong>{run_code}</strong> &bull; {ver_label} &bull; {badge_html(status_val)}
                &bull; DATA_HASH:{data_hash} &bull; REPRO:{fp} &bull; {engine_ver}
            </div>
        </div>
        """,
        unsafe_allow_html=True,
    )


def _render_summary_metrics(metrics: dict[str, Any]) -> None:
    max_dd_inr = metrics.get("max_drawdown_inr")
    max_dd_pct = metrics.get("max_drawdown_pct")
    dd_display = (
        f"-{fmt_inr(max_dd_inr)} (-{to_decimal(max_dd_pct):.2f}%)"
        if max_dd_inr is not None and max_dd_pct is not None
        else "N/A"
    )

    row1 = [
        ("Net P&L", _fmt_metric_inr(metrics.get("net_pnl"), signed=True), _metric_color_class(metrics.get("net_pnl")), "SIMULATED"),
        ("Return %", _fmt_metric_pct(metrics.get("return_pct"), signed=True), _metric_color_class(metrics.get("return_pct")), ""),
        ("Maximum Drawdown", dd_display, "num-neg" if to_decimal(max_dd_inr) > 0 else "num-flat", ""),
        ("Win Rate", _fmt_metric_pct(metrics.get("win_rate_pct")), "num-flat", ""),
        ("Profit Factor", _fmt_metric_num(metrics.get("profit_factor")), "num-flat", ""),
        ("Expectancy / Trade", _fmt_metric_inr(metrics.get("expectancy_inr"), signed=True), _metric_color_class(metrics.get("expectancy_inr")), ""),
        ("Number of Trades", _fmt_metric_num(metrics.get("total_trades")), "num-flat", ""),
    ]
    row2 = [
        ("Average Winner", _fmt_metric_inr(metrics.get("average_winner_inr"), signed=True), _metric_color_class(metrics.get("average_winner_inr")), ""),
        ("Average Loser", _fmt_metric_inr(metrics.get("average_loser_inr"), signed=True), _metric_color_class(metrics.get("average_loser_inr")), ""),
        ("Largest Winner", _fmt_metric_inr(metrics.get("largest_winner_inr"), signed=True), _metric_color_class(metrics.get("largest_winner_inr")), ""),
        ("Largest Loser", _fmt_metric_inr(metrics.get("largest_loser_inr"), signed=True), _metric_color_class(metrics.get("largest_loser_inr")), ""),
        ("Consecutive Wins", _fmt_metric_num(metrics.get("max_consecutive_wins")), "num-flat", ""),
        ("Consecutive Losses", _fmt_metric_num(metrics.get("max_consecutive_losses")), "num-flat", ""),
        ("Exposure Time", _fmt_metric_pct(metrics.get("exposure_time_pct")), "num-flat", ""),
    ]

    for row in (row1, row2):
        cells_html: list[str] = []
        for label, val_str, color_cls, pill in row:
            pill_html = f'<span class="kpi-demo-pill">{html.escape(pill)}</span>' if pill else ""
            cells_html.append(
                f'<div class="kpi-cell">'
                f'<div class="kpi-label"><span>{html.escape(label)}</span>{pill_html}</div>'
                f'<div class="kpi-value {color_cls}">{html.escape(val_str)}</div>'
                f"</div>"
            )
        st.markdown(f'<div class="kpi-grid">{"".join(cells_html)}</div>', unsafe_allow_html=True)

    # Capital & Cost Reconciliation Bar
    init_cap = _fmt_metric_inr(metrics.get("initial_capital"))
    end_eq = _fmt_metric_inr(metrics.get("ending_equity"))
    gross = _fmt_metric_inr(metrics.get("gross_pnl"), signed=True)
    charges = _fmt_metric_inr(metrics.get("total_charges"))
    slip = _fmt_metric_inr(metrics.get("total_slippage_cost"))
    st.markdown(
        f"""
        <div class="desk-panel" style="padding:0.42rem 0.75rem; margin-bottom:0.60rem;">
            <div class="mono" style="font-size:0.73rem; color:#CBD5E1; display:flex; justify-content:space-between; flex-wrap:wrap; gap:0.5rem;">
                <span>INITIAL PAPER CAPITAL: <strong>{html.escape(init_cap)}</strong></span>
                <span>GROSS P&amp;L (POST-SLIPPAGE): <strong>{html.escape(gross)}</strong></span>
                <span>SLIPPAGE COST ATTRIBUTED: <strong style="color:#FBBF24;">{html.escape(slip)}</strong></span>
                <span>CHARGES &amp; FEES: <strong style="color:#F87171;">{html.escape(charges)}</strong></span>
                <span>ENDING SIMULATED EQUITY: <strong>{html.escape(end_eq)}</strong></span>
            </div>
        </div>
        """,
        unsafe_allow_html=True,
    )


def _render_entry_exit_chart(detail: dict[str, Any]) -> None:
    bars: list[dict[str, Any]] = detail.get("chart_bars") or []
    trades: list[dict[str, Any]] = detail.get("trades") or []
    in_params = detail.get("input_parameters", {}).get("strategy_parameters", {})
    f_period = in_params.get("fast_ema_period", 9)
    s_period = in_params.get("slow_ema_period", 21)

    if not bars:
        render_empty_blotter(
            panel_title="Entry / Exit Price & EMA Attribution Chart",
            panel_meta="SIMULATED BACKTEST RESULTS • 0 VALID CANDLES",
            columns=[("Timestamp (IST)", False), ("Open", True), ("High", True), ("Low", True), ("Close", True)],
            empty_title="NO VALIDATED CANDLES AVAILABLE FOR PRICE OVERLAY",
            empty_description="Dataset failed pre-backtest structural validation or yielded zero candles in the selected date window.",
        )
        return

    xs = [b["timestamp"] for b in bars]
    fig = go.Figure()
    fig.add_trace(
        go.Candlestick(
            x=xs,
            open=[float(b["open"]) for b in bars],
            high=[float(b["high"]) for b in bars],
            low=[float(b["low"]) for b in bars],
            close=[float(b["close"]) for b in bars],
            name=f"{detail.get('instrument_symbol')} {detail.get('timeframe')} OHLC",
            increasing_line_color="#10B981",
            decreasing_line_color="#EF4444",
        )
    )

    fast_x = [b["timestamp"] for b in bars if b.get("fast_ema") is not None]
    fast_y = [float(b["fast_ema"]) for b in bars if b.get("fast_ema") is not None]
    slow_x = [b["timestamp"] for b in bars if b.get("slow_ema") is not None]
    slow_y = [float(b["slow_ema"]) for b in bars if b.get("slow_ema") is not None]

    if fast_x:
        fig.add_trace(
            go.Scatter(
                x=fast_x,
                y=fast_y,
                mode="lines",
                name=f"Fast EMA ({f_period})",
                line={"color": "#38BDF8", "width": 1.4},
            )
        )
    if slow_x:
        fig.add_trace(
            go.Scatter(
                x=slow_x,
                y=slow_y,
                mode="lines",
                name=f"Slow EMA ({s_period})",
                line={"color": "#F59E0B", "width": 1.4},
            )
        )

    long_entries = [t for t in trades if t.get("direction") == "LONG"]
    short_entries = [t for t in trades if t.get("direction") == "SHORT"]

    if long_entries:
        fig.add_trace(
            go.Scatter(
                x=[t["entry_time"] for t in long_entries],
                y=[float(t["entry_price"]) for t in long_entries],
                mode="markers",
                name="Simulated LONG Entry",
                marker={"symbol": "triangle-up", "size": 11, "color": "#22C55E", "line": {"width": 1, "color": "#052E16"}},
                text=[f"Trade #{t['trade_number']} LONG Qty={t['quantity']}" for t in long_entries],
            )
        )
    if short_entries:
        fig.add_trace(
            go.Scatter(
                x=[t["entry_time"] for t in short_entries],
                y=[float(t["entry_price"]) for t in short_entries],
                mode="markers",
                name="Simulated SHORT Entry",
                marker={"symbol": "triangle-down", "size": 11, "color": "#F97316", "line": {"width": 1, "color": "#431407"}},
                text=[f"Trade #{t['trade_number']} SHORT Qty={t['quantity']}" for t in short_entries],
            )
        )
    if trades:
        fig.add_trace(
            go.Scatter(
                x=[t["exit_time"] for t in trades],
                y=[float(t["exit_price"]) for t in trades],
                mode="markers",
                name="Simulated Exit",
                marker={"symbol": "x", "size": 9, "color": "#E2E8F0"},
                text=[f"Trade #{t['trade_number']} Exit ({t['exit_reason']}) Net={t['net_pnl']}" for t in trades],
            )
        )

    layout = _plotly_dark_layout("ENTRY / EXIT EXECUTION CHART (IST)", height=390, yaxis_title="Index Price")
    layout["xaxis"]["rangeslider"] = {"visible": False}
    fig.update_layout(**layout)
    st.plotly_chart(fig, use_container_width=True)


def _render_equity_and_drawdown_charts(detail: dict[str, Any]) -> None:
    eq_curve: list[dict[str, Any]] = detail.get("equity_curve") or []
    dd_curve: list[dict[str, Any]] = detail.get("drawdown_curve") or []
    init_cap = float(detail.get("metrics", {}).get("initial_capital") or 500000.0)

    c1, c2 = st.columns(2)
    with c1:
        if not eq_curve:
            st.info("SIMULATED BACKTEST RESULTS — Equity curve unavailable (0 evaluated bars).")
        else:
            fig_eq = go.Figure()
            fig_eq.add_trace(
                go.Scatter(
                    x=[pt["timestamp"] for pt in eq_curve],
                    y=[float(pt["equity"]) for pt in eq_curve],
                    mode="lines",
                    name="Simulated Equity (INR)",
                    line={"color": "#3B82F6", "width": 1.8},
                    fill="tozeroy",
                    fillcolor="rgba(59, 130, 246, 0.10)",
                )
            )
            fig_eq.add_hline(
                y=init_cap,
                line_dash="dash",
                line_color="#64748B",
                annotation_text="Initial Paper Capital",
                annotation_font_color="#94A3B8",
            )
            fig_eq.update_layout(**_plotly_dark_layout("SIMULATED EQUITY CURVE (IST)", height=295, yaxis_title="INR"))
            st.plotly_chart(fig_eq, use_container_width=True)

    with c2:
        if not dd_curve:
            st.info("SIMULATED BACKTEST RESULTS — Drawdown curve unavailable (0 evaluated bars).")
        else:
            fig_dd = go.Figure()
            fig_dd.add_trace(
                go.Scatter(
                    x=[pt["timestamp"] for pt in dd_curve],
                    y=[float(pt["drawdown_pct"]) for pt in dd_curve],
                    mode="lines",
                    name="Drawdown %",
                    line={"color": "#EF4444", "width": 1.6},
                    fill="tozeroy",
                    fillcolor="rgba(239, 68, 68, 0.18)",
                )
            )
            fig_dd.update_layout(**_plotly_dark_layout("UNDERWATER DRAWDOWN CURVE (%)", height=295, yaxis_title="Drawdown %"))
            st.plotly_chart(fig_dd, use_container_width=True)


def _render_monthly_and_distribution_charts(detail: dict[str, Any]) -> None:
    monthly: list[dict[str, Any]] = detail.get("monthly_returns") or []
    trades: list[dict[str, Any]] = detail.get("trades") or []

    c1, c2 = st.columns(2)
    with c1:
        if not monthly:
            render_empty_blotter(
                panel_title="Monthly Return Attribution",
                panel_meta="SIMULATED BACKTEST RESULTS • 0 MONTHS",
                columns=[("Month (IST)", False), ("Trades", True), ("Net P&L", True), ("Return %", True)],
                empty_title="NO CLOSED SIMULATED TRADES",
                empty_description="Monthly return attribution populates when at least one simulated trade closes.",
            )
        else:
            months = [m["month"] for m in monthly]
            rets = [float(m["return_pct"]) for m in monthly]
            colors = ["#10B981" if r >= 0 else "#EF4444" for r in rets]
            fig_m = go.Figure(
                data=[
                    go.Bar(
                        x=months,
                        y=rets,
                        marker_color=colors,
                        name="Monthly Return %",
                        text=[f"{r:+.2f}% ({fmt_inr(m['net_pnl'], signed=True)})" for r, m in zip(rets, monthly)],
                        textposition="auto",
                    )
                ]
            )
            fig_m.update_layout(**_plotly_dark_layout("MONTHLY RETURN ATTRIBUTION (%)", height=285, yaxis_title="Return %"))
            st.plotly_chart(fig_m, use_container_width=True)

    with c2:
        if not trades:
            render_empty_blotter(
                panel_title="Simulated Trade P&L Distribution",
                panel_meta="SIMULATED BACKTEST RESULTS • 0 TRADES",
                columns=[("Bucket (INR)", False), ("Trade Count", True)],
                empty_title="NO SIMULATED TRADES TO DISTRIBUTE",
                empty_description="Trade P&L distribution populates when simulated trades occur.",
            )
        else:
            pnls = [float(t["net_pnl"]) for t in trades]
            bar_colors = ["#10B981" if p > 0 else ("#EF4444" if p < 0 else "#64748B") for p in pnls]
            fig_dist = go.Figure(
                data=[
                    go.Bar(
                        x=[f"#{t['trade_number']}" for t in trades],
                        y=pnls,
                        marker_color=bar_colors,
                        name="Trade Net P&L (INR)",
                        text=[f"{t['direction']} • {t['exit_reason']}" for t in trades],
                    )
                ]
            )
            fig_dist.update_layout(
                **_plotly_dark_layout("TRADE NET P&L DISTRIBUTION (INR)", height=285, yaxis_title="Net P&L (INR)")
            )
            st.plotly_chart(fig_dist, use_container_width=True)


def _render_trade_ledger(trades: list[dict[str, Any]]) -> None:
    if not trades:
        render_empty_blotter(
            panel_title="Simulated Trade Ledger",
            panel_meta="SIMULATED BACKTEST RESULTS • 0 TRADES",
            columns=[
                ("Trade ID", False),
                ("Strategy Version", False),
                ("Instrument", False),
                ("Direction", False),
                ("Entry Time (IST)", False),
                ("Entry Price", True),
                ("Quantity", True),
                ("Exit Time (IST)", False),
                ("Exit Price", True),
                ("Exit Reason", False),
                ("Gross P&L", True),
                ("Charges", True),
                ("Slippage Cost", True),
                ("Net P&L", True),
            ],
            empty_title="ZERO SIMULATED TRADES GENERATED",
            empty_description=(
                "No crossover + RSI confirmation signal occurred within the configured IST trading window "
                "or validation halted before simulation."
            ),
        )
        return

    rows_html: list[str] = []
    for t in trades:
        tid_full = str(t.get("trade_id", ""))
        tid_short = html.escape(tid_full[:12])
        ver_lbl = html.escape(str(t.get("strategy_version_label", "")))
        inst = html.escape(str(t.get("instrument", "")))
        direction = str(t.get("direction", "LONG"))
        en_ts = html.escape(fmt_ist(t.get("entry_time")))
        en_px = html.escape(f"{to_decimal(t.get('entry_price')):.2f}")
        qty = html.escape(str(t.get("quantity", 0)))
        ex_ts = html.escape(fmt_ist(t.get("exit_time")))
        ex_px = html.escape(f"{to_decimal(t.get('exit_price')):.2f}")
        reason = html.escape(str(t.get("exit_reason", "")))
        gross = t.get("gross_pnl")
        charges = t.get("charges")
        slip = t.get("slippage_cost")
        net = t.get("net_pnl")

        rows_html.append(
            f'<tr title="Deterministic Trade UUID: {html.escape(tid_full)} | Signal @ {html.escape(fmt_ist(t.get("entry_signal_time")))}">'
            f'<td class="mono">#{t.get("trade_number")} ({tid_short})</td>'
            f'<td class="mono">{ver_lbl}</td>'
            f'<td class="mono">{inst}</td>'
            f"<td>{badge_html(direction)}</td>"
            f'<td class="mono">{en_ts}</td>'
            f'<td class="num">{en_px}</td>'
            f'<td class="num">{qty}</td>'
            f'<td class="mono">{ex_ts}</td>'
            f'<td class="num">{ex_px}</td>'
            f'<td class="mono">{reason}</td>'
            f'<td class="num {pnl_css_class(gross)}">{html.escape(fmt_inr(gross, signed=True))}</td>'
            f'<td class="num">{html.escape(fmt_inr(charges))}</td>'
            f'<td class="num">{html.escape(fmt_inr(slip))}</td>'
            f'<td class="num {pnl_css_class(net)}"><strong>{html.escape(fmt_inr(net, signed=True))}</strong></td>'
            f"</tr>"
        )

    st.markdown(
        f"""
        <div class="desk-panel">
            <div class="desk-panel-header">
                <span class="desk-panel-title">Simulated Trade Ledger (Zero-Lookahead Cost Attribution)</span>
                <span class="desk-panel-meta">SIMULATED BACKTEST RESULTS &bull; {len(rows_html)} CLOSED TRADES</span>
            </div>
            <div style="overflow-x:auto;">
                <table class="blotter-table">
                    <thead>
                        <tr>
                            <th>Trade ID</th>
                            <th>Strategy Version</th>
                            <th>Instrument</th>
                            <th>Direction</th>
                            <th>Entry Time (IST)</th>
                            <th class="num">Entry Price</th>
                            <th class="num">Quantity</th>
                            <th>Exit Time (IST)</th>
                            <th class="num">Exit Price</th>
                            <th>Exit Reason</th>
                            <th class="num">Gross P&amp;L</th>
                            <th class="num">Charges</th>
                            <th class="num">Slippage Cost</th>
                            <th class="num">Net P&amp;L</th>
                        </tr>
                    </thead>
                    <tbody>{"".join(rows_html)}</tbody>
                </table>
            </div>
        </div>
        """,
        unsafe_allow_html=True,
    )


def _render_data_quality_and_reproducibility(detail: dict[str, Any]) -> None:
    dq = detail.get("data_quality_report") or {}
    passed = bool(dq.get("passed_validation"))
    gate_badge = badge_html("PASS" if passed else "FAIL")
    warn_badge = badge_html("WARNING" if dq.get("has_warnings") else "HEALTHY")

    c1, c2 = st.columns([1.2, 1.0])
    with c1:
        checks = [
            ("Null / Non-Numeric Values", dq.get("null_value_count", 0), "HARD ERROR"),
            ("Out-of-Order Timestamps", dq.get("out_of_order_count", 0), "HARD ERROR"),
            ("Duplicate Timestamps", dq.get("duplicate_timestamp_count", 0), "HARD ERROR"),
            ("Invalid OHLC / Volume Relationships", dq.get("invalid_ohlc_count", 0), "HARD ERROR"),
            ("Timezone Normalization Events", dq.get("timezone_issue_count", 0), "WARNING"),
            ("Missing Session Grid Candles", dq.get("missing_candle_count", 0), "WARNING"),
            ("Unexpected Session / Off-Hours Gaps", dq.get("unexpected_session_gap_count", 0), "WARNING"),
        ]
        check_rows = "".join(
            f"<tr>"
            f"<td>{html.escape(label)}</td>"
            f'<td class="mono">{html.escape(level)}</td>'
            f'<td class="num {"num-neg" if int(cnt) > 0 else "num-pos"}">{int(cnt)}</td>'
            f"</tr>"
            for label, cnt, level in checks
        )
        st.markdown(
            f"""
            <div class="desk-panel">
                <div class="desk-panel-header">
                    <span class="desk-panel-title">Pre-Backtest Historical Data Quality Report</span>
                    <span class="desk-panel-meta">
                        STRUCTURAL GATE: {gate_badge} &bull; WARNINGS: {warn_badge}
                        &bull; {int(dq.get("valid_candle_count", 0))}/{int(dq.get("total_rows_inspected", 0))} VALID CANDLES
                    </span>
                </div>
                <table class="blotter-table">
                    <thead>
                        <tr><th>Validation Check</th><th>Policy</th><th class="num">Count</th></tr>
                    </thead>
                    <tbody>{check_rows}</tbody>
                </table>
            </div>
            """,
            unsafe_allow_html=True,
        )

        issues: list[dict[str, Any]] = dq.get("issues") or []
        if issues:
            issue_rows = "".join(
                f"<tr>"
                f"<td>{badge_html(str(iss.get('severity', 'WARNING')))}</td>"
                f'<td class="mono">{html.escape(str(iss.get("check_code", "")))}</td>'
                f'<td class="num">{html.escape(str(iss.get("row_index") or "—"))}</td>'
                f"<td>{html.escape(str(iss.get('message', '')))}</td>"
                f"</tr>"
                for iss in issues[:30]
            )
            st.markdown(
                f"""
                <div class="desk-panel" style="margin-top:0.5rem;">
                    <div class="desk-panel-header">
                        <span class="desk-panel-title">Recorded Data Quality Diagnostic Findings</span>
                        <span class="desk-panel-meta">SHOWING {min(len(issues), 30)} OF {len(issues)}</span>
                    </div>
                    <table class="blotter-table">
                        <thead><tr><th>Severity</th><th>Code</th><th class="num">Row</th><th>Diagnostic Detail</th></tr></thead>
                        <tbody>{issue_rows}</tbody>
                    </table>
                </div>
                """,
                unsafe_allow_html=True,
            )

    with c2:
        assumptions = detail.get("execution_assumptions") or {}
        inputs = detail.get("input_parameters") or {}
        meta_rows = [
            ("Backtest Run ID", str(detail.get("id", "—"))),
            ("Run Code", str(detail.get("run_code", "—"))),
            ("Strategy Version ID", str(detail.get("strategy_version_id", "—"))),
            ("Strategy Version Label", str(detail.get("strategy_version_label", "—"))),
            ("Strategy Parameter SHA-256", str(inputs.get("strategy_parameter_hash", "—"))[:24]),
            ("Dataset ID & Source", f"{detail.get('dataset_id')} ({detail.get('dataset_source')})"),
            ("Dataset Content SHA-256", str(detail.get("data_version_hash", "—"))[:24]),
            ("Reproducibility Fingerprint", str(detail.get("reproducibility_fingerprint", "—"))[:24]),
            ("Engine Version", str(detail.get("engine_version", "—"))),
            ("Execution Fill Model", str(assumptions.get("fill_model", "—"))),
            ("Intrabar Stop/Target Policy", str(assumptions.get("intrabar_tie_break", "—"))),
            ("Data Start (IST)", fmt_ist(detail.get("data_start_timestamp"))),
            ("Data End (IST)", fmt_ist(detail.get("data_end_timestamp"))),
            ("Run Started (IST)", fmt_ist(detail.get("started_at"))),
            ("Run Completed (IST)", fmt_ist(detail.get("completed_at"))),
        ]
        meta_html = "".join(
            f"<tr><td>{html.escape(k)}</td><td class='mono' style='text-align:right;'>{html.escape(v)}</td></tr>"
            for k, v in meta_rows
        )
        st.markdown(
            f"""
            <div class="desk-panel">
                <div class="desk-panel-header">
                    <span class="desk-panel-title">Deterministic Reproducibility &amp; Audit Lineage</span>
                    <span class="desk-panel-meta">IMMUTABLE ARTIFACT</span>
                </div>
                <table class="blotter-table">
                    <thead><tr><th>Lineage Attribute</th><th style="text-align:right;">Recorded Value</th></tr></thead>
                    <tbody>{meta_html}</tbody>
                </table>
            </div>
            """,
            unsafe_allow_html=True,
        )


def render(client: ControlCenterApiClient) -> None:
    render_screen_header(
        "BACKTESTS",
        "DETERMINISTIC ZERO-LOOKAHEAD HISTORICAL SIMULATION  |  SIMULATED BACKTEST RESULTS  |  IST (+05:30)",
    )

    strat_res = client.list_strategies()
    if not strat_res.ok or not isinstance(strat_res.data, list):
        render_api_offline_banner(strat_res.error or "Failed to load strategy catalog", client.base_url)
        return

    all_versions: list[dict[str, Any]] = []
    for s_def in strat_res.data:
        for v in s_def.get("versions", []):
            all_versions.append(v)

    ds_res = client.list_backtest_datasets()
    all_datasets: list[dict[str, Any]] = ds_res.data if (ds_res.ok and isinstance(ds_res.data, list)) else []

    # --- CSV Dataset Import Expander ---
    with st.expander("Import Historical OHLCV CSV Dataset (Persisted to PostgreSQL)", expanded=False):
        ic1, ic2, ic3, ic4 = st.columns([1.8, 1.0, 0.9, 1.3])
        with ic1:
            imp_name = st.text_input(
                "Dataset Label",
                value="Custom Historical OHLCV Import",
                key="bt_imp_name",
            )
        with ic2:
            imp_inst = st.selectbox("Instrument", ["NIFTY", "BANKNIFTY"], key="bt_imp_inst")
        with ic3:
            imp_tf = st.selectbox(
                "Timeframe",
                ["5m", "1m", "3m", "10m", "15m", "30m", "60m", "1d"],
                key="bt_imp_tf",
            )
        with ic4:
            imp_actor = st.text_input("Operator ID", value="desk.operator", key="bt_imp_actor")

        uploaded_file = st.file_uploader(
            "Select OHLCV CSV File (columns: timestamp, open, high, low, close, volume)",
            type=["csv"],
            key="bt_imp_file",
        )
        if st.button("IMPORT & VALIDATE CSV DATASET", key="bt_imp_btn"):
            if uploaded_file is None:
                st.warning("Attach a CSV file prior to importing.")
            else:
                csv_str = uploaded_file.getvalue().decode("utf-8", errors="replace")
                imp_resp = client.import_backtest_csv(
                    name=imp_name,
                    instrument=imp_inst,
                    timeframe=imp_tf,
                    csv_text=csv_str,
                    actor=imp_actor,
                )
                if imp_resp.ok and isinstance(imp_resp.data, dict):
                    ds_meta = imp_resp.data.get("dataset", {})
                    dq_meta = imp_resp.data.get("data_quality_report", {})
                    st.success(
                        f"Imported dataset {ds_meta.get('dataset_id')} "
                        f"({ds_meta.get('row_count')} rows, passed_validation={dq_meta.get('passed_validation')})."
                    )
                    st.rerun()
                else:
                    st.error(imp_resp.error or "CSV import failed.")

    # --- Backtest Execution Configuration Panel ---
    st.markdown(
        """
        <div class="desk-panel" style="margin-bottom:0.45rem; padding:0.40rem 0.75rem;">
            <div class="desk-panel-header" style="margin-bottom:0;">
                <span class="desk-panel-title">Configure &amp; Launch Deterministic Backtest Run</span>
                <span class="desk-panel-meta">IMMUTABLE STRATEGY VERSION &bull; EXECUTION DELAY &ge; 1 BAR &bull; NO LIVE ORDERS</span>
            </div>
        </div>
        """,
        unsafe_allow_html=True,
    )

    if not all_versions:
        st.warning("No Strategy Versions exist yet. Save a version in Strategy Lab first.")
        return

    ver_labels = [
        f"{v['version_label']}  [{v['instrument_symbol']} • {v['timeframe']} • {v['status']}]"
        for v in all_versions
    ]
    pre_bt_ds_id = st.session_state.pop("preselected_backtest_dataset_id", None)
    if pre_bt_ds_id:
        st.session_state["bt_preferred_ds_id"] = pre_bt_ds_id
        # Auto-select a matching strategy version if one exists for the preselected dataset
        target_ds_obj = next((d for d in all_datasets if d.get("dataset_id") == pre_bt_ds_id), None)
        if target_ds_obj:
            for idx_v, v_obj in enumerate(all_versions):
                if (
                    str(v_obj.get("instrument_symbol", "")).upper()
                    == str(target_ds_obj.get("instrument", "")).upper()
                    and str(v_obj.get("timeframe", "")) == str(target_ds_obj.get("timeframe", ""))
                ):
                    st.session_state["bt_sel_ver_idx"] = idx_v
                    break

    r1c1, r1c2, r1c3, r1c4 = st.columns([1.8, 2.0, 1.0, 1.0])
    with r1c1:
        ver_idx = st.selectbox(
            "Immutable Strategy Version",
            range(len(all_versions)),
            format_func=lambda idx: ver_labels[idx],
            key="bt_sel_ver_idx",
        )
    selected_ver = all_versions[ver_idx]
    ver_params = selected_ver.get("parameters", {})
    target_inst = str(selected_ver.get("instrument_symbol", "NIFTY"))
    target_tf = str(selected_ver.get("timeframe", "5m"))

    matching_datasets = [
        d
        for d in all_datasets
        if str(d.get("instrument", "")).upper() == target_inst.upper()
        and str(d.get("timeframe", "")) == target_tf
    ]

    with r1c2:
        if matching_datasets:
            ds_labels = [
                (
                    f"[{d.get('provider', d.get('source_type', 'DATA'))} • {d.get('quality_status', 'VALID')}] "
                    f"{d['name']}  ({d['row_count']} bars • ID:{d['dataset_id']})"
                )
                for d in matching_datasets
            ]
            pref_id = st.session_state.get("bt_preferred_ds_id")
            match_ids = [str(d["dataset_id"]) for d in matching_datasets]
            default_ds_idx = match_ids.index(pref_id) if pref_id in match_ids else 0
            ds_idx = st.selectbox(
                f"Historical Dataset ({target_inst} {target_tf})",
                range(len(matching_datasets)),
                index=default_ds_idx,
                format_func=lambda idx: ds_labels[idx],
                key=f"bt_sel_ds_idx_{target_inst}_{target_tf}",
            )
            selected_ds = matching_datasets[ds_idx]
        else:
            st.selectbox(
                f"Historical Dataset ({target_inst} {target_tf})",
                ["No matching dataset — fetch FYERS data in Historical Data or import CSV above"],
                disabled=True,
                key="bt_sel_ds_empty",
            )
            selected_ds = None

    ds_min_d = _parse_iso_date(selected_ds.get("start_timestamp")) if selected_ds else None
    ds_max_d = _parse_iso_date(selected_ds.get("end_timestamp")) if selected_ds else None

    with r1c3:
        start_d = st.date_input(
            "Start Date (IST)",
            value=ds_min_d or date(2026, 10, 28),
            key=f"bt_start_{selected_ds['dataset_id'] if selected_ds else 'none'}",
        )
    with r1c4:
        end_d = st.date_input(
            "End Date (IST)",
            value=ds_max_d or date(2026, 11, 4),
            key=f"bt_end_{selected_ds['dataset_id'] if selected_ds else 'none'}",
        )

    r2c1, r2c2, r2c3, r2c4, r2c5, r2c6, r2c7 = st.columns([1.2, 1.0, 1.0, 1.0, 1.2, 0.9, 1.3])
    with r2c1:
        init_capital = st.number_input(
            "Initial Capital (₹)",
            min_value=10000.0,
            max_value=100000000.0,
            value=500000.0,
            step=50000.0,
            key="bt_init_cap",
        )
    with r2c2:
        slip_bps = st.number_input(
            "Slippage (bps)",
            min_value=0.0,
            max_value=250.0,
            value=float(to_decimal(ver_params.get("slippage_bps", "2.0"))),
            step=0.5,
            key=f"bt_slip_{selected_ver['id']}",
        )
    with r2c3:
        chg_bps = st.number_input(
            "Charges (bps)",
            min_value=0.0,
            max_value=250.0,
            value=float(to_decimal(ver_params.get("estimated_charges_bps", "3.5"))),
            step=0.5,
            key=f"bt_chg_{selected_ver['id']}",
        )
    with r2c4:
        flat_fee = st.number_input(
            "Fee / Order (₹)",
            min_value=0.0,
            max_value=5000.0,
            value=float(to_decimal(ver_params.get("per_order_fee_inr", "20.0"))),
            step=5.0,
            key=f"bt_fee_{selected_ver['id']}",
        )
    default_mode = str(ver_params.get("position_sizing_mode", "FIXED_LOTS"))
    mode_index = SIZING_MODES.index(default_mode) if default_mode in SIZING_MODES else 0
    with r2c5:
        sizing_mode = st.selectbox(
            "Position Sizing",
            SIZING_MODES,
            index=mode_index,
            key=f"bt_sz_mode_{selected_ver['id']}",
        )
    with r2c6:
        sizing_val = st.number_input(
            "Size Value",
            min_value=0.1,
            max_value=10000.0,
            value=float(to_decimal(ver_params.get("position_size_value", "1"))),
            step=1.0,
            key=f"bt_sz_val_{selected_ver['id']}",
        )
    with r2c7:
        delay_bars = st.number_input(
            "Exec Delay (Bars)",
            min_value=1,
            max_value=10,
            value=1,
            step=1,
            key="bt_delay_bars",
        )

    partial_ack = False
    is_ds_partial = False
    is_ds_blocked = False
    if selected_ds is not None:
        ds_qual = str(selected_ds.get("quality_status", "VALID")).upper()
        render_data_source_banner(
            source_type=selected_ds.get("source_type"),
            provider=selected_ds.get("provider"),
            dataset_id=str(selected_ds.get("dataset_id")),
            quality_status=ds_qual,
        )
        is_ds_partial = ds_qual == "PARTIAL"
        is_ds_blocked = ds_qual in {"DOWNLOAD_FAILED", "AUTHENTICATION_REQUIRED"} or (
            str(selected_ds.get("source_type")) == "FYERS_HISTORICAL" and ds_qual == "INVALID"
        )
        if is_ds_partial:
            partial_ack = st.checkbox(
                "Acknowledge PARTIAL historical dataset: I have reviewed the data-quality report and confirm running a simulated backtest on available bars.",
                value=False,
                key=f"bt_ack_partial_{selected_ds['dataset_id']}",
            )
        if is_ds_blocked:
            st.error(
                f"Selected dataset '{selected_ds['dataset_id']}' has status {ds_qual} and cannot be used for backtesting."
            )

    btn_col, note_col = st.columns([1.4, 3.6])
    with btn_col:
        run_clicked = st.button(
            "EXECUTE DETERMINISTIC BACKTEST",
            type="primary",
            use_container_width=True,
            disabled=(
                selected_ds is None
                or is_ds_blocked
                or (is_ds_partial and not partial_ack)
            ),
            key="bt_exec_btn",
        )
    with note_col:
        st.caption(
            f"Locked Instrument: `{target_inst}` • Timeframe: `{target_tf}` • "
            f"Closed-bar signals fill at `t + {int(delay_bars)}` bar open with adverse slippage. "
            "SIMULATED BACKTEST RESULTS ONLY."
        )

    if run_clicked and selected_ds is not None:
        exec_payload = {
            "strategy_version_id": str(selected_ver["id"]),
            "dataset_id": str(selected_ds["dataset_id"]),
            "start_date": start_d.isoformat() if isinstance(start_d, date) else None,
            "end_date": end_d.isoformat() if isinstance(end_d, date) else None,
            "initial_capital": str(Decimal(str(init_capital)).quantize(Decimal("0.01"))),
            "slippage_bps": str(Decimal(str(slip_bps)).quantize(Decimal("0.01"))),
            "estimated_charges_bps": str(Decimal(str(chg_bps)).quantize(Decimal("0.01"))),
            "per_order_fee_inr": str(Decimal(str(flat_fee)).quantize(Decimal("0.01"))),
            "position_sizing_mode": sizing_mode,
            "position_size_value": str(Decimal(str(sizing_val)).quantize(Decimal("0.01"))),
            "execution_delay_bars": int(delay_bars),
            "allow_partial_dataset": bool(partial_ack),
            "actor": "desk.operator",
        }
        exec_res = client.execute_backtest(exec_payload)
        if exec_res.ok and isinstance(exec_res.data, dict):
            st.session_state["bt_active_run_id"] = str(exec_res.data["id"])
            st.rerun()
        else:
            st.error(exec_res.error or "Backtest execution failed.")

    st.markdown("<div style='height:0.35rem;'></div>", unsafe_allow_html=True)

    # --- Load Persisted Backtest Runs ---
    runs_res = client.list_backtests(limit=50)
    runs: list[dict[str, Any]] = (
        runs_res.data if (runs_res.ok and isinstance(runs_res.data, list)) else []
    )

    if not runs:
        render_empty_blotter(
            panel_title="Persisted Historical Backtest Runs — SIMULATED BACKTEST RESULTS",
            panel_meta="0 PERSISTED RUNS",
            columns=[
                ("Run Code", False),
                ("Strategy Version", False),
                ("Symbol", False),
                ("Dataset", False),
                ("Trades", True),
                ("Net P&L", True),
                ("Max DD %", True),
                ("Status", False),
            ],
            empty_title="NO SIMULATED BACKTEST RUNS EXECUTED YET",
            empty_description=(
                "Select an immutable Strategy Version and Historical Dataset above, then click "
                "'EXECUTE DETERMINISTIC BACKTEST' to generate reproducible simulated results."
            ),
        )
        return

    active_id = st.session_state.get("bt_active_run_id")
    run_ids = [str(r["id"]) for r in runs]
    default_run_idx = run_ids.index(active_id) if active_id in run_ids else 0

    sel_col, meta_col = st.columns([2.5, 2.5])
    with sel_col:
        chosen_idx = st.selectbox(
            "Inspect Persisted Backtest Run (SIMULATED BACKTEST RESULTS)",
            range(len(runs)),
            index=default_run_idx,
            format_func=lambda idx: (
                f"{runs[idx]['run_code']}  |  {runs[idx]['strategy_version_label']}  |  "
                f"{runs[idx]['status']}  |  Trades: {runs[idx]['total_trades']}  |  "
                f"Net P&L: {_fmt_metric_inr(runs[idx].get('net_pnl'), signed=True)}"
            ),
            key="bt_inspect_run_select",
        )
    chosen_run_id = str(runs[chosen_idx]["id"])
    st.session_state["bt_active_run_id"] = chosen_run_id

    detail_res = client.get_backtest_detail(chosen_run_id)
    if not detail_res.ok or not isinstance(detail_res.data, dict):
        render_api_offline_banner(
            detail_res.error or "Unable to fetch backtest run detail", client.base_url
        )
        return

    detail: dict[str, Any] = detail_res.data

    # 1. Mandatory Prominent Data Source + Simulation Banners
    render_data_source_banner(
        source_type=detail.get("dataset_source"),
        dataset_id=str(detail.get("dataset_id", "")),
    )
    _render_simulation_result_banner(detail)

    # 2. All 14 Summary Metrics + Capital/Cost Reconciliation
    _render_summary_metrics(detail.get("metrics") or {})

    # 3. Interactive Visualization & Audit Tabs
    tab_overview, tab_price, tab_ledger, tab_dq = st.tabs(
        [
            "EQUITY, DRAWDOWN & DISTRIBUTION",
            "ENTRY / EXIT PRICE CHART",
            "SIMULATED TRADE LEDGER",
            "DATA QUALITY & REPRODUCIBILITY",
        ]
    )

    with tab_overview:
        _render_equity_and_drawdown_charts(detail)
        _render_monthly_and_distribution_charts(detail)

    with tab_price:
        _render_entry_exit_chart(detail)

    with tab_ledger:
        _render_trade_ledger(detail.get("trades") or [])

    with tab_dq:
        _render_data_quality_and_reproducibility(detail)
