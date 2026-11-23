"""Streamlit Operations Console entrypoint for Algo Trading Control Center.

CRITICAL ARCHITECTURAL RULE:
This frontend owns ZERO trading state and executes ZERO trading logic.
All state and operational commands pass through `ControlCenterApiClient` -> FastAPI.
"""

from __future__ import annotations

import os
from collections.abc import Callable

import streamlit as st

from frontend.api_client import ControlCenterApiClient
from frontend.components.demo_guide import (
    render_guided_demo_ribbon,
    render_sidebar_demo_controls,
)
from frontend.components.ui import (
    inject_terminal_css,
    render_persistent_safety_banner,
)
from frontend.views import (
    audit_log,
    backtests,
    dashboard,
    historical_data,
    market_replay,
    orders,
    paper_trading,
    pnl_analytics,
    positions,
    risk_center,
    settings,
    strategy_lab,
    system_health,
)

NAVIGATION_SCREENS: dict[str, Callable[[ControlCenterApiClient], None]] = {
    "Dashboard": dashboard.render,
    "Strategy Lab": strategy_lab.render,
    "Historical Data": historical_data.render,
    "Backtests": backtests.render,
    "Market Replay": market_replay.render,
    "Paper Trading": paper_trading.render,
    "Orders": orders.render,
    "Positions": positions.render,
    "Risk Center": risk_center.render,
    "P&L Analytics": pnl_analytics.render,
    "Audit Log": audit_log.render,
    "System Health": system_health.render,
    "Settings": settings.render,
}


def main() -> None:
    st.set_page_config(
        page_title="Algo Trading Control Center | PAPER ONLY",
        layout="wide",
        initial_sidebar_state="expanded",
    )
    inject_terminal_css()

    api_base_url = os.getenv("API_BASE_URL", "http://localhost:8000")
    client = ControlCenterApiClient(base_url=api_base_url)

    if not st.session_state.get("api_awake_verified"):
        with st.spinner(
            "Trading Control Center services are starting. "
            "This demo uses scale-to-zero hosting and may take a short time after inactivity."
        ):
            live_res = client.check_liveness()
        if live_res.ok:
            st.session_state["api_awake_verified"] = True

    if st.session_state.get("api_awake_verified") and not st.session_state.get("session_login_audited"):
        login_res = client.record_operator_login("desk.operator")
        if login_res.ok:
            st.session_state["session_login_audited"] = True

    pending_screen = st.session_state.pop("pending_nav_screen", None)
    if pending_screen in NAVIGATION_SCREENS:
        st.session_state["main_nav_screen"] = pending_screen

    with st.sidebar:
        st.markdown(
            """
            <div style="border-bottom:1px solid #1E293B; padding-bottom:0.45rem; margin-bottom:0.55rem;">
                <div style="font-size:0.86rem; font-weight:700; letter-spacing:0.06em; color:#F8FAFC;">
                    ALGO CONTROL CENTER
                </div>
                <div style="font-family:Consolas,monospace; font-size:0.68rem; color:#FBBF24; margin-top:2px;">
                    PAPER TRADING &bull; IST (+05:30)
                </div>
            </div>
            """,
            unsafe_allow_html=True,
        )
        selected_screen = st.radio(
            "OPERATIONS NAVIGATION",
            options=list(NAVIGATION_SCREENS.keys()),
            key="main_nav_screen",
            label_visibility="collapsed",
        )
        demo_enabled = render_sidebar_demo_controls(client)
        st.markdown(
            f"""
            <div style="border-top:1px solid #1E293B; margin-top:0.65rem; padding-top:0.55rem; font-family:Consolas,monospace; font-size:0.67rem; color:#64748B;">
                API TARGET: {api_base_url}<br/>
                UNIVERSE: NIFTY, BANKNIFTY<br/>
                CAPITAL: DEMO / SIMULATED
            </div>
            """,
            unsafe_allow_html=True,
        )

    # Mandatory persistent simulated-execution banner across all 12 screens
    render_persistent_safety_banner()

    if demo_enabled:
        render_guided_demo_ribbon(client, current_screen=selected_screen)

    view_fn = NAVIGATION_SCREENS[selected_screen]
    view_fn(client)


if __name__ == "__main__":
    main()
