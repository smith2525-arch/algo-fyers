"""Screen 4: Market Replay — Live Simulated Market Stream & Strategy Explainability Desk.

Architectural Rule:
Streamlit owns ZERO replay state, indicator math, or signal logic. All normalized
`MarketEvent` / `CandleEvent` streaming, freshness tracking (`HEALTHY` / `STALE` /
`STOPPED`), EMA/RSI evaluation, and signal persistence execute in `core/` via FastAPI.
"""

from __future__ import annotations

import html
from typing import Any

import plotly.graph_objects as go
from plotly.subplots import make_subplots
import streamlit as st

from frontend.api_client import ControlCenterApiClient
from frontend.components.ui import (
    badge_html,
    render_api_offline_banner,
    render_data_source_banner,
    render_empty_blotter,
    render_screen_header,
)
from frontend.formatting import fmt_ist, pnl_css_class, to_decimal

SPEED_OPTIONS = ["1x", "5x", "10x", "25x", "50x"]


def _fmt_price(val: Any) -> str:
    if val is None or val == "":
        return "—"
    return f"{to_decimal(val):,.2f}"


def _render_pipeline_strip(
    *,
    session: dict[str, Any] | None,
    freshness: dict[str, Any],
    strat_eval: dict[str, Any],
    latest_signal: dict[str, Any] | None,
) -> None:
    """Visual end-to-end pipeline banner so a prospect grasps the flow at a glance."""
    rpl_status = str((session or {}).get("status", "STOPPED"))
    fresh_status = str(freshness.get("status", "STOPPED"))
    eval_status = str(strat_eval.get("evaluation_status", "STANDBY"))
    sig_txt = (
        f"{latest_signal['direction']} @ {_fmt_price(latest_signal.get('reference_price'))}"
        if latest_signal
        else "AWAITING CROSSOVER"
    )
    order_gate = (
        "NEW ORDERS PERMITTED"
        if freshness.get("new_orders_permitted")
        else "NEW ORDERS REFUSED (STALE / STOPPED)"
    )
    gate_badge = "HEALTHY" if freshness.get("new_orders_permitted") else (
        "STALE" if fresh_status == "STALE" else "STOPPED"
    )

    st.markdown(
        f"""
        <div class="desk-panel" style="padding:0.48rem 0.80rem; margin-bottom:0.55rem; background:#0D1526;">
            <div style="display:grid; grid-template-columns:repeat(5, minmax(0, 1fr)); gap:0.5rem; align-items:center;">
                <div style="border-right:1px solid #1E293B; padding-right:0.4rem;">
                    <div class="kpi-label">1. HISTORICAL FEED</div>
                    <div class="mono" style="font-size:0.76rem; color:#F8FAFC;">
                        {html.escape(str((session or {}).get("historical_session_date", "NOT STARTED")))}
                    </div>
                </div>
                <div style="border-right:1px solid #1E293B; padding-right:0.4rem;">
                    <div class="kpi-label">2. NORMALIZED STREAM</div>
                    <div class="mono" style="font-size:0.76rem;">
                        CandleEvent &bull; {badge_html(rpl_status)}
                    </div>
                </div>
                <div style="border-right:1px solid #1E293B; padding-right:0.4rem;">
                    <div class="kpi-label">3. DATA FRESHNESS GATE</div>
                    <div class="mono" style="font-size:0.74rem;">
                        {badge_html(fresh_status)} &bull; <span style="color:#CBD5E1;">{html.escape(order_gate)}</span>
                    </div>
                </div>
                <div style="border-right:1px solid #1E293B; padding-right:0.4rem;">
                    <div class="kpi-label">4. STRATEGY ENGINE</div>
                    <div class="mono" style="font-size:0.74rem;">
                        {badge_html(eval_status)}
                    </div>
                </div>
                <div>
                    <div class="kpi-label">5. LATEST SIGNAL</div>
                    <div class="mono" style="font-size:0.75rem; color:#FBBF24; font-weight:700;">
                        {html.escape(sig_txt)} &bull; {badge_html(gate_badge)}
                    </div>
                </div>
            </div>
        </div>
        """,
        unsafe_allow_html=True,
    )


def _render_kpi_strip(
    *,
    session: dict[str, Any] | None,
    freshness: dict[str, Any],
    last_candle: dict[str, Any] | None,
    strat_eval: dict[str, Any],
) -> None:
    s = session or {}
    market_clock = fmt_ist(s.get("replay_clock_ist")) if s.get("replay_clock_ist") else "—"
    inst_lbl = f"{s.get('instrument_symbol', '—')} ({s.get('timeframe', '5m')})" if s else "—"
    status_lbl = str(s.get("status", "STOPPED"))
    speed_lbl = str(s.get("replay_speed", "1x")) if s else "—"
    done_c = int(s.get("candles_processed", 0))
    tot_c = int(s.get("total_session_candles", 0))
    pct_c = float(s.get("progress_pct", 0.0))
    candles_str = f"{done_c} / {tot_c} ({pct_c:.1f}%)" if tot_c > 0 else "0 / 0"

    fresh_status = str(freshness.get("status", "STOPPED"))
    age_s = freshness.get("market_data_age_seconds")
    thresh_s = freshness.get("stale_threshold_seconds", 15)
    age_str = f"{age_s}s (max {thresh_s}s)" if age_s is not None else f"— ({fresh_status})"

    close_px = _fmt_price(last_candle.get("close")) if last_candle else "—"
    fast_ema = _fmt_price(strat_eval.get("fast_ema"))
    slow_ema = _fmt_price(strat_eval.get("slow_ema"))
    rsi_val = strat_eval.get("rsi")
    rsi_str = f"{to_decimal(rsi_val):.2f}" if rsi_val is not None else "—"

    cards = [
        ("Market Clock (IST)", market_clock, "num-flat", "REPLAY"),
        ("Current Instrument", inst_lbl, "num-flat", str(s.get("strategy_version_label", ""))[:18]),
        ("Replay Status & Speed", f"{status_lbl} @ {speed_lbl}", "num-pos" if status_lbl == "RUNNING" else "num-flat", ""),
        ("Candles Processed", candles_str, "num-flat", ""),
        ("Market-Data Age", f"{fresh_status} • {age_str}", "num-pos" if fresh_status == "HEALTHY" else ("num-neg" if fresh_status == "STALE" else "num-flat"), ""),
        ("Last Candle Close", close_px, "num-flat", ""),
        ("Fast / Slow EMA", f"{fast_ema} / {slow_ema}", pnl_css_class(strat_eval.get("ema_spread")), ""),
    ]

    cells_html = "".join(
        f'<div class="kpi-cell">'
        f'<div class="kpi-label"><span>{html.escape(lbl)}</span>'
        f'{f"<span class=kpi-demo-pill>{html.escape(pill)}</span>" if pill else ""}</div>'
        f'<div class="kpi-value {cls}" style="font-size:0.86rem;">{html.escape(val)}</div>'
        f"</div>"
        for lbl, val, cls, pill in cards
    )
    st.markdown(f'<div class="kpi-grid">{cells_html}</div>', unsafe_allow_html=True)

    # Secondary telemetry strip: RSI + Evaluation Status + Condition Headline
    eval_stat = str(strat_eval.get("evaluation_status", "STANDBY"))
    headline = html.escape(str(strat_eval.get("condition_headline", "")))
    l_thr = html.escape(str(strat_eval.get("long_rsi_threshold", "55.00")))
    s_thr = html.escape(str(strat_eval.get("short_rsi_threshold", "45.00")))
    st.markdown(
        f"""
        <div class="desk-panel" style="padding:0.44rem 0.80rem; margin-bottom:0.55rem;">
            <div style="display:flex; justify-content:space-between; align-items:center; flex-wrap:wrap; gap:0.5rem;">
                <div style="font-size:0.78rem; color:#F1F5F9;">
                    <span class="mono" style="color:#94A3B8;">LATEST STRATEGY CONDITION:</span>
                    &nbsp;{badge_html(eval_stat)}&nbsp;
                    <strong>{headline}</strong>
                </div>
                <div class="mono" style="font-size:0.75rem; color:#CBD5E1;">
                    WILDER RSI: <strong style="color:#38BDF8;">{html.escape(rsi_str)}</strong>
                    &nbsp;[Long &ge; {l_thr} | Short &le; {s_thr}]
                </div>
            </div>
        </div>
        """,
        unsafe_allow_html=True,
    )


def _render_three_card_explainability_deck(
    *,
    last_candle: dict[str, Any] | None,
    strat_eval: dict[str, Any],
    latest_signal: dict[str, Any] | None,
    freshness: dict[str, Any],
) -> None:
    c1, c2, c3 = st.columns([1.0, 1.25, 1.05])

    # Card 1: Last Received Normalized CandleEvent
    with c1:
        if not last_candle:
            render_empty_blotter(
                panel_title="Last Received CandleEvent",
                panel_meta="0 EVENTS RECEIVED",
                columns=[("Field", False), ("Value", True)],
                empty_title="NO CANDLE EVENT STREAMED YET",
                empty_description="Click START to emit normalized CandleEvents from the Historical Replay Feed.",
            )
        else:
            o_px = to_decimal(last_candle.get("open"))
            c_px = to_decimal(last_candle.get("close"))
            bar_chg = c_px - o_px
            chg_cls = pnl_css_class(bar_chg)
            st.markdown(
                f"""
                <div class="desk-panel">
                    <div class="desk-panel-header">
                        <span class="desk-panel-title">Last Received CandleEvent</span>
                        <span class="desk-panel-meta">SEQ #{int(last_candle.get("sequence_number", 0))} &bull; {html.escape(str(last_candle.get("source", "")))}</span>
                    </div>
                    <table class="blotter-table">
                        <tbody>
                            <tr><td>Candle Bar Time (IST)</td><td class="num mono">{html.escape(fmt_ist(last_candle.get("exchange_timestamp")))}</td></tr>
                            <tr><td>Stream Received (IST)</td><td class="num mono">{html.escape(fmt_ist(last_candle.get("received_at")))}</td></tr>
                            <tr><td>Open / High</td><td class="num">{_fmt_price(o_px)} &nbsp;/&nbsp; {_fmt_price(last_candle.get("high"))}</td></tr>
                            <tr><td>Low / Close</td><td class="num">{_fmt_price(last_candle.get("low"))} &nbsp;/&nbsp; <strong>{_fmt_price(c_px)}</strong></td></tr>
                            <tr><td>Candle Body Move</td><td class="num {chg_cls}">{bar_chg:+.2f} pts</td></tr>
                            <tr><td>Simulated Volume</td><td class="num">{int(last_candle.get("volume", 0)):,}</td></tr>
                        </tbody>
                    </table>
                </div>
                """,
                unsafe_allow_html=True,
            )

    # Card 2: Visual Strategy Rule Checklist
    with c2:
        checks: list[dict[str, Any]] = strat_eval.get("rule_checks") or []
        if not checks:
            render_empty_blotter(
                panel_title="Live Strategy Condition Checklist",
                panel_meta="STANDBY",
                columns=[("Condition Gate", False), ("State", False), ("Reading", True)],
                empty_title="STRATEGY ENGINE IN STANDBY",
                empty_description="Rule-by-rule EMA crossover, RSI confirmation, and IST session checks update on every candle.",
            )
        else:
            rows_html = "".join(
                f"<tr>"
                f"<td>{html.escape(str(chk.get('label', '')))}</td>"
                f"<td>{badge_html('PASS' if chk.get('passed') else 'STOPPED')}</td>"
                f'<td class="num mono">{html.escape(str(chk.get("detail", "")))}</td>'
                f"</tr>"
                for chk in checks
            )
            f_p = strat_eval.get("fast_ema_period", 9)
            s_p = strat_eval.get("slow_ema_period", 21)
            r_p = strat_eval.get("rsi_period", 14)
            st.markdown(
                f"""
                <div class="desk-panel">
                    <div class="desk-panel-header">
                        <span class="desk-panel-title">Strategy Rule Checklist (EMA {f_p}/{s_p} + RSI {r_p})</span>
                        <span class="desk-panel-meta">{html.escape(str(strat_eval.get("trend_regime", "—")))}</span>
                    </div>
                    <table class="blotter-table">
                        <thead><tr><th>Strategy Gate</th><th>Met</th><th class="num">Live Telemetry</th></tr></thead>
                        <tbody>{rows_html}</tbody>
                    </table>
                </div>
                """,
                unsafe_allow_html=True,
            )

    # Card 3: Latest Signal & Stale-Feed Safety Gate
    with c3:
        fresh_reason = html.escape(str(freshness.get("reason", "")))
        fresh_stat = str(freshness.get("status", "STOPPED"))
        if not latest_signal:
            st.markdown(
                f"""
                <div class="desk-panel">
                    <div class="desk-panel-header">
                        <span class="desk-panel-title">Latest Emitted Signal</span>
                        <span class="desk-panel-meta">0 SIGNALS IN SESSION</span>
                    </div>
                    <div class="empty-state-box" style="margin-top:0.35rem; padding:0.7rem;">
                        <div class="empty-state-title">AWAITING CONFIRMED SIGNAL</div>
                        <div class="empty-state-desc">
                            Signals emit only when Fast EMA crosses Slow EMA AND RSI confirms threshold inside the IST session.
                        </div>
                    </div>
                    <div style="margin-top:0.45rem; font-size:0.70rem; color:#94A3B8; border-top:1px solid #1E293B; padding-top:0.35rem;">
                        Freshness Policy: {badge_html(fresh_stat)} &mdash; {fresh_reason}
                    </div>
                </div>
                """,
                unsafe_allow_html=True,
            )
        else:
            sig_dir = str(latest_signal.get("direction", "ENTRY_LONG"))
            st.markdown(
                f"""
                <div class="desk-panel">
                    <div class="desk-panel-header">
                        <span class="desk-panel-title">Latest Emitted Signal</span>
                        <span class="desk-panel-meta">{html.escape(str(latest_signal.get("signal_code", "")))}</span>
                    </div>
                    <table class="blotter-table">
                        <tbody>
                            <tr><td>Signal Direction</td><td class="num">{badge_html(sig_dir)} &nbsp;{badge_html(str(latest_signal.get("side", "")))}</td></tr>
                            <tr><td>Signal Bar (IST)</td><td class="num mono">{html.escape(fmt_ist(latest_signal.get("bar_timestamp")))}</td></tr>
                            <tr><td>Reference Close</td><td class="num"><strong>{_fmt_price(latest_signal.get("reference_price"))}</strong></td></tr>
                            <tr><td>Lifecycle Status</td><td class="num">{badge_html(str(latest_signal.get("status", "GENERATED")))}</td></tr>
                        </tbody>
                    </table>
                    <div style="margin-top:0.38rem; font-size:0.71rem; color:#CBD5E1;">
                        {html.escape(str(latest_signal.get("rationale", "")))}
                    </div>
                    <div style="margin-top:0.38rem; font-size:0.69rem; color:#94A3B8; border-top:1px solid #1E293B; padding-top:0.30rem;">
                        Order Gate: {badge_html(fresh_stat)} &mdash; {fresh_reason}
                    </div>
                </div>
                """,
                unsafe_allow_html=True,
            )


def _render_live_replay_chart_and_signals(
    *,
    session: dict[str, Any] | None,
    chart_candles: list[dict[str, Any]],
    strat_eval: dict[str, Any],
    recent_signals: list[dict[str, Any]],
) -> None:
    c_chart, c_sigs = st.columns([1.45, 1.0])

    with c_chart:
        if not chart_candles:
            render_empty_blotter(
                panel_title="Live Replayed Market Stream (OHLC + Fast/Slow EMA + Wilder RSI)",
                panel_meta="STREAM IDLE",
                columns=[("Time (IST)", False), ("Open", True), ("High", True), ("Low", True), ("Close", True)],
                empty_title="REPLAY STREAM CHART READY",
                empty_description="Start or step a historical NIFTY / BANKNIFTY session to watch candles, EMAs, RSI, and signals build in real time.",
            )
        else:
            xs = [c["timestamp"] for c in chart_candles]
            fig = make_subplots(
                rows=2,
                cols=1,
                shared_xaxes=True,
                vertical_spacing=0.06,
                row_heights=[0.70, 0.30],
            )
            inst_sym = (session or {}).get("instrument_symbol", "INDEX")
            tf_lbl = (session or {}).get("timeframe", "5m")

            fig.add_trace(
                go.Candlestick(
                    x=xs,
                    open=[float(c["open"]) for c in chart_candles],
                    high=[float(c["high"]) for c in chart_candles],
                    low=[float(c["low"]) for c in chart_candles],
                    close=[float(c["close"]) for c in chart_candles],
                    name=f"{inst_sym} {tf_lbl}",
                    increasing_line_color="#10B981",
                    decreasing_line_color="#EF4444",
                ),
                row=1,
                col=1,
            )

            fx = [c["timestamp"] for c in chart_candles if c.get("fast_ema") is not None]
            fy = [float(c["fast_ema"]) for c in chart_candles if c.get("fast_ema") is not None]
            sx = [c["timestamp"] for c in chart_candles if c.get("slow_ema") is not None]
            sy = [float(c["slow_ema"]) for c in chart_candles if c.get("slow_ema") is not None]

            if fx:
                fig.add_trace(
                    go.Scatter(
                        x=fx,
                        y=fy,
                        mode="lines",
                        name=f"Fast EMA ({strat_eval.get('fast_ema_period', 9)})",
                        line={"color": "#38BDF8", "width": 1.5},
                    ),
                    row=1,
                    col=1,
                )
            if sx:
                fig.add_trace(
                    go.Scatter(
                        x=sx,
                        y=sy,
                        mode="lines",
                        name=f"Slow EMA ({strat_eval.get('slow_ema_period', 21)})",
                        line={"color": "#F59E0B", "width": 1.5},
                    ),
                    row=1,
                    col=1,
                )

            visible_ts = set(xs)
            long_sigs = [
                s for s in recent_signals if s.get("bar_timestamp") in visible_ts and "LONG" in str(s.get("direction"))
            ]
            short_sigs = [
                s for s in recent_signals if s.get("bar_timestamp") in visible_ts and "SHORT" in str(s.get("direction"))
            ]
            if long_sigs:
                fig.add_trace(
                    go.Scatter(
                        x=[s["bar_timestamp"] for s in long_sigs],
                        y=[float(s["reference_price"]) for s in long_sigs],
                        mode="markers",
                        name="LONG Signal",
                        marker={"symbol": "triangle-up", "size": 12, "color": "#22C55E"},
                    ),
                    row=1,
                    col=1,
                )
            if short_sigs:
                fig.add_trace(
                    go.Scatter(
                        x=[s["bar_timestamp"] for s in short_sigs],
                        y=[float(s["reference_price"]) for s in short_sigs],
                        mode="markers",
                        name="SHORT Signal",
                        marker={"symbol": "triangle-down", "size": 12, "color": "#F97316"},
                    ),
                    row=1,
                    col=1,
                )

            rx = [c["timestamp"] for c in chart_candles if c.get("rsi") is not None]
            ry = [float(c["rsi"]) for c in chart_candles if c.get("rsi") is not None]
            if rx:
                fig.add_trace(
                    go.Scatter(
                        x=rx,
                        y=ry,
                        mode="lines",
                        name=f"RSI ({strat_eval.get('rsi_period', 14)})",
                        line={"color": "#A78BFA", "width": 1.5},
                    ),
                    row=2,
                    col=1,
                )
            l_thr = float(to_decimal(strat_eval.get("long_rsi_threshold", "55.0")))
            s_thr = float(to_decimal(strat_eval.get("short_rsi_threshold", "45.0")))
            fig.add_hline(y=l_thr, line_dash="dot", line_color="#10B981", row=2, col=1)
            fig.add_hline(y=s_thr, line_dash="dot", line_color="#EF4444", row=2, col=1)

            fig.update_layout(
                title={
                    "text": f"SIMULATED MARKET REPLAY STREAM  •  {inst_sym} {tf_lbl} (IST)",
                    "font": {"size": 12, "color": "#CBD5E1", "family": "Consolas, monospace"},
                    "x": 0.01,
                },
                height=420,
                margin={"l": 50, "r": 18, "t": 34, "b": 28},
                paper_bgcolor="#101827",
                plot_bgcolor="#0A0F1A",
                font={"color": "#94A3B8", "size": 10, "family": "Consolas, monospace"},
                xaxis={"rangeslider": {"visible": False}, "gridcolor": "#1E293B"},
                xaxis2={"gridcolor": "#1E293B"},
                yaxis={"title": "Price", "gridcolor": "#1E293B"},
                yaxis2={"title": "RSI", "range": [0, 100], "gridcolor": "#1E293B"},
                legend={
                    "orientation": "h",
                    "yanchor": "bottom",
                    "y": 1.01,
                    "xanchor": "right",
                    "x": 1.0,
                    "bgcolor": "rgba(0,0,0,0)",
                },
            )
            st.plotly_chart(fig, use_container_width=True)

    with c_sigs:
        if not recent_signals:
            render_empty_blotter(
                panel_title="Replay Signal Dispatch Stream",
                panel_meta="0 SIGNALS IN CURRENT SESSION",
                columns=[
                    ("Bar Time (IST)", False),
                    ("Symbol", False),
                    ("Direction", False),
                    ("Ref Price", True),
                    ("Status", False),
                ],
                empty_title="NO REPLAY SIGNALS GENERATED YET",
                empty_description=(
                    "Normalized CandleEvents feed into the Strategy Engine; confirmed EMA + RSI signals "
                    "are deduplicated and persisted here."
                ),
            )
        else:
            sig_rows = "".join(
                f'<tr title="{html.escape(str(s.get("rationale", "")))}">'
                f'<td class="mono">{html.escape(fmt_ist(s.get("bar_timestamp")))}</td>'
                f'<td class="mono">{html.escape(str(s.get("instrument_symbol", "")))}</td>'
                f'<td>{badge_html(str(s.get("direction", "")))}</td>'
                f'<td class="num">{_fmt_price(s.get("reference_price"))}</td>'
                f'<td>{badge_html(str(s.get("status", "GENERATED")))}</td>'
                f"</tr>"
                for s in recent_signals
            )
            st.markdown(
                f"""
                <div class="desk-panel">
                    <div class="desk-panel-header">
                        <span class="desk-panel-title">Replay Signal Dispatch Stream</span>
                        <span class="desk-panel-meta">{len(recent_signals)} EMITTED SIGNAL(S)</span>
                    </div>
                    <table class="blotter-table">
                        <thead>
                            <tr>
                                <th>Bar Time (IST)</th>
                                <th>Symbol</th>
                                <th>Direction</th>
                                <th class="num">Ref Price</th>
                                <th>Status</th>
                            </tr>
                        </thead>
                        <tbody>{sig_rows}</tbody>
                    </table>
                </div>
                """,
                unsafe_allow_html=True,
            )


def render(client: ControlCenterApiClient) -> None:
    render_screen_header(
        "MARKET REPLAY",
        "SIMULATED MARKET EVENT STREAM  |  ANYTIME DESK DEMONSTRATION  |  IST (+05:30)",
    )

    state_res = client.get_replay_state(advance_due=True)
    if not state_res.ok or not isinstance(state_res.data, dict):
        render_api_offline_banner(
            state_res.error or "Unable to fetch Market Replay telemetry", client.base_url
        )
        return

    initial_state: dict[str, Any] = state_res.data
    cur_session: dict[str, Any] | None = initial_state.get("session")

    pre_rpl_inst = st.session_state.pop("preselected_replay_instrument", None)
    if pre_rpl_inst in ("NIFTY", "BANKNIFTY"):
        st.session_state["rpl_inst_sel"] = pre_rpl_inst
    pre_rpl_ds = st.session_state.pop("preselected_replay_dataset_id", None)
    if pre_rpl_ds:
        st.session_state["rpl_preferred_ds_id"] = pre_rpl_ds

    default_inst = str(cur_session.get("instrument_symbol", "NIFTY")) if cur_session else "NIFTY"

    # --- Top Configuration & Session Launch Deck ---
    s1, s2, s3 = st.columns([0.85, 1.35, 1.8])
    with s1:
        inst_options = ["NIFTY", "BANKNIFTY"]
        inst_idx = inst_options.index(default_inst) if default_inst in inst_options else 0
        sel_inst = st.selectbox("Instrument", inst_options, index=inst_idx, key="rpl_inst_sel")

    cat_res = client.get_replay_catalog(instrument=sel_inst)
    catalog: dict[str, Any] = (
        cat_res.data if (cat_res.ok and isinstance(cat_res.data, dict)) else {}
    )
    strat_versions: list[dict[str, Any]] = catalog.get("strategy_versions") or []
    all_cat_datasets: list[dict[str, Any]] = catalog.get("datasets") or []

    with s2:
        src_filter_options = [
            "ALL",
            "FYERS_HISTORICAL",
            "IMPORTED_CSV",
            "BUNDLED_CSV",
        ]
        src_filter_labels = {
            "ALL": "All Data Sources",
            "FYERS_HISTORICAL": "Real Historical (FYERS)",
            "IMPORTED_CSV": "User-Uploaded CSV",
            "BUNDLED_CSV": "Synthetic Demo Data",
        }
        sel_src_filter = st.selectbox(
            "Data Source",
            src_filter_options,
            format_func=lambda k: src_filter_labels.get(k, k),
            key="rpl_src_filter",
        )

    with s3:
        if strat_versions:
            ver_idx = st.selectbox(
                "Strategy Version",
                range(len(strat_versions)),
                format_func=lambda idx: f"{strat_versions[idx]['version_label']} ({strat_versions[idx]['timeframe']})",
                key=f"rpl_ver_{sel_inst}",
            )
            chosen_ver = strat_versions[ver_idx]
        else:
            st.selectbox("Strategy Version", ["No versions found"], disabled=True)
            chosen_ver = None

    target_tf = str(chosen_ver.get("timeframe", "5m")) if chosen_ver else None
    datasets = [
        d
        for d in all_cat_datasets
        if (sel_src_filter == "ALL" or str(d.get("source_type")) == sel_src_filter)
        and (target_tf is None or str(d.get("timeframe")) == target_tf)
    ]
    if not datasets and sel_src_filter == "ALL":
        datasets = all_cat_datasets

    d1, d2, d3 = st.columns([2.2, 1.9, 0.85])
    with d1:
        if datasets:
            pref_ds_id = st.session_state.get("rpl_preferred_ds_id") or (
                str(cur_session.get("dataset_id")) if cur_session else None
            )
            ds_ids = [str(d["dataset_id"]) for d in datasets]
            default_ds_idx = ds_ids.index(pref_ds_id) if pref_ds_id in ds_ids else 0
            ds_idx = st.selectbox(
                "Historical Dataset",
                range(len(datasets)),
                index=default_ds_idx,
                format_func=lambda idx: (
                    f"[{datasets[idx].get('provider', 'SYNTHETIC')} • {datasets[idx].get('quality_status', 'VALID')}] "
                    f"{datasets[idx]['name']} ({datasets[idx]['row_count']} bars)"
                ),
                key=f"rpl_ds_sel_{sel_inst}_{sel_src_filter}_{target_tf or 'any'}",
            )
            chosen_ds = datasets[ds_idx]
        else:
            st.selectbox(
                "Historical Dataset",
                ["No matching dataset — fetch FYERS data in Historical Data screen"],
                disabled=True,
                key="rpl_ds_empty",
            )
            chosen_ds = None

    hist_sessions: list[dict[str, Any]] = (
        chosen_ds.get("historical_sessions", []) if chosen_ds else []
    )

    with d2:
        if hist_sessions:
            # Default to second entry (Day 2 with prior warmup) if available for instant warm demo!
            default_sess_idx = 2 if len(hist_sessions) > 2 else 0
            sess_idx = st.selectbox(
                "Historical Session (IST)",
                range(len(hist_sessions)),
                index=default_sess_idx,
                format_func=lambda idx: hist_sessions[idx]["label"],
                key=f"rpl_sess_{sel_inst}_{chosen_ds['dataset_id'] if chosen_ds else 'none'}",
            )
            chosen_hist_session = hist_sessions[sess_idx]["session_key"]
        else:
            st.selectbox("Historical Session (IST)", ["No historical dataset"], disabled=True)
            chosen_hist_session = "ALL_SESSIONS"

    cur_speed = str(cur_session.get("replay_speed", "5x")) if cur_session else "5x"
    speed_idx = SPEED_OPTIONS.index(cur_speed) if cur_speed in SPEED_OPTIONS else 1
    with d3:
        sel_speed = st.selectbox("Replay Speed", SPEED_OPTIONS, index=speed_idx, key="rpl_speed_sel")

    # Prominent Data-Source & Simulated-Execution Classification Banner
    banner_src = (
        cur_session.get("dataset_source")
        if (cur_session and cur_session.get("status") in ("RUNNING", "PAUSED", "PAUSED_RECOVERY"))
        else (chosen_ds.get("source_type") if chosen_ds else "BUNDLED_CSV")
    )
    banner_prov = (
        cur_session.get("dataset_provider")
        if (cur_session and cur_session.get("status") in ("RUNNING", "PAUSED", "PAUSED_RECOVERY"))
        else (chosen_ds.get("provider") if chosen_ds else "SYNTHETIC")
    )
    banner_ds_id = (
        str(cur_session.get("dataset_id"))
        if (cur_session and cur_session.get("status") in ("RUNNING", "PAUSED", "PAUSED_RECOVERY"))
        else (str(chosen_ds.get("dataset_id")) if chosen_ds else None)
    )
    banner_qual = chosen_ds.get("quality_status") if chosen_ds else "VALID"
    render_data_source_banner(
        source_type=banner_src,
        provider=banner_prov,
        dataset_id=banner_ds_id,
        quality_status=banner_qual,
    )

    # Apply live speed update if operator changed dropdown on an active/paused session
    if (
        cur_session is not None
        and cur_session.get("status") in ("RUNNING", "PAUSED", "CONFIGURED")
        and sel_speed != cur_session.get("replay_speed")
    ):
        spd_res = client.update_replay_speed(str(cur_session["id"]), replay_speed=sel_speed)
        if spd_res.ok:
            st.rerun()

    # --- Transport Controls Row: START | PAUSE | RESUME | STOP | RESET | +1 CANDLE ---
    active_status = str(cur_session.get("status", "NONE")) if cur_session else "NONE"
    active_id = str(cur_session["id"]) if cur_session else None

    b1, b2, b3, b4, b5, b6 = st.columns(6)
    with b1:
        start_clicked = st.button(
            "START",
            type="primary",
            use_container_width=True,
            disabled=(chosen_ver is None or chosen_ds is None),
            key="rpl_btn_start",
        )
    with b2:
        pause_clicked = st.button(
            "PAUSE",
            use_container_width=True,
            disabled=(active_status != "RUNNING"),
            key="rpl_btn_pause",
        )
    with b3:
        resume_clicked = st.button(
            "RESUME",
            use_container_width=True,
            disabled=(active_status not in ("PAUSED", "PAUSED_RECOVERY")),
            key="rpl_btn_resume",
        )
    with b4:
        stop_clicked = st.button(
            "STOP",
            use_container_width=True,
            disabled=(active_status not in ("RUNNING", "PAUSED", "PAUSED_RECOVERY", "CONFIGURED")),
            key="rpl_btn_stop",
        )
    with b5:
        reset_clicked = st.button(
            "RESET",
            use_container_width=True,
            disabled=(active_id is None),
            key="rpl_btn_reset",
        )
    with b6:
        step_clicked = st.button(
            "+1 CANDLE",
            use_container_width=True,
            disabled=(
                active_id is None
                or active_status in ("STOPPED", "COMPLETED")
            ),
            key="rpl_btn_step",
        )

    if start_clicked and chosen_ver is not None and chosen_ds is not None:
        res = client.start_replay(
            instrument=sel_inst,
            strategy_version_id=str(chosen_ver["id"]),
            dataset_id=str(chosen_ds["dataset_id"]),
            historical_session_date=chosen_hist_session,
            replay_speed=sel_speed,
        )
        if res.ok:
            st.rerun()
        else:
            st.error(res.error or "Failed to start Market Replay.")

    if pause_clicked and active_id:
        res = client.pause_replay(active_id)
        if res.ok:
            st.rerun()
        else:
            st.error(res.error or "Failed to pause Market Replay.")

    if resume_clicked and active_id:
        res = client.resume_replay(active_id)
        if res.ok:
            st.rerun()
        else:
            st.error(res.error or "Failed to resume Market Replay.")

    if stop_clicked and active_id:
        res = client.stop_replay(active_id)
        if res.ok:
            st.rerun()
        else:
            st.error(res.error or "Failed to stop Market Replay.")

    if reset_clicked and active_id:
        res = client.reset_replay(active_id)
        if res.ok:
            st.rerun()
        else:
            st.error(res.error or "Failed to reset Market Replay.")

    if step_clicked and active_id:
        res = client.step_replay(active_id, bars=1)
        if res.ok:
            st.rerun()
        else:
            st.error(res.error or "Failed to step Market Replay.")

    # --- Live Telemetry Fragment (Auto-polls every 1s in browser while RUNNING) ---
    poll_interval = 1.0 if active_status == "RUNNING" else None

    @st.fragment(run_every=poll_interval)
    def _live_replay_monitor() -> None:
        live_res = client.get_replay_state(advance_due=True)
        payload = (
            live_res.data
            if (live_res.ok and isinstance(live_res.data, dict))
            else initial_state
        )
        sess = payload.get("session")
        fresh = payload.get("freshness") or {}
        last_c = payload.get("last_candle")
        s_eval = payload.get("strategy_evaluation") or {}
        lat_sig = payload.get("latest_signal")
        rec_sigs = payload.get("recent_signals") or []
        c_bars = payload.get("chart_candles") or []

        _render_pipeline_strip(
            session=sess,
            freshness=fresh,
            strat_eval=s_eval,
            latest_signal=lat_sig,
        )
        _render_kpi_strip(
            session=sess,
            freshness=fresh,
            last_candle=last_c,
            strat_eval=s_eval,
        )
        _render_three_card_explainability_deck(
            last_candle=last_c,
            strat_eval=s_eval,
            latest_signal=lat_sig,
            freshness=fresh,
        )
        _render_live_replay_chart_and_signals(
            session=sess,
            chart_candles=c_bars,
            strat_eval=s_eval,
            recent_signals=rec_sigs,
        )

    _live_replay_monitor()
