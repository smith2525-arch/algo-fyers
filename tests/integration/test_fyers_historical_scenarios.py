"""End-to-End Integration & Scenario Tests for FYERS Historical Market Data (Mocked FYERS).

Covers all 6 mandatory scenarios without requiring live FYERS credentials:
- SCENARIO 1: FYERS historical response -> normalized dataset -> valid quality report -> stored -> CSV export -> round-trip upload
- SCENARIO 2: Stored FYERS dataset -> Backtest -> completed result referencing immutable dataset ID + retention delete lock + partial dataset gate
- SCENARIO 3: Stored FYERS dataset -> Market Replay -> StrategyInstance signal -> Risk approval -> internal Paper Order -> simulated Fill -> Position -> P&L -> Audit Trace
- SCENARIO 4: Expired FYERS access token -> historical download returns FYERS_AUTHENTICATION_REQUIRED -> stored/CSV/synthetic paths remain usable (+ disabled feature flag check)
- SCENARIO 5: Render-style restart during real-data replay -> PAUSED_RECOVERY -> zero duplicate signals -> zero duplicate paper orders
- SCENARIO 6: Real FYERS historical data selected in Streamlit UI -> displays REAL HISTORICAL MARKET DATA, SOURCE: FYERS, and EXECUTION: SIMULATED PAPER TRADING
"""

from __future__ import annotations

from datetime import date, datetime, time, timedelta
from decimal import Decimal
import json
import os
from pathlib import Path
import subprocess
import sys
import time as time_mod

import uuid

from fastapi.testclient import TestClient
import httpx
import pytest
from sqlalchemy import select
from streamlit.testing.v1 import AppTest

from backend.main import create_app
from core.clock import IST
from core.config import AppSettings, clear_settings_cache
from core.market_data.dataset_service import HistoricalDatasetService
from core.market_data.service import MarketReplayService
from core.paper_trading.service import PaperTradingService
from core.persistence.models import PaperOrderModel, ReplaySessionModel, ReplaySignalModel, StrategyVersionModel
from core.persistence.seed import seed_foundation_data
from core.persistence.uow import unit_of_work

PROJECT_ROOT = Path(__file__).resolve().parent.parent.parent


def _build_realistic_fyers_two_day_5m_candles() -> list[list[object]]:
    """Build 150 realistic 5m NIFTY bars across 2 trading days producing deterministic EMA+RSI crossovers."""
    days = [date(2026, 1, 14), date(2026, 1, 15)]
    out: list[list[object]] = []
    px = Decimal("24200.00")

    # Day 1: steady base then afternoon dip; Day 2: strong morning uptrend crossover then afternoon pullback
    day_drifts = [
        [Decimal("-1.5")] * 35 + [Decimal("-4.0")] * 40,
        [Decimal("7.5")] * 30 + [Decimal("-8.5")] * 30 + [Decimal("2.0")] * 15,
    ]
    for d_idx, d in enumerate(days):
        cur = datetime.combine(d, time(9, 15), tzinfo=IST)
        for b_idx in range(75):
            drift = day_drifts[d_idx][b_idx]
            o_px = px
            c_px = o_px + drift
            h_px = max(o_px, c_px) + Decimal("6.25")
            l_px = min(o_px, c_px) - Decimal("5.75")
            epoch = int(cur.timestamp())
            out.append(
                [
                    epoch,
                    float(o_px),
                    float(h_px),
                    float(l_px),
                    float(c_px),
                    85000 + b_idx * 100,
                ]
            )
            px = c_px
            cur += timedelta(minutes=5)
    return out


def test_scenarios_1_2_3_4_5_fyers_download_csv_backtest_replay_auth_and_restart(
    migrated_db_url: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("REPLAY_WORKER_ENABLED", "false")
    monkeypatch.setenv("FYERS_HISTORICAL_DATA_ENABLED", "true")
    monkeypatch.setenv("FYERS_CLIENT_ID", "DEMOAPP-100")
    monkeypatch.setenv("FYERS_ACCESS_TOKEN", "mock-valid-fyers-access-token")
    clear_settings_cache()

    fyers_bars = _build_realistic_fyers_two_day_5m_candles()
    ref_now = datetime(2026, 1, 20, 12, 0, 0, tzinfo=IST)

    app = create_app()
    with TestClient(app) as client:
        # Verify /historical-data/providers & /historical-data/instruments expose zero secrets
        prov_r = client.get("/api/v1/historical-data/providers")
        assert prov_r.status_code == 200
        assert "mock-valid-fyers-access-token" not in prov_r.text
        fyers_p = next(p for p in prov_r.json()["providers"] if p["provider"] == "FYERS")
        assert fyers_p["status"] == "CONFIGURED"
        assert fyers_p["execution_capability"] == "NONE_HISTORICAL_READ_ONLY"

        inst_r = client.get("/api/v1/historical-data/instruments")
        assert inst_r.status_code == 200
        by_sym = {i["internal_symbol"]: i for i in inst_r.json()["instruments"]}
        assert by_sym["NIFTY"]["provider_symbol"] == "NSE:NIFTY50-INDEX"
        assert by_sym["BANKNIFTY"]["provider_symbol"] == "NSE:NIFTYBANK-INDEX"
        assert len(inst_r.json()["timeframes"]) == 8

        # =====================================================================
        # SCENARIO 1: FYERS historical response -> normalized -> stored -> CSV export -> round-trip upload
        # =====================================================================
        mock_transport = httpx.MockTransport(
            lambda _req: httpx.Response(200, json={"s": "ok", "candles": fyers_bars})
        )
        with unit_of_work() as session:
            dl_outcome = HistoricalDatasetService.download_fyers_dataset(
                session,
                instrument="NIFTY",
                timeframe="5m",
                from_date=date(2026, 1, 14),
                to_date=date(2026, 1, 15),
                actor="qa.scenario",
                settings=AppSettings(
                    DATABASE_URL=migrated_db_url,
                    FYERS_CLIENT_ID="DEMOAPP-100",
                    FYERS_ACCESS_TOKEN="mock-valid-fyers-access-token",
                ),
                http_transport=mock_transport,
                reference_now=ref_now,
            )
        assert dl_outcome["ok"] is True
        fyers_ds = dl_outcome["dataset"]
        fyers_ds_id = fyers_ds["dataset_id"]
        assert fyers_ds["source_type"] == "FYERS_HISTORICAL"
        assert fyers_ds["provider"] == "FYERS"
        assert fyers_ds["provider_symbol"] == "NSE:NIFTY50-INDEX"
        assert fyers_ds["quality_status"] == "VALID"
        assert fyers_ds["candle_count"] == 150
        assert fyers_ds["labels"]["ui_headline"] == "REAL HISTORICAL MARKET DATA"
        assert fyers_ds["labels"]["ui_source_line"] == "SOURCE: FYERS"
        assert fyers_ds["labels"]["ui_execution_line"] == "EXECUTION: SIMULATED PAPER TRADING"

        # Inspect download run & dataset quality endpoints
        dl_run_id = dl_outcome["download_run"]["run_code"]
        assert client.get(f"/api/v1/historical-data/downloads/{dl_run_id}").status_code == 200
        qual_r = client.get(f"/api/v1/historical-data/datasets/{fyers_ds_id}/quality")
        assert qual_r.status_code == 200
        assert qual_r.json()["quality_status"] == "VALID"
        assert qual_r.json()["valid_candle_count"] == 150

        # Export CSV and verify format + round-trip re-upload
        csv_r = client.get(f"/api/v1/historical-data/datasets/{fyers_ds_id}/csv")
        assert csv_r.status_code == 200
        assert csv_r.headers["content-type"].startswith("text/csv")
        assert csv_r.headers["X-Dataset-Filename"] == "NIFTY_5MIN_FYERS_2026-01-14_2026-01-15.csv"
        csv_text = csv_r.text
        assert csv_text.splitlines()[0] == "timestamp,open,high,low,close,volume"
        assert "mock-valid-fyers-access-token" not in csv_text

        up_r = client.post(
            "/api/v1/historical-data/uploads",
            json={
                "name": "Round-Trip Exported FYERS CSV",
                "instrument": "NIFTY",
                "timeframe": "5m",
                "csv_text": csv_text,
                "actor": "qa.scenario",
            },
        )
        assert up_r.status_code == 201
        assert up_r.json()["dataset"]["candle_count"] == 150
        assert up_r.json()["dataset"]["data_version_hash"] == fyers_ds["data_version_hash"]

        # =====================================================================
        # SCENARIO 2: Stored FYERS dataset -> Backtest -> immutable dataset ID & retention lock
        # =====================================================================
        strat_versions = client.get("/api/v1/strategies").json()[0]["versions"]
        nifty_v1 = next(v for v in strat_versions if v["version_label"] == "NIFTY_EMA_RSI_v1")

        bt_res = client.post(
            "/api/v1/backtests",
            json={
                "strategy_version_id": nifty_v1["id"],
                "dataset_id": fyers_ds_id,
                "initial_capital": "500000.00",
                "execution_delay_bars": 1,
                "actor": "qa.scenario",
            },
        )
        assert bt_res.status_code == 201
        bt_body = bt_res.json()
        assert bt_body["status"] == "COMPLETED"
        assert bt_body["dataset_id"] == fyers_ds_id
        assert bt_body["dataset_source"] == "FYERS_HISTORICAL"
        assert bt_body["data_version_hash"] == fyers_ds["data_version_hash"]
        assert bt_body["dataset_labels"]["ui_headline"] == "REAL HISTORICAL MARKET DATA"

        # Dataset referenced by completed backtest MUST block deletion (409 DATASET_IN_USE)
        del_blocked = client.delete(f"/api/v1/historical-data/datasets/{fyers_ds_id}")
        assert del_blocked.status_code == 409
        assert del_blocked.json()["error"] == "DATASET_IN_USE"

        # Verify PARTIAL dataset requires explicit acknowledgment before backtest
        partial_transport = httpx.MockTransport(
            lambda req: (
                httpx.Response(200, json={"s": "ok", "candles": fyers_bars[:75]})
                if req.url.params.get("range_from") == "2026-01-14"
                else httpx.Response(503, json={"s": "error", "message": "Upstream chunk timeout"})
            )
        )
        with unit_of_work() as session:
            part_out = HistoricalDatasetService.download_fyers_dataset(
                session,
                instrument="NIFTY",
                timeframe="5m",
                from_date=date(2026, 1, 14),
                to_date=date(2026, 5, 1),  # 2 chunks: chunk 1 succeeds, chunk 2 fails -> PARTIAL!
                force_refresh=True,
                settings=AppSettings(
                    DATABASE_URL=migrated_db_url,
                    FYERS_CLIENT_ID="DEMOAPP-100",
                    FYERS_ACCESS_TOKEN="mock-valid-fyers-access-token",
                    FYERS_MAX_RETRIES=0,
                ),
                http_transport=partial_transport,
                reference_now=datetime(2026, 5, 5, 12, 0, tzinfo=IST),
            )
        assert part_out["ok"] is True
        part_ds = part_out["dataset"]
        assert part_ds["quality_status"] == "PARTIAL"
        assert part_ds["is_complete"] is False

        bt_part_reject = client.post(
            "/api/v1/backtests",
            json={"strategy_version_id": nifty_v1["id"], "dataset_id": part_ds["dataset_id"]},
        )
        assert bt_part_reject.status_code == 400
        assert bt_part_reject.json()["error"] == "PARTIAL_DATASET_ACKNOWLEDGMENT_REQUIRED"

        bt_part_ack = client.post(
            "/api/v1/backtests",
            json={
                "strategy_version_id": nifty_v1["id"],
                "dataset_id": part_ds["dataset_id"],
                "allow_partial_dataset": True,
            },
        )
        assert bt_part_ack.status_code == 201

        # =====================================================================
        # SCENARIO 3: Stored FYERS dataset -> Market Replay -> Signal -> Risk -> Paper Order -> Fill -> Position -> P&L -> Audit Trace
        # =====================================================================
        inst_create = client.post(
            "/api/v1/paper-trading/instances",
            json={
                "strategy_version_id": nifty_v1["id"],
                "actor": "qa.scenario",
            },
        )
        assert inst_create.status_code == 201

        rpl_start = client.post(
            "/api/v1/replay/start",
            json={
                "instrument": "NIFTY",
                "strategy_version_id": nifty_v1["id"],
                "dataset_id": fyers_ds_id,
                "historical_session_date": "2026-01-15",
                "replay_speed": "10x",
                "initial_step_bars": 1,
                "actor": "qa.scenario",
            },
        )
        assert rpl_start.status_code == 201
        rpl_state = rpl_start.json()
        rpl_sess_id = rpl_state["session"]["id"]
        assert rpl_state["session"]["dataset_source"] == "FYERS_HISTORICAL"
        assert rpl_state["session"]["dataset_provider"] == "FYERS"
        assert rpl_state["session"]["labels"]["ui_headline"] == "REAL HISTORICAL MARKET DATA"
        assert rpl_state["last_candle"]["data_provider"] == "FYERS"

        # Step through the 2026-01-15 session so EMA+RSI generates signal -> Risk -> Paper Order -> Simulated Fill -> Position -> P&L
        rpl_step = client.post(
            f"/api/v1/replay/{rpl_sess_id}/step",
            json={"bars": 45, "actor": "qa.scenario"},
        )
        assert rpl_step.status_code == 200

        orders_list = client.get("/api/v1/orders").json()["orders"]
        filled_orders = [o for o in orders_list if o["status"] == "FILLED"]
        assert len(filled_orders) >= 1, "Expected at least 1 simulated paper fill on real FYERS replay"

        positions_list = client.get("/api/v1/positions").json()["positions"]
        assert len(positions_list) >= 1

        pnl_summary = client.get("/api/v1/pnl/analytics").json()["summary"]
        assert Decimal(pnl_summary["charges"]) >= Decimal("0")

        # Verify 8-stage Correlation Audit Trace & REAL_DATA_REPLAY_STARTED event
        filled_cid = filled_orders[0]["correlation_id"]
        trace = client.get(f"/api/v1/audit/trace/{filled_cid}").json()
        assert trace["found"] is True
        assert trace["stages"][2]["status"] == "APPROVED"
        assert trace["stages"][4]["present"] is True

        md_audits = client.get("/api/v1/audit", params={"category": "MARKET_DATA", "limit": 100}).json()
        md_actions = {e["action"] for e in md_audits}
        assert "HISTORICAL_DOWNLOAD_COMPLETED" in md_actions
        assert "HISTORICAL_DATASET_CSV_EXPORTED" in md_actions
        assert "HISTORICAL_DATASET_SELECTED_FOR_REPLAY" in md_actions
        assert "REAL_DATA_REPLAY_STARTED" in md_actions

        # =====================================================================
        # SCENARIO 5: Render-style restart during RUNNING replay -> PAUSED_RECOVERY & zero duplicates
        # =====================================================================
        with unit_of_work() as session:
            sigs_before = session.query(ReplaySignalModel).count()
            ords_before = session.query(PaperOrderModel).count()

            # Simulate Render container wake-up recovery while session is RUNNING
            recovered = MarketReplayService.recover_interrupted_sessions_on_startup(session)
            assert recovered == 1
            PaperTradingService.recover_and_reconcile_on_startup(session)

            sess_row = session.get(ReplaySessionModel, uuid.UUID(rpl_sess_id))
            assert sess_row is not None
            assert sess_row.status == "PAUSED_RECOVERY"

        after_rec_state = client.get("/api/v1/replay/state", params={"session_id": rpl_sess_id}).json()
        assert after_rec_state["session"]["status"] == "PAUSED_RECOVERY"

        with unit_of_work() as session:
            assert session.query(ReplaySignalModel).count() == sigs_before
            assert session.query(PaperOrderModel).count() == ords_before

        # Explicit operator resume + complete remaining bars -> REAL_DATA_REPLAY_COMPLETED
        resumed = client.post(
            f"/api/v1/replay/{rpl_sess_id}/resume",
            json={"actor": "qa.scenario"},
        )
        assert resumed.status_code == 200
        assert resumed.json()["session"]["status"] in ("RUNNING", "COMPLETED")

        client.post(
            f"/api/v1/replay/{rpl_sess_id}/step",
            json={"bars": 50, "actor": "qa.scenario"},
        )
        done_state = client.get("/api/v1/replay/state", params={"session_id": rpl_sess_id}).json()
        assert done_state["session"]["status"] == "COMPLETED"

        # =====================================================================
        # SCENARIO 4: Expired/missing FYERS token -> download fails safely -> stored/CSV/synthetic still work
        # =====================================================================
        monkeypatch.setenv("FYERS_ACCESS_TOKEN", "")
        clear_settings_cache()

        expired_dl = client.post(
            "/api/v1/historical-data/downloads",
            json={
                "instrument": "NIFTY",
                "timeframe": "5m",
                "from_date": "2026-01-16",
                "to_date": "2026-01-16",
                "actor": "qa.scenario",
            },
        )
        assert expired_dl.status_code == 401
        assert expired_dl.json()["error"] == "FYERS_AUTHENTICATION_REQUIRED"
        assert (
            expired_dl.json()["message"]
            == "FYERS historical-data authentication requires renewal. Existing stored and uploaded datasets remain available."
        )

        # Stored FYERS dataset & synthetic dataset STILL work without a token!
        bt_still_works = client.post(
            "/api/v1/backtests",
            json={"strategy_version_id": nifty_v1["id"], "dataset_id": fyers_ds_id},
        )
        assert bt_still_works.status_code == 201
        assert bt_still_works.json()["status"] == "COMPLETED"

        syn_rpl = client.post(
            "/api/v1/replay/start",
            json={
                "instrument": "NIFTY",
                "strategy_version_id": nifty_v1["id"],
                "dataset_id": "BUNDLED_NIFTY_5M_SAMPLE",
                "historical_session_date": "2026-10-29",
                "replay_speed": "5x",
            },
        )
        assert syn_rpl.status_code == 201

        # Feature flag disabled check
        monkeypatch.setenv("FYERS_HISTORICAL_DATA_ENABLED", "false")
        clear_settings_cache()
        prov_disabled = client.get("/api/v1/historical-data/providers").json()
        assert prov_disabled["fyers_historical_data_enabled"] is False


def test_scenario_6_ui_displays_real_historical_fyers_and_simulated_paper_execution(
    migrated_db_url: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Verify Streamlit UI renders REAL HISTORICAL MARKET DATA + SOURCE: FYERS + EXECUTION: SIMULATED PAPER TRADING."""
    fyers_bars = _build_realistic_fyers_two_day_5m_candles()
    with unit_of_work() as session:
        seed_foundation_data(session)
        dl = HistoricalDatasetService.download_fyers_dataset(
            session,
            instrument="NIFTY",
            timeframe="5m",
            from_date=date(2026, 1, 14),
            to_date=date(2026, 1, 15),
            settings=AppSettings(
                DATABASE_URL=migrated_db_url,
                FYERS_CLIENT_ID="DEMOAPP-100",
                FYERS_ACCESS_TOKEN="mock-valid-fyers-access-token",
            ),
            http_transport=httpx.MockTransport(
                lambda _req: httpx.Response(200, json={"s": "ok", "candles": fyers_bars})
            ),
            reference_now=datetime(2026, 1, 20, 12, 0, tzinfo=IST),
        )
        fyers_ds_id = dl["dataset"]["dataset_id"]
        strat_ver = session.scalar(
            select(StrategyVersionModel).where(
                StrategyVersionModel.version_label == "NIFTY_EMA_RSI_v1"
            )
        )
        assert strat_ver is not None
        MarketReplayService.start_session(
            session,
            instrument="NIFTY",
            strategy_version_id=strat_ver.id,
            dataset_id=fyers_ds_id,
            historical_session_date="2026-01-15",
            replay_speed="5x",
        )

    port = 18129
    base_url = f"http://127.0.0.1:{port}"
    env = os.environ.copy()
    env["DATABASE_URL"] = migrated_db_url
    env["EXECUTION_MODE"] = "PAPER_ONLY"
    env["TIMEZONE"] = "Asia/Kolkata"
    env["AUTO_SEED_REFERENCE_DATA"] = "true"
    env["REPLAY_WORKER_ENABLED"] = "false"

    log_fh = (tmp_path / "uvicorn_scen6.log").open("w", encoding="utf-8")
    proc = subprocess.Popen(
        [sys.executable, "-m", "uvicorn", "backend.main:app", "--host", "127.0.0.1", "--port", str(port)],
        cwd=str(PROJECT_ROOT),
        env=env,
        stdout=log_fh,
        stderr=subprocess.STDOUT,
        text=True,
    )
    try:
        ready = False
        for _ in range(50):
            try:
                if httpx.get(f"{base_url}/health/live", timeout=1.0, trust_env=False).status_code == 200:
                    ready = True
                    break
            except httpx.HTTPError:
                pass
            time_mod.sleep(0.1)
        assert ready

        monkeypatch.setenv("API_BASE_URL", base_url)
        at = AppTest.from_file(str(PROJECT_ROOT / "frontend" / "app.py"), default_timeout=60)
        at.run()
        assert not at.exception

        # 1. Navigate to Historical Data screen
        at.sidebar.radio[0].set_value("Historical Data").run()
        assert not at.exception
        hd_md = "\n".join(m.value for m in at.markdown)
        assert "REAL HISTORICAL MARKET DATA" in hd_md
        assert "SOURCE: FYERS" in hd_md
        assert "EXECUTION: SIMULATED PAPER TRADING" in hd_md
        assert "NSE:NIFTY50-INDEX" in hd_md
        assert "FYERS historical-data authentication requires renewal." in hd_md

        # 2. Navigate to Market Replay where active session uses the stored FYERS dataset
        at.sidebar.radio[0].set_value("Market Replay").run()
        assert not at.exception
        mr_md = "\n".join(m.value for m in at.markdown)
        assert "REAL HISTORICAL MARKET DATA" in mr_md
        assert "SOURCE: FYERS" in mr_md
        assert "EXECUTION: SIMULATED PAPER TRADING" in mr_md
        assert fyers_ds_id in mr_md
    finally:
        proc.terminate()
        try:
            proc.wait(timeout=5)
        except subprocess.TimeoutExpired:
            proc.kill()
        log_fh.close()
