"""Integration tests for Phase 2B Backtesting API, CSV Import, Reproducibility, and Immutability."""

from __future__ import annotations

from decimal import Decimal

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select

from backend.main import create_app
from core.exceptions import ImmutabilityViolationError
from core.persistence.models import BacktestRunModel, BacktestTradeModel
from core.persistence.uow import unit_of_work


def test_bundled_datasets_deterministic_reproducibility_and_pnl_reconciliation(
    migrated_db_url: str,
) -> None:
    app = create_app()
    with TestClient(app) as client:
        # 1. Discover bundled NIFTY & BANKNIFTY historical sample datasets
        ds_resp = client.get("/api/v1/backtests/datasets")
        assert ds_resp.status_code == 200
        datasets = {d["dataset_id"]: d for d in ds_resp.json()}
        assert "BUNDLED_NIFTY_5M_SAMPLE" in datasets
        assert "BUNDLED_BANKNIFTY_5M_SAMPLE" in datasets
        assert datasets["BUNDLED_NIFTY_5M_SAMPLE"]["row_count"] == 450
        assert len(datasets["BUNDLED_NIFTY_5M_SAMPLE"]["data_version_hash"]) == 64

        # 2. Fetch seeded immutable NIFTY_EMA_RSI_v1 and BANKNIFTY_EMA_RSI_v1
        strat_resp = client.get("/api/v1/strategies")
        assert strat_resp.status_code == 200
        versions = {
            v["version_label"]: v
            for s in strat_resp.json()
            for v in s["versions"]
        }
        nifty_v1 = versions["NIFTY_EMA_RSI_v1"]
        bnf_v1 = versions["BANKNIFTY_EMA_RSI_v1"]

        # 3. Instrument mismatch guard: NIFTY strategy cannot run on BANKNIFTY dataset
        mismatch_resp = client.post(
            "/api/v1/backtests",
            json={
                "strategy_version_id": nifty_v1["id"],
                "dataset_id": "BUNDLED_BANKNIFTY_5M_SAMPLE",
                "initial_capital": "500000.00",
            },
        )
        assert mismatch_resp.status_code == 400
        assert mismatch_resp.json()["error"] == "BACKTEST_INSTRUMENT_MISMATCH"

        # 4. Run 1: Deterministic backtest on NIFTY_EMA_RSI_v1
        run_payload = {
            "strategy_version_id": nifty_v1["id"],
            "dataset_id": "BUNDLED_NIFTY_5M_SAMPLE",
            "initial_capital": "500000.00",
            "slippage_bps": "2.0",
            "estimated_charges_bps": "3.5",
            "per_order_fee_inr": "20.0",
            "position_sizing_mode": "FIXED_LOTS",
            "position_size_value": "1",
            "execution_delay_bars": 1,
            "actor": "qa.engineer",
        }
        r1 = client.post("/api/v1/backtests", json=run_payload)
        assert r1.status_code == 201
        d1 = r1.json()

        assert d1["simulation_disclaimer"] == "SIMULATED BACKTEST RESULTS"
        assert d1["status"] == "COMPLETED"
        assert d1["engine_version"] == "backtest-engine-1.0.0"
        assert d1["started_at"].endswith("+05:30")
        assert d1["completed_at"].endswith("+05:30")
        assert len(d1["trades"]) >= 2
        assert len(d1["equity_curve"]) == 450
        assert len(d1["drawdown_curve"]) == 450
        assert len(d1["monthly_returns"]) == 2  # Spans 2026-10 and 2026-11
        assert len(d1["chart_bars"]) == 450

        # Reconcile every trade's Net P&L == Gross P&L - Charges, and Sum(Net P&L) == Metrics Net P&L
        sum_net = Decimal("0")
        for tr in d1["trades"]:
            assert tr["entry_time"].endswith("+05:30")
            assert tr["exit_time"].endswith("+05:30")
            assert tr["entry_time"] > tr["entry_signal_time"]
            gross = Decimal(tr["gross_pnl"])
            chg = Decimal(tr["charges"])
            net = Decimal(tr["net_pnl"])
            assert abs((gross - chg) - net) <= Decimal("0.01")
            sum_net += net

        assert abs(sum_net - Decimal(d1["metrics"]["net_pnl"])) <= Decimal("0.02")
        assert (
            Decimal("500000.00") + Decimal(d1["metrics"]["net_pnl"])
            == Decimal(d1["metrics"]["ending_equity"])
        )

        # 5. Run 2: Identical inputs MUST reproduce identical fingerprint, metrics, and trade IDs
        r2 = client.post("/api/v1/backtests", json=run_payload)
        assert r2.status_code == 201
        d2 = r2.json()

        assert d1["id"] != d2["id"]  # Distinct persisted run artifacts
        assert d1["reproducibility_fingerprint"] == d2["reproducibility_fingerprint"]
        assert d1["data_version_hash"] == d2["data_version_hash"]
        assert d1["metrics"] == d2["metrics"]
        assert d1["trades"] == d2["trades"]  # Exact deterministic Trade IDs, prices, and P&L!

        # 6. Run BANKNIFTY backtest as well
        r_bnf = client.post(
            "/api/v1/backtests",
            json={
                "strategy_version_id": bnf_v1["id"],
                "dataset_id": "BUNDLED_BANKNIFTY_5M_SAMPLE",
                "initial_capital": "500000.00",
            },
        )
        assert r_bnf.status_code == 201
        assert r_bnf.json()["status"] == "COMPLETED"

        # 7. Verify Audit Trail captured BACKTEST_EXECUTED
        audit_resp = client.get("/api/v1/audit", params={"category": "BACKTEST"})
        assert audit_resp.status_code == 200
        actions = [e["action"] for e in audit_resp.json()]
        assert "BACKTEST_EXECUTED" in actions

    # 8. Verify ORM Immutability Guard prevents mutating or deleting persisted BacktestRun / Trade
    with pytest.raises(ImmutabilityViolationError):
        with unit_of_work() as session:
            persisted_run = session.scalar(select(BacktestRunModel))
            assert persisted_run is not None
            persisted_run.status = "TAMPERED"
            session.flush()

    with pytest.raises(ImmutabilityViolationError):
        with unit_of_work() as session:
            persisted_trade = session.scalar(select(BacktestTradeModel))
            assert persisted_trade is not None
            session.delete(persisted_trade)
            session.flush()


def test_csv_import_and_failed_validation_backtest_workflow(migrated_db_url: str) -> None:
    app = create_app()
    with TestClient(app) as client:
        strat_resp = client.get("/api/v1/strategies")
        nifty_v1_id = next(
            v["id"]
            for s in strat_resp.json()
            for v in s["versions"]
            if v["version_label"] == "NIFTY_EMA_RSI_v1"
        )

        # Import a CSV with a duplicate timestamp to verify FAILED_VALIDATION persistence
        dirty_csv = "\n".join(
            [
                "timestamp,open,high,low,close,volume",
                "2026-11-05T09:15:00+05:30,24200.00,24210.00,24195.00,24205.00,12000",
                "2026-11-05T09:15:00+05:30,24205.00,24220.00,24200.00,24215.00,15000",
            ]
        )
        imp = client.post(
            "/api/v1/backtests/datasets/import-csv",
            json={
                "name": "Dirty Duplicate Timestamp Sample",
                "instrument": "NIFTY",
                "timeframe": "5m",
                "csv_text": dirty_csv,
                "actor": "qa.engineer",
            },
        )
        assert imp.status_code == 201
        imp_body = imp.json()
        ds_id = imp_body["dataset"]["dataset_id"]
        assert imp_body["data_quality_report"]["passed_validation"] is False
        assert imp_body["data_quality_report"]["duplicate_timestamp_count"] == 1

        # Running a backtest against the dirty dataset produces persisted FAILED_VALIDATION run
        bt_fail = client.post(
            "/api/v1/backtests",
            json={
                "strategy_version_id": nifty_v1_id,
                "dataset_id": ds_id,
                "initial_capital": "500000.00",
            },
        )
        assert bt_fail.status_code == 201
        fail_json = bt_fail.json()
        assert fail_json["status"] == "FAILED_VALIDATION"
        assert fail_json["trades"] == []
        assert fail_json["metrics"]["win_rate_pct"] is None
        assert fail_json["metrics"]["profit_factor"] is None
        assert fail_json["data_quality_report"]["duplicate_timestamp_count"] == 1
