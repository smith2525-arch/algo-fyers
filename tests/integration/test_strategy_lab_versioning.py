"""Integration tests for Phase 2A Strategy Lab: immutable versioning, cloning, diffing, and audits."""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select

from backend.main import create_app
from core.exceptions import ImmutabilityViolationError
from core.persistence.models import StrategyVersionModel
from core.persistence.uow import unit_of_work


def test_strategy_lab_versioning_cloning_comparison_and_audits(migrated_db_url: str) -> None:
    app = create_app()
    with TestClient(app) as client:
        # 1. Verify initial seeded baseline versions for NIFTY and BANKNIFTY
        cat_resp = client.get("/api/v1/strategies")
        assert cat_resp.status_code == 200
        defs = cat_resp.json()
        assert len(defs) == 1
        versions = defs[0]["versions"]
        labels = {v["version_label"]: v for v in versions}
        assert "NIFTY_EMA_RSI_v1" in labels
        assert "BANKNIFTY_EMA_RSI_v1" in labels

        nifty_v1 = labels["NIFTY_EMA_RSI_v1"]
        assert nifty_v1["created_at"].endswith("+05:30")

        # 2. Save a new NIFTY configuration -> creates NIFTY_EMA_RSI_v2
        new_params = dict(nifty_v1["parameters"])
        new_params["fast_ema_period"] = 8
        new_params["slow_ema_period"] = 26
        new_params["trailing_stop_enabled"] = True
        new_params["trailing_stop"] = "30.00"

        save_v2 = client.post(
            "/api/v1/strategies/EMA_RSI_CROSSOVER/versions",
            json={
                "parameters": new_params,
                "notes": "Desk configuration v2 with trailing stop",
                "actor": "quant.lead",
            },
            headers={"X-Correlation-ID": "cid-strat-v2"},
        )
        assert save_v2.status_code == 201
        v2_data = save_v2.json()
        assert v2_data["version_label"] == "NIFTY_EMA_RSI_v2"
        assert v2_data["version_number"] == 2
        assert v2_data["cloned_from_version_id"] is None

        # 3. Clone NIFTY_EMA_RSI_v2 with override -> creates NIFTY_EMA_RSI_v3 preserving v1 & v2
        clone_v3 = client.post(
            f"/api/v1/strategies/versions/{v2_data['id']}/clone",
            json={
                "parameter_overrides": {"max_trades_per_day": 6, "slippage_bps": 3.0},
                "notes": "Cloned v2 with higher trade cap",
                "actor": "desk.operator",
            },
            headers={"X-Correlation-ID": "cid-strat-v3"},
        )
        assert clone_v3.status_code == 201
        v3_data = clone_v3.json()
        assert v3_data["version_label"] == "NIFTY_EMA_RSI_v3"
        assert v3_data["cloned_from_version_id"] == v2_data["id"]
        assert v3_data["cloned_from_label"] == "NIFTY_EMA_RSI_v2"

        # Confirm NIFTY_EMA_RSI_v1 was NEVER modified
        v1_reload = client.get(f"/api/v1/strategies/versions/{nifty_v1['id']}")
        assert v1_reload.status_code == 200
        assert v1_reload.json()["parameter_hash"] == nifty_v1["parameter_hash"]
        assert v1_reload.json()["parameters"]["fast_ema_period"] == 9

        # 4. Compare NIFTY_EMA_RSI_v1 vs NIFTY_EMA_RSI_v3
        cmp_resp = client.get(
            "/api/v1/strategies/compare",
            params={"left_id": nifty_v1["id"], "right_id": v3_data["id"]},
        )
        assert cmp_resp.status_code == 200
        cmp_json = cmp_resp.json()
        assert cmp_json["identical_hash"] is False
        assert cmp_json["changed_count"] == 6
        changed_keys = {r["parameter"] for r in cmp_json["rows"] if r["changed"]}
        assert changed_keys == {
            "fast_ema_period",
            "slow_ema_period",
            "trailing_stop_enabled",
            "trailing_stop",
            "max_trades_per_day",
            "slippage_bps",
        }

        # 5. Reject invalid configuration via API (Fast EMA >= Slow EMA; Square-Off > Exchange Close)
        bad_params = dict(nifty_v1["parameters"])
        bad_params["fast_ema_period"] = 30
        bad_params["slow_ema_period"] = 20
        bad_resp = client.post(
            "/api/v1/strategies/EMA_RSI_CROSSOVER/versions",
            json={"parameters": bad_params, "actor": "desk.operator"},
        )
        assert bad_resp.status_code == 400
        assert bad_resp.json()["error"] == "STRATEGY_VALIDATION_ERROR"

        outside_exch = dict(nifty_v1["parameters"])
        outside_exch["mandatory_square_off_time"] = "15:45:00"
        exch_resp = client.post(
            "/api/v1/strategies/EMA_RSI_CROSSOVER/validate",
            json={"parameters": outside_exch},
        )
        assert exch_resp.status_code == 400

        # 6. Verify audit events persisted for both version creation and version clone
        aud_v2 = client.get("/api/v1/audit", params={"correlation_id": "cid-strat-v2"}).json()
        assert len(aud_v2) == 1
        assert aud_v2[0]["action"] == "STRATEGY_VERSION_CREATED"
        assert aud_v2[0]["entity_id"] == "NIFTY_EMA_RSI_v2"

        aud_v3 = client.get("/api/v1/audit", params={"correlation_id": "cid-strat-v3"}).json()
        assert len(aud_v3) == 1
        assert aud_v3[0]["action"] == "STRATEGY_VERSION_CLONED"
        assert aud_v3[0]["entity_id"] == "NIFTY_EMA_RSI_v3"

    # 7. Verify ORM immutability guard blocks mutating version_label or deleting any version
    with pytest.raises(ImmutabilityViolationError):
        with unit_of_work() as session:
            row = session.scalar(
                select(StrategyVersionModel).where(
                    StrategyVersionModel.version_label == "NIFTY_EMA_RSI_v1"
                )
            )
            assert row is not None
            row.version_label = "NIFTY_EMA_RSI_TAMPERED"
            session.flush()

    with pytest.raises(ImmutabilityViolationError):
        with unit_of_work() as session:
            row = session.scalar(
                select(StrategyVersionModel).where(
                    StrategyVersionModel.version_label == "NIFTY_EMA_RSI_v1"
                )
            )
            assert row is not None
            session.delete(row)
            session.flush()
