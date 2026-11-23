"""Screen 10: Audit Log & End-to-End Trade Lifecycle Correlation Trace View."""

from __future__ import annotations

import html
import json
from typing import Any

import streamlit as st

from frontend.api_client import ControlCenterApiClient
from frontend.components.ui import (
    badge_html,
    render_api_offline_banner,
    render_empty_blotter,
    render_screen_header,
)
from frontend.formatting import fmt_ist

CANONICAL_EVENT_TYPES: list[str] = [
    "ALL",
    "LOGIN",
    "STRATEGY_CREATED",
    "STRATEGY_VERSION_CREATED",
    "BACKTEST_STARTED",
    "BACKTEST_COMPLETED",
    "REPLAY_STARTED",
    "REPLAY_PAUSED",
    "STRATEGY_STARTED",
    "STRATEGY_PAUSED",
    "STRATEGY_STOPPED",
    "SIGNAL_GENERATED",
    "RISK_APPROVED",
    "RISK_REJECTED",
    "ORDER_CREATED",
    "ORDER_SUBMITTED",
    "ORDER_FILLED",
    "ORDER_REJECTED",
    "ORDER_CANCELLED",
    "POSITION_OPENED",
    "POSITION_CLOSED",
    "STOP_TRIGGERED",
    "TARGET_TRIGGERED",
    "KILL_SWITCH_ACTIVATED",
    "KILL_SWITCH_DEACTIVATED",
    "MARKET_DATA_STALE",
    "MARKET_DATA_RECOVERED",
    "RECONCILIATION_COMPLETED",
    "RECONCILIATION_FAILED",
]


def _render_lifecycle_trace_panel(client: ControlCenterApiClient) -> None:
    """Render the consulting demo centerpiece: 8-Stage Trade Lifecycle Correlation Trace View."""
    st.markdown(
        """
        <div class="desk-panel" style="margin-bottom:0.75rem; border-left:3px solid #38bdf8;">
            <div class="desk-panel-header">
                <span class="desk-panel-title">END-TO-END TRADE LIFECYCLE TRACE VIEW &nbsp;&bull;&nbsp; CONSULTING DEMO INSPECTOR</span>
                <span class="desk-panel-meta">MARKET EVENT &rarr; SIGNAL &rarr; RISK DECISION &rarr; ORDER &rarr; EXECUTION &rarr; POSITION &rarr; EXIT &rarr; FINAL P&amp;L</span>
            </div>
        </div>
        """,
        unsafe_allow_html=True,
    )

    traces_resp = client.list_traceable_correlations(limit=30)
    if traces_resp.ok and isinstance(traces_resp.data, dict):
        trace_items: list[dict[str, Any]] = list(traces_resp.data.get("traces", []))
    elif traces_resp.ok and isinstance(traces_resp.data, list):
        trace_items = traces_resp.data
    else:
        trace_items = []

    option_labels: list[str] = []
    option_cids: dict[str, str] = {}
    for item in trace_items:
        cid_val = str(item.get("correlation_id", ""))
        lbl = (
            f"{item.get('order_code', 'TRD')} | {item.get('instrument_symbol', '')} "
            f"{item.get('side', '')} | {item.get('order_status', '')} | "
            f"{item.get('strategy', '')} ({cid_val[:12]}...)"
        )
        option_labels.append(lbl)
        option_cids[lbl] = cid_val

    if trace_items:
        option_labels.append("-- Clear Trace Selection --")
        option_cids["-- Clear Trace Selection --"] = ""
    else:
        option_labels.append("-- No Traceable Paper Trades Yet --")
        option_cids["-- No Traceable Paper Trades Yet --"] = ""

    guided_cid = str(st.session_state.pop("guided_trace_cid", "") or "")
    if guided_cid:
        st.session_state["trace_manual_cid"] = guided_cid

    t_col1, t_col2 = st.columns([2.2, 1.8])
    with t_col1:
        picked_label = st.selectbox(
            "Recent Traceable Paper Trades & Signals",
            option_labels,
            index=0,
            key="trace_recent_picker",
        )
    with t_col2:
        manual_cid = st.text_input(
            "Or Enter Exact Correlation ID to Trace",
            value="",
            placeholder="Paste correlation_id UUID or code...",
            key="trace_manual_cid",
        )

    active_cid = manual_cid.strip() or option_cids.get(picked_label, "")
    if not active_cid:
        st.markdown(
            """
            <div class="desk-panel" style="margin-bottom:0.85rem;">
                <div class="empty-state-box">
                    <div class="empty-state-title">NO TRADE CORRELATION SELECTED YET</div>
                    <div class="empty-state-desc">
                        Run Market Replay + Paper Strategy or submit a Simulated Paper Order, then select or paste a
                        <code>correlation_id</code> above to inspect the full 8-stage deterministic trade lifecycle.
                    </div>
                </div>
            </div>
            """,
            unsafe_allow_html=True,
        )
        return

    trace_res = client.get_lifecycle_trace(active_cid)
    if not trace_res.ok or not isinstance(trace_res.data, dict):
        st.warning(trace_res.error or f"Unable to load trace for correlation_id '{active_cid}'.")
        return

    trace_data = trace_res.data
    if not trace_data.get("found"):
        st.info(f"No trade, signal, risk decision, or audit records found for Correlation ID `{active_cid}`.")
        return

    stages: list[dict[str, Any]] = trace_data.get("stages", [])
    related_cids: list[str] = trace_data.get("related_correlation_ids", [active_cid])

    # Render horizontal 8-stage pipeline strip
    flow_nodes: list[str] = []
    for idx, stg in enumerate(stages):
        stg_title = html.escape(str(stg.get("stage", "")))
        stg_status = str(stg.get("status", "N/A"))
        present = bool(stg.get("present"))
        border_col = "#22c55e" if present else "#334155"
        arrow_html = (
            '<div style="align-self:center; color:#64748b; font-weight:700; padding:0 0.2rem;">&rarr;</div>'
            if idx < len(stages) - 1
            else ""
        )
        flow_nodes.append(
            f'<div style="flex:1; min-width:105px; background:#0b1220; border:1px solid {border_col}; '
            f'border-radius:4px; padding:0.4rem 0.45rem;">'
            f'<div style="font-size:0.66rem; color:#94a3b8; font-weight:600; margin-bottom:0.2rem;">{stg_title}</div>'
            f'<div>{badge_html(stg_status)}</div>'
            f"</div>{arrow_html}"
        )

    st.markdown(
        f"""
        <div class="desk-panel" style="margin-bottom:0.65rem;">
            <div class="desk-panel-header">
                <span class="desk-panel-title">TRACE CORRELATION: <span class="mono">{html.escape(active_cid)}</span></span>
                <span class="desk-panel-meta">{html.escape(str(trace_data.get("simulation_banner", "SIMULATED PAPER-TRADING RESULTS")))} &bull; LINKED CIDS: {len(related_cids)}</span>
            </div>
            <div style="display:flex; flex-wrap:wrap; gap:0.25rem; padding:0.45rem 0.2rem;">
                {"".join(flow_nodes)}
            </div>
        </div>
        """,
        unsafe_allow_html=True,
    )

    # Render detailed 2x4 stage cards
    for row_start in (0, 4):
        cols = st.columns(4)
        for offset in range(4):
            s_idx = row_start + offset
            if s_idx >= len(stages):
                continue
            stg = stages[s_idx]
            with cols[offset]:
                s_name = html.escape(str(stg.get("stage", "")))
                s_stat = str(stg.get("status", "N/A"))
                s_head = html.escape(str(stg.get("headline", "—")))
                ts_ist = fmt_ist(stg.get("timestamp_ist")) if stg.get("timestamp_ist") else "—"
                ts_utc = str(stg.get("timestamp_utc") or "—")
                det: dict[str, Any] = stg.get("details") or {}

                kv_lines: list[str] = []
                for k, v in list(det.items())[:5]:
                    if isinstance(v, (dict, list)):
                        val_txt = f"{len(v)} item(s)"
                    else:
                        val_txt = str(v) if v is not None else "—"
                    kv_lines.append(
                        f'<div style="display:flex; justify-content:space-between; font-size:0.68rem; '
                        f'border-top:1px solid #1e293b; padding:0.15rem 0;">'
                        f'<span style="color:#94a3b8;">{html.escape(str(k))}</span>'
                        f'<span class="mono" style="color:#e2e8f0; max-width:140px; overflow:hidden; text-overflow:ellipsis; white-space:nowrap;">{html.escape(val_txt)}</span>'
                        f"</div>"
                    )

                st.markdown(
                    f"""
                    <div class="desk-panel" style="min-height:175px; margin-bottom:0.55rem;">
                        <div class="desk-panel-header">
                            <span class="desk-panel-title">{s_name}</span>
                            <span>{badge_html(s_stat)}</span>
                        </div>
                        <div style="padding:0.35rem 0.45rem;">
                            <div style="font-size:0.76rem; font-weight:600; color:#f8fafc; margin-bottom:0.25rem;">{s_head}</div>
                            <div class="mono" style="font-size:0.64rem; color:#94a3b8;">IST: {html.escape(ts_ist)}</div>
                            <div class="mono" style="font-size:0.64rem; color:#64748b; margin-bottom:0.3rem;">UTC: {html.escape(ts_utc)}</div>
                            {"".join(kv_lines)}
                        </div>
                    </div>
                    """,
                    unsafe_allow_html=True,
                )

    timeline: list[dict[str, Any]] = trace_data.get("audit_timeline", [])
    with st.expander(
        f"Inspect Chronological Correlation Audit Timeline ({len(timeline)} Immutable Events) & Stage JSON",
        expanded=False,
    ):
        if timeline:
            tl_rows: list[str] = []
            for ev in timeline:
                tl_rows.append(
                    f"<tr>"
                    f'<td class="mono">{html.escape(str(ev.get("occurred_at_utc", "—")))}</td>'
                    f'<td class="mono">{html.escape(fmt_ist(ev.get("occurred_at")))}</td>'
                    f'<td class="mono">{html.escape(str(ev.get("event_type") or ev.get("action", "")))}</td>'
                    f"<td>{badge_html(str(ev.get('severity', 'INFO')))}</td>"
                    f'<td class="mono">{html.escape(str(ev.get("source") or ev.get("actor", "")))}</td>'
                    f"<td>{html.escape(str(ev.get('summary', '')))}</td>"
                    f"</tr>"
                )
            st.markdown(
                f"""
                <table class="blotter-table">
                    <thead>
                        <tr>
                            <th>UTC Timestamp</th>
                            <th>IST Timestamp</th>
                            <th>Event Type</th>
                            <th>Severity</th>
                            <th>Source / Actor</th>
                            <th>Message</th>
                        </tr>
                    </thead>
                    <tbody>{"".join(tl_rows)}</tbody>
                </table>
                """,
                unsafe_allow_html=True,
            )
        st.json(stages)


def render(client: ControlCenterApiClient) -> None:
    render_screen_header(
        "AUDIT LOG & CORRELATION TRACE VIEW",
        "SIMULATED PAPER TRADING  |  APPEND-ONLY IMMUTABLE AUDIT LEDGER (IST +05:30 & UTC)  |  NO EDIT / NO DELETE",
    )

    _render_lifecycle_trace_panel(client)

    st.markdown(
        """
        <div class="control-bar" style="margin-bottom:0.65rem;">
            <div class="control-bar-title">APPEND-ONLY AUDIT COMPLIANCE GUARANTEE</div>
            <div class="control-bar-note">
                Historical audit events are protected by application-layer <code>409 IMMUTABILITY_VIOLATION</code> guards,
                SQLAlchemy <code>before_update</code> / <code>before_delete</code> hooks, and PostgreSQL triggers. No edit or delete operation exists.
            </div>
        </div>
        """,
        unsafe_allow_html=True,
    )

    f1, f2, f3, f4 = st.columns([1.1, 1.4, 1.0, 1.5])
    with f1:
        category = st.selectbox(
            "Category",
            [
                "ALL",
                "SYSTEM",
                "CONTROL",
                "RISK",
                "STRATEGY",
                "BACKTEST",
                "MARKET_DATA",
                "OMS",
                "EXECUTION",
                "POSITION",
                "RECONCILIATION",
                "INSTRUMENT",
                "ACCOUNT",
                "HEALTH",
            ],
        )
    with f2:
        event_type = st.selectbox("Canonical Event Type", CANONICAL_EVENT_TYPES)
    with f3:
        severity = st.selectbox("Severity", ["ALL", "INFO", "WARNING", "CRITICAL"])
    with f4:
        correlation_id = st.text_input(
            "Correlation ID Filter", placeholder="Exact correlation_id..."
        )

    s1, s2, s3, s4 = st.columns([1.2, 1.2, 1.2, 1.4])
    with s1:
        strategy_id = st.text_input("Strategy ID Filter", placeholder="Strategy / Version UUID...")
    with s2:
        signal_id = st.text_input("Signal ID Filter", placeholder="Signal UUID or code...")
    with s3:
        order_id = st.text_input("Order ID Filter", placeholder="Order UUID or code...")
    with s4:
        search = st.text_input("Search Message / Action", placeholder="Keyword...")

    with st.expander("Record Desk Operator Audit Note (Append-Only)", expanded=False):
        n1, n2, n3 = st.columns([1.3, 3.0, 1.0])
        with n1:
            note_action = st.text_input("Action Code", value="OPERATOR_DESK_CHECK")
        with n2:
            note_summary = st.text_input(
                "Audit Summary",
                placeholder="e.g. Verified morning pre-open simulated risk limits for NIFTY/BANKNIFTY",
            )
        with n3:
            st.write("")
            if st.button("LOG EVENT", use_container_width=True):
                if len(note_summary.strip()) < 3:
                    st.warning("Enter at least 3 characters for the summary.")
                else:
                    post_res = client.create_audit_note(
                        action=note_action.strip().upper(),
                        summary=note_summary.strip(),
                        actor="desk.operator",
                    )
                    if post_res.ok:
                        st.success("Persisted append-only audit event.")
                        st.rerun()
                    else:
                        st.error(post_res.error or "Failed to persist audit event")

    resp = client.list_audit_events(
        category=category,
        event_type=event_type,
        severity=severity,
        correlation_id=correlation_id.strip() or None,
        strategy_id=strategy_id.strip() or None,
        signal_id=signal_id.strip() or None,
        order_id=order_id.strip() or None,
        search=search.strip() or None,
        limit=150,
    )
    if not resp.ok or not isinstance(resp.data, list):
        render_api_offline_banner(resp.error or "Unable to fetch audit events", client.base_url)
        return

    events: list[dict[str, Any]] = resp.data
    if not events:
        render_empty_blotter(
            panel_title="Immutable Audit Events",
            panel_meta="0 MATCHING ROWS",
            columns=[
                ("UTC Timestamp", False),
                ("IST Timestamp", False),
                ("Event Type", False),
                ("Severity", False),
                ("Actor / Source", False),
                ("Entity", False),
                ("Refs (Strat/Sig/Ord)", False),
                ("Message", False),
                ("Correlation ID", False),
            ],
            empty_title="NO AUDIT EVENTS MATCH CURRENT FILTERS",
            empty_description="Clear filter criteria or trigger an operational command to inspect persisted events.",
        )
        return

    rows_html: list[str] = []
    for ev in events:
        ts_utc = html.escape(str(ev.get("occurred_at_utc") or "—"))
        ts_ist = html.escape(fmt_ist(ev.get("occurred_at")))
        ev_type = html.escape(str(ev.get("event_type") or ev.get("action", "")))
        sev = str(ev.get("severity", "INFO"))
        actor_src = html.escape(
            f"{ev.get('actor', '')} ({ev.get('source') or ev.get('category', '')})"
        )
        ent_txt = "—"
        if ev.get("entity_type"):
            eid_short = str(ev.get("entity_id") or "")[:10]
            ent_txt = f"{ev.get('entity_type')}:{eid_short}"
        refs_parts: list[str] = []
        if ev.get("strategy_id"):
            refs_parts.append(f"ST:{str(ev['strategy_id'])[:8]}")
        if ev.get("signal_id"):
            refs_parts.append(f"SG:{str(ev['signal_id'])[:8]}")
        if ev.get("order_id"):
            refs_parts.append(f"OR:{str(ev['order_id'])[:8]}")
        refs_txt = html.escape(" ".join(refs_parts) if refs_parts else "—")
        summary = html.escape(str(ev.get("summary", "")))
        cid = html.escape(str(ev.get("correlation_id", "")))
        details_str = html.escape(json.dumps(ev.get("details", {}), separators=(", ", ":")))
        rows_html.append(
            f'<tr title="{details_str}">'
            f'<td class="mono" style="font-size:0.68rem;">{ts_utc}</td>'
            f'<td class="mono" style="font-size:0.68rem;">{ts_ist}</td>'
            f'<td class="mono">{ev_type}</td>'
            f"<td>{badge_html(sev)}</td>"
            f"<td>{actor_src}</td>"
            f'<td class="mono" style="font-size:0.68rem;">{html.escape(ent_txt)}</td>'
            f'<td class="mono" style="font-size:0.66rem;">{refs_txt}</td>'
            f"<td>{summary}</td>"
            f'<td class="mono" style="font-size:0.66rem;">{cid}</td>'
            f"</tr>"
        )

    st.markdown(
        f"""
        <div class="desk-panel">
            <div class="desk-panel-header">
                <span class="desk-panel-title">Immutable Audit Events</span>
                <span class="desk-panel-meta">SHOWING {len(rows_html)} EVENTS (APPEND-ONLY POSTGRESQL LEDGER)</span>
            </div>
            <table class="blotter-table">
                <thead>
                    <tr>
                        <th>UTC Timestamp</th>
                        <th>IST Timestamp</th>
                        <th>Event Type</th>
                        <th>Severity</th>
                        <th>Actor / Source</th>
                        <th>Entity</th>
                        <th>Refs</th>
                        <th>Human-Readable Message</th>
                        <th>Correlation ID</th>
                    </tr>
                </thead>
                <tbody>{"".join(rows_html)}</tbody>
            </table>
        </div>
        """,
        unsafe_allow_html=True,
    )

    with st.expander("Inspect Structured Metadata Payload for Latest Filtered Event", expanded=False):
        st.json(events[0])
