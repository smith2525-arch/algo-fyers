"""Historical Data — FYERS Historical Market-Data Ingestion, CSV Upload/Export & Quality Desk.

CRITICAL SAFETY INVARIANT:
- FYERS is used EXCLUSIVELY to retrieve historical OHLCV market data via FastAPI.
- Zero FYERS credentials exist in this Streamlit frontend.
- Execution across Backtesting and Market Replay remains 100% SIMULATED PAPER TRADING.
"""

from __future__ import annotations

import html
from datetime import date, timedelta
from typing import Any

import streamlit as st

from frontend.api_client import ControlCenterApiClient
from frontend.components.ui import (
    badge_html,
    render_api_offline_banner,
    render_data_source_banner,
    render_empty_blotter,
    render_screen_header,
)
from frontend.formatting import fmt_ist

DATA_SOURCE_OPTIONS = [
    "FYERS Historical Data",
    "Upload CSV",
    "Synthetic Demo Data",
]


def _render_instrument_reference_strip(instruments: list[dict[str, Any]]) -> None:
    if not instruments:
        return
    rows_html = "".join(
        f"<tr>"
        f"<td class='mono'><strong>{html.escape(str(i.get('internal_symbol', '')))}</strong></td>"
        f"<td>{html.escape(str(i.get('display_name', '')))}</td>"
        f"<td class='mono'>{html.escape(str(i.get('data_provider', 'FYERS')))}</td>"
        f"<td class='mono' style='color:#38BDF8;'>{html.escape(str(i.get('provider_symbol', '—')))}</td>"
        f"<td class='mono'>{html.escape(str(i.get('exchange', 'NSE')))}</td>"
        f"<td class='mono'>{html.escape(str(i.get('instrument_type', '')))}</td>"
        f"<td>{badge_html('ACTIVE' if i.get('is_active', True) else 'STOPPED')}</td>"
        f"</tr>"
        for i in instruments
    )
    st.markdown(
        f"""
        <div class="desk-panel" style="margin-bottom:0.55rem;">
            <div class="desk-panel-header">
                <span class="desk-panel-title">Authoritative Instrument &amp; Historical Provider Symbol Registry</span>
                <span class="desk-panel-meta">HISTORICAL DATA ONLY &bull; EXECUTION: SIMULATED PAPER TRADING</span>
            </div>
            <table class="blotter-table">
                <thead>
                    <tr>
                        <th>Internal Code</th>
                        <th>Display Name</th>
                        <th>Data Provider</th>
                        <th>Provider Symbol</th>
                        <th>Exchange</th>
                        <th>Instrument Type</th>
                        <th>Status</th>
                    </tr>
                </thead>
                <tbody>{rows_html}</tbody>
            </table>
        </div>
        """,
        unsafe_allow_html=True,
    )


def _render_fyers_ingestion_panel(
    client: ControlCenterApiClient,
    *,
    fyers_status: dict[str, Any],
    instruments: list[dict[str, Any]],
    timeframes: list[dict[str, Any]],
    limits: dict[str, Any],
) -> None:
    enabled = bool(fyers_status.get("enabled", True))
    status_code = str(fyers_status.get("status", "FYERS_AUTHENTICATION_REQUIRED"))

    if not enabled:
        st.markdown(
            f"""
            <div class="control-bar" style="border-left-color:#64748B;">
                <div class="control-bar-title">
                    FYERS HISTORICAL DATA DISABLED &mdash; {badge_html("DISABLED")}
                </div>
                <div class="control-bar-note">
                    <code>FYERS_HISTORICAL_DATA_ENABLED=false</code>. Upload CSV, Synthetic Demo Data,
                    and previously stored PostgreSQL datasets remain fully available.
                </div>
            </div>
            """,
            unsafe_allow_html=True,
        )
        return

    if status_code == "FYERS_AUTHENTICATION_REQUIRED":
        missing_vars: list[str] = fyers_status.get("missing_totp_vars") or []
        missing_note = (
            f"<div class='control-bar-note mono' style='margin-top:0.22rem; color:#CBD5E1;'>"
            f"Backend env vars not yet set on API service: <strong>{html.escape(', '.join(missing_vars))}</strong>"
            f"</div>"
            if missing_vars
            else ""
        )
        st.markdown(
            f"""
            <div class="control-bar" style="border-left-color:#F59E0B; border-color:#F59E0B;">
                <div class="control-bar-title">
                    FYERS_AUTHENTICATION_REQUIRED &mdash; {badge_html("FYERS_AUTHENTICATION_REQUIRED")}
                </div>
                <div class="control-bar-note" style="color:#FDE68A;">
                    FYERS historical-data authentication requires renewal. Existing stored and uploaded datasets remain available.
                </div>
                {missing_note}
            </div>
            """,
            unsafe_allow_html=True,
        )

    inst_codes = [str(i["internal_symbol"]) for i in instruments] or ["NIFTY", "BANKNIFTY"]
    by_code = {str(i["internal_symbol"]): i for i in instruments}

    tf_codes = [str(t["code"]) for t in timeframes] or [
        "1m",
        "3m",
        "5m",
        "10m",
        "15m",
        "30m",
        "60m",
        "1d",
    ]
    tf_labels = {
        str(t["code"]): f"{t['display_name']}  [FYERS res: {t['fyers_resolution']}]"
        for t in timeframes
    }
    default_tf_idx = tf_codes.index("5m") if "5m" in tf_codes else 0

    max_days = int(limits.get("max_date_range_days", 365))
    default_days = int(limits.get("default_date_range_days", 90))
    today_d = date.today()
    default_from = today_d - timedelta(days=min(default_days, max_days) - 1)

    c1, c2, c3, c4 = st.columns([1.2, 1.5, 1.1, 1.1])
    with c1:
        sel_inst = st.selectbox("Instrument", inst_codes, index=0, key="hd_fyers_inst")
    with c2:
        sel_tf = st.selectbox(
            "Timeframe",
            tf_codes,
            index=default_tf_idx,
            format_func=lambda code: tf_labels.get(code, code),
            key="hd_fyers_tf",
        )
    with c3:
        from_d = st.date_input("From Date (IST)", value=default_from, key="hd_fyers_from")
    with c4:
        to_d = st.date_input("To Date (IST)", value=today_d, key="hd_fyers_to")

    prov_sym = by_code.get(sel_inst, {}).get("provider_symbol", f"NSE:{sel_inst}-INDEX")
    span_days = (to_d - from_d).days + 1 if isinstance(from_d, date) and isinstance(to_d, date) else 0

    n1, n2, n3 = st.columns([2.2, 1.2, 1.6])
    with n1:
        custom_label = st.text_input(
            "Optional Dataset Label",
            value="",
            placeholder=f"{sel_inst} {sel_tf} FYERS Historical ({from_d} .. {to_d})",
            key="hd_fyers_label",
        )
    with n2:
        force_refresh = st.checkbox(
            "Force fresh download",
            value=False,
            help="When unchecked, reuses an existing complete validated dataset for the identical range.",
            key="hd_fyers_force",
        )
    with n3:
        st.markdown("<div style='height:1.45rem;'></div>", unsafe_allow_html=True)
        fetch_clicked = st.button(
            "FETCH HISTORICAL DATA",
            type="primary",
            use_container_width=True,
            disabled=(span_days < 1 or span_days > max_days),
            key="hd_btn_fetch_fyers",
        )

    if span_days > max_days:
        st.warning(
            f"Selected interval ({span_days} days) exceeds conservative demo limit ({max_days} calendar days). "
            "Narrow From/To dates."
        )
    else:
        st.caption(
            f"Provider Symbol: `{prov_sym}` • Selected Window: `{max(0, span_days)}` calendar day(s) "
            f"(max `{max_days}` days) • Execution Mode: `SIMULATED PAPER TRADING ONLY`."
        )

    if fetch_clicked:
        with st.spinner(
            f"Fetching & validating historical {sel_inst} ({prov_sym}) candles from FYERS..."
        ):
            res = client.trigger_fyers_download(
                {
                    "instrument": sel_inst,
                    "timeframe": sel_tf,
                    "from_date": from_d.isoformat(),
                    "to_date": to_d.isoformat(),
                    "display_name": custom_label.strip() or None,
                    "force_refresh": force_refresh,
                    "actor": "desk.operator",
                }
            )
        if res.ok and isinstance(res.data, dict):
            ds = res.data.get("dataset") or {}
            st.session_state["hd_selected_dataset_id"] = ds.get("dataset_id")
            st.success(res.data.get("message") or f"Stored dataset {ds.get('dataset_id')}.")
            st.rerun()
        else:
            err_code = (
                res.data.get("error")
                if isinstance(res.data, dict) and res.data.get("error")
                else "DOWNLOAD_ERROR"
            )
            if err_code == "FYERS_AUTHENTICATION_REQUIRED" or res.status_code == 401:
                diag = (
                    res.data.get("auth_diagnostic")
                    if isinstance(res.data, dict)
                    else None
                )
                base_warn = (
                    "FYERS historical-data authentication requires renewal. "
                    "Existing stored and uploaded datasets remain available."
                )
                if diag:
                    st.warning(f"{base_warn}\n\nDiagnostic: {diag}")
                else:
                    st.warning(base_warn)
            else:
                st.error(res.error or "Historical data download failed.")


def _render_csv_upload_panel(
    client: ControlCenterApiClient,
    *,
    instruments: list[dict[str, Any]],
    timeframes: list[dict[str, Any]],
) -> None:
    inst_codes = [str(i["internal_symbol"]) for i in instruments] or ["NIFTY", "BANKNIFTY"]
    tf_codes = [str(t["code"]) for t in timeframes] or [
        "1m",
        "3m",
        "5m",
        "10m",
        "15m",
        "30m",
        "60m",
        "1d",
    ]
    tf_labels = {str(t["code"]): str(t["display_name"]) for t in timeframes}
    default_tf_idx = tf_codes.index("5m") if "5m" in tf_codes else 0

    u1, u2, u3 = st.columns([2.0, 1.0, 1.2])
    with u1:
        up_name = st.text_input(
            "Dataset Label",
            value="User Uploaded Historical OHLCV",
            key="hd_csv_name",
        )
    with u2:
        up_inst = st.selectbox("Instrument", inst_codes, key="hd_csv_inst")
    with u3:
        up_tf = st.selectbox(
            "Timeframe",
            tf_codes,
            index=default_tf_idx,
            format_func=lambda c: f"{tf_labels.get(c, c)} ({c})",
            key="hd_csv_tf",
        )

    uploaded = st.file_uploader(
        "Attach UTF-8 OHLCV CSV (columns: timestamp, open, high, low, close, volume)",
        type=["csv"],
        key="hd_csv_uploader",
    )
    if st.button("UPLOAD & VALIDATE CSV DATASET", type="primary", key="hd_btn_upload_csv"):
        if uploaded is None:
            st.warning("Select a CSV file before uploading.")
        else:
            csv_txt = uploaded.getvalue().decode("utf-8", errors="replace")
            res = client.upload_historical_csv(
                name=up_name,
                instrument=up_inst,
                timeframe=up_tf,
                csv_text=csv_txt,
                actor="desk.operator",
            )
            if res.ok and isinstance(res.data, dict):
                ds = res.data.get("dataset") or {}
                st.session_state["hd_selected_dataset_id"] = ds.get("dataset_id")
                st.success(
                    f"Validated & stored dataset {ds.get('dataset_id')} "
                    f"(quality={ds.get('quality_status')}, candles={ds.get('candle_count')})."
                )
                st.rerun()
            else:
                st.error(res.error or "CSV upload failed.")


def _render_quality_detail_panel(dataset_id: str, dq: dict[str, Any]) -> None:
    issues: list[dict[str, Any]] = dq.get("issues") or []
    q_status = str(dq.get("quality_status", "VALID"))
    st.markdown(
        f"""
        <div class="desk-panel" style="margin-top:0.45rem;">
            <div class="desk-panel-header">
                <span class="desk-panel-title">21-Point Data Quality Validation Report &mdash; {html.escape(dataset_id)}</span>
                <span class="desk-panel-meta">{badge_html(q_status)} &bull; TZ: {html.escape(str(dq.get("checked_timezone", "Asia/Kolkata")))}</span>
            </div>
            <div class="mono" style="font-size:0.72rem; color:#CBD5E1; display:flex; justify-content:space-between; flex-wrap:wrap; gap:0.5rem;">
                <span>INSPECTED: <strong>{dq.get("total_rows_inspected", 0)}</strong></span>
                <span>VALID CLOSED BARS: <strong>{dq.get("valid_candle_count", 0)}</strong></span>
                <span>MISSING BARS: <strong>{dq.get("missing_candle_count", 0)}</strong></span>
                <span>DUPLICATES: <strong>{dq.get("duplicate_timestamp_count", 0)}</strong></span>
                <span>CONFLICTS: <strong>{dq.get("conflicting_duplicate_count", 0)}</strong></span>
                <span>INVALID OHLC: <strong>{dq.get("invalid_ohlc_count", 0)}</strong></span>
                <span>ZERO-VOL BARS: <strong>{dq.get("zero_volume_candle_count", 0)}</strong></span>
                <span>PARTIAL CURRENT EXCLUDED: <strong>{dq.get("partial_current_candle_excluded", False)}</strong></span>
            </div>
        </div>
        """,
        unsafe_allow_html=True,
    )
    if issues:
        issue_rows = "".join(
            f"<tr>"
            f"<td>{badge_html(str(i.get('severity', 'WARNING')))}</td>"
            f"<td class='mono'>{html.escape(str(i.get('check_code', '')))}</td>"
            f"<td class='mono'>{html.escape(fmt_ist(i.get('timestamp')) if i.get('timestamp') else '—')}</td>"
            f"<td>{html.escape(str(i.get('message', '')))}</td>"
            f"</tr>"
            for i in issues[:40]
        )
        st.markdown(
            f"""
            <div class="desk-panel">
                <table class="blotter-table">
                    <thead><tr><th>Severity</th><th>Check Code</th><th>Timestamp (IST)</th><th>Diagnostic Detail</th></tr></thead>
                    <tbody>{issue_rows}</tbody>
                </table>
            </div>
            """,
            unsafe_allow_html=True,
        )


def render(client: ControlCenterApiClient) -> None:
    render_screen_header(
        "HISTORICAL DATA",
        "FYERS HISTORICAL MARKET DATA  |  CSV EXPORT & IMPORT  |  EXECUTION: SIMULATED PAPER TRADING",
    )

    prov_res = client.get_historical_providers()
    inst_res = client.get_historical_instruments()
    if not prov_res.ok or not isinstance(prov_res.data, dict):
        render_api_offline_banner(
            prov_res.error or "Unable to fetch historical data providers", client.base_url
        )
        return

    providers_list: list[dict[str, Any]] = prov_res.data.get("providers") or []
    fyers_status = next(
        (p for p in providers_list if p.get("provider") == "FYERS"),
        {"enabled": True, "status": "FYERS_AUTHENTICATION_REQUIRED"},
    )
    inst_payload: dict[str, Any] = (
        inst_res.data if (inst_res.ok and isinstance(inst_res.data, dict)) else {}
    )
    instruments: list[dict[str, Any]] = inst_payload.get("instruments") or []
    timeframes: list[dict[str, Any]] = inst_payload.get("timeframes") or []
    limits: dict[str, Any] = inst_payload.get("limits") or {}

    _render_instrument_reference_strip(instruments)

    # --- DATA SOURCE SELECTOR ---
    ds_mode = st.radio(
        "DATA SOURCE",
        options=DATA_SOURCE_OPTIONS,
        horizontal=True,
        key="hd_source_mode_radio",
    )

    if ds_mode == "FYERS Historical Data":
        render_data_source_banner(source_type="FYERS_HISTORICAL", provider="FYERS")
        _render_fyers_ingestion_panel(
            client,
            fyers_status=fyers_status,
            instruments=instruments,
            timeframes=timeframes,
            limits=limits,
        )
    elif ds_mode == "Upload CSV":
        render_data_source_banner(source_type="IMPORTED_CSV", provider="CSV")
        _render_csv_upload_panel(client, instruments=instruments, timeframes=timeframes)
    else:
        render_data_source_banner(source_type="BUNDLED_CSV", provider="SYNTHETIC")
        st.caption(
            "Bundled deterministic NIFTY & BANKNIFTY 5-minute demonstration datasets remain available "
            "at all times without external credentials."
        )

    st.markdown("<div style='height:0.35rem;'></div>", unsafe_allow_html=True)

    # --- STORED & BUNDLED DATASET CATALOG + INSPECTOR ---
    f1, f2, f3 = st.columns([1.2, 1.2, 1.6])
    with f1:
        flt_inst = st.selectbox("Filter Instrument", ["ALL", "NIFTY", "BANKNIFTY"], key="hd_flt_inst")
    with f2:
        flt_src = st.selectbox(
            "Filter Source",
            ["ALL", "FYERS_HISTORICAL", "IMPORTED_CSV", "BUNDLED_CSV"],
            key="hd_flt_src",
        )
    with f3:
        flt_qual = st.selectbox(
            "Filter Quality",
            ["ALL", "VALID", "VALID_WITH_WARNINGS", "PARTIAL", "INVALID"],
            key="hd_flt_qual",
        )

    cat_res = client.list_historical_datasets(
        instrument=flt_inst,
        source_type=flt_src,
        quality_status=flt_qual,
        include_synthetic=True,
    )
    datasets: list[dict[str, Any]] = (
        cat_res.data.get("datasets", [])
        if (cat_res.ok and isinstance(cat_res.data, dict))
        else []
    )

    if not datasets:
        render_empty_blotter(
            panel_title="Persisted & Bundled Historical Datasets",
            panel_meta="0 MATCHING DATASETS",
            columns=[
                ("Dataset ID", False),
                ("Provider", False),
                ("Instrument", False),
                ("Timeframe", False),
                ("Candles", True),
                ("Quality", False),
            ],
            empty_title="NO DATASETS MATCH CURRENT FILTER",
            empty_description="Fetch historical data from FYERS, upload an OHLCV CSV, or clear filters above.",
        )
        return

    pre_id = st.session_state.get("hd_selected_dataset_id")
    ds_ids = [str(d["dataset_id"]) for d in datasets]
    sel_idx = ds_ids.index(pre_id) if pre_id in ds_ids else 0

    picked_idx = st.selectbox(
        "Inspect Historical Dataset",
        range(len(datasets)),
        index=sel_idx,
        format_func=lambda i: (
            f"{datasets[i]['dataset_id']}  |  {datasets[i]['provider']}  |  "
            f"{datasets[i]['instrument']} {datasets[i].get('timeframe_display', datasets[i]['timeframe'])}  |  "
            f"{datasets[i]['candle_count']} bars  |  {datasets[i]['quality_status']}"
        ),
        key="hd_dataset_picker",
    )
    sel_ds = datasets[picked_idx]
    sel_ds_id = str(sel_ds["dataset_id"])
    st.session_state["hd_selected_dataset_id"] = sel_ds_id

    render_data_source_banner(
        source_type=sel_ds.get("source_type"),
        provider=sel_ds.get("provider"),
        dataset_id=sel_ds_id,
        quality_status=sel_ds.get("quality_status"),
    )

    # All 13 required metadata fields
    req_period = (
        f"{fmt_ist(sel_ds.get('requested_from'))} -> {fmt_ist(sel_ds.get('requested_to'))}"
        if sel_ds.get("requested_from")
        else "—"
    )
    act_period = (
        f"{fmt_ist(sel_ds.get('actual_from'))} -> {fmt_ist(sel_ds.get('actual_to'))}"
        if sel_ds.get("actual_from")
        else "—"
    )
    meta_pairs = [
        ("Provider", str(sel_ds.get("provider", "—"))),
        ("Provider Symbol", str(sel_ds.get("provider_symbol") or "—")),
        ("Requested Period", req_period),
        ("Actual Period", act_period),
        ("Timeframe", f"{sel_ds.get('timeframe_display', sel_ds.get('timeframe'))} ({sel_ds.get('timeframe')})"),
        ("Candle Count", str(sel_ds.get("candle_count", 0))),
        ("Quality Status", str(sel_ds.get("quality_status", "VALID"))),
        ("Missing Candles", str(sel_ds.get("missing_candle_count", 0))),
        ("Duplicate Candles", str(sel_ds.get("duplicate_candle_count", 0))),
        ("First Candle (IST)", fmt_ist(sel_ds.get("actual_from"))),
        ("Last Candle (IST)", fmt_ist(sel_ds.get("actual_to"))),
        ("Retrieved At (IST)", fmt_ist(sel_ds.get("retrieved_at"))),
        ("Dataset ID", sel_ds_id),
    ]
    rows_meta = "".join(
        f"<tr><td>{html.escape(k)}</td>"
        f"<td class='mono' style='text-align:right;'>"
        f"{badge_html(v) if k == 'Quality Status' else html.escape(v)}"
        f"</td></tr>"
        for k, v in meta_pairs
    )
    st.markdown(
        f"""
        <div class="desk-panel" style="margin-bottom:0.50rem;">
            <div class="desk-panel-header">
                <span class="desk-panel-title">Dataset Provenance &amp; Coverage Summary</span>
                <span class="desk-panel-meta">SHA256: {html.escape(str(sel_ds.get("data_version_hash", ""))[:16])}</span>
            </div>
            <table class="blotter-table">
                <thead><tr><th>Attribute</th><th style="text-align:right;">Recorded Value</th></tr></thead>
                <tbody>{rows_meta}</tbody>
            </table>
        </div>
        """,
        unsafe_allow_html=True,
    )

    is_usable = str(sel_ds.get("quality_status", "VALID")) not in {
        "INVALID",
        "DOWNLOAD_FAILED",
        "AUTHENTICATION_REQUIRED",
    }
    can_delete = bool(sel_ds.get("can_delete", False))
    del_reason = str(sel_ds.get("delete_block_reason") or "")

    a1, a2, a3, a4, a5 = st.columns(5)
    with a1:
        if st.button(
            "USE FOR BACKTEST",
            type="primary",
            use_container_width=True,
            disabled=not is_usable,
            key=f"hd_use_bt_{sel_ds_id}",
        ):
            st.session_state["preselected_backtest_dataset_id"] = sel_ds_id
            st.session_state["pending_nav_screen"] = "Backtests"
            st.rerun()

    with a2:
        if st.button(
            "USE FOR MARKET REPLAY",
            use_container_width=True,
            disabled=not is_usable,
            key=f"hd_use_rpl_{sel_ds_id}",
        ):
            st.session_state["preselected_replay_dataset_id"] = sel_ds_id
            st.session_state["preselected_replay_instrument"] = sel_ds.get("instrument", "NIFTY")
            st.session_state["pending_nav_screen"] = "Market Replay"
            st.rerun()

    with a3:
        csv_cache: dict[str, dict[str, str]] = st.session_state.setdefault("hd_csv_cache", {})
        if sel_ds_id in csv_cache:
            cached = csv_cache[sel_ds_id]
            st.download_button(
                label=f"SAVE {cached['filename'][:18]}...",
                data=cached["csv_text"].encode("utf-8"),
                file_name=cached["filename"],
                mime="text/csv",
                use_container_width=True,
                key=f"hd_dl_save_{sel_ds_id}",
            )
        else:
            if st.button(
                "DOWNLOAD CSV",
                use_container_width=True,
                disabled=(int(sel_ds.get("candle_count", 0)) == 0),
                key=f"hd_dl_prep_{sel_ds_id}",
            ):
                exp_res = client.export_historical_dataset_csv(sel_ds_id)
                if exp_res.ok and isinstance(exp_res.data, dict):
                    csv_cache[sel_ds_id] = exp_res.data
                    st.rerun()
                else:
                    st.error(exp_res.error or "Failed to export CSV.")

    with a4:
        show_dq_key = f"hd_show_dq_{sel_ds_id}"
        if st.button("VIEW DATA QUALITY", use_container_width=True, key=f"hd_btn_dq_{sel_ds_id}"):
            st.session_state[show_dq_key] = not bool(st.session_state.get(show_dq_key, False))

    with a5:
        del_clicked = st.button(
            "DELETE DATASET",
            use_container_width=True,
            disabled=not can_delete,
            help=del_reason if not can_delete else "Delete unreferenced dataset from PostgreSQL.",
            key=f"hd_btn_del_{sel_ds_id}",
        )

    if not can_delete and del_reason:
        st.caption(f"Retention Lock: {del_reason}")

    if del_clicked and can_delete:
        st.session_state["hd_confirm_delete_id"] = sel_ds_id

    if st.session_state.get("hd_confirm_delete_id") == sel_ds_id and can_delete:
        dc1, dc2, _ = st.columns([1.6, 1.2, 3.2])
        with dc1:
            if st.button(
                f"CONFIRM DELETE {sel_ds_id[:20]}",
                type="primary",
                use_container_width=True,
                key=f"hd_confirm_yes_{sel_ds_id}",
            ):
                d_res = client.delete_historical_dataset(sel_ds_id)
                st.session_state.pop("hd_confirm_delete_id", None)
                if d_res.ok:
                    st.session_state.pop("hd_selected_dataset_id", None)
                    st.success(f"Deleted dataset {sel_ds_id}.")
                    st.rerun()
                else:
                    st.error(d_res.error or "Delete blocked.")
        with dc2:
            if st.button("CANCEL", use_container_width=True, key=f"hd_confirm_no_{sel_ds_id}"):
                st.session_state.pop("hd_confirm_delete_id", None)
                st.rerun()

    if st.session_state.get(f"hd_show_dq_{sel_ds_id}", False):
        dq_res = client.get_historical_dataset_quality(sel_ds_id)
        if dq_res.ok and isinstance(dq_res.data, dict):
            _render_quality_detail_panel(sel_ds_id, dq_res.data)

