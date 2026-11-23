"""Integration tests for Phase 3B end-to-end Paper Trading pipeline, Strategy Instances,
API idempotency, direct position-mutation prohibition, explicit flatten vs STOP PAPER TRADING,
container restart recovery, and order/position reconciliation.
"""

from __future__ import annotations

import uuid
from datetime import timedelta
from decimal import Decimal

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import func, select

from backend.main import create_app
from core.clock import now_ist
from core.config import clear_settings_cache
from core.domain.enums import OrderStatus, PositionStatus
from core.persistence.db import reset_engine
from core.persistence.models import PaperFillModel, PaperOrderModel, PaperPositionModel
from core.persistence.uow import unit_of_work


def test_end_to_end_replay_strategy_instance_pipeline_and_explicit_flatten(
    migrated_db_url: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Verify Market Event -> Strategy -> Signal -> Risk -> OMS -> Paper Broker -> Execution -> Position."""
    monkeypatch.setenv("REPLAY_WORKER_ENABLED", "false")
    clear_settings_cache()

    app = create_app()
    with TestClient(app) as client:
        # 1. Start a Paper Strategy Instance on NIFTY
        cat = client.get("/api/v1/replay/catalog", params={"instrument": "NIFTY"}).json()
        ver_id = cat["strategy_versions"][0]["id"]
        ds_id = cat["datasets"][0]["dataset_id"]

        inst_resp = client.post(
            "/api/v1/paper-trading/instances",
            json={
                "strategy_version_id": ver_id,
                "order_type": "MARKET",
                "execution_latency_ms": 100,
                "latency_bars": 1,
                "slippage_bps": "2.0",
                "partial_fill_enabled": True,
                "max_fill_ratio_per_bar": "0.50",
                "auto_execute_signals": True,
                "actor": "desk.operator",
            },
        )
        assert inst_resp.status_code == 201
        inst_id = inst_resp.json()["id"]

        # 2. Start Market Replay and step 160 candles -> triggers Signal -> Order -> 2-bar Partial->Full Fill -> Position
        rpl_start = client.post(
            "/api/v1/replay/start",
            json={
                "instrument": "NIFTY",
                "strategy_version_id": ver_id,
                "dataset_id": ds_id,
                "historical_session_date": "ALL_SESSIONS",
                "replay_speed": "25x",
                "initial_step_bars": 1,
                "actor": "desk.operator",
            },
        )
        assert rpl_start.status_code == 201
        sess_id = rpl_start.json()["session"]["id"]

        step_resp = client.post(
            f"/api/v1/replay/{sess_id}/step",
            json={"bars": 160, "actor": "desk.operator"},
        )
        assert step_resp.status_code == 200

        ws = client.get("/api/v1/paper-trading/workspace").json()
        assert ws["simulation_banner"] == "SIMULATED PAPER TRADING"
        assert len(ws["recent_signals"]) >= 1
        assert len(ws["recent_orders"]) >= 1
        assert len(ws["recent_fills"]) >= 2  # 50% partial slice + 50% completion slice!

        # Verify duplicate HTTP order submission returns 200 with duplicate=True and single DB order
        ord_payload = {
            "client_order_key": "http-idem-order-key-001",
            "instrument_symbol": "BANKNIFTY",
            "side": "BUY",
            "order_type": "MARKET",
            "quantity": 30,
            "reference_price": "51500.00",
            "latency_bars": 0,
            "actor": "desk.operator",
        }
        first_sub = client.post("/api/v1/orders", json=ord_payload)
        assert first_sub.status_code == 201
        assert first_sub.json()["duplicate"] is False
        assert first_sub.json()["order"]["status"] == "FILLED"

        second_sub = client.post("/api/v1/orders", json=ord_payload)
        assert second_sub.status_code == 200
        assert second_sub.json()["duplicate"] is True
        assert second_sub.json()["order"]["id"] == first_sub.json()["order"]["id"]

        # 3. Direct mutation of position via PUT/PATCH is strictly forbidden (409)
        pos_book = client.get("/api/v1/positions").json()
        assert len(pos_book["positions"]) >= 2  # Strategy NIFTY trade(s) + BANKNIFTY open position
        open_positions = [p for p in pos_book["positions"] if p["status"] == "OPEN"]
        assert len(open_positions) >= 1
        target_pos_id = open_positions[0]["id"]

        forbid_put = client.put(f"/api/v1/positions/{target_pos_id}")
        assert forbid_put.status_code == 409
        assert forbid_put.json()["error"] == "DIRECT_POSITION_MUTATION_FORBIDDEN"

        # 4. Engaging STOP PAPER TRADING blocks NEW entry orders, does NOT auto-flatten open positions,
        #    while explicit /flatten-all command cleanly closes open positions via simulated fills!
        stop_pt = client.post(
            "/api/v1/controls/paper-trading/stop",
            json={"actor": "desk.operator", "reason": "Test halt vs flatten"},
        )
        assert stop_pt.status_code == 200

        blocked_entry = client.post(
            "/api/v1/orders",
            json={
                "client_order_key": "blocked-while-halted-001",
                "instrument_symbol": "BANKNIFTY",
                "side": "BUY",
                "order_type": "MARKET",
                "quantity": 30,
                "actor": "desk.operator",
            },
        )
        assert blocked_entry.status_code == 201
        assert blocked_entry.json()["order"]["status"] == "RISK_REJECTED"

        # Open positions are still open before explicit flatten
        still_open = [
            p
            for p in client.get("/api/v1/positions").json()["positions"]
            if p["status"] == "OPEN"
        ]
        assert len(still_open) >= 1

        flatten_resp = client.post(
            "/api/v1/positions/flatten-all", json={"actor": "desk.operator"}
        )
        assert flatten_resp.status_code == 200
        assert flatten_resp.json()["positions_flattened"] == len(still_open)

        after_flatten = client.get("/api/v1/positions").json()
        assert [p for p in after_flatten["positions"] if p["status"] == "OPEN"] == []
        assert after_flatten["reconciliation"]["reconciled"] is True

        # Pause & Stop Strategy Instance cleanly
        assert client.post(f"/api/v1/paper-trading/instances/{inst_id}/pause", json={"actor": "desk.operator"}).status_code == 200
        assert client.post(f"/api/v1/paper-trading/instances/{inst_id}/resume", json={"actor": "desk.operator"}).status_code == 200
        assert client.post(f"/api/v1/paper-trading/instances/{inst_id}/stop", json={"actor": "desk.operator"}).status_code == 200


def test_container_restart_recovery_identifies_incomplete_orders_and_reconciles_positions(
    migrated_db_url: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Verify Cloud Run restart survival: incomplete orders recovered, zero duplicates, positions healed."""
    monkeypatch.setenv("REPLAY_WORKER_ENABLED", "false")
    clear_settings_cache()

    app1 = create_app()
    with TestClient(app1) as client1:
        cat = client1.get("/api/v1/replay/catalog", params={"instrument": "NIFTY"}).json()
        client1.post(
            "/api/v1/replay/start",
            json={
                "instrument": "NIFTY",
                "strategy_version_id": cat["strategy_versions"][0]["id"],
                "dataset_id": cat["datasets"][0]["dataset_id"],
                "historical_session_date": "ALL_SESSIONS",
                "replay_speed": "5x",
                "initial_step_bars": 1,
                "actor": "ops.user",
            },
        )

        # Create 1 filled order (opening a position), 1 resting limit order, and 1 expiring order
        filled_res = client1.post(
            "/api/v1/orders",
            json={
                "client_order_key": "restart-filled-001",
                "instrument_symbol": "NIFTY",
                "side": "BUY",
                "order_type": "MARKET",
                "quantity": 75,
                "latency_bars": 0,
                "actor": "ops.user",
            },
        )
        assert filled_res.status_code == 201
        assert filled_res.json()["order"]["status"] == "FILLED"

        resting_res = client1.post(
            "/api/v1/orders",
            json={
                "client_order_key": "restart-resting-limit-001",
                "instrument_symbol": "NIFTY",
                "side": "BUY",
                "order_type": "LIMIT",
                "quantity": 75,
                "limit_price": "22000.00",
                "latency_bars": 1,
                "actor": "ops.user",
            },
        )
        resting_id = uuid.UUID(resting_res.json()["order"]["id"])

        expiring_res = client1.post(
            "/api/v1/orders",
            json={
                "client_order_key": "restart-expiring-limit-001",
                "instrument_symbol": "NIFTY",
                "side": "SELL",
                "order_type": "LIMIT",
                "quantity": 75,
                "limit_price": "26500.00",
                "latency_bars": 1,
                "expires_at": (now_ist() - timedelta(seconds=5)).isoformat(),
                "actor": "ops.user",
            },
        )
        expiring_id = uuid.UUID(expiring_res.json()["order"]["id"])

        midflight_res = client1.post(
            "/api/v1/orders",
            json={
                "client_order_key": "restart-midflight-001",
                "instrument_symbol": "NIFTY",
                "side": "BUY",
                "order_type": "LIMIT",
                "quantity": 75,
                "limit_price": "22100.00",
                "latency_bars": 1,
                "actor": "ops.user",
            },
        )
        midflight_id = uuid.UUID(midflight_res.json()["order"]["id"])

    # Simulate an abrupt mid-flight crash & position drift in DB before container restart
    with unit_of_work() as db_sess:
        total_orders_before = db_sess.scalar(select(func.count()).select_from(PaperOrderModel))
        total_fills_before = db_sess.scalar(select(func.count()).select_from(PaperFillModel))

        mid_row = db_sess.get(PaperOrderModel, midflight_id)
        assert mid_row is not None
        mid_row.status = OrderStatus.RISK_APPROVED.value  # Stranded pre-broker submission

        pos_row = db_sess.scalar(
            select(PaperPositionModel).where(
                PaperPositionModel.status == PositionStatus.OPEN.value
            )
        )
        assert pos_row is not None
        pos_row.quantity = 999  # Deliberate corrupted drift to verify startup reconciliation heals it!
        db_sess.flush()

    # Boot fresh FastAPI container instance -> lifespan runs recover_and_reconcile_on_startup
    reset_engine()
    app2 = create_app()
    with TestClient(app2) as client2:
        # 1. Zero duplicate orders or fills created during restart recovery
        with unit_of_work() as db_sess:
            assert db_sess.scalar(select(func.count()).select_from(PaperOrderModel)) == total_orders_before
            assert db_sess.scalar(select(func.count()).select_from(PaperFillModel)) == total_fills_before

        # 2. Expired order -> EXPIRED; stranded pre-submission order -> FAILED; resting order -> ACKNOWLEDGED
        exp_after = client2.get(f"/api/v1/orders/{expiring_id}").json()
        assert exp_after["status"] == "EXPIRED"

        mid_after = client2.get(f"/api/v1/orders/{midflight_id}").json()
        assert mid_after["status"] == "FAILED"

        rest_after = client2.get(f"/api/v1/orders/{resting_id}").json()
        assert rest_after["status"] == "ACKNOWLEDGED"

        # 3. Corrupted position quantity (999) was automatically healed back to exact execution fill (75)!
        pos_after = client2.get("/api/v1/positions").json()
        assert pos_after["reconciliation"]["reconciled"] is True
        open_after = [p for p in pos_after["positions"] if p["status"] == "OPEN"]
        assert len(open_after) == 1
        assert open_after[0]["quantity"] == 75
