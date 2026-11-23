"""Screen 6: Orders — OMS Order Blotter, Cancellation Controls & State-Machine Trail.

SIMULATED PAPER TRADING ONLY — No real broker routing.
"""

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
)
from frontend.formatting import fmt_inr, fmt_ist, to_decimal


def _fmt_px(val: Any) -> str:
    if val is None or val == "":
        return "—"
    return f"{to_decimal(val):,.2f}"


def render(client: ControlCenterApiClient) -> None:
    render_screen_header(
        "ORDERS",
        "SIMULATED PAPER TRADING  |  OMS ORDER BLOTTER & STATE MACHINE  |  IST (+05:30)",
    )

    f1, f2, f3 = st.columns([1.4, 1.2, 2.4])
    with f1:
        status_filter = st.selectbox(
            "OMS Status Filter",
            [
                "ALL",
                "WORKING",
                "FILLED",
                "PARTIALLY_FILLED",
                "ACKNOWLEDGED",
                "SUBMITTED",
                "RISK_APPROVED",
                "RISK_REJECTED",
                "REJECTED",
                "CANCELLED",
                "EXPIRED",
                "FAILED",
            ],
            key="oms_status_filter",
        )
    with f2:
        inst_filter = st.selectbox(
            "Instrument", ["ALL", "NIFTY", "BANKNIFTY"], key="oms_inst_filter"
        )

    res = client.list_orders(status=status_filter, instrument=inst_filter, limit=150)
    if not res.ok or not isinstance(res.data, dict):
        render_api_offline_banner(res.error or "Unable to fetch OMS orders", client.base_url)
        return

    orders: list[dict[str, Any]] = res.data.get("orders") or []
    working_orders = [o for o in orders if o.get("is_working")]

    with f3:
        c_chk, c_btn = st.columns([1.15, 1.25])
        with c_chk:
            st.write("")
            confirm_cancel_all = st.checkbox(
                f"Confirm cancel all ({len(working_orders)})",
                value=False,
                disabled=(len(working_orders) == 0),
                key="oms_confirm_cancel_all",
            )
        with c_btn:
            st.write("")
            if st.button(
                f"CANCEL ALL WORKING PAPER ORDERS ({len(working_orders)})",
                disabled=(len(working_orders) == 0 or not confirm_cancel_all),
                use_container_width=True,
                key="oms_btn_cancel_all",
            ):
                with st.spinner("Cancelling working paper orders..."):
                    c_res = client.cancel_all_orders(
                        instrument_symbol=None if inst_filter == "ALL" else inst_filter
                    )
                if c_res.ok:
                    st.rerun()
                else:
                    st.error(c_res.error or "Failed to cancel working orders.")

    # KPI Summary Strip
    filled_cnt = sum(1 for o in orders if o.get("status") == "FILLED")
    part_cnt = sum(1 for o in orders if o.get("status") == "PARTIALLY_FILLED")
    rej_cnt = sum(1 for o in orders if o.get("status") in ("RISK_REJECTED", "REJECTED", "FAILED"))
    canc_exp_cnt = sum(1 for o in orders if o.get("status") in ("CANCELLED", "EXPIRED"))
    kpis = [
        ("Visible OMS Orders", str(len(orders)), "num-flat"),
        ("Working Orders", str(len(working_orders)), "num-pos" if working_orders else "num-flat"),
        ("Filled", str(filled_cnt), "num-pos" if filled_cnt else "num-flat"),
        ("Partially Filled", str(part_cnt), "num-flat"),
        ("Rejected / Failed", str(rej_cnt), "num-neg" if rej_cnt else "num-flat"),
        ("Cancelled / Expired", str(canc_exp_cnt), "num-flat"),
        ("Execution Mode", "SIMULATED PAPER", "num-flat"),
    ]
    st.markdown(
        '<div class="kpi-grid">'
        + "".join(
            f'<div class="kpi-cell"><div class="kpi-label"><span>{html.escape(k)}</span></div>'
            f'<div class="kpi-value {c}" style="font-size:0.86rem;">{html.escape(v)}</div></div>'
            for k, v, c in kpis
        )
        + "</div>",
        unsafe_allow_html=True,
    )

    if not orders:
        render_empty_blotter(
            panel_title="Paper Order Blotter (SIMULATED PAPER TRADING)",
            panel_meta="0 MATCHING ORDERS",
            columns=[
                ("Created (IST)", False),
                ("Order ID", False),
                ("Client Key", False),
                ("Symbol", False),
                ("Side", False),
                ("Type", False),
                ("Qty", True),
                ("Filled", True),
                ("Limit / Stop", True),
                ("Avg Fill", True),
                ("Status", False),
                ("Correlation ID", False),
            ],
            empty_title="NO PAPER ORDERS IN OMS LEDGER",
            empty_description=(
                "Every OMS state transition (CREATED -> RISK_APPROVED -> SUBMITTED -> ACKNOWLEDGED "
                "-> PARTIALLY_FILLED -> FILLED | RISK_REJECTED | REJECTED | CANCELLED | EXPIRED | FAILED) "
                "is recorded immutably with idempotent duplicate-order prevention."
            ),
        )
        return

    rows_html: list[str] = []
    for o in orders:
        trig_px = (
            f"L:{_fmt_px(o.get('limit_price'))}"
            if o.get("order_type") == "LIMIT"
            else (
                f"S:{_fmt_px(o.get('stop_price'))}"
                if o.get("order_type") == "STOP"
                else "MKT"
            )
        )
        note = o.get("rejection_reason") or o.get("cancel_reason") or o.get("strategy_version_label") or ""
        rows_html.append(
            f"<tr>"
            f'<td class="mono">{html.escape(fmt_ist(o.get("created_at")))}</td>'
            f'<td class="mono">{html.escape(str(o.get("order_code", "")))}</td>'
            f'<td class="mono">{html.escape(str(o.get("client_order_key", ""))[:22])}</td>'
            f'<td class="mono">{html.escape(str(o.get("instrument_symbol", "")))}</td>'
            f'<td>{badge_html(str(o.get("side", "")))}</td>'
            f'<td class="mono">{html.escape(str(o.get("order_type", "")))}</td>'
            f'<td class="num">{int(o.get("filled_quantity", 0))} / {int(o.get("quantity", 0))}</td>'
            f'<td class="num mono">{html.escape(trig_px)}</td>'
            f'<td class="num"><strong>{_fmt_px(o.get("average_fill_price"))}</strong></td>'
            f'<td class="num">{html.escape(fmt_inr(o.get("total_charges", "0")))}</td>'
            f'<td>{badge_html(str(o.get("status", "")))}</td>'
            f'<td title="{html.escape(str(note))}">{html.escape(str(note)[:34])}</td>'
            f"</tr>"
        )

    st.markdown(
        f"""
        <div class="desk-panel">
            <div class="desk-panel-header">
                <span class="desk-panel-title">OMS Paper Order Blotter (SIMULATED PAPER TRADING)</span>
                <span class="desk-panel-meta">{len(orders)} ORDER(S) &bull; {len(working_orders)} WORKING</span>
            </div>
            <table class="blotter-table">
                <thead>
                    <tr>
                        <th>Created (IST)</th>
                        <th>Order ID</th>
                        <th>Idempotency Key</th>
                        <th>Symbol</th>
                        <th>Side</th>
                        <th>Type</th>
                        <th class="num">Filled / Qty</th>
                        <th class="num">Price Spec</th>
                        <th class="num">Avg Fill</th>
                        <th class="num">Charges</th>
                        <th>Status</th>
                        <th>Source / Diagnostic</th>
                    </tr>
                </thead>
                <tbody>{"".join(rows_html)}</tbody>
            </table>
        </div>
        """,
        unsafe_allow_html=True,
    )

    # Individual Order Lifecycle Trail & Cancel Control
    i1, i2 = st.columns([2.5, 1.0])
    with i1:
        sel_idx = st.selectbox(
            "Inspect Order State Machine Trail",
            range(len(orders)),
            format_func=lambda idx: f"{orders[idx]['order_code']} — {orders[idx]['side']} {orders[idx]['quantity']} {orders[idx]['instrument_symbol']} ({orders[idx]['status']})",
            key="oms_inspect_sel",
        )
        chosen_ord = orders[sel_idx]
    with i2:
        st.write("")
        if st.button(
            "CANCEL SELECTED ORDER",
            disabled=(not bool(chosen_ord.get("is_working"))),
            use_container_width=True,
            key="oms_btn_cancel_single",
        ):
            c_one = client.cancel_paper_order(str(chosen_ord["id"]))
            if c_one.ok:
                st.rerun()
            else:
                st.error(c_one.error or "Unable to cancel order.")

    det_res = client.get_order_detail(str(chosen_ord["id"]))
    if det_res.ok and isinstance(det_res.data, dict):
        events: list[dict[str, Any]] = det_res.data.get("state_events") or []
        if events:
            ev_rows = "".join(
                f"<tr>"
                f'<td class="mono">{html.escape(fmt_ist(ev.get("occurred_at")))}</td>'
                f'<td>{badge_html(str(ev.get("previous_status") or "NONE"))} &rarr; {badge_html(str(ev.get("new_status", "")))}</td>'
                f'<td class="mono">{html.escape(str(ev.get("actor", "")))}</td>'
                f"<td>{html.escape(str(ev.get("reason", "")))}</td>"
                f"</tr>"
                for ev in events
            )
            st.markdown(
                f"""
                <div class="desk-panel">
                    <div class="desk-panel-header">
                        <span class="desk-panel-title">Immutable OMS State-Transition Audit: {html.escape(str(chosen_ord["order_code"]))}</span>
                        <span class="desk-panel-meta">CORRELATION: {html.escape(str(chosen_ord.get("correlation_id", "")))}</span>
                    </div>
                    <table class="blotter-table">
                        <thead>
                            <tr>
                                <th>Timestamp (IST)</th>
                                <th>State Transition</th>
                                <th>Actor</th>
                                <th>Reason</th>
                            </tr>
                        </thead>
                        <tbody>{ev_rows}</tbody>
                    </table>
                </div>
                """,
                unsafe_allow_html=True,
            )
