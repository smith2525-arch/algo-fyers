"""End-to-end UI scenario tests verifying real Uvicorn process + Streamlit 12-screen navigation."""

from __future__ import annotations

import os
import subprocess
import sys
import time
from collections.abc import Iterator
from pathlib import Path

import httpx
import pytest
from streamlit.testing.v1 import AppTest

PROJECT_ROOT = Path(__file__).resolve().parent.parent.parent

EXPECTED_12_SCREENS = [
    "Dashboard",
    "Strategy Lab",
    "Historical Data",
    "Backtests",
    "Market Replay",
    "Paper Trading",
    "Orders",
    "Positions",
    "Risk Center",
    "P&L Analytics",
    "Audit Log",
    "System Health",
    "Settings",
]


@pytest.fixture()
def live_api_server(migrated_db_url: str, tmp_path: Path) -> Iterator[str]:
    """Launch a real standalone Uvicorn API process and wait until /health/live responds."""
    port = 18124
    base_url = f"http://127.0.0.1:{port}"
    env = os.environ.copy()
    env["DATABASE_URL"] = migrated_db_url
    env["EXECUTION_MODE"] = "PAPER_ONLY"
    env["TIMEZONE"] = "Asia/Kolkata"
    env["AUTO_SEED_REFERENCE_DATA"] = "true"
    env["REPLAY_WORKER_ENABLED"] = "false"

    log_file_path = tmp_path / "uvicorn_ui_test.log"
    log_fh = log_file_path.open("w", encoding="utf-8")

    proc = subprocess.Popen(
        [
            sys.executable,
            "-m",
            "uvicorn",
            "backend.main:app",
            "--host",
            "127.0.0.1",
            "--port",
            str(port),
        ],
        cwd=str(PROJECT_ROOT),
        env=env,
        stdout=log_fh,
        stderr=subprocess.STDOUT,
        text=True,
    )

    ready = False
    for _ in range(50):
        if proc.poll() is not None:
            log_fh.flush()
            out = log_file_path.read_text(encoding="utf-8")
            log_fh.close()
            pytest.fail(f"Uvicorn process exited prematurely:\n{out}")
        try:
            r = httpx.get(f"{base_url}/health/live", timeout=1.0, trust_env=False)
            if r.status_code == 200:
                ready = True
                break
        except httpx.HTTPError:
            pass
        time.sleep(0.1)

    if not ready:
        proc.terminate()
        log_fh.close()
        pytest.fail("Timed out waiting for standalone Uvicorn /health/live")

    yield base_url

    proc.terminate()
    try:
        proc.wait(timeout=5)
    except subprocess.TimeoutExpired:
        proc.kill()
    log_fh.close()


def test_streamlit_console_loads_and_navigates_all_12_screens(
    live_api_server: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("API_BASE_URL", live_api_server)

    at = AppTest.from_file(str(PROJECT_ROOT / "frontend" / "app.py"), default_timeout=60)
    at.run()
    assert not at.exception, f"Dashboard raised exception: {at.exception}"

    # Verify sidebar navigation contains the exact 12 screens
    nav_radio = at.sidebar.radio[0]
    assert list(nav_radio.options) == EXPECTED_12_SCREENS

    # Verify persistent safety banner & Dashboard live API data are rendered
    all_md = "\n".join(m.value for m in at.markdown)
    assert "PAPER TRADING" in all_md
    assert "SIMULATED EXECUTION" in all_md
    assert "NO REAL ORDERS" in all_md
    assert "ALGO TRADING CONTROL CENTER" in all_md
    assert "BACKEND API CONNECTION DEGRADED" not in all_md
    assert "₹5,00,000.00" in all_md

    # Navigate every single one of the 12 screens and assert zero exceptions + SIMULATED PAPER TRADING
    for screen_name in EXPECTED_12_SCREENS:
        at.sidebar.radio[0].set_value(screen_name).run()
        assert not at.exception, f"Screen {screen_name!r} raised exception: {at.exception}"
        screen_md = "\n".join(m.value for m in at.markdown)
        assert "NO REAL ORDERS" in screen_md
        assert "SIMULATED PAPER TRADING" in screen_md

    # Verify Strategy Lab renders all required sections and commits a new immutable version
    at.sidebar.radio[0].set_value("Strategy Lab").run()
    assert not at.exception
    sl_md = "\n".join(m.value for m in at.markdown)
    assert "EMA Crossover + RSI Confirmation" in sl_md
    assert "Parameters (Signal" in sl_md
    assert "Risk Defaults" in sl_md
    assert "Execution Assumptions" in sl_md
    assert "Immutable Strategy Version History" in sl_md
    assert "EMA_RSI_v1" in sl_md

    save_buttons = [b for b in at.button if b.label == "SAVE NEW VERSION"]
    assert len(save_buttons) == 1
    save_buttons[0].click().run()
    assert not at.exception
    sl_md_after = "\n".join(m.value for m in at.markdown)
    assert "_EMA_RSI_v2" in sl_md_after

    # Verify Backtests screen executes a deterministic backtest and displays SIMULATED BACKTEST RESULTS
    at.sidebar.radio[0].set_value("Backtests").run()
    assert not at.exception
    bt_buttons = [b for b in at.button if b.label == "EXECUTE DETERMINISTIC BACKTEST"]
    assert len(bt_buttons) == 1
    bt_buttons[0].click().run()
    assert not at.exception
    bt_md_after = "\n".join(m.value for m in at.markdown)
    assert "SIMULATED BACKTEST RESULTS" in bt_md_after
    assert "Net P&amp;L" in bt_md_after
    assert "Maximum Drawdown" in bt_md_after
    assert "Profit Factor" in bt_md_after
    assert "Pre-Backtest Historical Data Quality Report" in bt_md_after

    # Verify Market Replay screen controls (START -> PAUSE -> +1 CANDLE -> STOP -> RESET) and telemetry
    at.sidebar.radio[0].set_value("Market Replay").run()
    assert not at.exception
    mr_md_init = "\n".join(m.value for m in at.markdown)
    assert "MARKET REPLAY" in mr_md_init
    assert "Market Clock (IST)" in mr_md_init

    start_btns = [b for b in at.button if b.label == "START"]
    assert len(start_btns) == 1
    start_btns[0].click().run()
    assert not at.exception
    mr_md_run = "\n".join(m.value for m in at.markdown)
    assert "RUNNING" in mr_md_run
    assert "HEALTHY" in mr_md_run
    assert "LATEST STRATEGY CONDITION:" in mr_md_run
    assert "WILDER RSI:" in mr_md_run
    assert "Last Received CandleEvent" in mr_md_run

    pause_btns = [b for b in at.button if b.label == "PAUSE"]
    assert len(pause_btns) == 1
    pause_btns[0].click().run()
    assert not at.exception
    mr_md_paused = "\n".join(m.value for m in at.markdown)
    assert "PAUSED" in mr_md_paused
    assert "STALE" in mr_md_paused

    step_btns = [b for b in at.button if b.label == "+1 CANDLE"]
    assert len(step_btns) == 1
    step_btns[0].click().run()
    assert not at.exception

    stop_btns = [b for b in at.button if b.label == "STOP"]
    assert len(stop_btns) == 1
    stop_btns[0].click().run()
    assert not at.exception

    reset_btns = [b for b in at.button if b.label == "RESET"]
    assert len(reset_btns) == 1
    reset_btns[0].click().run()
    assert not at.exception

    # Start Market Replay again so Market Data is HEALTHY, then test Paper Trading, Orders & Positions
    start_btns2 = [b for b in at.button if b.label == "START"]
    start_btns2[0].click().run()
    assert not at.exception

    at.sidebar.radio[0].set_value("Paper Trading").run()
    assert not at.exception
    inst_start_btns = [b for b in at.button if b.label == "START PAPER STRATEGY INSTANCE"]
    assert len(inst_start_btns) == 1
    inst_start_btns[0].click().run()
    assert not at.exception

    sub_ord_btns = [b for b in at.button if b.label == "SUBMIT SIMULATED PAPER ORDER"]
    assert len(sub_ord_btns) == 1
    sub_ord_btns[0].click().run()
    assert not at.exception
    pt_md = "\n".join(m.value for m in at.markdown)
    assert "Strategy Instances (SIMULATED PAPER TRADING)" in pt_md

    # Step 1 candle on Market Replay so the acknowledged MARKET order fills -> opens Position
    at.sidebar.radio[0].set_value("Market Replay").run()
    step_btns2 = [b for b in at.button if b.label == "+1 CANDLE"]
    step_btns2[0].click().run()
    assert not at.exception

    # Verify Orders screen renders OMS blotter & state transition audit
    at.sidebar.radio[0].set_value("Orders").run()
    assert not at.exception
    ord_md = "\n".join(m.value for m in at.markdown)
    assert "OMS Paper Order Blotter (SIMULATED PAPER TRADING)" in ord_md
    assert "Immutable OMS State-Transition Audit" in ord_md

    # Verify Positions screen renders Open position & explicit FLATTEN ALL OPEN works
    at.sidebar.radio[0].set_value("Positions").run()
    assert not at.exception
    pos_md = "\n".join(m.value for m in at.markdown)
    assert "Open Paper Positions (SIMULATED PAPER TRADING)" in pos_md
    assert "RECONCILED" in pos_md
    flatten_btns = [b for b in at.button if b.label == "FLATTEN ALL OPEN"]
    assert len(flatten_btns) == 1
    flatten_btns[0].click().run()
    assert not at.exception

    # Verify Risk Center screen displays DEMO settings disclaimer, Kill Switch, and Persisted Risk Decisions
    at.sidebar.radio[0].set_value("Risk Center").run()
    assert not at.exception
    rc_md = "\n".join(m.value for m in at.markdown)
    assert "DEMONSTRATION SETTINGS ONLY" in rc_md
    assert "Persisted Pre-Trade Risk Decisions" in rc_md

    ks_on_btns = [b for b in at.button if b.label == "ACTIVATE KILL SWITCH"]
    assert len(ks_on_btns) == 1
    ks_on_btns[0].click().run()
    assert not at.exception
    assert "ACTIVE (BLOCKED)" in "\n".join(m.value for m in at.markdown)

    ks_off_btns = [b for b in at.button if b.label == "DEACTIVATE KILL SWITCH"]
    assert len(ks_off_btns) == 1
    ks_off_btns[0].click().run()
    assert not at.exception

    # Verify Phase 5 Screen 9: P&L Analytics displays SIMULATED PAPER-TRADING RESULTS & metrics
    at.sidebar.radio[0].set_value("P&L Analytics").run()
    assert not at.exception
    pnl_md = "\n".join(m.value for m in at.markdown)
    assert "SIMULATED PAPER-TRADING RESULTS" in pnl_md
    assert "Gross P&amp;L" in pnl_md or "Gross P&L" in pnl_md
    assert "Slippage Cost" in pnl_md
    assert "Filtered Paper-Trading P&amp;L Attribution Ledger" in pnl_md

    # Verify Phase 5 Screen 10: Audit Log displays 8-stage Correlation Trace View & Append-Only Ledger
    at.sidebar.radio[0].set_value("Audit Log").run()
    assert not at.exception
    aud_md = "\n".join(m.value for m in at.markdown)
    assert "END-TO-END TRADE LIFECYCLE TRACE VIEW" in aud_md
    assert "1. Market Event" in aud_md
    assert "3. Risk Decision" in aud_md
    assert "8. Final P&amp;L" in aud_md
    assert "Immutable Audit Events" in aud_md

    # Verify Phase 5 Screen 11: System Health displays all 9 subsystems & Deterministic Reconciliation
    at.sidebar.radio[0].set_value("System Health").run()
    assert not at.exception
    sh_md = "\n".join(m.value for m in at.markdown)
    for subsystem_label in (
        "API",
        "Database",
        "Market Replay",
        "Market Data",
        "Strategy Engine",
        "Risk Engine",
        "Paper Broker",
        "Position Engine",
        "Last Reconciliation",
    ):
        assert subsystem_label in sh_md

    recon_btns = [
        b
        for b in at.button
        if b.label == "RUN DETERMINISTIC RECONCILIATION (VERIFY ONLY)"
    ]
    assert len(recon_btns) == 1
    recon_btns[0].click().run()
    assert not at.exception
    sh_md_after = "\n".join(m.value for m in at.markdown)
    assert "1. Executions vs Positions" in sh_md_after
    assert "4. Realized P&amp;L Integrity" in sh_md_after

    # Verify Guided 17-Step Consulting Demo Walkthrough Ribbon renders cleanly when toggled on
    demo_chk = [
        c for c in at.sidebar.checkbox if "17-Step Presenter Walkthrough" in c.label
    ]
    assert len(demo_chk) == 1
    demo_chk[0].check().run()
    assert not at.exception
    demo_md = "\n".join(m.value for m in at.markdown)
    assert "GUIDED CONSULTING WALKTHROUGH" in demo_md
    assert "STEP 1 / 17" in demo_md
