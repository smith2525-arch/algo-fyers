"""Integration tests for Phase 3A Market Replay API, controls, speed selection,
signal persistence/deduplication, stale-data order blocking, health telemetry,
and crash recovery.
"""

from __future__ import annotations

import uuid
from datetime import timedelta

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import func, select

from backend.main import create_app
from core.clock import now_ist
from core.config import clear_settings_cache
from core.persistence.db import reset_engine
from core.persistence.models import AuditEventModel, ReplaySessionModel, ReplaySignalModel
from core.persistence.uow import unit_of_work


def test_market_replay_catalog_lifecycle_signals_and_stale_gate(
    migrated_db_url: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    # Disable background wall-clock worker here so candle step assertions are 100% deterministic
    monkeypatch.setenv("REPLAY_WORKER_ENABLED", "false")
    monkeypatch.setenv("MARKET_DATA_STALE_THRESHOLD_SECONDS", "15")
    clear_settings_cache()

    app = create_app()
    with TestClient(app) as client:
        # 1. Before replay starts, freshness is STOPPED and new-order gate refuses with 409
        init_fresh = client.get("/api/v1/replay/freshness")
        assert init_fresh.status_code == 200
        assert init_fresh.json()["status"] == "STOPPED"
        assert init_fresh.json()["new_orders_permitted"] is False

        gate_stopped = client.post("/api/v1/replay/freshness/check-order-gate")
        assert gate_stopped.status_code == 409
        assert gate_stopped.json()["error"] == "STALE_MARKET_DATA"

        # 2. Catalog returns NIFTY & BANKNIFTY datasets, historical sessions, and all 5 speeds
        for inst in ("NIFTY", "BANKNIFTY"):
            cat_resp = client.get("/api/v1/replay/catalog", params={"instrument": inst})
            assert cat_resp.status_code == 200
            cat = cat_resp.json()
            assert cat["instrument"] == inst
            assert cat["supported_speeds"] == ["1x", "5x", "10x", "25x", "50x"]
            assert len(cat["strategy_versions"]) >= 1
            assert len(cat["datasets"]) >= 1
            assert len(cat["datasets"][0]["historical_sessions"]) >= 2

        nifty_cat = client.get("/api/v1/replay/catalog", params={"instrument": "NIFTY"}).json()
        ver_id = nifty_cat["strategy_versions"][0]["id"]
        ds_id = nifty_cat["datasets"][0]["dataset_id"]
        # Pick a single trading session with prior-session warmup (index 2 = Day 2)
        sessions = nifty_cat["datasets"][0]["historical_sessions"]
        day2_key = sessions[2]["session_key"]

        # 3. START replay at 5x speed (emits 1 initial candle immediately)
        start_resp = client.post(
            "/api/v1/replay/start",
            json={
                "instrument": "NIFTY",
                "strategy_version_id": ver_id,
                "dataset_id": ds_id,
                "historical_session_date": day2_key,
                "replay_speed": "5x",
                "actor": "demo.presenter",
                "initial_step_bars": 1,
            },
        )
        assert start_resp.status_code == 201
        st_data = start_resp.json()
        sess_id = st_data["session"]["id"]
        assert st_data["session"]["status"] == "RUNNING"
        assert st_data["session"]["replay_speed"] == "5x"
        assert st_data["session"]["candles_processed"] == 1
        assert st_data["session"]["warmup_bars_count"] > 0
        assert st_data["session"]["replay_clock_ist"].endswith("+05:30")
        assert st_data["last_candle"]["symbol"] == "NIFTY"
        assert st_data["strategy_evaluation"]["fast_ema"] is not None
        assert st_data["strategy_evaluation"]["slow_ema"] is not None
        assert st_data["strategy_evaluation"]["rsi"] is not None
        assert st_data["freshness"]["status"] == "HEALTHY"
        assert st_data["freshness"]["new_orders_permitted"] is True

        # Order gate passes (200 OK) while stream is HEALTHY
        gate_ok = client.post("/api/v1/replay/freshness/check-order-gate")
        assert gate_ok.status_code == 200
        assert gate_ok.json()["status"] == "HEALTHY"
        assert gate_ok.json()["new_orders_permitted"] is True

        # System Health & Dashboard reflect active HEALTHY Market Data + Strategy Engine
        health_run = client.get("/api/v1/health").json()
        comp_status = {c["component"]: c["status"] for c in health_run["components"]}
        assert comp_status["MARKET_DATA"] == "HEALTHY"
        assert comp_status["STRATEGY_ENGINE"] == "HEALTHY"

        dash_run = client.get("/api/v1/dashboard").json()
        assert dash_run["kpis"]["active_strategies"] == 1
        assert len(dash_run["running_strategies"]) == 1

        # 4. Change speed across all supported speeds (1x, 10x, 25x, 50x)
        for spd in ("1x", "10x", "25x", "50x"):
            spd_resp = client.post(
                f"/api/v1/replay/{sess_id}/speed",
                json={"replay_speed": spd, "actor": "demo.presenter"},
            )
            assert spd_resp.status_code == 200
            assert spd_resp.json()["session"]["replay_speed"] == spd

        # 5. Step through the remainder of the session to emit & persist strategy signals
        step_resp = client.post(
            f"/api/v1/replay/{sess_id}/step",
            json={"bars": 60, "actor": "demo.presenter"},
        )
        assert step_resp.status_code == 200
        step_body = step_resp.json()
        assert step_body["session"]["candles_processed"] == 61
        assert len(step_body["chart_candles"]) == 61

        # 6. PAUSE -> Freshness transitions to STALE and refuses new executable paper orders
        pause_resp = client.post(
            f"/api/v1/replay/{sess_id}/pause", json={"actor": "demo.presenter"}
        )
        assert pause_resp.status_code == 200
        assert pause_resp.json()["session"]["status"] == "PAUSED"
        assert pause_resp.json()["freshness"]["status"] == "STALE"
        assert pause_resp.json()["freshness"]["new_orders_permitted"] is False

        gate_paused = client.post("/api/v1/replay/freshness/check-order-gate")
        assert gate_paused.status_code == 409
        assert gate_paused.json()["error"] == "STALE_MARKET_DATA"

        # 7. RESUME -> Freshness returns to HEALTHY; simulate wall-clock staleness > 15s
        resume_resp = client.post(
            f"/api/v1/replay/{sess_id}/resume", json={"actor": "demo.presenter"}
        )
        assert resume_resp.status_code == 200
        assert resume_resp.json()["session"]["status"] == "RUNNING"
        assert resume_resp.json()["freshness"]["status"] == "HEALTHY"

        with unit_of_work() as db_sess:
            row = db_sess.get(ReplaySessionModel, uuid.UUID(sess_id))
            assert row is not None
            row.last_candle_received_at = now_ist() - timedelta(seconds=30)
            db_sess.flush()

        stale_check = client.get("/api/v1/replay/freshness").json()
        assert stale_check["status"] == "STALE"
        assert stale_check["new_orders_permitted"] is False
        assert client.post("/api/v1/replay/freshness/check-order-gate").status_code == 409

        health_stale = client.get("/api/v1/health").json()
        comp_stale = {c["component"]: c["status"] for c in health_stale["components"]}
        assert comp_stale["MARKET_DATA"] == "STALE"

        # 8. STOP -> Freshness transitions to STOPPED
        stop_resp = client.post(
            f"/api/v1/replay/{sess_id}/stop", json={"actor": "demo.presenter"}
        )
        assert stop_resp.status_code == 200
        assert stop_resp.json()["session"]["status"] == "STOPPED"
        assert stop_resp.json()["freshness"]["status"] == "STOPPED"

        # 9. Start ALL_SESSIONS and step 200 candles to verify signal persistence & reset preservation
        all_start = client.post(
            "/api/v1/replay/start",
            json={
                "instrument": "NIFTY",
                "strategy_version_id": ver_id,
                "dataset_id": ds_id,
                "historical_session_date": "ALL_SESSIONS",
                "replay_speed": "25x",
                "actor": "demo.presenter",
                "initial_step_bars": 1,
            },
        )
        assert all_start.status_code == 201
        all_sess_id = all_start.json()["session"]["id"]

        all_step = client.post(
            f"/api/v1/replay/{all_sess_id}/step",
            json={"bars": 200, "actor": "demo.presenter"},
        )
        assert all_step.status_code == 200
        all_body = all_step.json()
        emitted_count = all_body["session"]["signals_emitted"]
        assert emitted_count >= 1
        assert all_body["latest_signal"] is not None
        assert all_body["latest_signal"]["bar_timestamp"].endswith("+05:30")

        # 10. RESET creates a fresh CONFIGURED session at 0 candles while preserving historical signals
        reset_resp = client.post(
            f"/api/v1/replay/{all_sess_id}/reset", json={"actor": "demo.presenter"}
        )
        assert reset_resp.status_code == 200
        reset_body = reset_resp.json()
        assert reset_body["session"]["id"] != all_sess_id
        assert reset_body["session"]["status"] == "CONFIGURED"
        assert reset_body["session"]["candles_processed"] == 0
        assert reset_body["freshness"]["status"] == "STOPPED"

        with unit_of_work() as db_sess:
            persisted_sigs = db_sess.scalar(select(func.count()).select_from(ReplaySignalModel))
            assert persisted_sigs is not None and persisted_sigs >= emitted_count
            audit_actions = set(
                db_sess.scalars(select(AuditEventModel.action)).all()
            )
            assert {
                "REPLAY_SESSION_STARTED",
                "REPLAY_SPEED_UPDATED",
                "REPLAY_SESSION_PAUSED",
                "REPLAY_SESSION_RESUMED",
                "REPLAY_SESSION_STOPPED",
                "REPLAY_SESSION_RESET",
                "STRATEGY_SIGNAL_GENERATED",
            } <= audit_actions


def test_replay_startup_recovery_moves_running_session_to_paused_recovery(
    migrated_db_url: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("REPLAY_WORKER_ENABLED", "false")
    clear_settings_cache()

    app1 = create_app()
    with TestClient(app1) as client1:
        cat = client1.get("/api/v1/replay/catalog", params={"instrument": "BANKNIFTY"}).json()
        start_resp = client1.post(
            "/api/v1/replay/start",
            json={
                "instrument": "BANKNIFTY",
                "strategy_version_id": cat["strategy_versions"][0]["id"],
                "dataset_id": cat["datasets"][0]["dataset_id"],
                "historical_session_date": "ALL_SESSIONS",
                "replay_speed": "10x",
                "actor": "ops.user",
                "initial_step_bars": 5,
            },
        )
        assert start_resp.status_code == 201
        sess_id = start_resp.json()["session"]["id"]
        assert start_resp.json()["session"]["status"] == "RUNNING"

    # Simulate container restart while replay was RUNNING -> lifespan recovers to PAUSED_RECOVERY
    reset_engine()
    app2 = create_app()
    with TestClient(app2) as client2:
        recovered = client2.get("/api/v1/replay/state", params={"session_id": sess_id}).json()
        assert recovered["session"]["status"] == "PAUSED_RECOVERY"
        assert recovered["session"]["candles_processed"] == 5
        assert recovered["freshness"]["status"] == "STALE"

        # Operator can cleanly RESUME from PAUSED_RECOVERY
        resumed = client2.post(
            f"/api/v1/replay/{sess_id}/resume", json={"actor": "ops.user"}
        )
        assert resumed.status_code == 200
        assert resumed.json()["session"]["status"] == "RUNNING"


def test_replay_wall_clock_speed_advancement(
    migrated_db_url: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("REPLAY_WORKER_ENABLED", "false")
    monkeypatch.setenv("REPLAY_BASE_CANDLES_PER_SECOND", "1.0")
    clear_settings_cache()

    app = create_app()
    with TestClient(app) as client:
        cat = client.get("/api/v1/replay/catalog", params={"instrument": "NIFTY"}).json()
        start_resp = client.post(
            "/api/v1/replay/start",
            json={
                "instrument": "NIFTY",
                "strategy_version_id": cat["strategy_versions"][0]["id"],
                "dataset_id": cat["datasets"][0]["dataset_id"],
                "historical_session_date": "ALL_SESSIONS",
                "replay_speed": "10x",
                "actor": "ops.user",
                "initial_step_bars": 1,
            },
        )
        assert start_resp.status_code == 201
        sess_id = start_resp.json()["session"]["id"]

        # Rewind last_step_wall_clock by 1.2s at 10x -> should advance ~12 candles on advance_due=True
        with unit_of_work() as db_sess:
            row = db_sess.get(ReplaySessionModel, uuid.UUID(sess_id))
            assert row is not None
            row.last_step_wall_clock = now_ist() - timedelta(seconds=1.2)
            db_sess.flush()

        advanced = client.get(
            "/api/v1/replay/state", params={"session_id": sess_id, "advance_due": "true"}
        ).json()
        assert advanced["session"]["candles_processed"] >= 12
