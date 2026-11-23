"""Screen 11: System Health & Deterministic Reconciliation Console."""

from __future__ import annotations

import html
from typing import Any

import streamlit as st

from frontend.api_client import ControlCenterApiClient
from frontend.components.ui import (
    COMPONENT_LABELS,
    badge_html,
    render_api_offline_banner,
    render_empty_blotter,
    render_screen_header,
    render_status_strip,
)
from frontend.formatting import fmt_ist


def _render_reconciliation_section(client: ControlCenterApiClient) -> None:
    """Render deterministic non-silent ledger reconciliation controls & audit history."""
    st.markdown(
        """
        <div class="desk-panel" style="margin-top:0.8rem; margin-bottom:0.6rem; border-left:3px solid #38bdf8;">
            <div class="desk-panel-header">
                <span class="desk-panel-title">DETERMINISTIC LEDGER RECONCILIATION ENGINE</span>
                <span class="desk-panel-meta">EXECUTIONS VS POSITIONS &bull; OPEN ORDERS &bull; POSITION QUANTITIES &bull; REALIZED P&amp;L INTEGRITY</span>
            </div>
            <div style="padding:0.4rem 0.55rem; font-size:0.75rem; color:#cbd5e1;">
                <strong>Non-Silent Guarantee:</strong> Verification runs (<code>apply_repair=False</code>) NEVER mutate drifted state silently;
                any mismatch records <code>DISCREPANCY_DETECTED</code> and emits <code>RECONCILIATION_FAILED</code>.
                Explicit repair runs record exact <code>before &rarr; after</code> field diffs in <code>reconciliation_runs</code> and the immutable Audit Log.
            </div>
        </div>
        """,
        unsafe_allow_html=True,
    )

    rc1, rc2, rc3 = st.columns([1.6, 1.9, 1.5])
    with rc1:
        verify_clicked = st.button(
            "RUN DETERMINISTIC RECONCILIATION (VERIFY ONLY)",
            use_container_width=True,
            type="primary",
        )
    with rc2:
        confirm_repair = st.checkbox(
            "Confirm explicit ledger repair (records before -> after diffs)",
            value=False,
            key="confirm_recon_repair",
        )
    with rc3:
        repair_clicked = st.button(
            "REPAIR & RECORD CHANGES",
            use_container_width=True,
            disabled=not confirm_repair,
        )

    if verify_clicked:
        r_res = client.run_deterministic_reconciliation(
            apply_repair=False, actor="desk.operator"
        )
        if r_res.ok and isinstance(r_res.data, dict):
            r_stat = r_res.data.get("status", "COMPLETED")
            disc_n = int(r_res.data.get("discrepancies_count", 0))
            if disc_n == 0:
                st.success(
                    f"Reconciliation {r_res.data.get('run_code')} COMPLETED — 0 discrepancies across executions, open orders, positions & P&L."
                )
            else:
                st.error(
                    f"Reconciliation {r_res.data.get('run_code')} detected {disc_n} discrepancy(ies) ({r_stat}). No silent repair was performed."
                )
            st.rerun()
        else:
            st.error(r_res.error or "Reconciliation check failed")

    if repair_clicked and confirm_repair:
        fix_res = client.run_deterministic_reconciliation(
            apply_repair=True, actor="desk.operator"
        )
        if fix_res.ok and isinstance(fix_res.data, dict):
            rep_n = int(fix_res.data.get("repairs_count", 0))
            st.warning(
                f"Reconciliation {fix_res.data.get('run_code')} finished ({fix_res.data.get('status')}) — {rep_n} explicit repair(s) recorded to audit trail."
            )
            st.rerun()
        else:
            st.error(fix_res.error or "Explicit reconciliation repair failed")

    runs_resp = client.list_reconciliation_runs(limit=20)
    if runs_resp.ok and isinstance(runs_resp.data, dict):
        runs: list[dict[str, Any]] = list(runs_resp.data.get("runs", []))
    elif runs_resp.ok and isinstance(runs_resp.data, list):
        runs = runs_resp.data
    else:
        runs = []
    if not runs:
        render_empty_blotter(
            panel_title="Reconciliation Run History",
            panel_meta="0 RECORDED RUNS",
            columns=[
                ("Completed (IST)", False),
                ("Run Code", False),
                ("Status", False),
                ("Fills", True),
                ("Open Orders", True),
                ("Positions", True),
                ("Discrepancies", True),
                ("Recorded Repairs", True),
                ("Actor", False),
            ],
            empty_title="NO RECONCILIATION RUNS RECORDED YET",
            empty_description="Click 'RUN DETERMINISTIC RECONCILIATION (VERIFY ONLY)' above to verify executions, orders, positions, and P&L.",
        )
        return

    latest = runs[0]
    checks: dict[str, Any] = latest.get("checks", {})
    check_defs = [
        ("executions_vs_positions", "1. Executions vs Positions"),
        ("open_orders", "2. Open Orders Invariants"),
        ("position_quantities", "3. Position Quantities"),
        ("realized_pnl_integrity", "4. Realized P&L Integrity"),
    ]
    chk_cols = st.columns(4)
    for idx, (c_key, c_title) in enumerate(check_defs):
        c_info: dict[str, Any] = checks.get(c_key, {})
        passed = bool(c_info.get("passed", True))
        disc_c = int(c_info.get("discrepancies", 0))
        b_label = "HEALTHY" if passed else "BLOCKED"
        with chk_cols[idx]:
            st.markdown(
                f"""
                <div class="desk-panel" style="margin-bottom:0.55rem;">
                    <div class="desk-panel-header">
                        <span class="desk-panel-title">{html.escape(c_title)}</span>
                        <span>{badge_html(b_label)}</span>
                    </div>
                    <div style="padding:0.35rem 0.5rem; font-size:0.74rem; color:#cbd5e1;">
                        Discrepancies: <span class="mono"><strong>{disc_c}</strong></span> &bull;
                        Run: <span class="mono">{html.escape(str(latest.get("run_code", "")))}</span>
                    </div>
                </div>
                """,
                unsafe_allow_html=True,
            )

    # Render Discrepancies & Recorded Repairs on latest run when present
    latest_discs: list[dict[str, Any]] = latest.get("discrepancies", [])
    latest_reps: list[dict[str, Any]] = latest.get("repairs", [])
    if latest_discs or latest_reps:
        d_rows: list[str] = []
        for d in latest_discs:
            d_rows.append(
                f"<tr>"
                f'<td class="mono">{html.escape(str(d.get("check", "")))}</td>'
                f'<td class="mono">{html.escape(str(d.get("code", "")))}</td>'
                f'<td class="mono">{html.escape(str(d.get("field", "")))}</td>'
                f'<td class="num">{html.escape(str(d.get("expected", "")))}</td>'
                f'<td class="num">{html.escape(str(d.get("actual", "")))}</td>'
                f"<td>{html.escape(str(d.get('detail', '')))}</td>"
                f"</tr>"
            )
        if d_rows:
            st.markdown(
                f"""
                <div class="desk-panel" style="margin-bottom:0.55rem;">
                    <div class="desk-panel-header">
                        <span class="desk-panel-title">Detected Discrepancies ({html.escape(str(latest.get("run_code", "")))})</span>
                        <span class="desk-panel-meta">{len(d_rows)} ITEM(S)</span>
                    </div>
                    <table class="blotter-table">
                        <thead>
                            <tr>
                                <th>Check</th>
                                <th>Entity Code</th>
                                <th>Field</th>
                                <th class="num">Expected</th>
                                <th class="num">Actual</th>
                                <th>Diagnostic Detail</th>
                            </tr>
                        </thead>
                        <tbody>{"".join(d_rows)}</tbody>
                    </table>
                </div>
                """,
                unsafe_allow_html=True,
            )

        if latest_reps:
            rep_rows: list[str] = []
            for rp in latest_reps:
                rep_rows.append(
                    f"<tr>"
                    f'<td class="mono">{html.escape(str(rp.get("entity_type", "")))}</td>'
                    f'<td class="mono">{html.escape(str(rp.get("code", "")))}</td>'
                    f'<td class="mono">{html.escape(str(rp.get("field", "")))}</td>'
                    f'<td class="num">{html.escape(str(rp.get("before", "")))}</td>'
                    f'<td class="num">{html.escape(str(rp.get("after", "")))}</td>'
                    f"<td>{html.escape(str(rp.get('reason', '')))}</td>"
                    f"</tr>"
                )
            st.markdown(
                f"""
                <div class="desk-panel" style="margin-bottom:0.55rem;">
                    <div class="desk-panel-header">
                        <span class="desk-panel-title">Recorded Explicit Repairs (Before &rarr; After Audit Ledger)</span>
                        <span class="desk-panel-meta">{len(rep_rows)} RECORDED CHANGE(S)</span>
                    </div>
                    <table class="blotter-table">
                        <thead>
                            <tr>
                                <th>Entity Type</th>
                                <th>Code</th>
                                <th>Field</th>
                                <th class="num">Before</th>
                                <th class="num">After</th>
                                <th>Audit Reason</th>
                            </tr>
                        </thead>
                        <tbody>{"".join(rep_rows)}</tbody>
                    </table>
                </div>
                """,
                unsafe_allow_html=True,
            )

    run_rows_html: list[str] = []
    for r in runs:
        run_rows_html.append(
            f"<tr>"
            f'<td class="mono">{html.escape(fmt_ist(r.get("completed_at")))}</td>'
            f'<td class="mono">{html.escape(str(r.get("run_code", "")))}</td>'
            f"<td>{badge_html(str(r.get('status', 'COMPLETED')))}</td>"
            f'<td class="num">{int(r.get("fills_checked", 0))}</td>'
            f'<td class="num">{int(r.get("open_orders_checked", 0))}</td>'
            f'<td class="num">{int(r.get("positions_checked", 0))}</td>'
            f'<td class="num">{int(r.get("discrepancies_count", 0))}</td>'
            f'<td class="num">{int(r.get("repairs_count", 0))}</td>'
            f'<td class="mono">{html.escape(str(r.get("actor", "")))}</td>'
            f'<td class="mono" style="font-size:0.66rem;">{html.escape(str(r.get("correlation_id", "")))}</td>'
            f"</tr>"
        )

    st.markdown(
        f"""
        <div class="desk-panel" style="margin-bottom:0.75rem;">
            <div class="desk-panel-header">
                <span class="desk-panel-title">Immutable Reconciliation Run Ledger</span>
                <span class="desk-panel-meta">LATEST {len(run_rows_html)} RUNS</span>
            </div>
            <table class="blotter-table">
                <thead>
                    <tr>
                        <th>Completed (IST)</th>
                        <th>Run Code</th>
                        <th>Status</th>
                        <th class="num">Fills</th>
                        <th class="num">Open Orders</th>
                        <th class="num">Positions</th>
                        <th class="num">Discrepancies</th>
                        <th class="num">Recorded Repairs</th>
                        <th>Actor</th>
                        <th>Correlation ID</th>
                    </tr>
                </thead>
                <tbody>{"".join(run_rows_html)}</tbody>
            </table>
        </div>
        """,
        unsafe_allow_html=True,
    )


def render(client: ControlCenterApiClient) -> None:
    render_screen_header(
        "SYSTEM HEALTH & RECONCILIATION",
        "9-SUBSYSTEM OPERATIONAL MATRIX (HEALTHY / DEGRADED / STALE / BLOCKED / STOPPED) & DETERMINISTIC RECONCILIATION",
    )

    top_l, top_r = st.columns([4.0, 1.2])
    with top_r:
        probe_clicked = st.button("RUN ACTIVE DB PROBE", use_container_width=True)

    resp = client.trigger_health_probe() if probe_clicked else client.get_system_health()
    if not resp.ok or not isinstance(resp.data, dict):
        render_api_offline_banner(resp.error or "Backend health endpoint unreachable", client.base_url)
        return

    data = resp.data
    op_components: list[dict[str, Any]] = (
        data.get("operational_components") or data.get("components", [])
    )
    render_status_strip(op_components)

    db_info: dict[str, Any] = data.get("database", {})
    db_details: dict[str, Any] = db_info.get("details", {})

    c_left, c_right = st.columns([1.35, 0.95])

    with c_left:
        comp_rows: list[str] = []
        for comp in op_components:
            c_key = str(comp.get("component", ""))
            label = html.escape(COMPONENT_LABELS.get(c_key, c_key))
            status = str(comp.get("status", "STOPPED"))
            lat = comp.get("latency_ms")
            lat_str = f"{lat} ms" if lat is not None else "—"
            msg = html.escape(str(comp.get("message", "")))
            comp_rows.append(
                f"<tr>"
                f"<td><strong>{label}</strong></td>"
                f"<td>{badge_html(status)}</td>"
                f'<td class="num">{html.escape(lat_str)}</td>'
                f"<td>{msg}</td>"
                f"</tr>"
            )

        st.markdown(
            f"""
            <div class="desk-panel">
                <div class="desk-panel-header">
                    <span class="desk-panel-title">9-Component Operational Status Matrix</span>
                    <span class="desk-panel-meta">OVERALL: {badge_html(str(data.get("overall_status", "HEALTHY")))} &bull; {html.escape(fmt_ist(data.get("checked_at")))}</span>
                </div>
                <table class="blotter-table">
                    <thead>
                        <tr>
                            <th>Subsystem</th>
                            <th>Status</th>
                            <th class="num">Latency</th>
                            <th>Operational Diagnostic</th>
                        </tr>
                    </thead>
                    <tbody>{"".join(comp_rows)}</tbody>
                </table>
            </div>
            """,
            unsafe_allow_html=True,
        )

    with c_right:
        st.markdown(
            f"""
            <div class="desk-panel">
                <div class="desk-panel-header">
                    <span class="desk-panel-title">PostgreSQL / Neon Persistence Telemetry</span>
                    <span class="desk-panel-meta">{badge_html(str(db_info.get("status", "STOPPED")))}</span>
                </div>
                <table class="blotter-table">
                    <tbody>
                        <tr>
                            <td>Masked DSN</td>
                            <td class="num">{html.escape(str(db_details.get("masked_dsn", "—")))}</td>
                        </tr>
                        <tr>
                            <td>SQL Dialect</td>
                            <td class="num">{html.escape(str(db_details.get("dialect") or "—"))}</td>
                        </tr>
                        <tr>
                            <td>Session Timezone</td>
                            <td class="num">{html.escape(str(db_details.get("db_timezone") or "Asia/Kolkata"))}</td>
                        </tr>
                        <tr>
                            <td>Alembic Schema Revision</td>
                            <td class="num">{html.escape(str(db_details.get("alembic_version") or "unmigrated"))}</td>
                        </tr>
                        <tr>
                            <td>Ping Latency</td>
                            <td class="num">{html.escape(str(db_info.get("latency_ms") or "—"))} ms</td>
                        </tr>
                        <tr>
                            <td>Allowed States</td>
                            <td class="num">HEALTHY / DEGRADED / STALE / BLOCKED / STOPPED</td>
                        </tr>
                    </tbody>
                </table>
            </div>
            """,
            unsafe_allow_html=True,
        )

    _render_reconciliation_section(client)

    events: list[dict[str, Any]] = data.get("recent_events", [])
    if not events:
        render_empty_blotter(
            panel_title="Persisted System Health Events",
            panel_meta="0 RECORDED EVENTS",
            columns=[
                ("Recorded (IST)", False),
                ("Component", False),
                ("Status", False),
                ("Latency", True),
                ("Message", False),
            ],
            empty_title="NO PERSISTED HEALTH EVENTS YET",
            empty_description="Click 'RUN ACTIVE DB PROBE' above to verify and persist a PostgreSQL health event.",
        )
    else:
        ev_rows: list[str] = []
        for ev in events[:25]:
            rec = html.escape(fmt_ist(ev.get("recorded_at")))
            comp = html.escape(str(ev.get("component", "")))
            st_val = str(ev.get("status", "STOPPED"))
            lat = ev.get("latency_ms")
            lat_txt = f"{lat} ms" if lat is not None else "—"
            msg = html.escape(str(ev.get("message", "")))
            ev_rows.append(
                f"<tr>"
                f'<td class="mono">{rec}</td>'
                f'<td class="mono">{comp}</td>'
                f"<td>{badge_html(st_val)}</td>"
                f'<td class="num">{html.escape(lat_txt)}</td>'
                f"<td>{msg}</td>"
                f"</tr>"
            )
        st.markdown(
            f"""
            <div class="desk-panel">
                <div class="desk-panel-header">
                    <span class="desk-panel-title">Persisted System Health Events</span>
                    <span class="desk-panel-meta">LATEST {len(ev_rows)} PROBES / TRANSITIONS</span>
                </div>
                <table class="blotter-table">
                    <thead>
                        <tr>
                            <th>Recorded (IST)</th>
                            <th>Component</th>
                            <th>Status</th>
                            <th class="num">Latency</th>
                            <th>Message</th>
                        </tr>
                    </thead>
                    <tbody>{"".join(ev_rows)}</tbody>
                </table>
            </div>
            """,
            unsafe_allow_html=True,
        )
