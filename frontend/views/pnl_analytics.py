"""Screen 9: P&L Analytics — Simulated Paper-Trading Attribution, Filters & 5 Visual Charts.

CRITICAL ARCHITECTURAL RULE:
Streamlit owns ZERO P&L calculation logic. All Gross P&L, Charges, Slippage Cost,
Realized/Unrealized/Net P&L, and chart series come from FastAPI (`/api/v1/pnl/analytics`).
"""

from __future__ import annotations

import html
from typing import Any

import plotly.graph_objects as go
import streamlit as st

from frontend.api_client import ControlCenterApiClient
from frontend.components.ui import (
    badge_html,
    render_api_offline_banner,
    render_empty_blotter,
    render_screen_header,
)
from frontend.formatting import fmt_inr, fmt_ist, pnl_css_class, to_decimal


def _dark_chart_layout(fig: go.Figure, *, title: str, height: int = 250, y_title: str = "₹") -> go.Figure:
    fig.update_layout(
        title={"text": title, "font": {"size": 12, "color": "#E2E8F0"}},
        height=height,
        margin={"l": 48, "r": 18, "t": 36, "b": 34},
        paper_bgcolor="#0B1120",
        plot_bgcolor="#0F172A",
        font={"family": "Consolas, monospace", "size": 10, "color": "#CBD5E1"},
        xaxis={"gridcolor": "#1E293B", "zerolinecolor": "#334155"},
        yaxis={"title": y_title, "gridcolor": "#1E293B", "zerolinecolor": "#334155"},
        showlegend=False,
    )
    return fig


def render(client: ControlCenterApiClient) -> None:
    render_screen_header(
        "P&L ANALYTICS",
        "SIMULATED PAPER-TRADING RESULTS  |  ATTRIBUTION, DRAWDOWN & COST LEDGER  |  IST (+05:30)",
    )

    # Initial unfiltered call to populate dynamic strategy & exit_reason dropdowns
    base_res = client.get_pnl_analytics()
    if not base_res.ok or not isinstance(base_res.data, dict):
        render_api_offline_banner(
            base_res.error or "Unable to fetch P&L analytics", client.base_url
        )
        return

    opts: dict[str, Any] = base_res.data.get("filter_options") or {}
    strat_opts = ["ALL"] + list(opts.get("strategies") or [])
    inst_opts = ["ALL"] + list(opts.get("instruments") or ["NIFTY", "BANKNIFTY"])
    dir_opts = ["ALL", "LONG", "SHORT"]
    exit_opts = ["ALL"] + list(opts.get("exit_reasons") or [])
    outcome_opts = ["ALL", "WINNERS", "LOSERS"]

    # Prominent Simulation Disclaimer Banner
    st.markdown(
        """
        <div class="desk-panel" style="padding:0.44rem 0.80rem; margin-bottom:0.55rem; background:#0D1526; border-left:3px solid #FBBF24;">
            <div style="display:flex; justify-content:space-between; align-items:center; flex-wrap:wrap; gap:0.4rem;">
                <span class="mono" style="font-size:0.76rem; color:#FBBF24; font-weight:700;">
                    SIMULATED PAPER-TRADING RESULTS &mdash; DEMONSTRATION CAPITAL ONLY
                </span>
                <span style="font-size:0.72rem; color:#94A3B8;">
                    Accounting Identity: Gross P&amp;L &minus; Charges = Realized + Unrealized = Net P&amp;L
                </span>
            </div>
        </div>
        """,
        unsafe_allow_html=True,
    )

    # 6-Dimension Filter Bar
    f1, f2, f3, f4, f5, f6 = st.columns([1.25, 1.0, 0.95, 1.15, 1.0, 1.25])
    with f1:
        sel_strat = st.selectbox("Strategy", strat_opts, key="pnl_f_strat")
    with f2:
        sel_inst = st.selectbox("Instrument", inst_opts, key="pnl_f_inst")
    with f3:
        sel_dir = st.selectbox("Direction", dir_opts, key="pnl_f_dir")
    with f4:
        sel_exit = st.selectbox("Exit Reason", exit_opts, key="pnl_f_exit")
    with f5:
        sel_outcome = st.selectbox("Winning / Losing", outcome_opts, key="pnl_f_out")
    with f6:
        date_txt = st.text_input(
            "Date Filter (YYYY-MM-DD)", placeholder="Optional IST date...", key="pnl_f_date"
        )

    clean_date = date_txt.strip() if len(date_txt.strip()) == 10 else None
    has_filters = any(
        [
            sel_strat != "ALL",
            sel_inst != "ALL",
            sel_dir != "ALL",
            sel_exit != "ALL",
            sel_outcome != "ALL",
            clean_date is not None,
        ]
    )
    if has_filters:
        f_res = client.get_pnl_analytics(
            start_date=clean_date,
            end_date=clean_date,
            strategy=sel_strat,
            instrument=sel_inst,
            direction=sel_dir,
            exit_reason=sel_exit,
            outcome=sel_outcome,
        )
        data: dict[str, Any] = f_res.data if (f_res.ok and isinstance(f_res.data, dict)) else base_res.data
    else:
        data = base_res.data

    summary: dict[str, Any] = data.get("summary") or {}
    charts: dict[str, Any] = data.get("charts") or {}
    trades: list[dict[str, Any]] = data.get("trades") or []

    gross = summary.get("gross_pnl", "0.00")
    charges = summary.get("charges", "0.00")
    slippage = summary.get("slippage_cost", "0.00")
    realized = summary.get("realized_pnl", "0.00")
    unrealized = summary.get("unrealized_pnl", "0.00")
    net_pnl = summary.get("net_pnl", "0.00")
    max_dd = summary.get("max_drawdown_inr", "0.00")

    kpis = [
        ("Gross P&L", fmt_inr(gross, signed=True), pnl_css_class(gross)),
        ("Slippage Cost", fmt_inr(slippage), "num-neg" if to_decimal(slippage) > 0 else "num-flat"),
        ("Simulated Charges", fmt_inr(charges), "num-neg" if to_decimal(charges) > 0 else "num-flat"),
        ("Realized P&L", fmt_inr(realized, signed=True), pnl_css_class(realized)),
        ("Unrealized P&L", fmt_inr(unrealized, signed=True), pnl_css_class(unrealized)),
        ("Net P&L", fmt_inr(net_pnl, signed=True), pnl_css_class(net_pnl)),
        ("Max Drawdown", fmt_inr(max_dd), "num-neg" if to_decimal(max_dd) > 0 else "num-flat"),
    ]
    st.markdown(
        '<div class="kpi-grid">'
        + "".join(
            f'<div class="kpi-cell"><div class="kpi-label"><span>{html.escape(k)}</span></div>'
            f'<div class="kpi-value {c}" style="font-size:0.84rem;">{html.escape(v)}</div></div>'
            for k, v, c in kpis
        )
        + "</div>",
        unsafe_allow_html=True,
    )

    if not trades:
        render_empty_blotter(
            panel_title="Simulated Paper-Trading P&L & Attribution Ledger",
            panel_meta="0 MATCHING POSITIONS",
            columns=[
                ("Timestamp (IST)", False),
                ("Position", False),
                ("Strategy", False),
                ("Instrument", False),
                ("Side", False),
                ("Gross P&L", True),
                ("Slippage", True),
                ("Charges", True),
                ("Realized", True),
                ("Unrealized", True),
                ("Net P&L", True),
            ],
            empty_title="NO SIMULATED PAPER TRADES MATCH CURRENT FILTERS",
            empty_description=(
                "SIMULATED PAPER-TRADING RESULTS: Zero matching positions found. Run Market Replay "
                "with an active Paper Strategy Instance or submit a simulated order to populate P&L charts."
            ),
        )
        return

    # 5 Analytical Charts: Row 1 (Daily P&L, Cumulative P&L), Row 2 (Strategy, Instrument), Row 3 (Drawdown)
    c1, c2 = st.columns(2)
    with c1:
        daily = charts.get("daily_pnl") or []
        d_x = [r["date"] for r in daily]
        d_y = [float(to_decimal(r["net_pnl"])) for r in daily]
        d_colors = ["#10B981" if v >= 0 else "#EF4444" for v in d_y]
        fig_daily = go.Figure(go.Bar(x=d_x, y=d_y, marker_color=d_colors))
        st.plotly_chart(
            _dark_chart_layout(fig_daily, title="Daily Net P&L (SIMULATED PAPER-TRADING RESULTS)"),
            use_container_width=True,
        )

    with c2:
        cum = charts.get("cumulative_pnl") or []
        c_x = [fmt_ist(r["timestamp"]) for r in cum]
        c_y = [float(to_decimal(r["cumulative_pnl"])) for r in cum]
        fig_cum = go.Figure(
            go.Scatter(
                x=c_x,
                y=c_y,
                mode="lines+markers",
                line={"color": "#38BDF8", "width": 2},
                fill="tozeroy",
                fillcolor="rgba(56, 189, 248, 0.12)",
            )
        )
        st.plotly_chart(
            _dark_chart_layout(fig_cum, title="Cumulative Session P&L Curve (IST)"),
            use_container_width=True,
        )

    c3, c4 = st.columns(2)
    with c3:
        sc = charts.get("strategy_contribution") or []
        s_x = [r["strategy"] for r in sc]
        s_y = [float(to_decimal(r["net_pnl"])) for r in sc]
        s_colors = ["#10B981" if v >= 0 else "#EF4444" for v in s_y]
        fig_strat = go.Figure(go.Bar(x=s_x, y=s_y, marker_color=s_colors))
        st.plotly_chart(
            _dark_chart_layout(fig_strat, title="Strategy Net P&L Contribution"),
            use_container_width=True,
        )

    with c4:
        ic = charts.get("instrument_contribution") or []
        i_x = [r["instrument"] for r in ic]
        i_y = [float(to_decimal(r["net_pnl"])) for r in ic]
        i_colors = ["#10B981" if v >= 0 else "#EF4444" for v in i_y]
        fig_inst = go.Figure(go.Bar(x=i_x, y=i_y, marker_color=i_colors))
        st.plotly_chart(
            _dark_chart_layout(fig_inst, title="Instrument Net P&L Contribution (NIFTY vs BANKNIFTY)"),
            use_container_width=True,
        )

    dd = charts.get("drawdown") or []
    dd_x = [fmt_ist(r["timestamp"]) for r in dd]
    dd_y = [float(to_decimal(r["drawdown_inr"])) for r in dd]
    fig_dd = go.Figure(
        go.Scatter(
            x=dd_x,
            y=dd_y,
            mode="lines",
            line={"color": "#EF4444", "width": 2},
            fill="tozeroy",
            fillcolor="rgba(239, 68, 68, 0.16)",
        )
    )
    st.plotly_chart(
        _dark_chart_layout(fig_dd, title="Underwater Drawdown Curve (₹)", height=210),
        use_container_width=True,
    )

    # Filtered Position / Trade Attribution Table
    tr_rows = "".join(
        f"<tr>"
        f'<td class="mono">{html.escape(fmt_ist(t.get("timestamp")))}</td>'
        f'<td class="mono">{html.escape(str(t.get("position_code", "")))}</td>'
        f'<td class="mono">{html.escape(str(t.get("strategy", "")))}</td>'
        f'<td class="mono">{html.escape(str(t.get("instrument", "")))}</td>'
        f'<td>{badge_html(str(t.get("direction", "")))}</td>'
        f'<td class="mono">{html.escape(str(t.get("exit_reason", "")))}</td>'
        f'<td class="num {pnl_css_class(t.get("gross_pnl"))}">{html.escape(fmt_inr(t.get("gross_pnl"), signed=True))}</td>'
        f'<td class="num">{html.escape(fmt_inr(t.get("slippage_cost")))}</td>'
        f'<td class="num">{html.escape(fmt_inr(t.get("charges")))}</td>'
        f'<td class="num {pnl_css_class(t.get("realized_pnl"))}">{html.escape(fmt_inr(t.get("realized_pnl"), signed=True))}</td>'
        f'<td class="num {pnl_css_class(t.get("unrealized_pnl"))}">{html.escape(fmt_inr(t.get("unrealized_pnl"), signed=True))}</td>'
        f'<td class="num {pnl_css_class(t.get("net_pnl"))}"><strong>{html.escape(fmt_inr(t.get("net_pnl"), signed=True))}</strong></td>'
        f'<td class="mono">{html.escape(str(t.get("correlation_id") or "—")[:12])}</td>'
        f"</tr>"
        for t in trades
    )
    st.markdown(
        f"""
        <div class="desk-panel">
            <div class="desk-panel-header">
                <span class="desk-panel-title">Filtered Paper-Trading P&amp;L Attribution Ledger (SIMULATED PAPER-TRADING RESULTS)</span>
                <span class="desk-panel-meta">{len(trades)} MATCHED POSITION(S)</span>
            </div>
            <table class="blotter-table">
                <thead>
                    <tr>
                        <th>Time (IST)</th>
                        <th>Position</th>
                        <th>Strategy</th>
                        <th>Symbol</th>
                        <th>Dir</th>
                        <th>Exit Reason</th>
                        <th class="num">Gross P&amp;L</th>
                        <th class="num">Slippage</th>
                        <th class="num">Charges</th>
                        <th class="num">Realized</th>
                        <th class="num">Unrealized</th>
                        <th class="num">Net P&amp;L</th>
                        <th>Correlation ID</th>
                    </tr>
                </thead>
                <tbody>{tr_rows}</tbody>
            </table>
        </div>
        """,
        unsafe_allow_html=True,
    )
