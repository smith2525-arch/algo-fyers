"""Screen 8: Risk Center — Independent Fail-Closed Risk Engine, Kill Switch & Decision Audit.

CRITICAL ARCHITECTURAL RULE:
Streamlit owns ZERO risk logic. All Operational, Strategy-Level, and Account-Level
rules, Kill Switch state, and immutable Risk Decisions execute in FastAPI + PostgreSQL.
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


def render(client: ControlCenterApiClient) -> None:
    render_screen_header(
        "RISK CENTER",
        "SIMULATED PAPER TRADING  |  INDEPENDENT FAIL-CLOSED RISK ENGINE  |  IST (+05:30)",
    )

    res = client.get_risk_overview()
    if not res.ok or not isinstance(res.data, dict):
        render_api_offline_banner(
            res.error or "Unable to fetch Risk Center overview", client.base_url
        )
        return

    data: dict[str, Any] = res.data
    acct: dict[str, Any] = data.get("account") or {}
    util: dict[str, Any] = data.get("utilization") or {}
    rcfg: dict[str, Any] = data.get("account_risk_config") or {}
    s_cfgs: list[dict[str, Any]] = data.get("strategy_risk_configs") or []
    decisions: list[dict[str, Any]] = data.get("recent_decisions") or []

    kill_active = bool(rcfg.get("global_kill_switch_active") or acct.get("paper_trading_halted"))
    fresh_info: dict[str, Any] = util.get("market_data_freshness") or {}
    recon_info: dict[str, Any] = util.get("reconciliation") or {}
    open_pos_cnt = int(util.get("open_positions_count", 0))
    is_reconciled = bool(recon_info.get("reconciled", True))

    # 1. Explicit Demo Risk Settings Banner (Not Trading Recommendations)
    st.markdown(
        f"""
        <div class="desk-panel" style="padding:0.46rem 0.80rem; margin-bottom:0.55rem; background:#0D1526; border-left:3px solid #38BDF8;">
            <div style="display:flex; justify-content:space-between; align-items:center; flex-wrap:wrap; gap:0.45rem;">
                <div style="font-size:0.74rem; color:#E2E8F0;">
                    <strong style="color:#38BDF8;">DEMONSTRATION SETTINGS ONLY:</strong>
                    &nbsp;Configurable paper-risk parameters (Capital {html.escape(fmt_inr(acct.get("paper_capital_inr", "500000")))},
                    Max Daily Loss {html.escape(fmt_inr(rcfg.get("max_daily_loss_inr", "10000")))},
                    Max Risk/Trade {html.escape(fmt_inr(rcfg.get("default_max_risk_per_trade_inr", "2500")))},
                    Stale Limit {int(rcfg.get("market_data_stale_threshold_seconds", 10))}s,
                    Signal Expiry {int(rcfg.get("max_signal_age_seconds", 15))}s) — <em>not trading recommendations</em>.
                </div>
                <div class="mono" style="font-size:0.72rem; color:#94A3B8;">
                    FAIL-CLOSED POLICY ACTIVE
                </div>
            </div>
        </div>
        """,
        unsafe_allow_html=True,
    )

    # 2. Top Risk Utilization KPI Strip
    fresh_stat = str(fresh_info.get("status", "STOPPED"))
    kpis = [
        (
            "Global Kill Switch",
            "ACTIVE (BLOCKED)" if kill_active else "INACTIVE (ARMED)",
            "num-neg" if kill_active else "num-pos",
        ),
        (
            "Daily Loss / Limit",
            f"{fmt_inr(util.get('daily_loss_used_inr', '0'))} / {fmt_inr(rcfg.get('max_daily_loss_inr', '10000'))}",
            "num-neg"
            if to_decimal(util.get("daily_loss_used_inr")) >= to_decimal(rcfg.get("max_daily_loss_inr"))
            else "num-flat",
        ),
        (
            "Open Positions",
            f"{open_pos_cnt} / {int(rcfg.get('max_open_positions', 4))}",
            "num-flat",
        ),
        (
            "Gross Exposure",
            f"{fmt_inr(util.get('gross_exposure_inr', '0'))}",
            "num-flat",
        ),
        (
            "Consecutive Losses",
            f"{int(util.get('consecutive_losses', 0))} / {int(rcfg.get('max_consecutive_losses', 3))}",
            "num-neg"
            if int(util.get("consecutive_losses", 0)) >= int(rcfg.get("max_consecutive_losses", 3))
            else "num-flat",
        ),
        (
            "Market Data Gate",
            f"{fresh_stat} (<={int(rcfg.get('market_data_stale_threshold_seconds', 10))}s)",
            "num-pos" if fresh_stat == "HEALTHY" else "num-neg",
        ),
        (
            "Reconciliation Gate",
            "RECONCILED" if is_reconciled else "BLOCKED (DRIFT)",
            "num-pos" if is_reconciled else "num-neg",
        ),
    ]
    st.markdown(
        '<div class="kpi-grid">'
        + "".join(
            f'<div class="kpi-cell"><div class="kpi-label"><span>{html.escape(lbl)}</span></div>'
            f'<div class="kpi-value {cls}" style="font-size:0.82rem;">{html.escape(val)}</div></div>'
            for lbl, val, cls in kpis
        )
        + "</div>",
        unsafe_allow_html=True,
    )

    # 3. Persistent Global Kill Switch vs Explicit-Confirmation FLATTEN PAPER POSITIONS
    ks_col, fl_col = st.columns([1.15, 1.0])
    with ks_col:
        ks_badge = badge_html("BLOCKED" if kill_active else "HEALTHY")
        ks_meta = (
            f"Activated by {rcfg.get('kill_switch_activated_by') or 'operator'} at {fmt_ist(rcfg.get('kill_switch_activated_at'))}"
            if kill_active
            else "Persists in PostgreSQL & survives Cloud Run restarts"
        )
        st.markdown(
            f"""
            <div class="desk-panel">
                <div class="desk-panel-header">
                    <span class="desk-panel-title">Global Kill Switch &nbsp;{ks_badge}</span>
                    <span class="desk-panel-meta">{html.escape(ks_meta)}</span>
                </div>
                <div style="font-size:0.74rem; color:#CBD5E1; margin-bottom:0.4rem;">
                    Immediately blocks all NEW paper orders across strategies &amp; manual tickets.
                    <strong>Does NOT silently close or auto-flatten open positions.</strong>
                </div>
            </div>
            """,
            unsafe_allow_html=True,
        )
        ks_reason = st.text_input(
            "Kill Switch Audit Reason",
            value="Risk Officer emergency paper-order halt" if not kill_active else "Risk Officer verified safety gates — resuming",
            key="rc_ks_reason",
            label_visibility="collapsed",
        )
        kb1, kb2 = st.columns(2)
        with kb1:
            if st.button(
                "ACTIVATE KILL SWITCH",
                type="primary",
                disabled=kill_active,
                use_container_width=True,
                help="Emergency Kill Switch: immediately block all new paper orders without closing open positions.",
                key="rc_btn_ks_on",
            ):
                with st.spinner("Activating Global Kill Switch..."):
                    r_on = client.activate_kill_switch(reason=ks_reason)
                if r_on.ok:
                    st.rerun()
                else:
                    st.error(r_on.error or "Failed to activate Global Kill Switch.")
        with kb2:
            if st.button(
                "DEACTIVATE KILL SWITCH",
                disabled=not kill_active,
                use_container_width=True,
                key="rc_btn_ks_off",
            ):
                r_off = client.deactivate_kill_switch(reason=ks_reason)
                if r_off.ok:
                    st.rerun()
                else:
                    st.error(r_off.error or "Failed to deactivate Global Kill Switch.")

    with fl_col:
        st.markdown(
            f"""
            <div class="desk-panel">
                <div class="desk-panel-header">
                    <span class="desk-panel-title">Separate Explicit Flatten Control</span>
                    <span class="desk-panel-meta">{open_pos_cnt} OPEN PAPER POSITION(S)</span>
                </div>
                <div style="font-size:0.74rem; color:#CBD5E1; margin-bottom:0.4rem;">
                    Submits reduce-only simulated MARKET closing orders through OMS &rarr; Paper Broker.
                    Requires explicit operator confirmation below.
                </div>
            </div>
            """,
            unsafe_allow_html=True,
        )
        confirm_flat = st.checkbox(
            f"Confirm explicit flatten of all {open_pos_cnt} open paper position(s)",
            value=False,
            key="rc_confirm_flatten_chk",
        )
        if st.button(
            "FLATTEN PAPER POSITIONS",
            disabled=(not confirm_flat or open_pos_cnt == 0),
            use_container_width=True,
            key="rc_btn_flatten_confirmed",
        ):
            fl_res = client.flatten_positions_confirmed(confirm_flatten=True)
            if fl_res.ok:
                st.rerun()
            else:
                st.error(fl_res.error or "Failed to flatten open paper positions.")

    # 4. Configurable Account-Level, Operational & Strategy-Level Rules Editors
    e1, e2 = st.columns(2)
    with e1:
        with st.expander("Configurable Account & Operational Risk Rules (DEMO SETTINGS)", expanded=False):
            a1, a2, a3 = st.columns(3)
            with a1:
                u_max_dl = st.number_input(
                    "Max Daily Loss (₹)",
                    min_value=100.0,
                    value=float(to_decimal(rcfg.get("max_daily_loss_inr", "10000"))),
                    step=500.0,
                    key="rc_u_dl",
                )
                u_max_ord_val = st.number_input(
                    "Max Order Value (₹)",
                    min_value=10000.0,
                    value=float(to_decimal(rcfg.get("max_order_value_inr", "3000000"))),
                    step=100000.0,
                    key="rc_u_ov",
                )
                u_stale_s = st.number_input(
                    "Stale Data Limit (s)",
                    min_value=1,
                    max_value=600,
                    value=int(rcfg.get("market_data_stale_threshold_seconds", 10)),
                    step=1,
                    key="rc_u_stale",
                )
            with a2:
                u_max_pos = st.number_input(
                    "Max Open Positions",
                    min_value=1,
                    max_value=50,
                    value=int(rcfg.get("max_open_positions", 4)),
                    step=1,
                    key="rc_u_pos",
                )
                u_max_gross = st.number_input(
                    "Max Gross Exposure (₹)",
                    min_value=50000.0,
                    value=float(to_decimal(rcfg.get("max_gross_exposure_inr", "6000000"))),
                    step=250000.0,
                    key="rc_u_gross",
                )
                u_sig_exp = st.number_input(
                    "Max Signal Age (s)",
                    min_value=1,
                    max_value=600,
                    value=int(rcfg.get("max_signal_age_seconds", 15)),
                    step=1,
                    key="rc_u_sigexp",
                )
            with a3:
                u_max_trd = st.number_input(
                    "Account Max Trades/Day",
                    min_value=1,
                    max_value=500,
                    value=int(rcfg.get("max_trades_per_day", 15)),
                    step=1,
                    key="rc_u_atrd",
                )
                u_consec = st.number_input(
                    "Consecutive Loss Pause",
                    min_value=1,
                    max_value=50,
                    value=int(rcfg.get("max_consecutive_losses", 3)),
                    step=1,
                    key="rc_u_consec",
                )
                u_sess_en = st.checkbox(
                    "Enforce IST Session",
                    value=bool(rcfg.get("trading_session_enabled", True)),
                    key="rc_u_sessen",
                )

            if st.button("SAVE ACCOUNT & OPERATIONAL LIMITS", use_container_width=True, key="rc_btn_save_acct"):
                upd = client.update_account_risk_config(
                    {
                        "max_daily_loss_inr": str(u_max_dl),
                        "max_open_positions": int(u_max_pos),
                        "max_gross_exposure_inr": str(u_max_gross),
                        "max_order_value_inr": str(u_max_ord_val),
                        "max_trades_per_day": int(u_max_trd),
                        "max_consecutive_losses": int(u_consec),
                        "market_data_stale_threshold_seconds": int(u_stale_s),
                        "max_signal_age_seconds": int(u_sig_exp),
                        "trading_session_enabled": bool(u_sess_en),
                        "actor": "risk.officer",
                    }
                )
                if upd.ok:
                    st.rerun()
                else:
                    st.error(upd.error or "Failed to update account risk configuration.")

    with e2:
        with st.expander("Configurable Strategy-Level Risk Rules (DEMO SETTINGS)", expanded=False):
            if not s_cfgs:
                st.info("No strategy versions registered.")
            else:
                s_idx = st.selectbox(
                    "Strategy Version",
                    range(len(s_cfgs)),
                    format_func=lambda idx: s_cfgs[idx]["strategy_version_label"],
                    key="rc_strat_sel",
                )
                sc = s_cfgs[s_idx]
                s1, s2, s3 = st.columns(3)
                with s1:
                    s_rpt = st.number_input(
                        "Max Risk/Trade (₹)",
                        min_value=100.0,
                        value=float(to_decimal(sc.get("max_risk_per_trade_inr", "2500"))),
                        step=250.0,
                        key=f"rc_s_rpt_{sc['id']}",
                    )
                    s_dl = st.number_input(
                        "Strategy Daily Loss (₹)",
                        min_value=100.0,
                        value=float(to_decimal(sc.get("strategy_daily_loss_inr", "6000"))),
                        step=500.0,
                        key=f"rc_s_dl_{sc['id']}",
                    )
                with s2:
                    s_qty = st.number_input(
                        "Max Quantity",
                        min_value=1,
                        max_value=5000,
                        value=int(sc.get("max_quantity", 300)),
                        step=15,
                        key=f"rc_s_qty_{sc['id']}",
                    )
                    s_dd = st.number_input(
                        "Max Drawdown (₹)",
                        min_value=100.0,
                        value=float(to_decimal(sc.get("max_strategy_drawdown_inr", "8000"))),
                        step=500.0,
                        key=f"rc_s_dd_{sc['id']}",
                    )
                with s3:
                    s_tpd = st.number_input(
                        "Max Trades/Day",
                        min_value=1,
                        max_value=200,
                        value=int(sc.get("max_trades_per_day", 5)),
                        step=1,
                        key=f"rc_s_tpd_{sc['id']}",
                    )
                    s_cd = st.number_input(
                        "Loss Cooldown (s)",
                        min_value=0,
                        max_value=86400,
                        value=int(sc.get("cooldown_seconds_after_loss", 300)),
                        step=60,
                        key=f"rc_s_cd_{sc['id']}",
                    )

                if st.button("SAVE STRATEGY RISK LIMITS", use_container_width=True, key="rc_btn_save_strat"):
                    sup = client.update_strategy_risk_config(
                        str(sc["strategy_version_id"]),
                        {
                            "max_risk_per_trade_inr": str(s_rpt),
                            "strategy_daily_loss_inr": str(s_dl),
                            "max_quantity": int(s_qty),
                            "max_strategy_drawdown_inr": str(s_dd),
                            "max_trades_per_day": int(s_tpd),
                            "cooldown_seconds_after_loss": int(s_cd),
                            "actor": "risk.officer",
                        },
                    )
                    if sup.ok:
                        st.rerun()
                    else:
                        st.error(sup.error or "Failed to update strategy risk configuration.")

    # 5. Persisted Risk Decisions Blotter (Explaining WHY Every Order Was Approved or Blocked)
    df1, df2 = st.columns([1.4, 3.6])
    with df1:
        dec_filter = st.selectbox(
            "Filter Risk Decisions",
            ["ALL", "REJECTED", "APPROVED"],
            key="rc_dec_filter",
        )
    filtered_decs = (
        decisions
        if dec_filter == "ALL"
        else [d for d in decisions if d.get("decision") == dec_filter]
    )

    if not filtered_decs:
        render_empty_blotter(
            panel_title="Persisted Pre-Trade Risk Decisions (SIMULATED PAPER TRADING)",
            panel_meta="0 DECISIONS RECORDED",
            columns=[
                ("Timestamp (IST)", False),
                ("Decision", False),
                ("Rule", False),
                ("Current / Proposed", True),
                ("Limit", True),
                ("Strategy", False),
                ("Why Approved / Blocked", False),
            ],
            empty_title="NO RISK DECISIONS RECORDED YET",
            empty_description=(
                "Every attempted paper order generates an immutable RiskDecision record detailing "
                "Decision, Rule, Current Value, Limit, Strategy, Signal ID, Correlation ID, and Reason."
            ),
        )
        return

    rows_html: list[str] = []
    for d in filtered_decs[:40]:
        sig_short = str(d.get("signal_id") or "—")[:8]
        corr_short = str(d.get("correlation_id") or "—")[:10]
        rows_html.append(
            f"<tr>"
            f'<td class="mono">{html.escape(fmt_ist(d.get("timestamp")))}</td>'
            f'<td>{badge_html(str(d.get("decision", "")))}</td>'
            f'<td class="mono">{html.escape(str(d.get("rule", "")))}</td>'
            f'<td class="num mono">{html.escape(str(d.get("current_value", "")))}</td>'
            f'<td class="num mono">{html.escape(str(d.get("limit_value", "")))}</td>'
            f'<td class="mono">{html.escape(str(d.get("strategy", "")))}</td>'
            f'<td class="mono">{html.escape(sig_short)} / {html.escape(corr_short)}</td>'
            f'<td>{html.escape(str(d.get("reason", "")))}</td>'
            f"</tr>"
        )

    st.markdown(
        f"""
        <div class="desk-panel">
            <div class="desk-panel-header">
                <span class="desk-panel-title">Persisted Pre-Trade Risk Decisions (SIMULATED PAPER TRADING)</span>
                <span class="desk-panel-meta">{len(filtered_decs)} IMMUTABLE DECISION(S)</span>
            </div>
            <table class="blotter-table">
                <thead>
                    <tr>
                        <th>Timestamp (IST)</th>
                        <th>Decision</th>
                        <th>Primary Rule</th>
                        <th class="num">Current / Proposed</th>
                        <th class="num">Configured Limit</th>
                        <th>Strategy</th>
                        <th>Signal / Corr ID</th>
                        <th>Plain-English Explanation (Why Approved / Blocked)</th>
                    </tr>
                </thead>
                <tbody>{"".join(rows_html)}</tbody>
            </table>
        </div>
        """,
        unsafe_allow_html=True,
    )
