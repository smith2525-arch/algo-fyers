"""Screen 5: Paper Trading — Strategy Instances, Signal->OMS->Broker Pipeline & Execution Desk.

CRITICAL ARCHITECTURAL RULE:
Streamlit owns ZERO trading, risk, OMS, broker, or position logic. Every action calls
FastAPI endpoints backed by PostgreSQL (`SIMULATED PAPER TRADING` — NO REAL ORDERS).
"""

from __future__ import annotations

import html
import uuid
from typing import Any

import streamlit as st

from frontend.api_client import ControlCenterApiClient
from frontend.components.ui import (
    badge_html,
    render_api_offline_banner,
    render_empty_blotter,
    render_screen_header,
)
from frontend.formatting import fmt_inr, fmt_ist, pnl_css_class, to_decimal


def _fmt_px(val: Any) -> str:
    if val is None or val == "":
        return "—"
    return f"{to_decimal(val):,.2f}"


def render(client: ControlCenterApiClient) -> None:
    render_screen_header(
        "PAPER TRADING",
        "SIMULATED PAPER TRADING  |  PAPER_ONLY  |  ZERO REAL BROKER ROUTING  |  IST (+05:30)",
    )

    ws_res = client.get_paper_trading_workspace(advance_due=True)
    if not ws_res.ok or not isinstance(ws_res.data, dict):
        render_api_offline_banner(
            ws_res.error or "Unable to load Paper Trading workspace", client.base_url
        )
        return

    ws: dict[str, Any] = ws_res.data
    acct: dict[str, Any] = ws.get("account") or {}
    risk: dict[str, Any] = ws.get("risk_status") or {}
    rpl: dict[str, Any] = ws.get("market_replay") or {}
    fresh: dict[str, Any] = rpl.get("freshness") or {}
    instances: list[dict[str, Any]] = ws.get("strategy_instances") or []
    signals: list[dict[str, Any]] = ws.get("recent_signals") or []
    fills: list[dict[str, Any]] = ws.get("recent_fills") or []
    versions: list[dict[str, Any]] = ws.get("available_strategy_versions") or []
    fill_model: dict[str, Any] = ws.get("fill_model") or {}

    halted = bool(acct.get("paper_trading_halted"))
    fresh_status = str(fresh.get("status", "STOPPED"))
    rpl_status = str(rpl.get("status", "STOPPED"))
    running_inst_cnt = sum(1 for i in instances if i.get("status") == "RUNNING")

    # 1. Pipeline Architecture Strip
    st.markdown(
        f"""
        <div class="desk-panel" style="padding:0.46rem 0.80rem; margin-bottom:0.55rem; background:#0D1526;">
            <div style="display:flex; justify-content:space-between; align-items:center; flex-wrap:wrap; gap:0.45rem;">
                <div class="mono" style="font-size:0.74rem; color:#E2E8F0;">
                    <strong style="color:#FBBF24;">SIMULATED PAPER TRADING PIPELINE:</strong>
                    &nbsp;Market Event &rarr; Strategy &rarr; Signal &rarr; Risk Evaluation &rarr; OMS &rarr; Paper Broker &rarr; Execution &rarr; Position
                </div>
                <div>
                    Replay: {badge_html(rpl_status)} &nbsp;
                    Feed: {badge_html(fresh_status)} &nbsp;
                    Risk Gate: {badge_html("BLOCKED" if halted else ("HEALTHY" if risk.get("new_orders_permitted") else "STALE"))}
                </div>
            </div>
        </div>
        """,
        unsafe_allow_html=True,
    )

    # 2. Top KPI Strip
    realized = acct.get("realized_pnl_today", "0.00")
    unrealized = acct.get("unrealized_pnl", "0.00")
    kpi_cards = [
        ("Available Paper Capital", fmt_inr(acct.get("available_capital", "500000")), "num-flat", "DEMO"),
        ("Session Realized P&L", fmt_inr(realized, signed=True), pnl_css_class(realized), ""),
        ("Open Unrealized P&L", fmt_inr(unrealized, signed=True), pnl_css_class(unrealized), ""),
        ("Running Strategy Instances", str(running_inst_cnt), "num-pos" if running_inst_cnt > 0 else "num-flat", ""),
        ("Market Replay Status", f"{rpl_status} ({rpl.get('replay_speed', '1x')})", "num-pos" if rpl_status == "RUNNING" else "num-flat", fresh_status),
        ("Pre-Trade Risk Status", "HALTED" if halted else "ARMED", "num-neg" if halted else "num-pos", ""),
        ("Recent Executions", str(len(fills)), "num-flat", "SIMULATED"),
    ]
    kpi_html = "".join(
        f'<div class="kpi-cell">'
        f'<div class="kpi-label"><span>{html.escape(lbl)}</span>'
        f'{f"<span class=kpi-demo-pill>{html.escape(pill)}</span>" if pill else ""}</div>'
        f'<div class="kpi-value {cls}" style="font-size:0.86rem;">{html.escape(val)}</div>'
        f"</div>"
        for lbl, val, cls, pill in kpi_cards
    )
    st.markdown(f'<div class="kpi-grid">{kpi_html}</div>', unsafe_allow_html=True)

    # 3. Launch Strategy Instance & Desk Controls
    c_inst, c_ord = st.columns([1.15, 1.0])
    with c_inst:
        with st.expander("Launch Paper Strategy Instance (Automated Signal -> OMS -> Paper Broker)", expanded=not bool(instances)):
            if not versions:
                st.info("No immutable strategy versions available.")
            else:
                p1, p2, p3 = st.columns([1.8, 1.0, 1.0])
                with p1:
                    ver_idx = st.selectbox(
                        "Strategy Version",
                        range(len(versions)),
                        format_func=lambda idx: f"{versions[idx]['version_label']} ({versions[idx]['instrument_symbol']} • {versions[idx]['timeframe']})",
                        key="pt_ver_sel",
                    )
                    sel_ver = versions[ver_idx]
                with p2:
                    inst_ord_type = st.selectbox(
                        "Order Type", ["MARKET", "LIMIT", "STOP"], key="pt_inst_ord_type"
                    )
                with p3:
                    inst_lat_bars = st.number_input(
                        "Latency (Bars)", min_value=1, max_value=10, value=1, step=1, key="pt_lat_bars"
                    )

                p4, p5, p6 = st.columns(3)
                with p4:
                    inst_lat_ms = st.number_input(
                        "Broker Latency (ms)", min_value=0, max_value=5000, value=120, step=20, key="pt_lat_ms"
                    )
                with p5:
                    inst_slip = st.number_input(
                        "Slippage (bps)", min_value=0.0, max_value=50.0, value=2.0, step=0.5, key="pt_slip_bps"
                    )
                with p6:
                    inst_fill_ratio = st.selectbox(
                        "Max Fill / Bar",
                        [("100% (Full Fill)", "1.0"), ("50% (Partial Fills)", "0.5"), ("25% (Sliced Fills)", "0.25")],
                        format_func=lambda x: x[0],
                        key="pt_fill_ratio",
                    )

                if st.button("START PAPER STRATEGY INSTANCE", type="primary", use_container_width=True, key="pt_btn_start_inst"):
                    start_res = client.start_strategy_instance(
                        {
                            "strategy_version_id": sel_ver["id"],
                            "order_type": inst_ord_type,
                            "execution_latency_ms": int(inst_lat_ms),
                            "latency_bars": int(inst_lat_bars),
                            "slippage_bps": str(inst_slip),
                            "partial_fill_enabled": inst_fill_ratio[1] != "1.0",
                            "max_fill_ratio_per_bar": inst_fill_ratio[1],
                            "auto_execute_signals": True,
                            "actor": "desk.operator",
                        }
                    )
                    if start_res.ok:
                        st.rerun()
                    else:
                        st.error(start_res.error or "Failed to start Strategy Instance.")

    with c_ord:
        with st.expander("Manual Simulated Paper Order Ticket (MARKET / LIMIT / STOP)", expanded=False):
            m1, m2, m3, m4 = st.columns(4)
            with m1:
                m_sym = st.selectbox("Instrument", ["NIFTY", "BANKNIFTY"], key="pt_man_sym")
            with m2:
                m_side = st.selectbox("Side", ["BUY", "SELL"], key="pt_man_side")
            with m3:
                m_type = st.selectbox("Type", ["MARKET", "LIMIT", "STOP"], key="pt_man_type")
            with m4:
                default_qty = 75 if m_sym == "NIFTY" else 30
                m_qty = st.number_input("Quantity", min_value=1, max_value=1800, value=default_qty, step=15, key="pt_man_qty")

            m5, m6, m7 = st.columns(3)
            with m5:
                m_px = st.number_input(
                    "Limit / Stop Price",
                    min_value=0.0,
                    value=24200.0 if m_sym == "NIFTY" else 51500.0,
                    step=5.0,
                    key="pt_man_px",
                )
            with m6:
                m_ratio = st.selectbox("Fill Ratio / Bar", ["1.0", "0.5"], key="pt_man_ratio")
            with m7:
                m_tif = st.number_input("Valid Bars (0=GTC)", min_value=0, max_value=100, value=0, step=1, key="pt_man_tif")

            if st.button("SUBMIT SIMULATED PAPER ORDER", use_container_width=True, key="pt_btn_sub_ord"):
                payload: dict[str, Any] = {
                    "client_order_key": f"ui-ord-{uuid.uuid4().hex[:12]}",
                    "instrument_symbol": m_sym,
                    "side": m_side,
                    "order_type": m_type,
                    "quantity": int(m_qty),
                    "latency_bars": 1,
                    "max_fill_ratio_per_bar": m_ratio,
                    "actor": "desk.operator",
                }
                if m_type == "LIMIT":
                    payload["limit_price"] = str(m_px)
                elif m_type == "STOP":
                    payload["stop_price"] = str(m_px)
                if int(m_tif) > 0:
                    payload["valid_for_bars"] = int(m_tif)
                sub_res = client.submit_paper_order(payload)
                if sub_res.ok:
                    st.rerun()
                else:
                    st.error(sub_res.error or "Simulated order submission refused.")

    # 4. Running Strategy Instances Blotter + Instance Controls
    if not instances:
        render_empty_blotter(
            panel_title="Running Strategy Instances",
            panel_meta="0 STRATEGY INSTANCES",
            columns=[
                ("Instance Code", False),
                ("Strategy Version", False),
                ("Symbol", False),
                ("Order Model", False),
                ("Latency / Slip", False),
                ("Signals", True),
                ("Orders", True),
                ("Status", False),
            ],
            empty_title="NO PAPER STRATEGY INSTANCES STARTED",
            empty_description=(
                "Launch a Paper Strategy Instance above and run Market Replay to watch normalized "
                "CandleEvents drive Signals -> Risk -> OMS -> Paper Broker -> Positions automatically."
            ),
        )
    else:
        inst_rows = "".join(
            f"<tr>"
            f'<td class="mono">{html.escape(str(i.get("instance_code", "")))}</td>'
            f'<td class="mono">{html.escape(str(i.get("strategy_version_label", "")))}</td>'
            f'<td class="mono">{html.escape(str(i.get("instrument_symbol", "")))} ({html.escape(str(i.get("timeframe", "")))})</td>'
            f'<td class="mono">{html.escape(str(i.get("order_type", "")))} &bull; {html.escape(str(i.get("max_fill_ratio_per_bar", "1.00")))}x/bar</td>'
            f'<td class="mono">{int(i.get("latency_bars", 1))} bar / {int(i.get("execution_latency_ms", 0))}ms / {html.escape(str(i.get("slippage_bps", "0")))}bps</td>'
            f'<td class="num">{int(i.get("signals_emitted", 0))}</td>'
            f'<td class="num">{int(i.get("orders_created", 0))}</td>'
            f'<td>{badge_html(str(i.get("status", "")))}</td>'
            f"</tr>"
            for i in instances
        )
        st.markdown(
            f"""
            <div class="desk-panel">
                <div class="desk-panel-header">
                    <span class="desk-panel-title">Strategy Instances (SIMULATED PAPER TRADING)</span>
                    <span class="desk-panel-meta">{len(instances)} REGISTERED INSTANCE(S)</span>
                </div>
                <table class="blotter-table">
                    <thead>
                        <tr>
                            <th>Instance</th>
                            <th>Strategy Ver</th>
                            <th>Instrument</th>
                            <th>Order / Slice</th>
                            <th>Latency / Slippage</th>
                            <th class="num">Signals</th>
                            <th class="num">Orders</th>
                            <th>Status</th>
                        </tr>
                    </thead>
                    <tbody>{inst_rows}</tbody>
                </table>
            </div>
            """,
            unsafe_allow_html=True,
        )
        non_stopped = [i for i in instances if i.get("status") in ("RUNNING", "PAUSED")]
        if non_stopped:
            ic1, ic2, ic3, ic4 = st.columns([2.2, 1.0, 1.0, 1.0])
            with ic1:
                sel_inst_ctrl = st.selectbox(
                    "Manage Instance",
                    range(len(non_stopped)),
                    format_func=lambda idx: f"{non_stopped[idx]['instance_code']} ({non_stopped[idx]['status']})",
                    label_visibility="collapsed",
                    key="pt_manage_inst_sel",
                )
                target_inst = non_stopped[sel_inst_ctrl]
            with ic2:
                if st.button("PAUSE INSTANCE", disabled=(target_inst["status"] != "RUNNING"), use_container_width=True, key="pt_pause_inst"):
                    if client.pause_strategy_instance(str(target_inst["id"])).ok:
                        st.rerun()
            with ic3:
                if st.button("RESUME INSTANCE", disabled=(target_inst["status"] != "PAUSED"), use_container_width=True, key="pt_resume_inst"):
                    if client.resume_strategy_instance(str(target_inst["id"])).ok:
                        st.rerun()
            with ic4:
                if st.button("STOP INSTANCE", use_container_width=True, key="pt_stop_inst"):
                    if client.stop_strategy_instance(str(target_inst["id"])).ok:
                        st.rerun()

    # 5. Recent Signals & Execution Activity Side-by-Side
    s_col, f_col = st.columns([1.05, 1.05])
    with s_col:
        if not signals:
            render_empty_blotter(
                panel_title="Recent Signals",
                panel_meta="0 SIGNALS",
                columns=[
                    ("Bar Time (IST)", False),
                    ("Signal Code", False),
                    ("Symbol", False),
                    ("Direction", False),
                    ("Ref Px", True),
                    ("Status", False),
                ],
                empty_title="NO SIGNALS EMITTED YET",
                empty_description="Signals require unique IDs and transition GENERATED -> RISK_APPROVED -> ORDER_CREATED (or RISK_REJECTED).",
            )
        else:
            sig_rows = "".join(
                f"<tr>"
                f'<td class="mono">{html.escape(fmt_ist(s.get("bar_timestamp")))}</td>'
                f'<td class="mono" title="Reason: {html.escape(str(s.get("reason", "")))}">{html.escape(str(s.get("signal_code", "")))}</td>'
                f'<td class="mono">{html.escape(str(s.get("instrument_symbol", "")))}</td>'
                f'<td>{badge_html(str(s.get("direction", "")))}</td>'
                f'<td class="num">{_fmt_px(s.get("reference_price"))}</td>'
                f'<td>{badge_html(str(s.get("status", "")))}</td>'
                f"</tr>"
                for s in signals[:12]
            )
            st.markdown(
                f"""
                <div class="desk-panel">
                    <div class="desk-panel-header">
                        <span class="desk-panel-title">Recent Signals</span>
                        <span class="desk-panel-meta">DEDUPLICATED SIGNAL LEDGER</span>
                    </div>
                    <table class="blotter-table">
                        <thead>
                            <tr>
                                <th>Bar Time (IST)</th>
                                <th>Signal ID</th>
                                <th>Symbol</th>
                                <th>Direction</th>
                                <th class="num">Ref Px</th>
                                <th>Status</th>
                            </tr>
                        </thead>
                        <tbody>{sig_rows}</tbody>
                    </table>
                </div>
                """,
                unsafe_allow_html=True,
            )

    with f_col:
        if not fills:
            render_empty_blotter(
                panel_title="Simulated Execution Activity",
                panel_meta="0 FILLS",
                columns=[
                    ("Executed (IST)", False),
                    ("Fill Code", False),
                    ("Symbol", False),
                    ("Side", False),
                    ("Qty", True),
                    ("Fill Px", True),
                    ("Model", False),
                ],
                empty_title="NO SIMULATED EXECUTIONS RECORDED",
                empty_description=str(fill_model.get("limit_order", "Paper Broker fills appear here with slippage & charges.")),
            )
        else:
            fill_rows = "".join(
                f"<tr>"
                f'<td class="mono">{html.escape(fmt_ist(f.get("executed_at")))}</td>'
                f'<td class="mono">{html.escape(str(f.get("fill_code", "")))}</td>'
                f'<td class="mono">{html.escape(str(f.get("instrument_symbol", "")))}</td>'
                f'<td>{badge_html(str(f.get("side", "")))}</td>'
                f'<td class="num">{int(f.get("fill_quantity", 0))}</td>'
                f'<td class="num"><strong>{_fmt_px(f.get("fill_price"))}</strong></td>'
                f'<td class="mono">{html.escape(str(f.get("liquidity_note", "")))}</td>'
                f"</tr>"
                for f in fills[:12]
            )
            st.markdown(
                f"""
                <div class="desk-panel">
                    <div class="desk-panel-header">
                        <span class="desk-panel-title">Simulated Execution Activity</span>
                        <span class="desk-panel-meta">PAPER BROKER FILLS</span>
                    </div>
                    <table class="blotter-table">
                        <thead>
                            <tr>
                                <th>Executed (IST)</th>
                                <th>Fill ID</th>
                                <th>Symbol</th>
                                <th>Side</th>
                                <th class="num">Qty</th>
                                <th class="num">Fill Px</th>
                                <th>Fill Note</th>
                            </tr>
                        </thead>
                        <tbody>{fill_rows}</tbody>
                    </table>
                </div>
                """,
                unsafe_allow_html=True,
            )
