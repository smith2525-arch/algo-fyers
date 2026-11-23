"""Integration tests for Alembic migrations, PostgreSQL DDL, FastAPI endpoints,
immutability invariants, STOP PAPER TRADING semantics, and container restart persistence.
"""

from __future__ import annotations

import io
from pathlib import Path

import pytest
from alembic import command
from alembic.config import Config
from fastapi.testclient import TestClient
from sqlalchemy import select

from backend.main import create_app
from core.controls.service import ControlService
from core.exceptions import ImmutabilityViolationError, PaperTradingHaltedError
from core.persistence.db import reset_engine
from core.persistence.models import AuditEventModel, StrategyVersionModel
from core.persistence.uow import unit_of_work

PROJECT_ROOT = Path(__file__).resolve().parent.parent.parent


def test_postgresql_offline_migration_generates_tables_and_immutability_triggers() -> None:
    """Verify Alembic emits valid PostgreSQL DDL including PL/pgSQL immutability triggers."""
    buf = io.StringIO()
    alembic_cfg = Config(str(PROJECT_ROOT / "alembic.ini"))
    alembic_cfg.output_buffer = buf
    alembic_cfg.set_main_option("script_location", str(PROJECT_ROOT / "migrations"))
    alembic_cfg.set_main_option(
        "sqlalchemy.url", "postgresql+psycopg://user:pass@localhost:5432/algo_control_center"
    )
    command.upgrade(alembic_cfg, "head", sql=True)
    ddl = buf.getvalue()

    for table_name in (
        "users",
        "paper_accounts",
        "instruments",
        "strategy_definitions",
        "strategy_versions",
        "historical_datasets",
        "historical_candles",
        "backtest_runs",
        "backtest_trades",
        "replay_sessions",
        "signals",
        "strategy_instances",
        "paper_orders",
        "order_state_events",
        "paper_fills",
        "paper_positions",
        "position_events",
        "account_risk_configs",
        "strategy_risk_configs",
        "risk_decisions",
        "reconciliation_runs",
        "audit_events",
        "system_health_events",
    ):
        assert f"CREATE TABLE {table_name}" in ddl

    assert "trg_audit_events_immutable" in ddl
    assert "trg_strategy_versions_immutable" in ddl
    assert "trg_backtest_runs_immutable" in ddl
    assert "trg_backtest_trades_immutable" in ddl
    assert "trg_paper_fills_immutable" in ddl
    assert "trg_order_state_events_immutable" in ddl
    assert "trg_position_events_immutable" in ddl
    assert "trg_risk_decisions_immutable" in ddl
    assert "trg_reconciliation_runs_immutable" in ddl


def test_alembic_downgrade_and_upgrade_cycle(migrated_db_url: str) -> None:
    alembic_cfg = Config(str(PROJECT_ROOT / "alembic.ini"))
    alembic_cfg.set_main_option("script_location", str(PROJECT_ROOT / "migrations"))
    alembic_cfg.set_main_option("sqlalchemy.url", migrated_db_url)

    command.downgrade(alembic_cfg, "base")
    command.upgrade(alembic_cfg, "head")


def test_api_health_seeding_stop_resume_and_restart_survival(migrated_db_url: str) -> None:
    app1 = create_app()
    with TestClient(app1) as client:
        # 1. Liveness & Readiness Health checks
        live = client.get("/health/live")
        assert live.status_code == 200
        assert live.json()["execution_mode"] == "PAPER_ONLY"
        assert live.json()["timestamp_ist"].endswith("+05:30")
        assert live.headers["X-Execution-Mode"] == "PAPER_ONLY"
        assert live.headers["X-Correlation-ID"]

        ready = client.get("/health")
        assert ready.status_code == 200
        ready_body = ready.json()
        assert ready_body["database"]["status"] == "HEALTHY"

        # 2. Active probe persists system_health_events
        probe_resp = client.post("/api/v1/health/probe")
        assert probe_resp.status_code == 200
        assert len(probe_resp.json()["recent_events"]) >= 1

        # 3. Dashboard verifies ₹500,000 DEMO capital, 5 status strip items, zero fabricated trades
        dash = client.get("/api/v1/dashboard")
        assert dash.status_code == 200
        d_json = dash.json()
        assert float(d_json["kpis"]["paper_capital"]) == 500000.0
        assert d_json["kpis"]["is_demo_capital"] is True
        assert d_json["kpis"]["account_designation"] == "DEMO_PAPER_ONLY"
        assert d_json["kpis"]["open_positions"] == 0
        assert d_json["kpis"]["pending_orders"] == 0
        assert len(d_json["system_status_strip"]) == 5

        # 4. Seeded Configurable Instruments: NIFTY & BANKNIFTY
        inst_resp = client.get("/api/v1/instruments")
        assert inst_resp.status_code == 200
        symbols = {i["symbol"]: i for i in inst_resp.json()}
        assert {"NIFTY", "BANKNIFTY"} <= set(symbols.keys())

        # Update configurable lot size and verify persistence + audit
        upd_inst = client.put(
            "/api/v1/instruments/NIFTY",
            json={
                "lot_size": 65,
                "tick_size": "0.05",
                "session_open_ist": "09:15:00",
                "session_close_ist": "15:30:00",
                "is_active": True,
                "actor": "test.operator",
            },
        )
        assert upd_inst.status_code == 200
        assert upd_inst.json()["lot_size"] == 65

        # 5. Engage STOP PAPER TRADING
        stop_resp = client.post(
            "/api/v1/controls/paper-trading/stop",
            json={"actor": "desk.lead", "reason": "Volatility circuit test"},
            headers={"X-Correlation-ID": "cid-stop-test-001"},
        )
        assert stop_resp.status_code == 200
        assert stop_resp.json()["paper_trading_halted"] is True
        assert stop_resp.json()["status"] == "HALTED"

        # Verify gate blocks new orders & audit records auto_flatten_positions=False
        with unit_of_work() as session:
            acct = ControlService.get_primary_paper_account(session)
            with pytest.raises(PaperTradingHaltedError):
                ControlService.assert_new_orders_permitted(acct)

        audit_resp = client.get("/api/v1/audit", params={"correlation_id": "cid-stop-test-001"})
        assert audit_resp.status_code == 200
        events = audit_resp.json()
        assert len(events) == 1
        assert events[0]["action"] == "PAPER_TRADING_STOPPED"
        assert events[0]["details"]["auto_flatten_positions"] is False

    # 6. Simulate full container restart: dispose engine & boot fresh FastAPI instance
    reset_engine()
    app2 = create_app()
    with TestClient(app2) as client2:
        acct_after_restart = client2.get("/api/v1/accounts/primary")
        assert acct_after_restart.status_code == 200
        assert acct_after_restart.json()["paper_trading_halted"] is True

        health_after_restart = client2.get("/api/v1/health")
        assert health_after_restart.status_code == 200
        comp_map = {
            c["component"]: c["status"] for c in health_after_restart.json()["components"]
        }
        assert comp_map["DATABASE"] == "HEALTHY"
        assert comp_map["RISK_ENGINE"] == "BLOCKED"

        # Resume paper trading cleanly
        resume_resp = client2.post(
            "/api/v1/controls/paper-trading/resume",
            json={"actor": "desk.lead", "reason": "Desk check passed"},
        )
        assert resume_resp.status_code == 200
        assert resume_resp.json()["paper_trading_halted"] is False
        assert resume_resp.json()["status"] == "ACTIVE"


def test_strategy_version_and_audit_immutability_enforced(migrated_db_url: str) -> None:
    app = create_app()
    with TestClient(app) as client:
        # Create immutable v2 via API
        create_v2 = client.post(
            "/api/v1/strategies/EMA_RSI_CROSSOVER/versions",
            json={
                "parameters": {
                    "instrument": "NIFTY",
                    "timeframe": "5m",
                    "fast_ema_period": 8,
                    "slow_ema_period": 21,
                    "rsi_period": 14,
                    "long_rsi_threshold": 56.0,
                    "short_rsi_threshold": 44.0,
                    "stop_loss": 35.0,
                    "profit_target": 70.0,
                },
                "notes": "Immutable v2 test",
                "actor": "quant.researcher",
            },
        )
        assert create_v2.status_code == 201
        assert create_v2.json()["version_number"] == 2
        assert create_v2.json()["version_label"] == "NIFTY_EMA_RSI_v2"

    # Attempting to mutate parameters_json or delete StrategyVersion / AuditEvent must fail
    with pytest.raises(ImmutabilityViolationError):
        with unit_of_work() as session:
            ver = session.scalar(select(StrategyVersionModel).limit(1))
            assert ver is not None
            ver.parameters_json = {"tampered": True}
            session.flush()

    with pytest.raises(ImmutabilityViolationError):
        with unit_of_work() as session:
            ev = session.scalar(select(AuditEventModel).limit(1))
            assert ev is not None
            session.delete(ev)
            session.flush()
