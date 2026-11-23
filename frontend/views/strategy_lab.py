"""Screen 2: Strategy Lab — Institutional Strategy Configuration & Versioning Desk.

Provides:
- Strategy Summary (signal contract, entry/exit logic, immutable version metadata)
- Parameters (instrument, timeframe, EMA/RSI periods & thresholds, IST session bounds)
- Risk Defaults (SL, TP, trailing stop, opposite-signal, max holding, EOD square-off, sizing, max trades/day)
- Execution Assumptions (slippage, estimated charges, flat fee, live instrument contract specs)
- Workflows: Save New Version, Clone Version, Compare Versions, and Immutable Version History
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
from frontend.formatting import fmt_inr, fmt_ist


def _bool_pill(enabled: object, detail: str = "") -> str:
    is_on = bool(enabled)
    badge = badge_html("ACTIVE" if is_on else "STOPPED")
    label = "ENABLED" if is_on else "DISABLED"
    suffix = f" &bull; {html.escape(detail)}" if (is_on and detail) else ""
    return f'{badge.replace("ACTIVE", label).replace("STOPPED", label)}{suffix}'


def _render_strategy_summary(
    strategy_def: dict[str, Any],
    selected_ver: dict[str, Any],
) -> None:
    code = html.escape(str(strategy_def.get("code", "EMA_RSI_CROSSOVER")))
    name = html.escape(str(strategy_def.get("name", "EMA Crossover + RSI Confirmation")))
    category = html.escape(str(strategy_def.get("category", "TREND_MOMENTUM")))
    ver_label = html.escape(str(selected_ver.get("version_label", "—")))
    ver_status = str(selected_ver.get("status", "VALIDATED"))
    ver_hash = html.escape(str(selected_ver.get("parameter_hash", ""))[:16])
    created_at = html.escape(fmt_ist(selected_ver.get("created_at")))
    author = html.escape(str(selected_ver.get("created_by", "—")))
    cloned_from = selected_ver.get("cloned_from_label")
    lineage_txt = f"CLONED FROM {html.escape(str(cloned_from))}" if cloned_from else "ROOT BASELINE"
    notes = html.escape(str(selected_ver.get("notes") or "No operator notes attached."))

    p = selected_ver.get("parameters", {})
    f_ema = html.escape(str(p.get("fast_ema_period", 9)))
    s_ema = html.escape(str(p.get("slow_ema_period", 21)))
    rsi_p = html.escape(str(p.get("rsi_period", 14)))
    l_rsi = html.escape(str(p.get("long_rsi_threshold", "55.00")))
    s_rsi = html.escape(str(p.get("short_rsi_threshold", "45.00")))
    t_start = html.escape(str(p.get("trading_start_time", "09:20:00")))
    t_last = html.escape(str(p.get("last_entry_time", "14:45:00")))
    t_sq = html.escape(str(p.get("mandatory_square_off_time", "15:20:00")))

    st.markdown(
        f"""
        <div class="strategy-summary-strip">
            <div style="display:flex; justify-content:space-between; align-items:center; flex-wrap:wrap; gap:0.5rem;">
                <div>
                    <span style="font-size:0.90rem; font-weight:700; color:#F8FAFC; letter-spacing:0.04em;">
                        {name}
                    </span>
                    <span class="mono" style="font-size:0.74rem; color:#94A3B8; margin-left:0.5rem;">
                        [{code} &bull; {category}]
                    </span>
                </div>
                <div class="mono" style="font-size:0.72rem; color:#CBD5E1;">
                    INSPECTING: <strong style="color:#FBBF24;">{ver_label}</strong>
                    &nbsp;{badge_html(ver_status)}
                    &nbsp;|&nbsp; SHA256: {ver_hash}
                    &nbsp;|&nbsp; {lineage_txt}
                </div>
            </div>
            <div class="strategy-rule-grid">
                <div class="strategy-rule-box">
                    <strong>LONG SIGNAL CANDIDATE</strong>
                    EMA({f_ema}) crosses above EMA({s_ema}) on bar close &amp; RSI({rsi_p}) &ge; {l_rsi} inside [{t_start}..{t_last} IST]
                </div>
                <div class="strategy-rule-box">
                    <strong>SHORT SIGNAL CANDIDATE</strong>
                    EMA({f_ema}) crosses below EMA({s_ema}) on bar close &amp; RSI({rsi_p}) &le; {s_rsi} inside [{t_start}..{t_last} IST]
                </div>
                <div class="strategy-rule-box">
                    <strong>OPERATIONAL INVARIANT &amp; LINEAGE</strong>
                    Signals only (never direct fills) &bull; EOD Square-Off {t_sq} IST &bull; By {author} @ {created_at}
                </div>
            </div>
            <div style="margin-top:0.36rem; font-size:0.71rem; color:#94A3B8; display:flex; justify-content:space-between;">
                <span>Notes: {notes}</span>
                <span class="mono" style="color:#FBBF24;">NON-OPTIMIZED ENGINEERING CONFIGURATION</span>
            </div>
        </div>
        """,
        unsafe_allow_html=True,
    )


def _render_specification_panels(
    selected_ver: dict[str, Any],
    instruments_by_symbol: dict[str, dict[str, Any]],
) -> None:
    p: dict[str, Any] = selected_ver.get("parameters", {})
    sym = str(p.get("instrument", selected_ver.get("instrument_symbol", "NIFTY"))).upper()
    inst_ref = instruments_by_symbol.get(sym, {})
    unit = str(p.get("stop_target_unit", "POINTS"))
    unit_short = "%" if unit == "PERCENT" else "pts"

    col_p, col_r, col_e = st.columns(3)

    with col_p:
        st.markdown(
            f"""
            <div class="desk-panel">
                <div class="desk-panel-header">
                    <span class="desk-panel-title">Parameters (Signal &amp; Session)</span>
                    <span class="desk-panel-meta">{html.escape(sym)} &bull; {html.escape(str(p.get("timeframe", "5m")))}</span>
                </div>
                <table class="blotter-table">
                    <tbody>
                        <tr><td>Instrument</td><td class="num"><strong>{html.escape(sym)}</strong></td></tr>
                        <tr><td>Bar Timeframe</td><td class="num">{html.escape(str(p.get("timeframe", "5m")))}</td></tr>
                        <tr><td>Fast EMA Period</td><td class="num">{html.escape(str(p.get("fast_ema_period", 9)))}</td></tr>
                        <tr><td>Slow EMA Period</td><td class="num">{html.escape(str(p.get("slow_ema_period", 21)))}</td></tr>
                        <tr><td>Wilder RSI Period</td><td class="num">{html.escape(str(p.get("rsi_period", 14)))}</td></tr>
                        <tr><td>Long RSI Threshold (&ge;)</td><td class="num num-pos">{html.escape(str(p.get("long_rsi_threshold", "55.00")))}</td></tr>
                        <tr><td>Short RSI Threshold (&le;)</td><td class="num num-neg">{html.escape(str(p.get("short_rsi_threshold", "45.00")))}</td></tr>
                        <tr><td>Trading Start (IST)</td><td class="num">{html.escape(str(p.get("trading_start_time", "09:20:00")))}</td></tr>
                        <tr><td>Last Entry Cutoff (IST)</td><td class="num">{html.escape(str(p.get("last_entry_time", "14:45:00")))}</td></tr>
                        <tr><td>Mandatory Square-Off (IST)</td><td class="num">{html.escape(str(p.get("mandatory_square_off_time", "15:20:00")))}</td></tr>
                    </tbody>
                </table>
            </div>
            """,
            unsafe_allow_html=True,
        )

    with col_r:
        tp_cell = _bool_pill(p.get("profit_target_enabled", True), f"{p.get('profit_target', '70.00')} {unit_short}")
        ts_cell = _bool_pill(p.get("trailing_stop_enabled", False), f"{p.get('trailing_stop', '25.00')} {unit_short}")
        opp_cell = _bool_pill(p.get("exit_on_opposite_signal", True))
        mh_cell = _bool_pill(p.get("max_holding_enabled", True), f"{p.get('max_holding_bars', 24)} bars")
        eod_cell = _bool_pill(p.get("eod_square_off_enabled", True), str(p.get("mandatory_square_off_time", "15:20:00")))

        st.markdown(
            f"""
            <div class="desk-panel">
                <div class="desk-panel-header">
                    <span class="desk-panel-title">Risk Defaults &amp; Exits</span>
                    <span class="desk-panel-meta">UNIT: {html.escape(unit)}</span>
                </div>
                <table class="blotter-table">
                    <tbody>
                        <tr><td>Hard Stop Loss</td><td class="num num-neg">{html.escape(str(p.get("stop_loss", "35.00")))} {html.escape(unit_short)}</td></tr>
                        <tr><td>Profit Target</td><td class="num">{tp_cell}</td></tr>
                        <tr><td>Trailing Stop</td><td class="num">{ts_cell}</td></tr>
                        <tr><td>Opposite Signal Exit</td><td class="num">{opp_cell}</td></tr>
                        <tr><td>Max Holding Period</td><td class="num">{mh_cell}</td></tr>
                        <tr><td>EOD Mandatory Square-Off</td><td class="num">{eod_cell}</td></tr>
                        <tr><td>Position Sizing Mode</td><td class="num">{html.escape(str(p.get("position_sizing_mode", "FIXED_LOTS")))}</td></tr>
                        <tr><td>Position Size Value</td><td class="num">{html.escape(str(p.get("position_size_value", "1")))}</td></tr>
                        <tr><td>Maximum Trades / Day</td><td class="num">{html.escape(str(p.get("max_trades_per_day", 4)))}</td></tr>
                    </tbody>
                </table>
            </div>
            """,
            unsafe_allow_html=True,
        )

    with col_e:
        ref_lot = inst_ref.get("lot_size", "—")
        ref_tick = inst_ref.get("tick_size", "—")
        ref_exch = inst_ref.get("exchange", "NSE")
        ref_hours = (
            f"{inst_ref.get('session_open_ist', '09:15:00')} - {inst_ref.get('session_close_ist', '15:30:00')} IST"
        )
        flat_fee = fmt_inr(p.get("per_order_fee_inr", "20.00"))

        st.markdown(
            f"""
            <div class="desk-panel">
                <div class="desk-panel-header">
                    <span class="desk-panel-title">Execution Assumptions</span>
                    <span class="desk-panel-meta">PAPER SIMULATOR SPECS</span>
                </div>
                <table class="blotter-table">
                    <tbody>
                        <tr><td>Execution Mode</td><td class="num">{badge_html("INFO")} PAPER_ONLY</td></tr>
                        <tr><td>Slippage Assumption</td><td class="num">{html.escape(str(p.get("slippage_bps", "2.00")))} bps</td></tr>
                        <tr><td>Estimated Variable Charges</td><td class="num">{html.escape(str(p.get("estimated_charges_bps", "3.50")))} bps</td></tr>
                        <tr><td>Per-Order Flat Fee</td><td class="num">{html.escape(flat_fee)}</td></tr>
                        <tr><td>Reference Exchange ({html.escape(sym)})</td><td class="num">{html.escape(str(ref_exch))}</td></tr>
                        <tr><td>Reference Lot Size</td><td class="num">{html.escape(str(ref_lot))} units</td></tr>
                        <tr><td>Reference Tick Size</td><td class="num">{html.escape(str(ref_tick))} INR</td></tr>
                        <tr><td>Reference Exchange Hours</td><td class="num">{html.escape(ref_hours)}</td></tr>
                        <tr><td>Version Immutability</td><td class="num">{badge_html("HEALTHY")} LOCKED</td></tr>
                    </tbody>
                </table>
            </div>
            """,
            unsafe_allow_html=True,
        )


def _render_authoring_and_clone_desk(
    client: ControlCenterApiClient,
    strategy_code: str,
    selected_ver: dict[str, Any],
    supported_symbols: list[str],
) -> None:
    p: dict[str, Any] = selected_ver.get("parameters", {})
    source_id = str(selected_ver.get("id", ""))
    source_label = str(selected_ver.get("version_label", "v1"))

    st.markdown(
        f"""
        <div class="desk-panel" style="margin-bottom:0.45rem;">
            <div class="desk-panel-header" style="margin-bottom:0.2rem; border-bottom:none; padding-bottom:0;">
                <span class="desk-panel-title">Immutable Version Workbench &mdash; Template Loaded: {html.escape(source_label)}</span>
                <span class="desk-panel-meta">EXISTING VERSIONS ARE NEVER EDITED IN PLACE &bull; COMMITS CREATE v(N+1)</span>
            </div>
        </div>
        """,
        unsafe_allow_html=True,
    )

    sym_options = supported_symbols or ["NIFTY", "BANKNIFTY"]
    cur_sym = str(p.get("instrument", "NIFTY")).upper()
    sym_idx = sym_options.index(cur_sym) if cur_sym in sym_options else 0

    tf_options = ["1m", "3m", "5m", "10m", "15m", "30m", "60m", "1d"]
    cur_tf = str(p.get("timeframe", "5m"))
    tf_idx = tf_options.index(cur_tf) if cur_tf in tf_options else 2

    unit_options = ["POINTS", "PERCENT"]
    cur_unit = str(p.get("stop_target_unit", "POINTS"))
    unit_idx = unit_options.index(cur_unit) if cur_unit in unit_options else 0

    sizing_options = ["FIXED_LOTS", "FIXED_QUANTITY", "RISK_PER_TRADE_PCT"]
    cur_sizing = str(p.get("position_sizing_mode", "FIXED_LOTS"))
    sizing_idx = sizing_options.index(cur_sizing) if cur_sizing in sizing_options else 0

    # Key widgets by selected version ID so switching template updates defaults cleanly
    vk = source_id[:8]

    z1, z2, z3 = st.columns(3)

    with z1:
        st.markdown('<div class="zone-caption">1. Signal &amp; IST Session Window</div>', unsafe_allow_html=True)
        a1, a2 = st.columns(2)
        with a1:
            instrument = st.selectbox("Instrument", sym_options, index=sym_idx, key=f"sl_inst_{vk}")
            fast_ema = st.number_input(
                "Fast EMA Period",
                min_value=2,
                max_value=200,
                value=int(p.get("fast_ema_period", 9)),
                key=f"sl_fema_{vk}",
            )
            rsi_period = st.number_input(
                "RSI Period",
                min_value=2,
                max_value=100,
                value=int(p.get("rsi_period", 14)),
                key=f"sl_rsip_{vk}",
            )
            short_rsi = st.number_input(
                "Short RSI (<=)",
                min_value=1.0,
                max_value=99.0,
                value=float(p.get("short_rsi_threshold", 45.0)),
                step=1.0,
                key=f"sl_srsi_{vk}",
            )
            start_time = st.text_input(
                "Trading Start (IST)",
                value=str(p.get("trading_start_time", "09:20:00")),
                key=f"sl_tstart_{vk}",
            )
        with a2:
            timeframe = st.selectbox("Timeframe", tf_options, index=tf_idx, key=f"sl_tf_{vk}")
            slow_ema = st.number_input(
                "Slow EMA Period",
                min_value=3,
                max_value=500,
                value=int(p.get("slow_ema_period", 21)),
                key=f"sl_sema_{vk}",
            )
            long_rsi = st.number_input(
                "Long RSI (>=)",
                min_value=1.0,
                max_value=99.0,
                value=float(p.get("long_rsi_threshold", 55.0)),
                step=1.0,
                key=f"sl_lrsi_{vk}",
            )
            last_entry = st.text_input(
                "Last Entry (IST)",
                value=str(p.get("last_entry_time", "14:45:00")),
                key=f"sl_tlast_{vk}",
            )
            sqoff_time = st.text_input(
                "Square-Off (IST)",
                value=str(p.get("mandatory_square_off_time", "15:20:00")),
                key=f"sl_tsq_{vk}",
            )

    with z2:
        st.markdown('<div class="zone-caption">2. Exit Controls &amp; Risk Sizing</div>', unsafe_allow_html=True)
        b1, b2 = st.columns(2)
        with b1:
            stop_unit = st.selectbox("Stop/Target Unit", unit_options, index=unit_idx, key=f"sl_unit_{vk}")
            stop_loss = st.number_input(
                "Stop Loss (> 0)",
                min_value=0.05,
                max_value=5000.0,
                value=float(p.get("stop_loss", 35.0)),
                step=5.0,
                key=f"sl_sl_{vk}",
            )
            profit_target = st.number_input(
                "Profit Target (> 0)",
                min_value=0.05,
                max_value=10000.0,
                value=float(p.get("profit_target", 70.0)),
                step=5.0,
                key=f"sl_tp_{vk}",
            )
            trailing_stop = st.number_input(
                "Trailing Distance",
                min_value=0.05,
                max_value=5000.0,
                value=float(p.get("trailing_stop", 25.0)),
                step=5.0,
                key=f"sl_ts_{vk}",
            )
            sizing_mode = st.selectbox(
                "Position Sizing",
                sizing_options,
                index=sizing_idx,
                key=f"sl_szm_{vk}",
            )
        with b2:
            max_trades = st.number_input(
                "Max Trades / Day",
                min_value=1,
                max_value=100,
                value=int(p.get("max_trades_per_day", 4)),
                key=f"sl_maxt_{vk}",
            )
            tp_enabled = st.checkbox(
                "Enable Profit Target",
                value=bool(p.get("profit_target_enabled", True)),
                key=f"sl_tpen_{vk}",
            )
            ts_enabled = st.checkbox(
                "Enable Trailing Stop",
                value=bool(p.get("trailing_stop_enabled", False)),
                key=f"sl_tsen_{vk}",
            )
            opp_enabled = st.checkbox(
                "Opposite Signal Exit",
                value=bool(p.get("exit_on_opposite_signal", True)),
                key=f"sl_opp_{vk}",
            )
            mh_enabled = st.checkbox(
                "Max Holding Exit",
                value=bool(p.get("max_holding_enabled", True)),
                key=f"sl_mhen_{vk}",
            )
            eod_enabled = st.checkbox(
                "EOD Square-Off",
                value=bool(p.get("eod_square_off_enabled", True)),
                key=f"sl_eoden_{vk}",
            )
            pos_size_val = st.number_input(
                "Position Size Value",
                min_value=0.1,
                max_value=10000.0,
                value=float(p.get("position_size_value", 1.0)),
                step=1.0,
                key=f"sl_szv_{vk}",
            )

    with z3:
        st.markdown('<div class="zone-caption">3. Execution Friction &amp; Lineage</div>', unsafe_allow_html=True)
        max_hold_bars = st.number_input(
            "Max Holding Period (Bars)",
            min_value=1,
            max_value=500,
            value=int(p.get("max_holding_bars", 24)),
            key=f"sl_mhb_{vk}",
        )
        c1, c2 = st.columns(2)
        with c1:
            slippage_bps = st.number_input(
                "Slippage (bps)",
                min_value=0.0,
                max_value=250.0,
                value=float(p.get("slippage_bps", 2.0)),
                step=0.5,
                key=f"sl_slip_{vk}",
            )
        with c2:
            charges_bps = st.number_input(
                "Est. Charges (bps)",
                min_value=0.0,
                max_value=250.0,
                value=float(p.get("estimated_charges_bps", 3.5)),
                step=0.5,
                key=f"sl_chg_{vk}",
            )
        flat_fee_inr = st.number_input(
            "Per-Order Flat Fee (INR)",
            min_value=0.0,
            max_value=5000.0,
            value=float(p.get("per_order_fee_inr", 20.0)),
            step=5.0,
            key=f"sl_fee_{vk}",
        )
        actor = st.text_input("Operator ID", value="desk.operator", key=f"sl_actor_{vk}")
        notes = st.text_input(
            "Version Change Rationale (Audited)",
            placeholder=f"e.g. Adjusted {instrument} session cutoffs & trailing stop",
            key=f"sl_notes_{vk}",
        )

    candidate_params: dict[str, Any] = {
        "instrument": instrument,
        "timeframe": timeframe,
        "fast_ema_period": int(fast_ema),
        "slow_ema_period": int(slow_ema),
        "rsi_period": int(rsi_period),
        "long_rsi_threshold": round(float(long_rsi), 2),
        "short_rsi_threshold": round(float(short_rsi), 2),
        "trading_start_time": start_time.strip(),
        "last_entry_time": last_entry.strip(),
        "mandatory_square_off_time": sqoff_time.strip(),
        "max_trades_per_day": int(max_trades),
        "stop_target_unit": stop_unit,
        "stop_loss": round(float(stop_loss), 2),
        "profit_target_enabled": bool(tp_enabled),
        "profit_target": round(float(profit_target), 2),
        "trailing_stop_enabled": bool(ts_enabled),
        "trailing_stop": round(float(trailing_stop), 2),
        "exit_on_opposite_signal": bool(opp_enabled),
        "max_holding_enabled": bool(mh_enabled),
        "max_holding_bars": int(max_hold_bars),
        "eod_square_off_enabled": bool(eod_enabled),
        "position_sizing_mode": sizing_mode,
        "position_size_value": int(pos_size_val)
        if sizing_mode in ("FIXED_LOTS", "FIXED_QUANTITY") and float(pos_size_val).is_integer()
        else round(float(pos_size_val), 2),
        "slippage_bps": round(float(slippage_bps), 2),
        "estimated_charges_bps": round(float(charges_bps), 2),
        "per_order_fee_inr": round(float(flat_fee_inr), 2),
    }

    st.write("")
    btn_v, btn_save, btn_clone = st.columns([1.1, 1.4, 1.6])
    with btn_v:
        validate_clicked = st.button("VALIDATE PARAMETERS", use_container_width=True)
    with btn_save:
        save_new_clicked = st.button("SAVE NEW VERSION", use_container_width=True)
    with btn_clone:
        clone_clicked = st.button(
            f"CLONE VERSION ({source_label} -> NEW)",
            use_container_width=True,
        )

    if validate_clicked:
        vres = client.validate_strategy_config(strategy_code, parameters=candidate_params)
        if vres.ok and isinstance(vres.data, dict):
            ph = str(vres.data.get("parameter_hash", ""))[:16]
            st.success(f"Configuration valid against domain & exchange rules (SHA-256: {ph}).")
        else:
            st.error(f"Configuration rejected: {vres.error}")

    if save_new_clicked:
        sres = client.create_strategy_version(
            strategy_code,
            parameters=candidate_params,
            notes=notes,
            actor=actor.strip() or "desk.operator",
        )
        if sres.ok and isinstance(sres.data, dict):
            lbl = sres.data.get("version_label", "new version")
            st.session_state["strategy_lab_selected_label"] = lbl
            st.toast(f"Saved immutable version {lbl}")
            st.rerun()
        else:
            st.error(f"Save New Version rejected: {sres.error}")

    if clone_clicked:
        cres = client.clone_strategy_version(
            source_id,
            parameter_overrides=candidate_params,
            notes=notes or f"Cloned from {source_label}",
            actor=actor.strip() or "desk.operator",
        )
        if cres.ok and isinstance(cres.data, dict):
            lbl = cres.data.get("version_label", "cloned version")
            st.session_state["strategy_lab_selected_label"] = lbl
            st.toast(f"Cloned {source_label} -> immutable {lbl}")
            st.rerun()
        else:
            st.error(f"Clone Version rejected: {cres.error}")


def _render_compare_versions_tab(
    client: ControlCenterApiClient,
    versions: list[dict[str, Any]],
) -> None:
    if len(versions) < 1:
        render_empty_blotter(
            panel_title="Version Comparison Matrix",
            panel_meta="INSUFFICIENT VERSIONS",
            columns=[("Group", False), ("Parameter", False), ("Left", True), ("Right", True), ("Delta", False)],
            empty_title="NO VERSIONS AVAILABLE TO COMPARE",
            empty_description="Save or clone a strategy version first.",
        )
        return

    labels = [str(v["version_label"]) for v in versions]
    by_label = {str(v["version_label"]): v for v in versions}

    c1, c2, c3 = st.columns([1.5, 1.5, 1.2])
    with c1:
        left_lbl = st.selectbox("Baseline Version (Left)", labels, index=min(1, len(labels) - 1))
    with c2:
        right_lbl = st.selectbox("Candidate Version (Right)", labels, index=0)
    with c3:
        st.write("")
        only_changed = st.checkbox("Show Modified Rows Only", value=False)

    left_id = str(by_label[left_lbl]["id"])
    right_id = str(by_label[right_lbl]["id"])

    cmp_res = client.compare_strategy_versions(left_id=left_id, right_id=right_id)
    if not cmp_res.ok or not isinstance(cmp_res.data, dict):
        st.error(cmp_res.error or "Unable to compare strategy versions.")
        return

    cmp_data = cmp_res.data
    rows: list[dict[str, Any]] = cmp_data.get("rows", [])
    if only_changed:
        rows = [r for r in rows if r.get("changed")]

    changed_cnt = int(cmp_data.get("changed_count", 0))
    unchanged_cnt = int(cmp_data.get("unchanged_count", 0))

    if not rows:
        render_empty_blotter(
            panel_title=f"Comparison: {left_lbl} vs {right_lbl}",
            panel_meta=f"0 DIFFERENCES ({unchanged_cnt} IDENTICAL)",
            columns=[("Section", False), ("Parameter", False), (left_lbl, True), (right_lbl, True), ("State", False)],
            empty_title="PARAMETERS ARE IDENTICAL",
            empty_description=f"{left_lbl} and {right_lbl} have identical canonical parameter values.",
        )
        return

    html_rows: list[str] = []
    for r in rows:
        changed = bool(r.get("changed"))
        tr_cls = ' class="diff-changed"' if changed else ""
        grp = html.escape(str(r.get("group", "")))
        lbl = html.escape(str(r.get("label", "")))
        lv = html.escape(str(r.get("left_value", "—")))
        rv = html.escape(str(r.get("right_value", "—")))
        state_badge = badge_html("WARNING" if changed else "INFO").replace(
            "WARNING", "MODIFIED"
        ).replace("INFO", "IDENTICAL")
        html_rows.append(
            f"<tr{tr_cls}>"
            f'<td class="mono">{grp}</td>'
            f"<td>{lbl}</td>"
            f'<td class="num">{lv}</td>'
            f'<td class="num">{rv}</td>'
            f"<td>{state_badge}</td>"
            f"</tr>"
        )

    st.markdown(
        f"""
        <div class="desk-panel">
            <div class="desk-panel-header">
                <span class="desk-panel-title">Parameter Comparison &mdash; {html.escape(left_lbl)} vs {html.escape(right_lbl)}</span>
                <span class="desk-panel-meta">{changed_cnt} MODIFIED &bull; {unchanged_cnt} IDENTICAL</span>
            </div>
            <table class="blotter-table">
                <thead>
                    <tr>
                        <th>Section</th>
                        <th>Parameter</th>
                        <th class="num">{html.escape(left_lbl)}</th>
                        <th class="num">{html.escape(right_lbl)}</th>
                        <th>Comparison</th>
                    </tr>
                </thead>
                <tbody>{"".join(html_rows)}</tbody>
            </table>
        </div>
        """,
        unsafe_allow_html=True,
    )


def _render_version_history_tab(versions: list[dict[str, Any]]) -> None:
    inst_filter = st.selectbox(
        "Filter Version Ledger by Instrument",
        options=["ALL", "NIFTY", "BANKNIFTY"],
        key="sl_history_inst_filter",
    )
    filtered = [
        v
        for v in versions
        if inst_filter == "ALL" or str(v.get("instrument_symbol", "")).upper() == inst_filter
    ]

    if not filtered:
        render_empty_blotter(
            panel_title="Immutable Strategy Version History",
            panel_meta="0 VERSIONS",
            columns=[("Version", False), ("Instrument", False), ("EMA", True), ("RSI", True), ("Status", False)],
            empty_title="NO VERSIONS FOR SELECTED INSTRUMENT",
            empty_description="Create or clone a strategy version in the Authoring Desk tab.",
        )
        return

    rows_html: list[str] = []
    for v in filtered:
        p = v.get("parameters", {})
        lbl = html.escape(str(v.get("version_label", "")))
        sym = html.escape(str(v.get("instrument_symbol", "")))
        tf = html.escape(str(v.get("timeframe", "")))
        ema_txt = f"{p.get('fast_ema_period', '—')}/{p.get('slow_ema_period', '—')}"
        rsi_txt = f"{p.get('rsi_period', '—')} (&ge;{p.get('long_rsi_threshold', '—')} / &le;{p.get('short_rsi_threshold', '—')})"
        unit_s = "%" if p.get("stop_target_unit") == "PERCENT" else "pts"
        sl_tp = f"SL {p.get('stop_loss', '—')} / TP {p.get('profit_target', '—')} {unit_s}"
        sess_txt = (
            f"{p.get('trading_start_time', '—')} &rarr; {p.get('last_entry_time', '—')} "
            f"(Sq {p.get('mandatory_square_off_time', '—')})"
        )
        sizing_txt = f"{p.get('position_sizing_mode', '—')}:{p.get('position_size_value', '—')}"
        max_t = html.escape(str(p.get("max_trades_per_day", "—")))
        cloned = html.escape(str(v.get("cloned_from_label") or "ROOT"))
        phash = html.escape(str(v.get("parameter_hash", ""))[:10])
        author = html.escape(str(v.get("created_by", "")))
        created = html.escape(fmt_ist(v.get("created_at")))
        status = str(v.get("status", "VALIDATED"))

        rows_html.append(
            f"<tr>"
            f'<td class="mono"><strong>{lbl}</strong></td>'
            f'<td class="mono">{sym}</td>'
            f'<td class="mono">{tf}</td>'
            f'<td class="num">{html.escape(ema_txt)}</td>'
            f'<td class="num">{rsi_txt}</td>'
            f'<td class="num">{html.escape(sl_tp)}</td>'
            f'<td class="mono">{sess_txt}</td>'
            f'<td class="mono">{html.escape(sizing_txt)}</td>'
            f'<td class="num">{max_t}</td>'
            f'<td class="mono">{cloned}</td>'
            f'<td class="mono">{phash}</td>'
            f"<td>{author}</td>"
            f'<td class="mono">{created}</td>'
            f"<td>{badge_html(status)}</td>"
            f"</tr>"
        )

    st.markdown(
        f"""
        <div class="desk-panel">
            <div class="desk-panel-header">
                <span class="desk-panel-title">Immutable Strategy Version History</span>
                <span class="desk-panel-meta">{len(rows_html)} VERSIONS PERSISTED IN POSTGRESQL (APPEND-ONLY)</span>
            </div>
            <table class="blotter-table">
                <thead>
                    <tr>
                        <th>Version</th>
                        <th>Inst</th>
                        <th>TF</th>
                        <th class="num">EMA (F/S)</th>
                        <th class="num">RSI (P | Gate)</th>
                        <th class="num">SL / TP</th>
                        <th>Session Window (IST)</th>
                        <th>Sizing</th>
                        <th class="num">Max/Day</th>
                        <th>Lineage</th>
                        <th>SHA-256</th>
                        <th>Created By</th>
                        <th>Created (IST)</th>
                        <th>Status</th>
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
        "STRATEGY LAB",
        "EMA CROSSOVER + RSI CONFIRMATION  |  IMMUTABLE VERSIONING  |  SIGNALS ONLY (PAPER DESK)",
    )

    strat_res = client.list_strategies()
    if not strat_res.ok or not isinstance(strat_res.data, list):
        render_api_offline_banner(strat_res.error or "Unable to fetch Strategy Lab catalog", client.base_url)
        return

    strategies: list[dict[str, Any]] = strat_res.data
    if not strategies:
        render_empty_blotter(
            panel_title="Strategy Catalog",
            panel_meta="0 DEFINITIONS",
            columns=[("Code", False), ("Name", False), ("Category", False), ("Status", False)],
            empty_title="NO STRATEGY DEFINITIONS SEEDED",
            empty_description="Run Alembic migrations (`alembic upgrade head`) and restart FastAPI to seed EMA_RSI_CROSSOVER.",
        )
        return

    inst_res = client.list_instruments()
    instruments_by_symbol: dict[str, dict[str, Any]] = {}
    if inst_res.ok and isinstance(inst_res.data, list):
        instruments_by_symbol = {str(i["symbol"]).upper(): i for i in inst_res.data}

    selected_def = strategies[0]
    code = str(selected_def.get("code", "EMA_RSI_CROSSOVER"))
    versions: list[dict[str, Any]] = selected_def.get("versions", [])
    supported_symbols: list[str] = list(selected_def.get("supported_symbols", ["NIFTY", "BANKNIFTY"]))

    if not versions:
        render_empty_blotter(
            panel_title="Immutable Strategy Versions",
            panel_meta="0 VERSIONS",
            columns=[("Version", False), ("Instrument", False), ("Status", False)],
            empty_title="NO BASELINE VERSIONS FOUND",
            empty_description="Seed baseline NIFTY_EMA_RSI_v1 and BANKNIFTY_EMA_RSI_v1 via backend startup.",
        )
        return

    version_labels = [str(v["version_label"]) for v in versions]
    default_idx = 0
    remembered = st.session_state.get("strategy_lab_selected_label")
    if remembered in version_labels:
        default_idx = version_labels.index(remembered)

    sel_col1, sel_col2 = st.columns([2.2, 3.8])
    with sel_col1:
        chosen_label = st.selectbox(
            "Active Inspected Strategy Version",
            options=version_labels,
            index=default_idx,
        )
        st.session_state["strategy_lab_selected_label"] = chosen_label
    with sel_col2:
        st.markdown(
            """
            <div style="padding-top:1.35rem; font-family:Consolas,monospace; font-size:0.72rem; color:#94A3B8;">
                SELECT ANY VERSION TO INSPECT ITS SPECIFICATION SNAPSHOT OR USE AS CLONE TEMPLATE BELOW.
            </div>
            """,
            unsafe_allow_html=True,
        )

    selected_ver = next(v for v in versions if v["version_label"] == chosen_label)

    _render_strategy_summary(selected_def, selected_ver)
    _render_specification_panels(selected_ver, instruments_by_symbol)

    tab_author, tab_compare, tab_history = st.tabs(
        [
            "AUTHOR / CLONE IMMUTABLE VERSION",
            "COMPARE VERSIONS",
            f"VERSION HISTORY ({len(versions)})",
        ]
    )

    with tab_author:
        _render_authoring_and_clone_desk(
            client,
            strategy_code=code,
            selected_ver=selected_ver,
            supported_symbols=supported_symbols,
        )

    with tab_compare:
        _render_compare_versions_tab(client, versions)

    with tab_history:
        _render_version_history_tab(versions)
