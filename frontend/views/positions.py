"""Screen 7: Positions — Execution-Derived Position Book, Exit Controls & Reconciliation.

ARCHITECTURAL INVARIANT:
Positions derive strictly from simulated executions (`PaperFillModel`). UI code NEVER
edits positions directly; exit buttons submit closing orders via OMS -> Paper Broker.
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
from frontend.formatting import fmt_inr, fmt_ist, pnl_css_class, to_decimal


def _fmt_px(val: Any) -> str:
    if val is None or val == "":
        return "—"
    return f"{to_decimal(val):,.2f}"


def _render_positions_table(
    *, title: str, meta: str, rows: list[dict[str, Any]], empty_title: str, empty_desc: str
) -> None:
    if not rows:
        render_empty_blotter(
            panel_title=title,
            panel_meta=meta,
            columns=[
                ("Strategy", False),
                ("Instrument", False),
                ("Side", False),
                ("Qty", True),
                ("Avg Entry", True),
                ("Last Price", True),
                ("Realized P&L", True),
                ("Unrealized P&L", True),
                ("Stop", True),
                ("Target", True),
                ("Holding", False),
                ("Status", False),
            ],
            empty_title=empty_title,
            empty_description=empty_desc,
        )
        return

    tr_list: list[str] = []
    for p in rows:
        qty_disp = (
            str(int(p.get("quantity", 0)))
            if p.get("status") == "OPEN"
            else f"0 (closed {int(p.get('closed_quantity', 0))})"
        )
        r_pnl = p.get("realized_pnl", "0.00")
        u_pnl = p.get("unrealized_pnl", "0.00")
        tr_list.append(
            f"<tr>"
            f'<td class="mono" title="{html.escape(str(p.get("position_code", "")))}">{html.escape(str(p.get("strategy_version_label", "")))}</td>'
            f'<td class="mono">{html.escape(str(p.get("instrument_symbol", "")))}</td>'
            f'<td>{badge_html(str(p.get("side", "")))}</td>'
            f'<td class="num">{html.escape(qty_disp)}</td>'
            f'<td class="num">{_fmt_px(p.get("average_entry_price"))}</td>'
            f'<td class="num">{_fmt_px(p.get("last_price"))}</td>'
            f'<td class="num {pnl_css_class(r_pnl)}">{html.escape(fmt_inr(r_pnl, signed=True))}</td>'
            f'<td class="num {pnl_css_class(u_pnl)}">{html.escape(fmt_inr(u_pnl, signed=True))}</td>'
            f'<td class="num">{_fmt_px(p.get("stop_loss_price"))}</td>'
            f'<td class="num">{_fmt_px(p.get("target_price"))}</td>'
            f'<td class="mono">{html.escape(str(p.get("holding_duration_display", "0s")))}</td>'
            f'<td>{badge_html(str(p.get("status", "")))}</td>'
            f"</tr>"
        )

    st.markdown(
        f"""
        <div class="desk-panel">
            <div class="desk-panel-header">
                <span class="desk-panel-title">{html.escape(title)}</span>
                <span class="desk-panel-meta">{html.escape(meta)}</span>
            </div>
            <table class="blotter-table">
                <thead>
                    <tr>
                        <th>Strategy</th>
                        <th>Instrument</th>
                        <th>Side</th>
                        <th class="num">Quantity</th>
                        <th class="num">Avg Entry</th>
                        <th class="num">Last Price</th>
                        <th class="num">Realized P&amp;L</th>
                        <th class="num">Unrealized P&amp;L</th>
                        <th class="num">Stop</th>
                        <th class="num">Target</th>
                        <th>Holding Duration</th>
                        <th>Status</th>
                    </tr>
                </thead>
                <tbody>{"".join(tr_list)}</tbody>
            </table>
        </div>
        """,
        unsafe_allow_html=True,
    )


def render(client: ControlCenterApiClient) -> None:
    render_screen_header(
        "POSITIONS",
        "SIMULATED PAPER TRADING  |  EXECUTION-DERIVED POSITION BOOK  |  IST (+05:30)",
    )

    res = client.list_positions(status="ALL", instrument="ALL")
    if not res.ok or not isinstance(res.data, dict):
        render_api_offline_banner(res.error or "Unable to fetch positions book", client.base_url)
        return

    all_positions: list[dict[str, Any]] = res.data.get("positions") or []
    recon: dict[str, Any] = res.data.get("reconciliation") or {}
    open_positions = [p for p in all_positions if p.get("status") == "OPEN"]
    closed_positions = [p for p in all_positions if p.get("status") == "CLOSED"]

    tot_realized = recon.get("total_realized_pnl", "0.00")
    tot_unrealized = recon.get("total_unrealized_pnl", "0.00")
    net_total = to_decimal(tot_realized) + to_decimal(tot_unrealized)
    is_reconciled = bool(recon.get("reconciled", True))

    kpis = [
        ("Open Paper Positions", str(len(open_positions)), "num-pos" if open_positions else "num-flat"),
        ("Closed Positions", str(len(closed_positions)), "num-flat"),
        ("Total Realized P&L", fmt_inr(tot_realized, signed=True), pnl_css_class(tot_realized)),
        ("Open Unrealized P&L", fmt_inr(tot_unrealized, signed=True), pnl_css_class(tot_unrealized)),
        ("Net Book P&L", fmt_inr(net_total, signed=True), pnl_css_class(net_total)),
        ("Execution Fills Verified", str(int(recon.get("fills_inspected", 0))), "num-flat"),
        ("Book Integrity", "RECONCILED" if is_reconciled else "DRIFT_DETECTED", "num-pos" if is_reconciled else "num-neg"),
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

    # Explicit Position Exit & Reconciliation Controls
    if open_positions:
        st.markdown(
            f"""
            <div class="control-bar" style="border-left-color:#F59E0B; margin-bottom:0.48rem;">
                <div class="control-bar-title">EXPLICIT POSITION LIQUIDATION CONTROLS &bull; {len(open_positions)} OPEN PAPER POSITION(S)</div>
                <div class="control-bar-note">
                    Positions are never edited directly. Clicking <strong>EXIT POSITION</strong> or <strong>FLATTEN ALL OPEN</strong>
                    routes reduce-only simulated MARKET closing orders through OMS &rarr; Paper Broker (for checkbox-gated Risk Officer flatten, see Risk Center).
                </div>
            </div>
            """,
            unsafe_allow_html=True,
        )
    c1, c2, c3, c4 = st.columns([2.2, 1.2, 1.3, 1.1])
    with c1:
        if open_positions:
            sel_pos_idx = st.selectbox(
                "Select Open Position to Exit via Simulated Market Order",
                range(len(open_positions)),
                format_func=lambda idx: (
                    f"{open_positions[idx]['position_code']} — {open_positions[idx]['side']} "
                    f"{open_positions[idx]['quantity']} {open_positions[idx]['instrument_symbol']} "
                    f"@ {open_positions[idx]['average_entry_price']}"
                ),
                label_visibility="collapsed",
                key="pos_exit_sel",
            )
            chosen_open_pos = open_positions[sel_pos_idx]
        else:
            st.selectbox(
                "No Open Positions",
                ["Book is currently flat (0 open positions)"],
                disabled=True,
                label_visibility="collapsed",
            )
            chosen_open_pos = None

    with c2:
        if st.button(
            "EXIT POSITION",
            disabled=(chosen_open_pos is None),
            use_container_width=True,
            key="pos_btn_exit_one",
        ):
            assert chosen_open_pos is not None
            ex_res = client.exit_position(str(chosen_open_pos["id"]))
            if ex_res.ok:
                st.rerun()
            else:
                st.error(ex_res.error or "Failed to submit closing paper order.")

    with c3:
        if st.button(
            "FLATTEN ALL OPEN",
            type="primary",
            disabled=(len(open_positions) == 0),
            use_container_width=True,
            help="Liquidate all open paper positions via simulated MARKET closing orders.",
            key="pos_btn_flatten_all",
        ):
            with st.spinner("Submitting closing paper orders through OMS -> Paper Broker..."):
                fl_res = client.flatten_all_positions()
            if fl_res.ok:
                st.rerun()
            else:
                st.error(fl_res.error or "Failed to flatten open positions.")

    with c4:
        if st.button(
            "RECONCILE BOOK",
            use_container_width=True,
            key="pos_btn_reconcile",
        ):
            rc_res = client.reconcile_positions()
            if rc_res.ok:
                st.toast("Execution-to-position reconciliation verified.")
                st.rerun()
            else:
                st.error(rc_res.error or "Reconciliation failed.")

    _render_positions_table(
        title="Open Paper Positions (SIMULATED PAPER TRADING)",
        meta=f"{len(open_positions)} OPEN POSITION(S) — DERIVED FROM SIMULATED FILLS",
        rows=open_positions,
        empty_title="NO OPEN PAPER POSITIONS",
        empty_desc=(
            "Open positions appear automatically when Paper Broker executes fills. "
            "Note: STOP PAPER TRADING halts NEW orders without auto-flattening open positions."
        ),
    )

    _render_positions_table(
        title="Closed Paper Positions Ledger",
        meta=f"{len(closed_positions)} CLOSED POSITION(S)",
        rows=closed_positions,
        empty_title="NO CLOSED PAPER POSITIONS YET",
        empty_desc="Closed positions record realized P&L, charges, holding duration, and exit reason.",
    )
