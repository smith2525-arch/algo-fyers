"""Interactive 17-Step Guided Consulting Demonstration Ribbon (`frontend/components/demo_guide.py`).

Stateless UI presentation widget: queries `/api/v1/demo/status` and triggers
`/api/v1/demo/run-showcase` or `/api/v1/demo/drill/{drill}` via `ControlCenterApiClient`.
Owns ZERO trading or risk logic.
"""

from __future__ import annotations

import html
from typing import Any

import streamlit as st

from frontend.api_client import ControlCenterApiClient
from frontend.components.ui import badge_html


def render_sidebar_demo_controls(client: ControlCenterApiClient) -> bool:
    """Render compact Guided Consulting Demo launcher in the sidebar below main nav."""
    with st.expander("Guided 3–5 Min Consulting Demo", expanded=False):
        enabled = st.checkbox(
            "Show 17-Step Presenter Walkthrough",
            value=bool(st.session_state.get("guided_demo_enabled", False)),
            key="guided_demo_enabled",
        )
        st.caption(
            "Pre-populates or walks through Backtest, Replay, Signal, Risk Approval, "
            "Order, Fill, Position, P&L, Risk Rejection, Stale Block, Recovery & Audit Trace."
        )
        if st.button(
            "LOAD DETERMINISTIC DEMO SHOWCASE",
            use_container_width=True,
            key="sidebar_btn_run_demo_showcase",
        ):
            with st.spinner("Executing real Backtest, Replay, Strategy, Risk & Recovery drills..."):
                res = client.run_demo_showcase(actor="consulting.presenter")
            if res.ok:
                st.session_state["guided_demo_enabled"] = True
                st.session_state["guided_demo_step_idx"] = 0
                st.toast("Deterministic Consulting Demo Showcase ready across all 12 screens.")
                st.rerun()
            else:
                st.error(res.error or "Failed to execute demo showcase.")
    return enabled


def render_guided_demo_ribbon(
    client: ControlCenterApiClient, *, current_screen: str
) -> None:
    """Render the 17-step Guided Consulting Walkthrough presenter bar when enabled."""
    status_res = client.get_guided_demo_status()
    if not status_res.ok or not isinstance(status_res.data, dict):
        return

    data: dict[str, Any] = status_res.data
    steps: list[dict[str, Any]] = data.get("steps") or []
    if not steps:
        return

    total_steps = len(steps)
    completed_cnt = int(data.get("completed_steps", 0))
    artifacts: dict[str, Any] = data.get("showcase_artifacts") or {}
    corrs: dict[str, Any] = artifacts.get("correlations") or {}

    cur_idx = int(st.session_state.get("guided_demo_step_idx", 0))
    cur_idx = max(0, min(cur_idx, total_steps - 1))
    step_obj = steps[cur_idx]
    step_num = int(step_obj.get("step", cur_idx + 1))
    target_screen = str(step_obj.get("screen", "Dashboard"))
    is_done = bool(step_obj.get("completed"))
    drill_key = step_obj.get("drill")

    step_badge = badge_html("COMPLETED" if is_done else "INFO")
    screen_match_txt = (
        f"ON SCREEN: {target_screen.upper()}"
        if current_screen == target_screen
        else f"TARGET SCREEN: {target_screen.upper()}"
    )

    st.markdown(
        f"""
        <div class="demo-ribbon">
            <div class="demo-ribbon-header">
                <div class="demo-ribbon-title">
                    GUIDED CONSULTING WALKTHROUGH &bull; STEP {step_num} / {total_steps}:
                    <span style="color:#38BDF8;">{html.escape(str(step_obj.get("title", "")).upper())}</span>
                    &nbsp;{step_badge}
                </div>
                <div class="mono" style="font-size:0.70rem; color:#94A3B8;">
                    {html.escape(screen_match_txt)} &bull; ~{int(step_obj.get("duration_sec", 15))}s &bull;
                    LEDGER VERIFIED: <strong style="color:#34D399;">{completed_cnt}/{total_steps}</strong>
                </div>
            </div>
            <div class="demo-ribbon-note">
                <strong>Presenter Note:</strong> {html.escape(str(step_obj.get("talking_point", "")))}
            </div>
        </div>
        """,
        unsafe_allow_html=True,
    )

    c_sel, c_prev, c_jump, c_next, c_act = st.columns([2.4, 0.75, 1.25, 0.75, 1.55])
    with c_sel:
        chosen_idx = st.selectbox(
            "Jump to Demo Step",
            range(total_steps),
            index=cur_idx,
            format_func=lambda i: (
                f"[{'✓' if steps[i].get('completed') else '•'}] Step {steps[i]['step']}: "
                f"{steps[i]['title']} ({steps[i]['screen']})"
            ),
            label_visibility="collapsed",
            key="guided_demo_picker",
        )
        if chosen_idx != cur_idx:
            st.session_state["guided_demo_step_idx"] = chosen_idx
            st.session_state["pending_nav_screen"] = str(steps[chosen_idx]["screen"])
            st.rerun()

    with c_prev:
        if st.button(
            "◀ PREV",
            disabled=(cur_idx == 0),
            use_container_width=True,
            key="demo_btn_prev",
        ):
            new_i = max(0, cur_idx - 1)
            st.session_state["guided_demo_step_idx"] = new_i
            st.session_state["pending_nav_screen"] = str(steps[new_i]["screen"])
            st.rerun()

    with c_jump:
        if st.button(
            f"OPEN {target_screen.upper()}",
            disabled=(current_screen == target_screen),
            use_container_width=True,
            key="demo_btn_open_screen",
        ):
            st.session_state["pending_nav_screen"] = target_screen
            st.rerun()

    with c_next:
        if st.button(
            "NEXT ▶",
            disabled=(cur_idx >= total_steps - 1),
            use_container_width=True,
            key="demo_btn_next",
        ):
            new_i = min(total_steps - 1, cur_idx + 1)
            st.session_state["guided_demo_step_idx"] = new_i
            st.session_state["pending_nav_screen"] = str(steps[new_i]["screen"])
            st.rerun()

    with c_act:
        if drill_key:
            drill_labels = {
                "risk_rejection": "RUN RISK REJECTION DRILL",
                "stale_data_block": "RUN STALE-DATA DRILL",
                "recover_and_reconcile": "RECOVER & RECONCILE",
            }
            if st.button(
                drill_labels.get(str(drill_key), "RUN STEP DRILL"),
                type="primary",
                use_container_width=True,
                key=f"demo_btn_drill_{drill_key}",
            ):
                with st.spinner(f"Executing deterministic {drill_key} drill..."):
                    d_res = client.trigger_demo_drill(str(drill_key))
                if d_res.ok:
                    st.session_state["pending_nav_screen"] = target_screen
                    st.rerun()
                else:
                    st.error(d_res.error or "Drill execution failed.")
        elif step_num == 17 and corrs.get("recommended_trace_cid"):
            if st.button(
                "TRACE RECOMMENDED TRADE",
                type="primary",
                use_container_width=True,
                key="demo_btn_trace_cid",
            ):
                st.session_state["guided_trace_cid"] = str(corrs["recommended_trace_cid"])
                st.session_state["pending_nav_screen"] = "Audit Log"
                st.rerun()
        else:
            if st.button(
                "AUTO-RUN ALL 17 STEPS",
                use_container_width=True,
                key="demo_btn_autorun_top",
            ):
                with st.spinner("Populating deterministic consulting showcase..."):
                    s_res = client.run_demo_showcase(actor="consulting.presenter")
                if s_res.ok:
                    st.rerun()
                else:
                    st.error(s_res.error or "Failed to run showcase.")
