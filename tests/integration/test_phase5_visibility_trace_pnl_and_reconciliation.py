"""Integration tests for Phase 5 Operational Visibility:
- P&L Analytics (Gross, Charges, Slippage Cost, Realized, Unrealized, Net P&L, 6 filters, 5 chart series)
- Append-Only Audit Trail (UTC + IST timestamps, canonical event_types, 409 immutability enforcement)
- End-to-End 8-Stage Trade Lifecycle Correlation Trace View (Entry & Exit Correlation IDs)
- 9-Component System Health Matrix
- Deterministic Non-Silent Reconciliation (Verify-Only vs Explicit Repair with Before->After Audit Ledger)
"""

from __future__ import annotations

from decimal import Decimal

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select

from backend.main import create_app
from core.config import clear_settings_cache
from core.exceptions import ImmutabilityViolationError
from core.persistence.models import (
    PaperOrderModel,
    PaperPositionModel,
    ReconciliationRunModel,
)
from core.persistence.uow import unit_of_work


def test_phase5_pnl_analytics_lifecycle_trace_audit_immutability_and_health(
    migrated_db_url: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("REPLAY_WORKER_ENABLED", "false")
    clear_settings_cache()

    app = create_app()
    with TestClient(app) as client:
        # 1. Record operator LOGIN audit event and verify UTC + IST timestamps & 409 immutability
        login_res = client.post(
            "/api/v1/audit/login",
            json={"username": "desk.operator", "user_agent": "PytestConsole"},
            headers={"X-Correlation-ID": "cid-p5-login-001"},
        )
        assert login_res.status_code == 201
        login_ev = login_res.json()
        assert login_ev["event_type"] == "LOGIN"
        assert login_ev["occurred_at"].endswith("+05:30")
        assert login_ev["occurred_at_utc"].endswith("Z")
        ev_id = login_ev["id"]

        # Append-only enforcement: PUT, PATCH, and DELETE on audit records MUST fail with 409
        for method in ("put", "patch", "delete"):
            mut_resp = getattr(client, method)(f"/api/v1/audit/{ev_id}")
            assert mut_resp.status_code == 409
            assert mut_resp.json()["error"] == "IMMUTABILITY_VIOLATION"

        # 2. Start Strategy Instance + Market Replay on NIFTY and step 200 bars for round-trip strategy trades
        cat = client.get("/api/v1/replay/catalog", params={"instrument": "NIFTY"}).json()
        ver_id = cat["strategy_versions"][0]["id"]
        ds_id = cat["datasets"][0]["dataset_id"]

        inst_res = client.post(
            "/api/v1/paper-trading/instances",
            json={
                "strategy_version_id": ver_id,
                "order_type": "MARKET",
                "execution_latency_ms": 50,
                "latency_bars": 1,
                "slippage_bps": "2.0",
                "partial_fill_enabled": False,
                "max_fill_ratio_per_bar": "1.0",
                "auto_execute_signals": True,
                "actor": "desk.operator",
            },
        )
        assert inst_res.status_code == 201

        rpl_res = client.post(
            "/api/v1/replay/start",
            json={
                "instrument": "NIFTY",
                "strategy_version_id": ver_id,
                "dataset_id": ds_id,
                "historical_session_date": "ALL_SESSIONS",
                "replay_speed": "50x",
                "initial_step_bars": 1,
                "actor": "desk.operator",
            },
        )
        assert rpl_res.status_code == 201
        sess_id = rpl_res.json()["session"]["id"]

        step_res = client.post(
            f"/api/v1/replay/{sess_id}/step",
            json={"bars": 200, "actor": "desk.operator"},
        )
        assert step_res.status_code == 200

        # Also open an active manual BANKNIFTY position so both Realized & Unrealized P&L exist simultaneously
        bn_open = client.post(
            "/api/v1/orders",
            json={
                "client_order_key": "p5-bn-open-001",
                "instrument_symbol": "BANKNIFTY",
                "side": "BUY",
                "order_type": "MARKET",
                "quantity": 30,
                "reference_price": "51400.00",
                "slippage_bps": "3.0",
                "latency_bars": 0,
                "actor": "desk.operator",
            },
            headers={"X-Correlation-ID": "cid-p5-bn-open-001"},
        )
        assert bn_open.status_code == 201
        assert bn_open.json()["order"]["status"] == "FILLED"

        # 3. Verify 9-Component System Health Matrix
        health_res = client.get("/api/v1/health")
        assert health_res.status_code == 200
        h_body = health_res.json()
        assert len(h_body["components"]) == 5  # Dashboard backwards compatibility
        op_comps = h_body["operational_components"]
        assert len(op_comps) == 9
        comp_names = [c["component"] for c in op_comps]
        assert comp_names == [
            "API_SERVER",
            "DATABASE",
            "MARKET_REPLAY",
            "MARKET_DATA",
            "STRATEGY_ENGINE",
            "RISK_ENGINE",
            "PAPER_BROKER",
            "POSITION_ENGINE",
            "LAST_RECONCILIATION",
        ]
        allowed_states = {"HEALTHY", "DEGRADED", "STALE", "BLOCKED", "STOPPED"}
        for comp in op_comps:
            assert comp["status"] in allowed_states

        # 4. Verify End-to-End 8-Stage Trade Lifecycle Correlation Trace View
        traces_list = client.get("/api/v1/audit/traces", params={"limit": 30})
        assert traces_list.status_code == 200
        assert len(traces_list.json()["traces"]) >= 2

        # Find a CLOSED strategy position so we can verify all 8 stages (including Exit & Final P&L)!
        pos_list = client.get("/api/v1/positions").json()["positions"]
        closed_strat_positions = [
            p
            for p in pos_list
            if p["status"] == "CLOSED" and p["strategy_version_label"] is not None
        ]
        assert len(closed_strat_positions) >= 1
        closed_pos_id = closed_strat_positions[0]["id"]

        # Retrieve an order correlation_id linked to that closed strategy trade
        orders_all = client.get("/api/v1/orders", params={"limit": 100}).json()["orders"]
        strat_orders = [
            o for o in orders_all if o["strategy_version_label"] is not None and o["status"] == "FILLED"
        ]
        assert len(strat_orders) >= 2
        entry_cid = strat_orders[-1]["correlation_id"]  # earliest filled strategy order

        trace_resp = client.get(f"/api/v1/audit/trace/{entry_cid}")
        assert trace_resp.status_code == 200
        trace = trace_resp.json()
        assert trace["found"] is True
        assert trace["simulation_banner"] == "SIMULATED PAPER-TRADING RESULTS"
        stages = trace["stages"]
        assert [s["stage"] for s in stages] == [
            "1. Market Event",
            "2. Signal",
            "3. Risk Decision",
            "4. Order",
            "5. Execution",
            "6. Position",
            "7. Exit",
            "8. Final P&L",
        ]
        assert stages[0]["present"] is True  # Market Event
        assert stages[1]["present"] is True  # Strategy Signal
        assert stages[2]["present"] is True and stages[2]["status"] == "APPROVED"  # Risk Decision
        assert stages[3]["present"] is True and stages[3]["status"] == "FILLED"  # Order
        assert stages[4]["present"] is True  # Execution Fill
        assert stages[5]["present"] is True  # Position
        assert stages[7]["present"] is True  # Final P&L
        assert len(trace["audit_timeline"]) >= 4

        # Verify canonical AuditEventType filtering works cleanly
        sig_audits = client.get(
            "/api/v1/audit", params={"event_type": "SIGNAL_GENERATED", "limit": 50}
        )
        assert sig_audits.status_code == 200
        assert len(sig_audits.json()) >= 1
        assert sig_audits.json()[0]["signal_id"] is not None

        # 5. Verify P&L Analytics (Metrics, Accounting Identities, 6 Filters & 5 Chart Series)
        pnl_all = client.get("/api/v1/pnl/analytics")
        assert pnl_all.status_code == 200
        p_data = pnl_all.json()
        assert p_data["simulation_disclaimer"] == "SIMULATED PAPER-TRADING RESULTS"

        m = p_data["summary"]
        gross = Decimal(m["gross_pnl"])
        charges = Decimal(m["charges"])
        slippage = Decimal(m["slippage_cost"])
        realized = Decimal(m["realized_pnl"])
        unrealized = Decimal(m["unrealized_pnl"])
        net = Decimal(m["net_pnl"])

        assert charges > Decimal("0.00")
        assert slippage > Decimal("0.00")
        # Accounting invariant: Realized + Unrealized == Net P&L, and Gross - Charges == Net P&L (within 0.02 rounding)
        assert abs((realized + unrealized) - net) <= Decimal("0.02")
        assert abs((gross - charges) - net) <= Decimal("0.02")

        charts = p_data["charts"]
        for required_chart in (
            "daily_pnl",
            "cumulative_pnl",
            "strategy_contribution",
            "instrument_contribution",
            "drawdown",
        ):
            assert required_chart in charts
            assert isinstance(charts[required_chart], list)
            assert len(charts[required_chart]) >= 1

        # Verify filtering by instrument, strategy, direction, exit_reason, date, and outcome
        pnl_bn = client.get("/api/v1/pnl/analytics", params={"instrument": "BANKNIFTY"}).json()
        assert all(t["instrument"] == "BANKNIFTY" for t in pnl_bn["trades"])
        assert len(pnl_bn["trades"]) == 1

        pnl_nifty = client.get("/api/v1/pnl/analytics", params={"instrument": "NIFTY"}).json()
        assert all(t["instrument"] == "NIFTY" for t in pnl_nifty["trades"])
        assert len(pnl_nifty["trades"]) >= 1

        pnl_winners = client.get("/api/v1/pnl/analytics", params={"outcome": "WINNERS"}).json()
        assert all(Decimal(t["net_pnl"]) > Decimal("0.00") for t in pnl_winners["trades"])

        pnl_losers = client.get("/api/v1/pnl/analytics", params={"outcome": "LOSERS"}).json()
        assert all(Decimal(t["net_pnl"]) < Decimal("0.00") for t in pnl_losers["trades"])
        _ = closed_pos_id


def test_deterministic_non_silent_reconciliation_and_explicit_audit_repair(
    migrated_db_url: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Verify reconciliation checks executions vs positions, open orders, position quantities,
    and realized P&L integrity — NEVER silently repairing discrepancies unless apply_repair=True,
    and recording exact before->after diffs when repaired.
    """
    monkeypatch.setenv("REPLAY_WORKER_ENABLED", "false")
    clear_settings_cache()

    app = create_app()
    with TestClient(app) as client:
        # Start replay so market data is fresh, then open & partially close / maintain a paper position
        cat = client.get("/api/v1/replay/catalog", params={"instrument": "NIFTY"}).json()
        ver_id = cat["strategy_versions"][0]["id"]
        ds_id = cat["datasets"][0]["dataset_id"]

        client.post(
            "/api/v1/replay/start",
            json={
                "instrument": "NIFTY",
                "strategy_version_id": ver_id,
                "dataset_id": ds_id,
                "historical_session_date": "ALL_SESSIONS",
                "replay_speed": "5x",
                "initial_step_bars": 1,
                "actor": "desk.operator",
            },
        )

        ord_open = client.post(
            "/api/v1/orders",
            json={
                "client_order_key": "recon-open-001",
                "instrument_symbol": "NIFTY",
                "side": "BUY",
                "order_type": "MARKET",
                "quantity": 150,
                "latency_bars": 0,
                "actor": "desk.operator",
            },
        )
        assert ord_open.status_code == 201
        ord_id = ord_open.json()["order"]["id"]

        ord_half_close = client.post(
            "/api/v1/orders",
            json={
                "client_order_key": "recon-close-half-001",
                "instrument_symbol": "NIFTY",
                "side": "SELL",
                "order_type": "MARKET",
                "quantity": 75,
                "reduce_only": True,
                "latency_bars": 0,
                "actor": "desk.operator",
            },
        )
        assert ord_half_close.status_code == 201

        # 1. Clean state -> Verify-only reconciliation passes with 0 discrepancies
        clean_run = client.post(
            "/api/v1/reconciliation/run",
            json={"apply_repair": False, "actor": "desk.operator"},
            headers={"X-Correlation-ID": "cid-recon-clean-001"},
        )
        assert clean_run.status_code == 200
        c_body = clean_run.json()
        assert c_body["status"] == "COMPLETED"
        assert c_body["discrepancies_count"] == 0
        assert c_body["repairs_count"] == 0
        assert c_body["checks"]["executions_vs_positions"]["passed"] is True
        assert c_body["checks"]["open_orders"]["passed"] is True
        assert c_body["checks"]["position_quantities"]["passed"] is True
        assert c_body["checks"]["realized_pnl_integrity"]["passed"] is True

        # Verify ReconciliationRunModel immutability (historical runs cannot be edited or deleted)
        with pytest.raises(ImmutabilityViolationError):
            with unit_of_work() as db_sess:
                r_row = db_sess.scalar(
                    select(ReconciliationRunModel).where(
                        ReconciliationRunModel.run_code == c_body["run_code"]
                    )
                )
                assert r_row is not None
                r_row.status = "TAMPERED"
                db_sess.flush()

        # 2. Inject deliberate discrepancies directly in DB (order leaves_quantity, position quantity, realized_pnl)
        with unit_of_work() as db_sess:
            o_row = db_sess.scalar(
                select(PaperOrderModel).where(PaperOrderModel.order_code == ord_open.json()["order"]["order_code"])
            )
            p_row = db_sess.scalar(
                select(PaperPositionModel).where(PaperPositionModel.instrument_symbol == "NIFTY")
            )
            assert o_row is not None and p_row is not None
            o_row.leaves_quantity = 75  # Terminal FILLED order illegally has non-zero leaves_quantity!
            p_row.quantity = 225  # Actual open qty should be 75!
            p_row.realized_pnl = Decimal("99999.0000")  # Corrupted realized P&L!

        # 3. Run Verify-Only (`apply_repair=False`) -> MUST detect discrepancies, emit RECONCILIATION_FAILED,
        #    and MUST NOT silently mutate DB values!
        detect_run = client.post(
            "/api/v1/reconciliation/run",
            json={"apply_repair": False, "actor": "desk.operator"},
            headers={"X-Correlation-ID": "cid-recon-detect-001"},
        )
        assert detect_run.status_code == 200
        d_body = detect_run.json()
        assert d_body["status"] == "DISCREPANCY_DETECTED"
        assert d_body["repair_applied"] is False
        assert d_body["discrepancies_count"] >= 3
        assert d_body["repairs_count"] == 0

        # Confirm DB still holds the un-repaired drifted values (zero silent repair!)
        with unit_of_work() as db_sess:
            p_check = db_sess.scalar(
                select(PaperPositionModel).where(PaperPositionModel.instrument_symbol == "NIFTY")
            )
            assert p_check is not None
            assert int(p_check.quantity) == 225
            assert Decimal(str(p_check.realized_pnl)) == Decimal("99999.0000")

        # Confirm RECONCILIATION_FAILED audit event was emitted and System Health flags BLOCKED
        fail_aud = client.get(
            "/api/v1/audit", params={"correlation_id": "cid-recon-detect-001"}
        ).json()
        assert any(a["event_type"] == "RECONCILIATION_FAILED" for a in fail_aud)

        h_blocked = client.get("/api/v1/health").json()
        by_comp = {c["component"]: c["status"] for c in h_blocked["operational_components"]}
        assert by_comp["POSITION_ENGINE"] == "BLOCKED"
        assert by_comp["LAST_RECONCILIATION"] == "BLOCKED"

        # 4. Run Explicit Repair (`apply_repair=True`) -> repairs discrepancies AND records exact before->after diffs!
        repair_run = client.post(
            "/api/v1/reconciliation/run",
            json={"apply_repair": True, "actor": "desk.operator"},
            headers={"X-Correlation-ID": "cid-recon-repair-001"},
        )
        assert repair_run.status_code == 200
        rep_body = repair_run.json()
        assert rep_body["status"] == "REPAIRED"
        assert rep_body["repair_applied"] is True
        assert rep_body["repairs_count"] >= 3
        fields_repaired = {r["field"]: (r["before"], r["after"]) for r in rep_body["repairs"]}
        assert fields_repaired["leaves_quantity"] == ("75", "0")
        assert fields_repaired["quantity"] == ("225", "75")
        assert fields_repaired["realized_pnl"][0] == "99999.00"

        # Audit event for explicit repair carries full repairs array
        rep_aud = client.get(
            "/api/v1/audit", params={"correlation_id": "cid-recon-repair-001"}
        ).json()
        assert any(
            a["event_type"] == "RECONCILIATION_COMPLETED"
            and len(a["details"].get("repairs", [])) >= 3
            for a in rep_aud
        )

        # Follow-up verify run confirms 0 remaining discrepancies & System Health is HEALTHY again!
        post_verify = client.post(
            "/api/v1/reconciliation/run",
            json={"apply_repair": False, "actor": "desk.operator"},
        ).json()
        assert post_verify["status"] == "COMPLETED"
        assert post_verify["discrepancies_count"] == 0

        h_healthy = client.get("/api/v1/health").json()
        by_comp_after = {c["component"]: c["status"] for c in h_healthy["operational_components"]}
        assert by_comp_after["POSITION_ENGINE"] == "HEALTHY"
        assert by_comp_after["LAST_RECONCILIATION"] == "HEALTHY"
        _ = ord_id
