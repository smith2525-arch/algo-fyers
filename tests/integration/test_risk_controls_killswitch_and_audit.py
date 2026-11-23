"""Integration tests for Phase 4 Risk Center API, Global Kill Switch,
Restart Survival with Kill Switch Active, Explicit-Confirmation Flatten,
Duplicate Signal/Order Risk Decisions, and Persisted Risk Decision Audit Trail.
"""

from __future__ import annotations

from decimal import Decimal

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select

from backend.main import create_app
from core.clock import now_ist
from core.config import clear_settings_cache
from core.domain.enums import Side, SignalDirection
from core.paper_trading.service import PaperTradingService
from core.persistence.db import reset_engine
from core.persistence.models import StrategyVersionModel
from core.persistence.uow import unit_of_work
from core.strategies.base import SignalCandidate


def test_kill_switch_restart_survival_confirmed_flatten_and_risk_decision_audit(
    migrated_db_url: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("REPLAY_WORKER_ENABLED", "false")
    clear_settings_cache()

    app1 = create_app()
    with TestClient(app1) as client1:
        # 1. Start replay so market data is HEALTHY (< 10s stale threshold)
        cat = client1.get("/api/v1/replay/catalog", params={"instrument": "NIFTY"}).json()
        ver_id = cat["strategy_versions"][0]["id"]
        ds_id = cat["datasets"][0]["dataset_id"]

        rpl = client1.post(
            "/api/v1/replay/start",
            json={
                "instrument": "NIFTY",
                "strategy_version_id": ver_id,
                "dataset_id": ds_id,
                "historical_session_date": "ALL_SESSIONS",
                "replay_speed": "5x",
                "initial_step_bars": 1,
                "actor": "risk.officer",
            },
        )
        assert rpl.status_code == 201

        # 2. Verify Risk Center Overview returns DEMO defaults & disclaimer
        ov = client1.get("/api/v1/risk/overview").json()
        assert "DEMONSTRATION" in ov["settings_disclaimer"]
        assert ov["account_risk_config"]["max_daily_loss_inr"] == "10000.00"
        assert ov["account_risk_config"]["default_max_risk_per_trade_inr"] == "2500.00"
        assert ov["account_risk_config"]["default_max_trades_per_strategy_day"] == 5
        assert ov["account_risk_config"]["max_consecutive_losses"] == 3
        assert ov["account_risk_config"]["market_data_stale_threshold_seconds"] == 10
        assert ov["account_risk_config"]["max_signal_age_seconds"] == 15

        # 3. Open 1 NIFTY paper position via simulated immediate MARKET order (generates APPROVED RiskDecision)
        open_ord = client1.post(
            "/api/v1/orders",
            json={
                "client_order_key": "p4-open-pos-001",
                "instrument_symbol": "NIFTY",
                "side": "BUY",
                "order_type": "MARKET",
                "quantity": 75,
                "latency_bars": 0,
                "actor": "desk.operator",
            },
            headers={"X-Correlation-ID": "cid-p4-approved-001"},
        )
        assert open_ord.status_code == 201
        assert open_ord.json()["order"]["status"] == "FILLED"

        # 4. Duplicate Order Request generates persisted REJECTED RiskDecision (DUPLICATE_ORDER_CHECK)
        dup_ord = client1.post(
            "/api/v1/orders",
            json={
                "client_order_key": "p4-open-pos-001",
                "instrument_symbol": "NIFTY",
                "side": "BUY",
                "order_type": "MARKET",
                "quantity": 75,
                "latency_bars": 0,
                "actor": "desk.operator",
            },
        )
        assert dup_ord.status_code == 200
        assert dup_ord.json()["duplicate"] is True

        # 5. Duplicate Signal Routing generates persisted REJECTED RiskDecision (DUPLICATE_SIGNAL_CHECK)
        with unit_of_work() as db_sess:
            sv = db_sess.scalar(
                select(StrategyVersionModel).where(StrategyVersionModel.instrument_symbol == "NIFTY")
            )
            assert sv is not None
            sig_row, _ = PaperTradingService.persist_signal_idempotent(
                db_sess,
                candidate=SignalCandidate(
                    strategy_code="EMA_RSI_CROSSOVER",
                    symbol="NIFTY",
                    timeframe="5m",
                    bar_timestamp=now_ist().replace(hour=10, minute=30, second=0, microsecond=0),
                    side=Side.BUY,
                    direction=SignalDirection.ENTRY_LONG,
                    reference_price=Decimal("24200.00"),
                    rationale="Phase 4 duplicate signal routing check",
                ),
                strategy_version_id=sv.id,
                strategy_version_label=sv.version_label,
                market_event_id="evt-p4-sig-dup",
            )
            sig_id = str(sig_row.id)

        r1 = client1.post(f"/api/v1/paper-trading/signals/{sig_id}/route", json={"actor": "desk.operator"})
        assert r1.status_code == 200
        assert r1.json()["duplicate"] is False

        r2 = client1.post(f"/api/v1/paper-trading/signals/{sig_id}/route", json={"actor": "desk.operator"})
        assert r2.status_code == 200
        assert r2.json()["duplicate"] is True

        # 6. Activate Global Kill Switch -> blocks NEW orders, creates audit event, does NOT close open position!
        ks_on = client1.post(
            "/api/v1/risk/kill-switch/activate",
            json={"actor": "risk.officer", "reason": "Intraday volatility spike drill"},
            headers={"X-Correlation-ID": "cid-p4-killswitch-on"},
        )
        assert ks_on.status_code == 200
        assert ks_on.json()["global_kill_switch_active"] is True

        # Open position MUST still be OPEN (kill switch never silently flattens positions!)
        pos_while_ks = client1.get("/api/v1/positions").json()["positions"]
        assert len([p for p in pos_while_ks if p["status"] == "OPEN"]) == 1

        # New order while Kill Switch active -> REJECTED with Rule=GLOBAL_KILL_SWITCH
        blocked_ks = client1.post(
            "/api/v1/orders",
            json={
                "client_order_key": "p4-blocked-by-ks-001",
                "instrument_symbol": "BANKNIFTY",
                "side": "BUY",
                "order_type": "MARKET",
                "quantity": 30,
                "reference_price": "51500.00",
                "actor": "desk.operator",
            },
        )
        assert blocked_ks.status_code == 201
        assert blocked_ks.json()["order"]["status"] == "RISK_REJECTED"

        # Audit trail contains GLOBAL_KILL_SWITCH_ACTIVATED with auto_flatten_positions=False
        ks_audits = client1.get(
            "/api/v1/audit", params={"correlation_id": "cid-p4-killswitch-on"}
        ).json()
        actions = {a["action"]: a for a in ks_audits}
        assert "GLOBAL_KILL_SWITCH_ACTIVATED" in actions
        assert actions["GLOBAL_KILL_SWITCH_ACTIVATED"]["details"]["auto_flatten_positions"] is False

    # 7. Simulate Cloud Run Container Restart with Kill Switch Active!
    reset_engine()
    app2 = create_app()
    with TestClient(app2) as client2:
        ov_after = client2.get("/api/v1/risk/overview").json()
        assert ov_after["account_risk_config"]["global_kill_switch_active"] is True
        assert ov_after["account"]["paper_trading_halted"] is True

        # Attempting a new order AFTER restart is STILL blocked by GLOBAL_KILL_SWITCH!
        blocked_after_restart = client2.post(
            "/api/v1/orders",
            json={
                "client_order_key": "p4-blocked-after-restart-001",
                "instrument_symbol": "BANKNIFTY",
                "side": "BUY",
                "order_type": "MARKET",
                "quantity": 30,
                "reference_price": "51500.00",
                "actor": "desk.operator",
            },
        )
        assert blocked_after_restart.status_code == 201
        assert blocked_after_restart.json()["order"]["status"] == "RISK_REJECTED"

        # 8. Separate FLATTEN PAPER POSITIONS requires explicit confirmation (`confirm_flatten=True`)
        unconfirmed_flat = client2.post(
            "/api/v1/risk/positions/flatten",
            json={"confirm_flatten": False, "actor": "risk.officer"},
        )
        assert unconfirmed_flat.status_code == 400
        assert unconfirmed_flat.json()["error"] == "FLATTEN_CONFIRMATION_REQUIRED"

        confirmed_flat = client2.post(
            "/api/v1/risk/positions/flatten",
            json={"confirm_flatten": True, "actor": "risk.officer"},
        )
        assert confirmed_flat.status_code == 200
        assert confirmed_flat.json()["positions_flattened"] == 1
        assert [
            p
            for p in client2.get("/api/v1/positions").json()["positions"]
            if p["status"] == "OPEN"
        ] == []

        # 9. Verify persisted Risk Decisions ledger & Risk Decision Audit records
        decs = client2.get("/api/v1/risk/decisions").json()["decisions"]
        rules_recorded = {d["rule"] for d in decs}
        assert "MAX_DAILY_LOSS" in rules_recorded  # Approved order primary rule
        assert "DUPLICATE_ORDER_CHECK" in rules_recorded
        assert "DUPLICATE_SIGNAL_CHECK" in rules_recorded
        assert "GLOBAL_KILL_SWITCH" in rules_recorded

        # Inspect structure of an APPROVED and a REJECTED decision
        approved_list = [d for d in decs if d["decision"] == "APPROVED" and d["correlation_id"] == "cid-p4-approved-001"]
        assert len(approved_list) == 1
        assert approved_list[0]["limit_value"] == "₹10,000.00"
        assert approved_list[0]["timestamp"].endswith("+05:30")

        # Deactivate Kill Switch & update configurable paper-risk settings via API
        upd_cfg = client2.put(
            "/api/v1/risk/account-config",
            json={"max_daily_loss_inr": "12000.00", "max_open_positions": 5, "actor": "risk.officer"},
        )
        assert upd_cfg.status_code == 200
        assert upd_cfg.json()["max_daily_loss_inr"] == "12000.00"

        ks_off = client2.post(
            "/api/v1/risk/kill-switch/deactivate",
            json={"actor": "risk.officer", "reason": "Drill complete"},
        )
        assert ks_off.status_code == 200
        assert ks_off.json()["global_kill_switch_active"] is False
