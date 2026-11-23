"""Screen 12: Settings — Environment inspection (zero secrets) & configurable instruments."""

from __future__ import annotations

import html
from typing import Any

import streamlit as st

from frontend.api_client import ControlCenterApiClient
from frontend.components.ui import badge_html, render_api_offline_banner, render_screen_header
from frontend.formatting import fmt_ist


def render(client: ControlCenterApiClient) -> None:
    render_screen_header(
        "SETTINGS",
        "ENVIRONMENT CONFIGURATION (MASKED SECRETS) & CONFIGURABLE INSTRUMENT REFERENCE DATA",
    )

    cfg_res = client.get_settings()
    if not cfg_res.ok or not isinstance(cfg_res.data, dict):
        render_api_offline_banner(cfg_res.error or "Unable to load runtime settings", client.base_url)
        return

    cfg = cfg_res.data
    fyers_en = bool(cfg.get("fyers_historical_data_enabled", True))
    fyers_tok = bool(cfg.get("fyers_token_configured", False))
    fyers_mode = html.escape(str(cfg.get("fyers_auth_mode", "UNCONFIGURED")))
    fyers_tok_status = "CONFIGURED" if fyers_tok else "FYERS_AUTHENTICATION_REQUIRED"
    st.markdown(
        f"""
        <div class="desk-panel">
            <div class="desk-panel-header">
                <span class="desk-panel-title">Stateless Container Runtime Configuration</span>
                <span class="desk-panel-meta">INSPECTED: {html.escape(fmt_ist(cfg.get("timestamp_ist")))}</span>
            </div>
            <table class="blotter-table">
                <tbody>
                    <tr><td>Application Environment</td><td class="num">{html.escape(str(cfg.get("app_env")))} (v{html.escape(str(cfg.get("app_version")))})</td></tr>
                    <tr><td>Locked Execution Mode</td><td class="num">{badge_html("INFO")} &nbsp;{html.escape(str(cfg.get("execution_mode")))}</td></tr>
                    <tr><td>Market Data Mode</td><td class="num">{html.escape(str(cfg.get("market_mode")))}</td></tr>
                    <tr><td>FYERS Historical Data Ingestion</td><td class="num">{badge_html("ACTIVE" if fyers_en else "DISABLED")} &nbsp;{"HISTORICAL READ-ONLY" if fyers_en else "DISABLED"}</td></tr>
                    <tr><td>FYERS Historical Credential State</td><td class="num">{badge_html(fyers_tok_status)} &nbsp;[{fyers_mode}]</td></tr>
                    <tr><td>System Timezone</td><td class="num">{html.escape(str(cfg.get("timezone")))} (IST +05:30)</td></tr>
                    <tr><td>FastAPI Endpoint</td><td class="num">{html.escape(str(cfg.get("api_base_url")))}</td></tr>
                    <tr><td>PostgreSQL Connection (Masked)</td><td class="num">{html.escape(str(cfg.get("masked_database_url")))}</td></tr>
                    <tr><td>SQLAlchemy Pool (Size / Max Overflow / Recycle)</td><td class="num">{cfg.get("db_pool_size")} / {cfg.get("db_max_overflow")} / {cfg.get("db_pool_recycle_seconds")}s</td></tr>
                    <tr><td>Alembic Schema Revision</td><td class="num">{html.escape(str(cfg.get("alembic_version") or "unmigrated"))}</td></tr>
                </tbody>
            </table>
        </div>
        """,
        unsafe_allow_html=True,
    )

    inst_res = client.list_instruments()
    if not inst_res.ok or not isinstance(inst_res.data, list):
        return

    instruments: list[dict[str, Any]] = inst_res.data
    rows: list[str] = []
    for inst in instruments:
        sym = html.escape(str(inst.get("symbol", "")))
        name = html.escape(str(inst.get("display_name", "")))
        prov = html.escape(str(inst.get("data_provider", "FYERS")))
        prov_sym = html.escape(str(inst.get("provider_symbol", "—")))
        exch = html.escape(str(inst.get("exchange", "")))
        lot = int(inst.get("lot_size", 1))
        tick = html.escape(str(inst.get("tick_size", "0.05")))
        sess = f"{html.escape(str(inst.get('session_open_ist')))} - {html.escape(str(inst.get('session_close_ist')))} IST"
        act_badge = badge_html("ACTIVE" if inst.get("is_active") else "STOPPED")
        rows.append(
            f"<tr>"
            f'<td class="mono"><strong>{sym}</strong></td>'
            f"<td>{name}</td>"
            f'<td class="mono">{prov}</td>'
            f'<td class="mono" style="color:#38BDF8;">{prov_sym}</td>'
            f'<td class="mono">{exch}</td>'
            f'<td class="num">{lot}</td>'
            f'<td class="num">{tick}</td>'
            f'<td class="num">{sess}</td>'
            f"<td>{act_badge}</td>"
            f"</tr>"
        )

    st.markdown(
        f"""
        <div class="desk-panel">
            <div class="desk-panel-header">
                <span class="desk-panel-title">Configurable Instrument Reference Data (PostgreSQL)</span>
                <span class="desk-panel-meta">LOT SIZES &amp; EXCHANGE SPECS ARE EDITABLE REFERENCE DATA</span>
            </div>
            <table class="blotter-table">
                <thead>
                    <tr>
                        <th>Symbol</th>
                        <th>Display Name</th>
                        <th>Provider</th>
                        <th>Provider Symbol</th>
                        <th>Exchange</th>
                        <th class="num">Lot Size</th>
                        <th class="num">Tick Size</th>
                        <th class="num">Trading Hours</th>
                        <th>Status</th>
                    </tr>
                </thead>
                <tbody>{"".join(rows)}</tbody>
            </table>
        </div>
        """,
        unsafe_allow_html=True,
    )

    if instruments:
        with st.expander("Update Configurable Instrument Reference Parameters", expanded=False):
            symbols = [str(i["symbol"]) for i in instruments]
            selected_sym = st.selectbox("Select Instrument", symbols)
            current = next(i for i in instruments if i["symbol"] == selected_sym)
            with st.form("update_instrument_form"):
                u1, u2, u3, u4 = st.columns(4)
                with u1:
                    new_lot = st.number_input(
                        "Lot Size", min_value=1, max_value=10000, value=int(current["lot_size"])
                    )
                with u2:
                    new_tick = st.text_input("Tick Size (INR)", value=str(current["tick_size"]))
                with u3:
                    new_open = st.text_input("Session Open (IST)", value=str(current["session_open_ist"]))
                with u4:
                    new_close = st.text_input("Session Close (IST)", value=str(current["session_close_ist"]))
                new_active = st.checkbox("Instrument Enabled", value=bool(current["is_active"]))
                if st.form_submit_button("SAVE REFERENCE PARAMETERS"):
                    upd = client.update_instrument(
                        selected_sym,
                        lot_size=int(new_lot),
                        tick_size=new_tick.strip(),
                        session_open_ist=new_open.strip(),
                        session_close_ist=new_close.strip(),
                        is_active=new_active,
                        actor="desk.operator",
                    )
                    if upd.ok:
                        st.success(f"Updated {selected_sym} reference spec and logged audit event.")
                        st.rerun()
                    else:
                        st.error(upd.error or "Failed to update instrument")

