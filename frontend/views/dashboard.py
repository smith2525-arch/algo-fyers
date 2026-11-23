"""Screen 1: Primary Operational Dashboard."""

from __future__ import annotations

import html
from typing import Any

import streamlit as st

from frontend.api_client import ControlCenterApiClient
from frontend.components.ui import (
    badge_html,
    render_api_offline_banner,
    render_empty_blotter,
    render_screen_header,
    render_status_strip,
)
from frontend.formatting import fmt_inr, fmt_ist, pnl_css_class


def _render_kpi_row(kpis: dict[str, Any]) -> None:
    paper_cap = fmt_inr(kpis.get("paper_capital", "500000"))
    avail_cap = fmt_inr(kpis.get("available_capital", "500000"))
    realized = fmt_inr(kpis.get("todays_realized_pnl", "0"), signed=True)
    unrealized = fmt_inr(kpis.get("unrealized_pnl", "0"), signed=True)
    realized_cls = pnl_css_class(kpis.get("todays_realized_pnl", "0"))
    unrealized_cls = pnl_css_class(kpis.get("unrealized_pnl", "0"))
    open_pos = int(kpis.get("open_positions", 0))
    active_strats = int(kpis.get("active_strategies", 0))
    pending_orders = int(kpis.get("pending_orders", 0))

    st.markdown(
        f"""
        <div class="kpi-grid">
            <div class="kpi-cell">
                <div class="kpi-label">
                    <span>Paper Capital</span>
                    <span class="kpi-demo-pill">DEMO</span>
                </div>
                <div class="kpi-value">{html.escape(paper_cap)}</div>
            </div>
            <div class="kpi-cell">
                <div class="kpi-label">
                    <span>Available Capital</span>
                    <span class="kpi-demo-pill">PAPER</span>
                </div>
                <div class="kpi-value">{html.escape(avail_cap)}</div>
            </div>
            <div class="kpi-cell">
                <div class="kpi-label"><span>Today's Realized P&amp;L</span></div>
                <div class="kpi-value {realized_cls}">{html.escape(realized)}</div>
            </div>
            <div class="kpi-cell">
                <div class="kpi-label"><span>Unrealized P&amp;L</span></div>
                <div class="kpi-value {unrealized_cls}">{html.escape(unrealized)}</div>
            </div>
            <div class="kpi-cell">
                <div class="kpi-label"><span>Open Positions</span></div>
                <div class="kpi-value">{open_pos}</div>
            </div>
            <div class="kpi-cell">
                <div class="kpi-label"><span>Active Strategies</span></div>
                <div class="kpi-value">{active_strats}</div>
            </div>
            <div class="kpi-cell">
                <div class="kpi-label"><span>Pending Orders</span></div>
                <div class="kpi-value">{pending_orders}</div>
            </div>
        </div>
        """,
        unsafe_allow_html=True,
    )


def _render_stop_paper_trading_control(
    client: ControlCenterApiClient,
    account: dict[str, Any] | None,
    db_connected: bool,
) -> None:
    halted = bool(account and account.get("paper_trading_halted"))
    halt_reason = (account or {}).get("halt_reason") or ""
    halted_at = fmt_ist((account or {}).get("halted_at"))
    halted_by = (account or {}).get("halted_by") or "—"

    bar_cls = "control-bar halted" if halted else "control-bar"
    gate_badge = badge_html("BLOCKED" if halted else ("HEALTHY" if db_connected else "DEGRADED"))
    state_label = (
        "STOP PAPER TRADING ENGAGED — NEW ORDER GENERATION BLOCKED"
        if halted
        else "PAPER ORDER GATE ARMED — NEW SIMULATED ORDERS PERMITTED"
    )
    meta_detail = (
        f"Halted by {html.escape(str(halted_by))} at {html.escape(halted_at)} | Reason: {html.escape(str(halt_reason))}"
        if halted
        else "Safety Rule: Engaging STOP PAPER TRADING halts generation of NEW orders only. Open positions are NOT automatically flattened (flatten is a separate explicit command)."
    )

    st.markdown(
        f"""
        <div class="{bar_cls}">
            <div class="control-bar-title">{html.escape(state_label)} &nbsp;{gate_badge}</div>
            <div class="control-bar-note">{meta_detail}</div>
        </div>
        """,
        unsafe_allow_html=True,
    )

    c1, c2, c3 = st.columns([3.2, 1.3, 1.3])
    with c1:
        reason_input = st.text_input(
            "Operator Control Reason (recorded in immutable Audit Trail)",
            value="Operator manual desk halt" if not halted else "Operator verified desk readiness to resume",
            key="dashboard_halt_reason",
            label_visibility="collapsed",
            placeholder="Enter operator reason for audit log...",
        )
    with c2:
        stop_clicked = st.button(
            "STOP PAPER TRADING",
            type="primary",
            use_container_width=True,
            disabled=(not db_connected) or halted,
            help="Immediately block generation of all NEW paper orders. Does NOT flatten open positions.",
        )
    with c3:
        resume_clicked = st.button(
            "RESUME PAPER TRADING",
            type="secondary",
            use_container_width=True,
            disabled=(not db_connected) or (not halted),
            help="Re-enable generation of new paper orders.",
        )

    if stop_clicked:
        with st.spinner("Engaging STOP PAPER TRADING gate..."):
            res = client.stop_paper_trading(actor="desk.operator", reason=reason_input)
        if res.ok:
            st.toast("STOP PAPER TRADING engaged. New orders blocked; open positions untouched.")
            st.rerun()
        else:
            st.error(f"Failed to stop paper trading: {res.error}")

    if resume_clicked:
        with st.spinner("Resuming paper order gate..."):
            res = client.resume_paper_trading(actor="desk.operator", reason=reason_input)
        if res.ok:
            st.toast("Paper trading resumed.")
            st.rerun()
        else:
            st.error(f"Failed to resume paper trading: {res.error}")


def _render_risk_utilization_panel(risk_util: dict[str, Any], account: dict[str, Any] | None) -> None:
    gate = str(risk_util.get("new_orders_gate", "PERMITTED"))
    cap_pct = str(risk_util.get("capital_deployed_pct", "0.00"))
    loss_used = fmt_inr(risk_util.get("daily_loss_used_inr", "0.00"))
    acct_code = (account or {}).get("account_code", "PAPER-DESK-001")
    designation = (account or {}).get("account_designation", "DEMO_PAPER_ONLY")

    st.markdown(
        f"""
        <div class="desk-panel">
            <div class="desk-panel-header">
                <span class="desk-panel-title">Risk Utilization</span>
                <span class="desk-panel-meta">{html.escape(str(acct_code))} &bull; {html.escape(str(designation))}</span>
            </div>
            <table class="blotter-table">
                <thead>
                    <tr>
                        <th>Control / Metric</th>
                        <th>Status</th>
                        <th class="num">Current Value</th>
                    </tr>
                </thead>
                <tbody>
                    <tr>
                        <td>Pre-Trade New Order Gate</td>
                        <td>{badge_html("BLOCKED" if gate == "BLOCKED" else ("DEGRADED" if "DEGRADED" in gate else "HEALTHY"))}</td>
                        <td class="num">{html.escape(gate)}</td>
                    </tr>
                    <tr>
                        <td>Paper Capital Deployed</td>
                        <td>{badge_html("HEALTHY")}</td>
                        <td class="num">{html.escape(cap_pct)}%</td>
                    </tr>
                    <tr>
                        <td>Intraday Realized Loss Consumed</td>
                        <td>{badge_html("HEALTHY")}</td>
                        <td class="num">{html.escape(loss_used)}</td>
                    </tr>
                    <tr>
                        <td>Auto-Flatten on Stop Trading</td>
                        <td>{badge_html("STOPPED")}</td>
                        <td class="num">DISABLED (EXPLICIT ONLY)</td>
                    </tr>
                </tbody>
            </table>
        </div>
        """,
        unsafe_allow_html=True,
    )


def _render_recent_alerts_panel(alerts: list[dict[str, Any]]) -> None:
    if not alerts:
        render_empty_blotter(
            panel_title="Recent Alerts & Control Events",
            panel_meta="AUDIT STREAM",
            columns=[("Time (IST)", False), ("Severity", False), ("Action", False), ("Summary", False)],
            empty_title="NO OPERATIONAL ALERTS RECORDED",
            empty_description="Persistent control transitions, risk rejections, and health warnings appear here automatically.",
        )
        return

    rows_html: list[str] = []
    for ev in alerts[:6]:
        ts = html.escape(fmt_ist(ev.get("occurred_at")))
        sev = str(ev.get("severity", "INFO"))
        action = html.escape(str(ev.get("action", "")))
        summary = html.escape(str(ev.get("summary", "")))
        rows_html.append(
            f"<tr>"
            f'<td class="mono">{ts}</td>'
            f"<td>{badge_html(sev)}</td>"
            f'<td class="mono">{action}</td>'
            f"<td>{summary}</td>"
            f"</tr>"
        )

    st.markdown(
        f"""
        <div class="desk-panel">
            <div class="desk-panel-header">
                <span class="desk-panel-title">Recent Alerts &amp; Control Events</span>
                <span class="desk-panel-meta">LATEST {len(rows_html)} PERSISTED EVENTS</span>
            </div>
            <table class="blotter-table">
                <thead>
                    <tr>
                        <th>Time (IST)</th>
                        <th>Severity</th>
                        <th>Action</th>
                        <th>Summary</th>
                    </tr>
                </thead>
                <tbody>{"".join(rows_html)}</tbody>
            </table>
        </div>
        """,
        unsafe_allow_html=True,
    )


def render(client: ControlCenterApiClient) -> None:
    render_screen_header(
        "ALGO TRADING CONTROL CENTER",
        "DESK OVERVIEW  |  INSTRUMENTS: NIFTY, BANKNIFTY  |  TIMEZONE: IST (+05:30)",
    )

    resp = client.get_dashboard()
    if not resp.ok or not isinstance(resp.data, dict):
        render_api_offline_banner(resp.error or "Unable to fetch dashboard snapshot", client.base_url)
        fallback_components = [
            {"component": "MARKET_DATA", "status": "STOPPED", "message": "API offline"},
            {"component": "STRATEGY_ENGINE", "status": "STOPPED", "message": "API offline"},
            {"component": "RISK_ENGINE", "status": "DEGRADED", "message": "API offline"},
            {"component": "PAPER_BROKER", "status": "STOPPED", "message": "API offline"},
            {"component": "DATABASE", "status": "DEGRADED", "message": "API offline"},
        ]
        render_status_strip(fallback_components)
        return

    data = resp.data
    _render_kpi_row(data.get("kpis", {}))
    render_status_strip(data.get("system_status_strip", []))
    _render_stop_paper_trading_control(
        client,
        data.get("account"),
        bool(data.get("database_connected")),
    )

    col_left, col_right = st.columns([1.15, 1.0])

    with col_left:
        running_strats: list[dict[str, Any]] = data.get("running_strategies") or []
        if not running_strats:
            render_empty_blotter(
                panel_title="Running Strategies",
                panel_meta="0 ACTIVE RUNNERS",
                columns=[
                    ("Strategy", False),
                    ("Version", False),
                    ("Symbol", False),
                    ("Timeframe", False),
                    ("Status", False),
                ],
                empty_title="NO ACTIVE STRATEGY RUNNERS",
                empty_description=(
                    "EMA Crossover + RSI Confirmation (v1) is registered in Strategy Lab. "
                    "Automated signal generation activates during Market Replay sessions."
                ),
            )
        else:
            rs_rows = "".join(
                f"<tr>"
                f'<td class="mono">{html.escape(str(rs.get("strategy_version", "")))}</td>'
                f'<td class="mono">{html.escape(str(rs.get("instrument", "")))}</td>'
                f'<td class="mono">{html.escape(str(rs.get("timeframe", "")))}</td>'
                f'<td class="mono">{html.escape(str(rs.get("mode", "")))}</td>'
                f'<td>{badge_html(str(rs.get("market_data_freshness", "STOPPED")))}</td>'
                f'<td>{badge_html(str(rs.get("status", "STOPPED")))}</td>'
                f'<td class="num">{int(rs.get("signals_emitted", 0))}</td>'
                f"</tr>"
                for rs in running_strats
            )
            st.markdown(
                f"""
                <div class="desk-panel">
                    <div class="desk-panel-header">
                        <span class="desk-panel-title">Running Strategies</span>
                        <span class="desk-panel-meta">{len(running_strats)} SESSION RUNNER(S)</span>
                    </div>
                    <table class="blotter-table">
                        <thead>
                            <tr>
                                <th>Strategy Ver</th>
                                <th>Symbol</th>
                                <th>TF</th>
                                <th>Feed Mode</th>
                                <th>Data Freshness</th>
                                <th>Status</th>
                                <th class="num">Signals</th>
                            </tr>
                        </thead>
                        <tbody>{rs_rows}</tbody>
                    </table>
                </div>
                """,
                unsafe_allow_html=True,
            )
        open_positions_list: list[dict[str, Any]] = data.get("open_positions") or []
        if not open_positions_list:
            render_empty_blotter(
                panel_title="Open Positions",
                panel_meta="BOOK FLAT",
                columns=[
                    ("Symbol", False),
                    ("Side", False),
                    ("Net Qty", True),
                    ("Avg Price", True),
                    ("Mark Price", True),
                    ("Unrealized P&L", True),
                ],
                empty_title="NO OPEN PAPER POSITIONS",
                empty_description="Paper position book is completely flat across NIFTY and BANKNIFTY.",
            )
        else:
            pos_rows = "".join(
                f"<tr>"
                f'<td class="mono">{html.escape(str(p.get("instrument_symbol", "")))}</td>'
                f'<td>{badge_html(str(p.get("side", "")))}</td>'
                f'<td class="num">{int(p.get("quantity", 0))}</td>'
                f'<td class="num">{html.escape(str(p.get("average_entry_price", "0.00")))}</td>'
                f'<td class="num">{html.escape(str(p.get("last_price", "0.00")))}</td>'
                f'<td class="num {pnl_css_class(p.get("unrealized_pnl", "0"))}">{html.escape(fmt_inr(p.get("unrealized_pnl", "0"), signed=True))}</td>'
                f"</tr>"
                for p in open_positions_list
            )
            st.markdown(
                f"""
                <div class="desk-panel">
                    <div class="desk-panel-header">
                        <span class="desk-panel-title">Open Positions</span>
                        <span class="desk-panel-meta">{len(open_positions_list)} OPEN PAPER POSITION(S)</span>
                    </div>
                    <table class="blotter-table">
                        <thead>
                            <tr>
                                <th>Symbol</th>
                                <th>Side</th>
                                <th class="num">Net Qty</th>
                                <th class="num">Avg Price</th>
                                <th class="num">Mark Price</th>
                                <th class="num">Unrealized P&amp;L</th>
                            </tr>
                        </thead>
                        <tbody>{pos_rows}</tbody>
                    </table>
                </div>
                """,
                unsafe_allow_html=True,
            )

        recent_orders_list: list[dict[str, Any]] = data.get("recent_orders") or []
        if not recent_orders_list:
            render_empty_blotter(
                panel_title="Recent Orders",
                panel_meta="0 ORDERS TODAY",
                columns=[
                    ("Time (IST)", False),
                    ("Order ID", False),
                    ("Symbol", False),
                    ("Side", False),
                    ("Qty", True),
                    ("Status", False),
                ],
                empty_title="NO SIMULATED ORDERS SUBMITTED",
                empty_description="Orders generated through Risk Engine -> OMS -> Paper Broker appear here with correlation IDs.",
            )
        else:
            ord_rows = "".join(
                f"<tr>"
                f'<td class="mono">{html.escape(fmt_ist(o.get("created_at")))}</td>'
                f'<td class="mono">{html.escape(str(o.get("order_code", "")))}</td>'
                f'<td class="mono">{html.escape(str(o.get("instrument_symbol", "")))}</td>'
                f'<td>{badge_html(str(o.get("side", "")))}</td>'
                f'<td class="num">{int(o.get("filled_quantity", 0))}/{int(o.get("quantity", 0))}</td>'
                f'<td>{badge_html(str(o.get("status", "")))}</td>'
                f"</tr>"
                for o in recent_orders_list
            )
            st.markdown(
                f"""
                <div class="desk-panel">
                    <div class="desk-panel-header">
                        <span class="desk-panel-title">Recent Orders</span>
                        <span class="desk-panel-meta">LATEST {len(recent_orders_list)} OMS ORDER(S)</span>
                    </div>
                    <table class="blotter-table">
                        <thead>
                            <tr>
                                <th>Time (IST)</th>
                                <th>Order ID</th>
                                <th>Symbol</th>
                                <th>Side</th>
                                <th class="num">Filled/Qty</th>
                                <th>Status</th>
                            </tr>
                        </thead>
                        <tbody>{ord_rows}</tbody>
                    </table>
                </div>
                """,
                unsafe_allow_html=True,
            )

    with col_right:
        _render_risk_utilization_panel(data.get("risk_utilization", {}), data.get("account"))
        kpis_map: dict[str, Any] = data.get("kpis", {})
        has_activity = bool(
            (data.get("open_positions") or [])
            or (data.get("recent_orders") or [])
            or str(kpis_map.get("todays_realized_pnl", "0.00")) not in ("0", "0.0", "0.00")
            or str(kpis_map.get("unrealized_pnl", "0.00")) not in ("0", "0.0", "0.00")
        )
        if not has_activity:
            render_empty_blotter(
                panel_title="Daily P&L",
                panel_meta="SESSION BASELINE ₹0.00",
                columns=[
                    ("Snapshot (IST)", False),
                    ("Realized P&L", True),
                    ("Unrealized P&L", True),
                    ("Net Session P&L", True),
                ],
                empty_title="NO INTRADAY P&L SNAPSHOTS YET",
                empty_description=(
                    "Demonstration capital stands at ₹5,00,000.00 (DEMO / PAPER ONLY) with ₹0.00 session P&L. "
                    "No simulated fills have occurred today."
                ),
            )
        else:
            from frontend.formatting import to_decimal

            real_v = kpis_map.get("todays_realized_pnl", "0.00")
            unreal_v = kpis_map.get("unrealized_pnl", "0.00")
            net_v = to_decimal(real_v) + to_decimal(unreal_v)
            st.markdown(
                f"""
                <div class="desk-panel">
                    <div class="desk-panel-header">
                        <span class="desk-panel-title">Daily P&amp;L</span>
                        <span class="desk-panel-meta">SIMULATED PAPER-TRADING RESULTS</span>
                    </div>
                    <table class="blotter-table">
                        <thead>
                            <tr>
                                <th>Ledger Component</th>
                                <th class="num">Amount (INR)</th>
                            </tr>
                        </thead>
                        <tbody>
                            <tr>
                                <td>Initial Paper Capital (DEMO)</td>
                                <td class="num">{html.escape(fmt_inr(kpis_map.get("paper_capital", "500000")))}</td>
                            </tr>
                            <tr>
                                <td>Available Paper Capital</td>
                                <td class="num">{html.escape(fmt_inr(kpis_map.get("available_capital", "500000")))}</td>
                            </tr>
                            <tr>
                                <td>Today's Realized P&amp;L (Net of Charges)</td>
                                <td class="num {pnl_css_class(real_v)}">{html.escape(fmt_inr(real_v, signed=True))}</td>
                            </tr>
                            <tr>
                                <td>Open Mark-to-Market Unrealized P&amp;L</td>
                                <td class="num {pnl_css_class(unreal_v)}">{html.escape(fmt_inr(unreal_v, signed=True))}</td>
                            </tr>
                            <tr>
                                <td><strong>Net Session Paper P&amp;L</strong></td>
                                <td class="num {pnl_css_class(net_v)}"><strong>{html.escape(fmt_inr(net_v, signed=True))}</strong></td>
                            </tr>
                        </tbody>
                    </table>
                </div>
                """,
                unsafe_allow_html=True,
            )
        _render_recent_alerts_panel(data.get("recent_alerts", []))
