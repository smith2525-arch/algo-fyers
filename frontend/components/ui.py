"""Reusable HTML/CSS institutional trading desk widgets (stateless presentation)."""

from __future__ import annotations

import html
from pathlib import Path
from typing import Any

import streamlit as st

from frontend.formatting import now_ist_str

_CSS_PATH = Path(__file__).resolve().parent.parent / "theme" / "terminal.css"

COMPONENT_LABELS: dict[str, str] = {
    "API_SERVER": "API",
    "DATABASE": "Database",
    "MARKET_REPLAY": "Market Replay",
    "MARKET_DATA": "Market Data",
    "STRATEGY_ENGINE": "Strategy Engine",
    "RISK_ENGINE": "Risk Engine",
    "PAPER_BROKER": "Paper Broker",
    "POSITION_ENGINE": "Position Engine",
    "LAST_RECONCILIATION": "Last Reconciliation",
}


def inject_terminal_css() -> None:
    """Load compact dark trading-desk CSS into the active Streamlit page."""
    css_text = _CSS_PATH.read_text(encoding="utf-8")
    st.markdown(f"<style>{css_text}</style>", unsafe_allow_html=True)


def render_persistent_safety_banner() -> None:
    """Render the mandatory persistent simulated-trading safety banner."""
    st.markdown(
        f"""
        <div class="paper-safety-banner">
            <span>SIMULATED PAPER TRADING &nbsp;|&nbsp; PAPER TRADING &nbsp;|&nbsp; SIMULATED EXECUTION &nbsp;|&nbsp; NO REAL ORDERS</span>
            <span class="banner-tag">MODE: PAPER_ONLY &bull; {html.escape(now_ist_str())}</span>
        </div>
        """,
        unsafe_allow_html=True,
    )


def render_screen_header(title: str, subtitle: str) -> None:
    """Render compact institutional screen title bar."""
    st.markdown(
        f"""
        <div class="terminal-header">
            <h1 class="terminal-title">{html.escape(title)}</h1>
            <span class="terminal-subtitle">{html.escape(subtitle)}</span>
        </div>
        """,
        unsafe_allow_html=True,
    )


def badge_html(status_value: str) -> str:
    """Map status/severity string to canonical semantic badge HTML."""
    norm = (status_value or "STOPPED").strip().upper()
    css_map = {
        "HEALTHY": "badge-healthy",
        "PERMITTED": "badge-healthy",
        "ACTIVE": "badge-healthy",
        "RUNNING": "badge-healthy",
        "SIGNAL_GENERATED": "badge-healthy",
        "ENTRY_LONG": "badge-healthy",
        "BUY": "badge-healthy",
        "COMPLETED": "badge-healthy",
        "LONG": "badge-healthy",
        "PASS": "badge-healthy",
        "FILLED": "badge-healthy",
        "OPEN": "badge-healthy",
        "RISK_APPROVED": "badge-healthy",
        "APPROVED": "badge-healthy",
        "RECONCILED": "badge-healthy",
        "REPAIRED": "badge-warning",
        "DISCREPANCY_DETECTED": "badge-blocked",
        "CAPTURED": "badge-healthy",
        "REALIZED": "badge-healthy",
        "VALID": "badge-healthy",
        "AVAILABLE": "badge-healthy",
        "DEDUPLICATED": "badge-info",
        "MARK_TO_MARKET": "badge-info",
        "VALIDATED": "badge-info",
        "CONFIGURED": "badge-info",
        "EVALUATING_NO_SIGNAL": "badge-info",
        "GENERATED": "badge-info",
        "CREATED": "badge-info",
        "SUBMITTED": "badge-info",
        "ACKNOWLEDGED": "badge-info",
        "ORDER_CREATED": "badge-info",
        "CLOSED": "badge-stopped",
        "DISABLED": "badge-stopped",
        "DEGRADED": "badge-degraded",
        "STALE": "badge-stale",
        "PAUSED": "badge-stale",
        "PARTIALLY_FILLED": "badge-warning",
        "PARTIAL": "badge-warning",
        "VALID_WITH_WARNINGS": "badge-warning",
        "AUTHENTICATION_REQUIRED": "badge-warning",
        "FYERS_AUTHENTICATION_REQUIRED": "badge-warning",
        "EXPIRED": "badge-stale",
        "CANCELLED": "badge-stopped",
        "PAUSED_RECOVERY": "badge-warning",
        "WARMING_UP": "badge-warning",
        "OUTSIDE_TRADING_WINDOW": "badge-stale",
        "DAILY_LIMIT_REACHED": "badge-warning",
        "INSUFFICIENT_DATA": "badge-stale",
        "WARNING": "badge-warning",
        "COMPLETED_WITH_WARNINGS": "badge-warning",
        "DRAFT": "badge-warning",
        "BLOCKED": "badge-blocked",
        "HALTED_BY_OPERATOR": "badge-blocked",
        "ENTRY_SHORT": "badge-blocked",
        "EXIT_LONG": "badge-warning",
        "EXIT_SHORT": "badge-warning",
        "SELL": "badge-blocked",
        "SHORT": "badge-blocked",
        "HALTED": "badge-blocked",
        "RISK_REJECTED": "badge-blocked",
        "REJECTED": "badge-blocked",
        "FAILED": "badge-critical",
        "INVALID": "badge-critical",
        "DOWNLOAD_FAILED": "badge-critical",
        "CRITICAL": "badge-critical",
        "FAILED_VALIDATION": "badge-critical",
        "FAIL": "badge-critical",
        "ERROR": "badge-critical",
        "STOPPED": "badge-stopped",
        "INFO": "badge-info",
    }
    cls = css_map.get(norm, "badge-stopped")
    return f'<span class="status-badge {cls}">{html.escape(norm)}</span>'


def render_status_strip(components: list[dict[str, Any]]) -> None:
    """Render the 5-subsystem operational status strip."""
    cells: list[str] = []
    for item in components:
        raw_comp = str(item.get("component", ""))
        label = COMPONENT_LABELS.get(raw_comp, raw_comp)
        status = str(item.get("status", "STOPPED"))
        msg = html.escape(str(item.get("message", "")))
        cells.append(
            f'<div class="status-cell" title="{msg}">'
            f'<span class="status-comp-name">{html.escape(label)}</span>'
            f"{badge_html(status)}"
            f"</div>"
        )
    st.markdown(f'<div class="status-strip">{"".join(cells)}</div>', unsafe_allow_html=True)


def render_empty_blotter(
    *,
    panel_title: str,
    panel_meta: str,
    columns: list[tuple[str, bool]],
    empty_title: str,
    empty_description: str,
) -> None:
    """Render an institutional blotter header with a clean truthful empty state."""
    ths = "".join(
        f'<th class="{"num" if is_num else ""}">{html.escape(col_name)}</th>'
        for col_name, is_num in columns
    )
    st.markdown(
        f"""
        <div class="desk-panel">
            <div class="desk-panel-header">
                <span class="desk-panel-title">{html.escape(panel_title)}</span>
                <span class="desk-panel-meta">{html.escape(panel_meta)}</span>
            </div>
            <table class="blotter-table">
                <thead><tr>{ths}</tr></thead>
            </table>
            <div class="empty-state-box" style="margin-top:0.45rem;">
                <div class="empty-state-title">{html.escape(empty_title)}</div>
                <div class="empty-state-desc">{html.escape(empty_description)}</div>
            </div>
        </div>
        """,
        unsafe_allow_html=True,
    )


def render_api_offline_banner(error_message: str, base_url: str) -> None:
    """Render professional scale-to-zero wake-up / degraded connectivity banner with manual retry."""
    st.markdown(
        f"""
        <div class="control-bar halted" style="border-left-color:#F59E0B; background:rgba(30, 41, 59, 0.85); border-color:#F59E0B;">
            <div class="control-bar-title">
                TRADING CONTROL CENTER SERVICES ARE STARTING &mdash; {badge_html("WARMING_UP")}
                <span style="font-size:0.68rem; color:#94A3B8; margin-left:0.45rem;">[BACKEND API CONNECTION DEGRADED]</span>
            </div>
            <div class="control-bar-note" style="color:#E2E8F0; margin-top:0.25rem;">
                Trading Control Center services are starting. This demo uses scale-to-zero hosting and may take a short time after inactivity.
            </div>
            <div class="control-bar-note" style="margin-top:0.20rem;">
                Target API: <span class="mono">{html.escape(base_url)}</span> &bull;
                Diagnostic: {html.escape(error_message)}
            </div>
        </div>
        """,
        unsafe_allow_html=True,
    )
    r_col1, _ = st.columns([1.4, 4.6])
    with r_col1:
        if st.button("RETRY CONNECTION", use_container_width=True, key="btn_manual_wakeup_retry"):
            st.rerun()


def resolve_ui_data_source_labels(
    source_type: str | None, provider: str | None = None
) -> tuple[str, str | None, str, str]:
    """Return `(headline, source_line_or_none, execution_line, accent_hex)` for unmistakable UI labeling."""
    src = (source_type or "").strip().upper()
    prov = (provider or "").strip().upper()
    if src == "FYERS_HISTORICAL" or prov == "FYERS":
        return (
            "REAL HISTORICAL MARKET DATA",
            "SOURCE: FYERS",
            "EXECUTION: SIMULATED PAPER TRADING",
            "#10B981",
        )
    if src == "IMPORTED_CSV" or prov == "CSV":
        return (
            "USER-UPLOADED HISTORICAL DATA",
            None,
            "EXECUTION: SIMULATED PAPER TRADING",
            "#38BDF8",
        )
    return (
        "SYNTHETIC DEMO DATA",
        None,
        "EXECUTION: SIMULATED PAPER TRADING",
        "#F59E0B",
    )


def render_data_source_banner(
    *,
    source_type: str | None,
    provider: str | None = None,
    dataset_id: str | None = None,
    quality_status: str | None = None,
) -> None:
    """Render unmistakable active data-source & simulated-execution strip."""
    headline, src_line, exec_line, accent = resolve_ui_data_source_labels(source_type, provider)
    src_span = (
        f'<span class="mono" style="font-size:0.74rem; font-weight:700; color:#E2E8F0; margin-left:0.65rem;">'
        f"&bull; {html.escape(src_line)}</span>"
        if src_line
        else ""
    )
    ds_span = (
        f'<span class="mono" style="font-size:0.70rem; color:#CBD5E1;">DATASET: <strong>{html.escape(dataset_id)}</strong></span>'
        if dataset_id
        else ""
    )
    qual_span = (
        f"&nbsp;{badge_html(quality_status)}" if quality_status else ""
    )
    st.markdown(
        f"""
        <div style="background:rgba(15, 23, 42, 0.92); border:1px solid {accent};
                    border-left:5px solid {accent}; border-radius:3px;
                    padding:0.42rem 0.80rem; margin-bottom:0.55rem; display:flex;
                    justify-content:space-between; align-items:center; flex-wrap:wrap; gap:0.45rem;">
            <div>
                <span class="mono" style="font-size:0.80rem; font-weight:800; color:{accent}; letter-spacing:0.06em;">
                    {html.escape(headline)}
                </span>
                {src_span}
                <span class="mono" style="font-size:0.74rem; font-weight:700; color:#FBBF24; margin-left:0.65rem;">
                    &bull; {html.escape(exec_line)}
                </span>
            </div>
            <div>
                {ds_span}{qual_span}
            </div>
        </div>
        """,
        unsafe_allow_html=True,
    )

