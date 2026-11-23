"""Final Quality Gate verification suite (`tests/integration/test_final_quality_gate_and_demo.py`).

Executes end-to-end verification for portfolio/consulting readiness:
1. Complete 17-Step Guided Consulting Demo Showcase (successful trade, profitable trade,
   loss-making trade, risk rejection, stale-data blocking, data recovery, kill switch,
   reconciliation, and 8-stage Correlation ID Audit Trace)
2. Reproducible backtests across independent invocations
3. Expired signal blocking (`MAX_SIGNAL_AGE`) with exact reason & values
4. Workspace secret hygiene & Dockerfile / Cloud Run contract validation
5. Standalone Cloud Run (`PORT=8080` contract) startup, readiness & database recovery probe
"""

from __future__ import annotations

import os
import re
import subprocess
import sys
import time
from datetime import timedelta
from decimal import Decimal
from pathlib import Path

import httpx
import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select

from backend.main import create_app
from core.clock import now_ist
from core.config import clear_settings_cache
from core.domain.enums import Side, SignalDirection
from core.paper_trading.service import PaperTradingService
from core.persistence.models import StrategyVersionModel
from core.persistence.uow import unit_of_work
from core.strategies.base import SignalCandidate

PROJECT_ROOT = Path(__file__).resolve().parent.parent.parent


def test_guided_demo_showcase_reproducibility_and_all_17_steps(
    migrated_db_url: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Verify the deterministic consulting showcase populates all 17 steps honestly via real engines."""
    monkeypatch.setenv("REPLAY_WORKER_ENABLED", "false")
    clear_settings_cache()

    app = create_app()
    with TestClient(app) as client:
        # 1. Initial status before showcase
        init_status = client.get("/api/v1/demo/status")
        assert init_status.status_code == 200
        assert init_status.json()["total_steps"] == 17

        # 2. Execute full deterministic showcase
        run_res = client.post(
            "/api/v1/demo/run-showcase",
            json={"actor": "consulting.presenter"},
        )
        assert run_res.status_code == 200
        payload = run_res.json()
        assert payload["showcase_executed"] is True

        st_after = payload["status"]
        assert st_after["completed_steps"] == 17
        assert all(step["completed"] is True for step in st_after["steps"])

        art = st_after["showcase_artifacts"]
        assert art["has_profitable_trade"] is True
        assert art["has_loss_making_trade"] is True
        assert art["has_risk_rejection"] is True
        assert art["has_stale_data_block"] is True
        assert art["has_kill_switch_block"] is True
        assert art["has_data_recovery"] is True
        assert art["has_clean_reconciliation"] is True

        corrs = art["correlations"]
        assert corrs["profitable_trade_cid"]
        assert corrs["loss_making_trade_cid"]
        assert corrs["risk_rejection_cid"]
        assert corrs["stale_data_rejection_cid"]
        assert corrs["kill_switch_rejection_cid"]
        assert corrs["recommended_trace_cid"]

        # 3. Verify 8-stage Audit Trace on both profitable and risk-rejected correlations
        win_trace = client.get(
            f"/api/v1/audit/trace/{corrs['profitable_trade_cid']}"
        ).json()
        assert win_trace["found"] is True
        assert len(win_trace["stages"]) == 8
        assert win_trace["stages"][0]["present"] is True  # Market Event
        assert win_trace["stages"][2]["status"] == "APPROVED"  # Risk Decision
        assert win_trace["stages"][4]["present"] is True  # Execution Fill
        assert win_trace["stages"][5]["present"] is True  # Position
        assert win_trace["stages"][7]["present"] is True  # Final P&L

        rej_trace = client.get(
            f"/api/v1/audit/trace/{corrs['risk_rejection_cid']}"
        ).json()
        assert rej_trace["found"] is True
        assert rej_trace["stages"][2]["status"] == "REJECTED"
        assert rej_trace["stages"][2]["details"]["primary_rule"]
        assert rej_trace["stages"][2]["details"]["current_value"]
        assert rej_trace["stages"][2]["details"]["limit_value"]
        assert rej_trace["stages"][2]["details"]["reason"]

        drill_rej_cid = payload["drills"]["risk_rejection"]["correlation_id"]
        drill_rej_trace = client.get(f"/api/v1/audit/trace/{drill_rej_cid}").json()
        assert drill_rej_trace["found"] is True
        assert drill_rej_trace["stages"][2]["status"] == "REJECTED"
        assert drill_rej_trace["stages"][2]["details"]["primary_rule"] in (
            "AVAILABLE_DEMO_MARGIN",
            "STRATEGY_MAX_QUANTITY",
            "MAXIMUM_ORDER_VALUE",
        )

        # 4. Verify Backtest Reproducibility (identical inputs -> identical fingerprint & net P&L)
        bt_runs = client.get("/api/v1/backtests").json()
        assert len(bt_runs) >= 1
        first_bt = client.get(f"/api/v1/backtests/{bt_runs[0]['id']}").json()
        cat = client.get("/api/v1/replay/catalog", params={"instrument": "NIFTY"}).json()
        second_bt_resp = client.post(
            "/api/v1/backtests",
            json={
                "strategy_version_id": cat["strategy_versions"][0]["id"],
                "dataset_id": cat["datasets"][0]["dataset_id"],
                "initial_capital": "500000.00",
                "execution_delay_bars": 1,
                "actor": "qa.gate",
            },
        )
        assert second_bt_resp.status_code == 201
        second_bt = second_bt_resp.json()
        assert (
            first_bt["reproducibility_fingerprint"]
            == second_bt["reproducibility_fingerprint"]
        )
        assert first_bt["metrics"]["net_pnl"] == second_bt["metrics"]["net_pnl"]
        assert first_bt["metrics"]["total_trades"] == second_bt["metrics"]["total_trades"]

        # 5. Verify P&L Analytics reconciles with 0 discrepancies
        pnl = client.get("/api/v1/pnl/analytics").json()["summary"]
        assert abs(
            (Decimal(pnl["realized_pnl"]) + Decimal(pnl["unrealized_pnl"]))
            - Decimal(pnl["net_pnl"])
        ) <= Decimal("0.02")
        assert abs(
            (Decimal(pnl["gross_pnl"]) - Decimal(pnl["charges"]))
            - Decimal(pnl["net_pnl"])
        ) <= Decimal("0.02")


def test_expired_signal_blocks_execution_with_exact_max_signal_age_reason(
    migrated_db_url: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Verify an expired signal (> max_signal_age_seconds) is deterministically rejected by RiskEngine."""
    monkeypatch.setenv("REPLAY_WORKER_ENABLED", "false")
    clear_settings_cache()

    app = create_app()
    with TestClient(app) as client:
        # Ensure fresh market data so the gate specifically tests MAX_SIGNAL_AGE (not stale market data)
        cat = client.get("/api/v1/replay/catalog", params={"instrument": "NIFTY"}).json()
        ver_id = cat["strategy_versions"][0]["id"]
        ds_id = cat["datasets"][0]["dataset_id"]

        rpl = client.post(
            "/api/v1/replay/start",
            json={
                "instrument": "NIFTY",
                "strategy_version_id": ver_id,
                "dataset_id": ds_id,
                "historical_session_date": "ALL_SESSIONS",
                "replay_speed": "5x",
                "initial_step_bars": 2,
                "actor": "qa.gate",
            },
        )
        assert rpl.status_code == 201

        # Persist a signal and age its emitted_at timestamp beyond the 15s limit (e.g. 60s old)
        with unit_of_work() as db_sess:
            sv = db_sess.scalar(
                select(StrategyVersionModel).where(
                    StrategyVersionModel.instrument_symbol == "NIFTY"
                )
            )
            assert sv is not None
            sig_row, _ = PaperTradingService.persist_signal_idempotent(
                db_sess,
                candidate=SignalCandidate(
                    strategy_code="EMA_RSI_CROSSOVER",
                    symbol="NIFTY",
                    timeframe="5m",
                    bar_timestamp=now_ist().replace(
                        hour=10, minute=25, second=0, microsecond=0
                    ),
                    side=Side.BUY,
                    direction=SignalDirection.ENTRY_LONG,
                    reference_price=Decimal("24210.00"),
                    rationale="Expired signal gate verification",
                ),
                strategy_version_id=sv.id,
                strategy_version_label=sv.version_label,
                market_event_id="evt-expired-sig-gate-001",
            )
            sig_row.emitted_at = now_ist() - timedelta(seconds=60)
            db_sess.flush()
            sig_id = str(sig_row.id)

        route_res = client.post(
            f"/api/v1/paper-trading/signals/{sig_id}/route",
            json={"actor": "qa.gate"},
            headers={"X-Correlation-ID": "cid-expired-sig-001"},
        )
        assert route_res.status_code == 200
        routed_ord = route_res.json()["order"]
        assert routed_ord["status"] == "RISK_REJECTED"
        assert "SIGNAL_EXPIRED" in (routed_ord["rejection_reason"] or "")

        decisions = client.get(
            "/api/v1/risk/decisions", params={"decision": "REJECTED", "limit": 20}
        ).json()["decisions"]
        matching_dec = [
            d for d in decisions if d["correlation_id"] == "cid-expired-sig-001"
        ]
        assert len(matching_dec) == 1
        assert matching_dec[0]["primary_rule"] == "MAXIMUM_SIGNAL_AGE"
        assert "15s" in matching_dec[0]["limit_value"]


def test_no_secrets_committed_and_dockerfile_readme_contracts() -> None:
    """Scan workspace to verify zero committed secrets, valid Dockerfiles, and README compliance."""
    # 1. No .env file committed in workspace root; .dockerignore & .gitignore exclude .env while keeping .env.example
    assert not (PROJECT_ROOT / ".env").exists(), ".env must not be committed to workspace"
    dockerignore_txt = (PROJECT_ROOT / ".dockerignore").read_text(encoding="utf-8")
    gitignore_txt = (PROJECT_ROOT / ".gitignore").read_text(encoding="utf-8")
    assert ".env" in dockerignore_txt and "!.env.example" in dockerignore_txt
    assert ".env" in gitignore_txt and ".env.*" in gitignore_txt and "!.env.example" in gitignore_txt

    # 2. Scan source, config, migration, and documentation files for secret patterns
    secret_patterns = [
        re.compile(r"AKIA[0-9A-Z]{16}"),  # AWS Access Key ID
        re.compile(r"-----BEGIN (?:RSA |EC )?PRIVATE KEY-----"),
        re.compile(r"sk-[A-Za-z0-9]{32,}"),  # OpenAI / live secret key
        re.compile(r"npg_[A-Za-z0-9]{12,}"),  # Neon real password token
        re.compile(r"ghp_[A-Za-z0-9]{30,}"),  # GitHub PAT
    ]
    skip_dirs = {".venv", "__pycache__", ".pytest_cache", "algo_trading_control_center.egg-info"}
    scanned_files = 0
    for path in PROJECT_ROOT.rglob("*"):
        if any(part in skip_dirs for part in path.parts):
            continue
        if not path.is_file():
            continue
        if path.suffix.lower() in {".py", ".toml", ".ini", ".yml", ".yaml", ".md", ".css", ".example"} or path.name.startswith("Dockerfile"):
            content = path.read_text(encoding="utf-8", errors="ignore")
            scanned_files += 1
            for pat in secret_patterns:
                assert not pat.search(content), f"Potential secret pattern {pat.pattern!r} found in {path}"
    assert scanned_files >= 45

    # 3. Validate Dockerfile.api, Dockerfile.ui, .env.example & Cloud Run manifests
    df_api = (PROJECT_ROOT / "Dockerfile.api").read_text(encoding="utf-8")
    df_ui = (PROJECT_ROOT / "Dockerfile.ui").read_text(encoding="utf-8")
    compose_txt = (PROJECT_ROOT / "docker-compose.yml").read_text(encoding="utf-8")
    env_example = (PROJECT_ROOT / ".env.example").read_text(encoding="utf-8")
    cr_api_yaml = (PROJECT_ROOT / "deploy" / "cloudrun-api.yaml").read_text(encoding="utf-8")
    cr_ui_yaml = (PROJECT_ROOT / "deploy" / "cloudrun-ui.yaml").read_text(encoding="utf-8")

    assert "<YOUR_NEON_DATABASE_URL>" in env_example
    assert "DATABASE_URL=" not in df_api and "DATABASE_URL=" not in df_ui

    assert "EXECUTION_MODE=PAPER_ONLY" in df_api
    assert "USER appuser" in df_api
    assert "HEALTHCHECK" in df_api
    assert "exec uvicorn backend.main:app --host 0.0.0.0 --port ${PORT:-8080}" in df_api
    assert "--timeout-graceful-shutdown" in df_api
    for copied_dir in ("core", "backend", "migrations", "demo_data", "deploy"):
        assert (PROJECT_ROOT / copied_dir).is_dir()

    assert "USER uiuser" in df_ui
    assert "HEALTHCHECK" in df_ui
    assert "exec streamlit run frontend/app.py --server.address=0.0.0.0 --server.port=${PORT:-8080}" in df_ui
    assert "COPY core" not in df_ui and "COPY backend" not in df_ui

    assert "Dockerfile.api" in compose_txt
    assert "Dockerfile.ui" in compose_txt
    assert "deploy.run_migrations" in compose_txt

    assert 'autoscaling.knative.dev/minScale: "0"' in cr_api_yaml
    assert 'autoscaling.knative.dev/maxScale: "2"' in cr_api_yaml
    assert "secretKeyRef:" in cr_api_yaml
    assert 'autoscaling.knative.dev/minScale: "0"' in cr_ui_yaml
    assert 'autoscaling.knative.dev/maxScale: "2"' in cr_ui_yaml

    # 4. Validate README mandatory disclaimer and required sections
    readme_txt = (PROJECT_ROOT / "README.md").read_text(encoding="utf-8")
    assert (
        "This application is an engineering demonstration using simulated/backtested execution. "
        "It does not provide investment advice and does not place real-money orders."
    ) in readme_txt
    for heading in (
        "Product Overview",
        "Architecture",
        "Features",
        "Technology Stack",
        "Screens",
        "Local Setup",
        "Environment Variables",
        "Database Migrations",
        "Testing",
        "Docker",
        "Cloud Deployment",
        "Security",
        "Known Limitations",
    ):
        assert heading.lower() in readme_txt.lower(), f"Missing README section: {heading}"


def test_cloud_run_contract_startup_on_dynamic_port(
    migrated_db_url: str, tmp_path: Path
) -> None:
    """Verify the exact Cloud Run startup contract (`PORT` env var + Uvicorn) starts and passes readiness."""
    port = 18080
    base_url = f"http://127.0.0.1:{port}"
    env = os.environ.copy()
    env["PORT"] = str(port)
    env["DATABASE_URL"] = migrated_db_url
    env["EXECUTION_MODE"] = "PAPER_ONLY"
    env["TIMEZONE"] = "Asia/Kolkata"
    env["AUTO_SEED_REFERENCE_DATA"] = "true"
    env["REPLAY_WORKER_ENABLED"] = "false"

    log_file = tmp_path / "cloud_run_startup.log"
    fh = log_file.open("w", encoding="utf-8")
    proc = subprocess.Popen(
        [
            sys.executable,
            "-m",
            "uvicorn",
            "backend.main:app",
            "--host",
            "127.0.0.1",
            "--port",
            env["PORT"],
        ],
        cwd=str(PROJECT_ROOT),
        env=env,
        stdout=fh,
        stderr=subprocess.STDOUT,
        text=True,
    )
    try:
        ready = False
        for _ in range(50):
            if proc.poll() is not None:
                break
            try:
                resp = httpx.get(f"{base_url}/health/live", timeout=1.0, trust_env=False)
                if resp.status_code == 200:
                    ready = True
                    break
            except httpx.HTTPError:
                pass
            time.sleep(0.1)

        assert ready, f"Cloud Run contract startup failed:\n{log_file.read_text(encoding='utf-8')}"

        health_r = httpx.get(f"{base_url}/health", timeout=3.0, trust_env=False)
        assert health_r.status_code == 200
        assert health_r.json()["execution_mode"] == "PAPER_ONLY"
        assert health_r.json()["database"]["status"] == "HEALTHY"
        assert len(health_r.json()["operational_components"]) == 9
    finally:
        proc.terminate()
        try:
            proc.wait(timeout=5)
        except subprocess.TimeoutExpired:
            proc.kill()
        fh.close()


def test_neon_ssl_nullpool_and_controlled_migration_runner(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Verify Neon SSL auto-enforcement, serverless NullPool mode, and controlled migration runner."""
    from sqlalchemy.pool import NullPool, QueuePool

    from core.config import AppSettings
    from core.persistence.db import (
        _resolve_postgresql_connect_args,
        build_engine,
        reset_engine,
    )
    from deploy.run_migrations import execute_controlled_migrations

    # 1. Neon host without explicit ?sslmode= automatically enforces sslmode=require
    cfg_null = AppSettings(
        DATABASE_URL="postgresql+psycopg://u:p@ep-demo-pooler.ap-southeast-1.aws.neon.tech/neondb",
        DB_POOL_MODE="NULLPOOL",
        DB_SSLMODE="prefer",
    )
    args_neon = _resolve_postgresql_connect_args(cfg_null.DATABASE_URL, cfg_null)
    assert args_neon["sslmode"] == "require"
    assert args_neon["connect_timeout"] == 15

    eng_null = build_engine(cfg_null.DATABASE_URL, cfg_null)
    assert isinstance(eng_null.pool, NullPool)
    eng_null.dispose()

    # 2. QUEUE mode uses conservative serverless QueuePool
    cfg_queue = AppSettings(
        DATABASE_URL="postgresql+psycopg://u:p@ep-demo.ap-southeast-1.aws.neon.tech/neondb?sslmode=require",
        DB_POOL_MODE="QUEUE",
        DB_POOL_SIZE=2,
        DB_MAX_OVERFLOW=3,
    )
    eng_queue = build_engine(cfg_queue.DATABASE_URL, cfg_queue)
    assert isinstance(eng_queue.pool, QueuePool)
    eng_queue.dispose()

    # 3. Controlled migration runner applies all migrations + seeds reference data cleanly
    db_file = tmp_path / "controlled_mig_test.sqlite3"
    dsn = f"sqlite:///{db_file.as_posix()}"
    monkeypatch.setenv("DATABASE_URL", dsn)
    monkeypatch.setenv("REPLAY_WORKER_ENABLED", "false")
    clear_settings_cache()
    reset_engine()

    rev = execute_controlled_migrations(seed_reference=True, run_demo_showcase=False)
    assert rev == "0008_fyers_historical_market_data"
    reset_engine()


def test_render_blueprint_cloudrun_coexistence_redaction_and_bounded_wakeup_retry(
    migrated_db_url: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Verify Render Blueprint syntax, Cloud Run coexistence, production DSN redaction, and bounded cold-start retry."""
    import yaml

    from core.config import AppSettings
    from frontend.api_client import ControlCenterApiClient

    # 1. Both Cloud Run AND Render deployment assets coexist intact
    for required_asset in (
        PROJECT_ROOT / "docs" / "CLOUD_RUN_NEON_DEPLOYMENT.md",
        PROJECT_ROOT / "deploy" / "cloudbuild.yaml",
        PROJECT_ROOT / "deploy" / "cloudrun-api.yaml",
        PROJECT_ROOT / "deploy" / "cloudrun-ui.yaml",
        PROJECT_ROOT / "deploy" / "run_migrations.py",
        PROJECT_ROOT / "render.yaml",
        PROJECT_ROOT / "RENDER_NEON_DEPLOYMENT.md",
        PROJECT_ROOT / "deploy" / "render" / "README.md",
        PROJECT_ROOT / "deploy" / "render" / "MIGRATIONS.md",
        PROJECT_ROOT / "deploy" / "render" / "verification.md",
    ):
        assert required_asset.is_file(), f"Missing required deployment asset: {required_asset}"

    # 2. Validate render.yaml against Render-supported Blueprint syntax & security rules
    bp = yaml.safe_load((PROJECT_ROOT / "render.yaml").read_text(encoding="utf-8"))
    assert isinstance(bp, dict) and set(bp.keys()) == {"services"}
    services = bp["services"]
    assert len(services) == 2

    allowed_service_keys = {
        "type",
        "name",
        "runtime",
        "plan",
        "dockerfilePath",
        "dockerContext",
        "healthCheckPath",
        "autoDeploy",
        "envVars",
    }
    for svc in services:
        assert set(svc.keys()).issubset(allowed_service_keys)
        assert svc["type"] == "web"
        assert svc["runtime"] == "docker"
        assert svc["plan"] == "free"

    api_svc = next(s for s in services if s["name"] == "atcc-api")
    ui_svc = next(s for s in services if s["name"] == "atcc-ui")

    assert api_svc["dockerfilePath"] == "./Dockerfile.api"
    assert api_svc["healthCheckPath"] == "/health/live"
    api_env = {item["key"]: item for item in api_svc["envVars"]}
    assert api_env["EXECUTION_MODE"]["value"] == "PAPER_ONLY"
    assert api_env["TIMEZONE"]["value"] == "Asia/Kolkata"
    assert api_env["DB_SSLMODE"]["value"] == "require"
    assert api_env["DB_POOL_MODE"]["value"] == "NULLPOOL"
    assert api_env["REPLAY_WORKER_ENABLED"]["value"] == "false"
    assert api_env["AUTO_SEED_REFERENCE_DATA"]["value"] == "false"
    assert api_env["DATABASE_URL"].get("sync") is False and "value" not in api_env["DATABASE_URL"]
    assert api_env["CORS_ALLOWED_ORIGINS"].get("sync") is False

    assert ui_svc["dockerfilePath"] == "./Dockerfile.ui"
    assert ui_svc["healthCheckPath"] == "/_stcore/health"
    ui_env = {item["key"]: item for item in ui_svc["envVars"]}
    assert ui_env["API_BASE_URL"].get("sync") is False
    assert "DATABASE_URL" not in ui_env  # UI must NEVER receive DATABASE_URL

    # 3. Verify Production Health/Settings Redaction (zero host/user/password exposure)
    prod_cfg = AppSettings(
        APP_ENV="production",
        DATABASE_URL="postgresql+psycopg://secret_user:secret_pw@ep-private.ap-southeast-1.aws.neon.tech/neondb?sslmode=require",
    )
    pub_disp = prod_cfg.public_database_display
    assert "secret_user" not in pub_disp
    assert "secret_pw" not in pub_disp
    assert "ep-private" not in pub_disp
    assert "<redacted-host>" in pub_disp

    monkeypatch.setenv("APP_ENV", "production")
    monkeypatch.setenv("REPLAY_WORKER_ENABLED", "false")
    clear_settings_cache()
    app = create_app()
    with TestClient(app) as test_client:
        live = test_client.get("/health/live").json()
        assert live["status"] == "ALIVE"
        assert live["execution_mode"] == "PAPER_ONLY"
        assert "database" not in live

        ready = test_client.get("/health/ready")
        assert ready.status_code == 200
        assert "<redacted-host>" in ready.json()["database"]["details"]["masked_dsn"]

    monkeypatch.setenv("APP_ENV", "development")
    clear_settings_cache()

    # 4. Verify ControlCenterApiClient bare-host normalization & bounded finite wake-up retry
    ui_client = ControlCenterApiClient(base_url="atcc-api.onrender.com", timeout=2.0)
    assert ui_client.base_url == "https://atcc-api.onrender.com"

    attempts = {"count": 0}

    class _FakeResp:
        def __init__(self, code: int, body: dict[str, str]) -> None:
            self.status_code = code
            self.headers = {"X-Correlation-ID": "cid-wakeup-test"}
            self._body = body

        def json(self) -> dict[str, str]:
            return self._body

    class _FakeHttpxClient:
        def __init__(self, *args: object, **kwargs: object) -> None:
            pass

        def __enter__(self) -> "_FakeHttpxClient":
            return self

        def __exit__(self, *args: object) -> None:
            pass

        def request(self, **kwargs: object) -> _FakeResp:
            attempts["count"] += 1
            if attempts["count"] == 1:
                return _FakeResp(502, {"message": "Service waking from sleep"})
            return _FakeResp(200, {"status": "ALIVE", "execution_mode": "PAPER_ONLY"})

    monkeypatch.setattr("frontend.api_client.httpx.Client", _FakeHttpxClient)
    monkeypatch.setattr("frontend.api_client.time.sleep", lambda _s: None)
    wake_res = ui_client.check_liveness()
    assert wake_res.ok is True
    assert attempts["count"] == 2  # Bounded retry succeeded on 2nd attempt without looping


