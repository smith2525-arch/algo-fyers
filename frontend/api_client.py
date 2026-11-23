"""Typed HTTP client communicating exclusively with the FastAPI backend."""

from __future__ import annotations

import os
import time
import uuid
from dataclasses import dataclass
from typing import Any

import httpx


def _normalize_api_base_url(raw_url: str) -> str:
    cleaned = (raw_url or "http://localhost:8000").strip().rstrip("/")
    if not cleaned.startswith(("http://", "https://")):
        if cleaned.startswith(("localhost", "127.0.0.1")):
            cleaned = f"http://{cleaned}"
        else:
            cleaned = f"https://{cleaned}"
    return cleaned


@dataclass(frozen=True)
class ApiResult:
    ok: bool
    status_code: int
    data: Any
    error: str | None = None
    correlation_id: str | None = None
    waking_up: bool = False


class ControlCenterApiClient:
    """Stateless HTTP wrapper for all FastAPI endpoints with bounded cold-start retry."""

    def __init__(self, base_url: str | None = None, timeout: float | None = None) -> None:
        raw_url = base_url or os.getenv("API_BASE_URL", "http://localhost:8000")
        self.base_url = _normalize_api_base_url(raw_url)
        self.timeout = timeout or float(os.getenv("API_TIMEOUT_SECONDS", "25.0"))

    def _request(
        self,
        method: str,
        path: str,
        *,
        params: dict[str, Any] | None = None,
        json_body: dict[str, Any] | None = None,
        timeout_override: float | None = None,
    ) -> ApiResult:
        cid = str(uuid.uuid4())
        headers = {
            "X-Correlation-ID": cid,
            "Accept": "application/json",
        }
        url = f"{self.base_url}{path}"
        is_loopback = "localhost" in self.base_url or "127.0.0.1" in self.base_url
        req_timeout = float(timeout_override) if timeout_override is not None else self.timeout
        # Bounded finite retry ONLY for remote scale-to-zero cold starts (Render Free / Cloud Run).
        # Never loops infinitely and never generates background keep-alive traffic.
        max_attempts = 1 if is_loopback else 3
        retry_delays = (2.0, 4.0)

        last_exc: Exception | None = None
        for attempt in range(max_attempts):
            try:
                with httpx.Client(timeout=req_timeout, trust_env=not is_loopback) as client:
                    resp = client.request(
                        method=method,
                        url=url,
                        params=params,
                        json=json_body,
                        headers=headers,
                    )
                resp_cid = resp.headers.get("X-Correlation-ID", cid)
                payload: Any = None
                try:
                    payload = resp.json()
                except ValueError:
                    payload = None

                if 200 <= resp.status_code < 300:
                    return ApiResult(
                        ok=True,
                        status_code=resp.status_code,
                        data=payload,
                        correlation_id=resp_cid,
                    )

                # Transient gateway status while a sleeping container is booting on Render / Cloud Run
                if resp.status_code in {502, 503, 504} and attempt + 1 < max_attempts:
                    time.sleep(retry_delays[attempt])
                    continue

                err_msg = (
                    payload.get("message")
                    if isinstance(payload, dict) and "message" in payload
                    else f"HTTP {resp.status_code}"
                )
                return ApiResult(
                    ok=False,
                    status_code=resp.status_code,
                    data=payload,
                    error=str(err_msg),
                    correlation_id=resp_cid,
                    waking_up=(resp.status_code in {502, 503, 504}),
                )
            except httpx.RequestError as exc:
                last_exc = exc
                if attempt + 1 < max_attempts:
                    time.sleep(retry_delays[attempt])
                    continue

        return ApiResult(
            ok=False,
            status_code=0,
            data=None,
            error=f"Scale-to-zero backend service starting or temporarily unavailable ({type(last_exc).__name__ if last_exc else 'timeout'}).",
            correlation_id=cid,
            waking_up=True,
        )

    def check_liveness(self) -> ApiResult:
        return self._request("GET", "/health/live")

    def get_dashboard(self) -> ApiResult:
        return self._request("GET", "/api/v1/dashboard")

    def get_system_health(self) -> ApiResult:
        return self._request("GET", "/api/v1/health")

    def trigger_health_probe(self) -> ApiResult:
        return self._request("POST", "/api/v1/health/probe")

    def stop_paper_trading(self, *, actor: str, reason: str) -> ApiResult:
        return self._request(
            "POST",
            "/api/v1/controls/paper-trading/stop",
            json_body={"actor": actor, "reason": reason},
        )

    def resume_paper_trading(self, *, actor: str, reason: str) -> ApiResult:
        return self._request(
            "POST",
            "/api/v1/controls/paper-trading/resume",
            json_body={"actor": actor, "reason": reason},
        )

    def get_primary_account(self) -> ApiResult:
        return self._request("GET", "/api/v1/accounts/primary")

    def list_instruments(self) -> ApiResult:
        return self._request("GET", "/api/v1/instruments")

    def update_instrument(
        self,
        symbol: str,
        *,
        lot_size: int,
        tick_size: str,
        session_open_ist: str,
        session_close_ist: str,
        is_active: bool,
        actor: str,
    ) -> ApiResult:
        return self._request(
            "PUT",
            f"/api/v1/instruments/{symbol}",
            json_body={
                "lot_size": lot_size,
                "tick_size": tick_size,
                "session_open_ist": session_open_ist,
                "session_close_ist": session_close_ist,
                "is_active": is_active,
                "actor": actor,
            },
        )

    def list_strategies(self, *, instrument: str | None = None) -> ApiResult:
        params: dict[str, Any] = {}
        if instrument and instrument != "ALL":
            params["instrument"] = instrument
        return self._request("GET", "/api/v1/strategies", params=params or None)

    def get_strategy_defaults(self, instrument: str) -> ApiResult:
        return self._request("GET", f"/api/v1/strategies/defaults/{instrument}")

    def validate_strategy_config(
        self,
        strategy_code: str,
        *,
        parameters: dict[str, Any],
    ) -> ApiResult:
        return self._request(
            "POST",
            f"/api/v1/strategies/{strategy_code}/validate",
            json_body={"parameters": parameters},
        )

    def create_strategy_version(
        self,
        strategy_code: str,
        *,
        parameters: dict[str, Any],
        notes: str | None,
        actor: str,
    ) -> ApiResult:
        return self._request(
            "POST",
            f"/api/v1/strategies/{strategy_code}/versions",
            json_body={"parameters": parameters, "notes": notes, "actor": actor},
        )

    def clone_strategy_version(
        self,
        source_version_id: str,
        *,
        parameter_overrides: dict[str, Any],
        notes: str | None,
        actor: str,
    ) -> ApiResult:
        return self._request(
            "POST",
            f"/api/v1/strategies/versions/{source_version_id}/clone",
            json_body={
                "parameter_overrides": parameter_overrides,
                "notes": notes,
                "actor": actor,
            },
        )

    def compare_strategy_versions(self, *, left_id: str, right_id: str) -> ApiResult:
        return self._request(
            "GET",
            "/api/v1/strategies/compare",
            params={"left_id": left_id, "right_id": right_id},
        )

    def list_audit_events(
        self,
        *,
        category: str | None = None,
        event_type: str | None = None,
        severity: str | None = None,
        correlation_id: str | None = None,
        strategy_id: str | None = None,
        signal_id: str | None = None,
        order_id: str | None = None,
        search: str | None = None,
        limit: int = 100,
    ) -> ApiResult:
        params: dict[str, Any] = {"limit": limit}
        if category and category != "ALL":
            params["category"] = category
        if event_type and event_type != "ALL":
            params["event_type"] = event_type
        if severity and severity != "ALL":
            params["severity"] = severity
        if correlation_id:
            params["correlation_id"] = correlation_id
        if strategy_id:
            params["strategy_id"] = strategy_id
        if signal_id:
            params["signal_id"] = signal_id
        if order_id:
            params["order_id"] = order_id
        if search:
            params["search"] = search
        return self._request("GET", "/api/v1/audit", params=params)

    def record_operator_login(self, username: str = "desk.operator") -> ApiResult:
        return self._request(
            "POST",
            "/api/v1/audit/login",
            json_body={"username": username, "user_agent": "StreamlitControlConsole"},
        )

    def list_traceable_correlations(self, *, limit: int = 30) -> ApiResult:
        return self._request("GET", "/api/v1/audit/traces", params={"limit": limit})

    def get_lifecycle_trace(self, correlation_id: str) -> ApiResult:
        return self._request("GET", f"/api/v1/audit/trace/{correlation_id}")

    def create_audit_note(
        self,
        *,
        action: str,
        summary: str,
        actor: str,
        category: str = "CONTROL",
        severity: str = "INFO",
    ) -> ApiResult:
        return self._request(
            "POST",
            "/api/v1/audit",
            json_body={
                "action": action,
                "summary": summary,
                "actor": actor,
                "actor_type": "OPERATOR",
                "category": category,
                "severity": severity,
            },
        )

    def get_settings(self) -> ApiResult:
        return self._request("GET", "/api/v1/settings")

    def list_backtest_datasets(
        self,
        *,
        instrument: str | None = None,
        timeframe: str | None = None,
    ) -> ApiResult:
        params: dict[str, Any] = {}
        if instrument and instrument != "ALL":
            params["instrument"] = instrument
        if timeframe:
            params["timeframe"] = timeframe
        return self._request("GET", "/api/v1/backtests/datasets", params=params or None)

    def import_backtest_csv(
        self,
        *,
        name: str,
        instrument: str,
        timeframe: str,
        csv_text: str,
        actor: str = "desk.operator",
    ) -> ApiResult:
        return self._request(
            "POST",
            "/api/v1/backtests/datasets/import-csv",
            json_body={
                "name": name,
                "instrument": instrument,
                "timeframe": timeframe,
                "csv_text": csv_text,
                "actor": actor,
            },
        )

    def execute_backtest(self, payload: dict[str, Any]) -> ApiResult:
        return self._request("POST", "/api/v1/backtests", json_body=payload)

    def list_backtests(
        self,
        *,
        instrument: str | None = None,
        strategy_version_id: str | None = None,
        status: str | None = None,
        limit: int = 50,
    ) -> ApiResult:
        params: dict[str, Any] = {"limit": limit}
        if instrument and instrument != "ALL":
            params["instrument"] = instrument
        if strategy_version_id:
            params["strategy_version_id"] = strategy_version_id
        if status and status != "ALL":
            params["status"] = status
        return self._request("GET", "/api/v1/backtests", params=params)

    def get_backtest_detail(self, backtest_id: str) -> ApiResult:
        return self._request("GET", f"/api/v1/backtests/{backtest_id}")

    def get_replay_catalog(self, *, instrument: str = "NIFTY") -> ApiResult:
        return self._request(
            "GET", "/api/v1/replay/catalog", params={"instrument": instrument}
        )

    def get_replay_state(
        self,
        *,
        session_id: str | None = None,
        advance_due: bool = False,
    ) -> ApiResult:
        params: dict[str, Any] = {"advance_due": "true" if advance_due else "false"}
        if session_id:
            params["session_id"] = session_id
        return self._request("GET", "/api/v1/replay/state", params=params)

    def start_replay(
        self,
        *,
        instrument: str,
        strategy_version_id: str,
        dataset_id: str,
        historical_session_date: str,
        replay_speed: str,
        actor: str = "desk.operator",
    ) -> ApiResult:
        return self._request(
            "POST",
            "/api/v1/replay/start",
            json_body={
                "instrument": instrument,
                "strategy_version_id": strategy_version_id,
                "dataset_id": dataset_id,
                "historical_session_date": historical_session_date,
                "replay_speed": replay_speed,
                "initial_step_bars": 1,
                "actor": actor,
            },
        )

    def pause_replay(self, session_id: str, *, actor: str = "desk.operator") -> ApiResult:
        return self._request(
            "POST", f"/api/v1/replay/{session_id}/pause", json_body={"actor": actor}
        )

    def resume_replay(self, session_id: str, *, actor: str = "desk.operator") -> ApiResult:
        return self._request(
            "POST", f"/api/v1/replay/{session_id}/resume", json_body={"actor": actor}
        )

    def stop_replay(self, session_id: str, *, actor: str = "desk.operator") -> ApiResult:
        return self._request(
            "POST", f"/api/v1/replay/{session_id}/stop", json_body={"actor": actor}
        )

    def reset_replay(self, session_id: str, *, actor: str = "desk.operator") -> ApiResult:
        return self._request(
            "POST", f"/api/v1/replay/{session_id}/reset", json_body={"actor": actor}
        )

    def update_replay_speed(
        self, session_id: str, *, replay_speed: str, actor: str = "desk.operator"
    ) -> ApiResult:
        return self._request(
            "POST",
            f"/api/v1/replay/{session_id}/speed",
            json_body={"replay_speed": replay_speed, "actor": actor},
        )

    def step_replay(
        self, session_id: str, *, bars: int = 1, actor: str = "desk.operator"
    ) -> ApiResult:
        return self._request(
            "POST",
            f"/api/v1/replay/{session_id}/step",
            json_body={"bars": bars, "actor": actor},
        )

    def get_paper_trading_workspace(self, *, advance_due: bool = False) -> ApiResult:
        return self._request(
            "GET",
            "/api/v1/paper-trading/workspace",
            params={"advance_due": "true" if advance_due else "false"},
        )

    def start_strategy_instance(self, payload: dict[str, Any]) -> ApiResult:
        return self._request("POST", "/api/v1/paper-trading/instances", json_body=payload)

    def pause_strategy_instance(
        self, instance_id: str, *, actor: str = "desk.operator"
    ) -> ApiResult:
        return self._request(
            "POST",
            f"/api/v1/paper-trading/instances/{instance_id}/pause",
            json_body={"actor": actor},
        )

    def resume_strategy_instance(
        self, instance_id: str, *, actor: str = "desk.operator"
    ) -> ApiResult:
        return self._request(
            "POST",
            f"/api/v1/paper-trading/instances/{instance_id}/resume",
            json_body={"actor": actor},
        )

    def stop_strategy_instance(
        self, instance_id: str, *, actor: str = "desk.operator"
    ) -> ApiResult:
        return self._request(
            "POST",
            f"/api/v1/paper-trading/instances/{instance_id}/stop",
            json_body={"actor": actor},
        )

    def run_paper_reconciliation(self) -> ApiResult:
        return self._request("POST", "/api/v1/paper-trading/reconcile")

    def list_orders(
        self,
        *,
        status: str = "ALL",
        instrument: str = "ALL",
        limit: int = 100,
    ) -> ApiResult:
        return self._request(
            "GET",
            "/api/v1/orders",
            params={"status": status, "instrument": instrument, "limit": limit},
        )

    def get_order_detail(self, order_id: str) -> ApiResult:
        return self._request("GET", f"/api/v1/orders/{order_id}")

    def submit_paper_order(self, payload: dict[str, Any]) -> ApiResult:
        return self._request("POST", "/api/v1/orders", json_body=payload)

    def cancel_paper_order(
        self,
        order_id: str,
        *,
        reason: str = "Cancelled from OMS desk",
        actor: str = "desk.operator",
    ) -> ApiResult:
        return self._request(
            "POST",
            f"/api/v1/orders/{order_id}/cancel",
            json_body={"actor": actor, "reason": reason},
        )

    def cancel_all_orders(
        self,
        *,
        instrument_symbol: str | None = None,
        reason: str = "Operator cancelled all working paper orders",
        actor: str = "desk.operator",
    ) -> ApiResult:
        return self._request(
            "POST",
            "/api/v1/orders/cancel-all",
            json_body={
                "instrument_symbol": instrument_symbol,
                "actor": actor,
                "reason": reason,
            },
        )

    def list_positions(
        self, *, status: str = "ALL", instrument: str = "ALL"
    ) -> ApiResult:
        return self._request(
            "GET",
            "/api/v1/positions",
            params={"status": status, "instrument": instrument},
        )

    def exit_position(
        self,
        position_id: str,
        *,
        client_order_key: str | None = None,
        reason: str = "OPERATOR_EXIT",
        actor: str = "desk.operator",
    ) -> ApiResult:
        body: dict[str, Any] = {"actor": actor, "reason": reason}
        if client_order_key:
            body["client_order_key"] = client_order_key
        return self._request("POST", f"/api/v1/positions/{position_id}/exit", json_body=body)

    def flatten_all_positions(
        self,
        *,
        instrument_symbol: str | None = None,
        actor: str = "desk.operator",
    ) -> ApiResult:
        return self._request(
            "POST",
            "/api/v1/positions/flatten-all",
            json_body={"instrument_symbol": instrument_symbol, "actor": actor},
        )

    def reconcile_positions(self) -> ApiResult:
        return self._request("POST", "/api/v1/positions/reconcile")

    def get_risk_overview(self) -> ApiResult:
        return self._request("GET", "/api/v1/risk/overview")

    def list_risk_decisions(
        self, *, decision: str = "ALL", rule: str = "ALL", limit: int = 100
    ) -> ApiResult:
        return self._request(
            "GET",
            "/api/v1/risk/decisions",
            params={"decision": decision, "rule": rule, "limit": limit},
        )

    def update_account_risk_config(self, payload: dict[str, Any]) -> ApiResult:
        return self._request("PUT", "/api/v1/risk/account-config", json_body=payload)

    def update_strategy_risk_config(
        self, strategy_version_id: str, payload: dict[str, Any]
    ) -> ApiResult:
        return self._request(
            "PUT",
            f"/api/v1/risk/strategy-config/{strategy_version_id}",
            json_body=payload,
        )

    def activate_kill_switch(
        self, *, reason: str, actor: str = "risk.officer"
    ) -> ApiResult:
        return self._request(
            "POST",
            "/api/v1/risk/kill-switch/activate",
            json_body={"actor": actor, "reason": reason},
        )

    def deactivate_kill_switch(
        self, *, reason: str, actor: str = "risk.officer"
    ) -> ApiResult:
        return self._request(
            "POST",
            "/api/v1/risk/kill-switch/deactivate",
            json_body={"actor": actor, "reason": reason},
        )

    def flatten_positions_confirmed(
        self,
        *,
        confirm_flatten: bool,
        instrument_symbol: str | None = None,
        actor: str = "risk.officer",
    ) -> ApiResult:
        return self._request(
            "POST",
            "/api/v1/risk/positions/flatten",
            json_body={
                "confirm_flatten": confirm_flatten,
                "instrument_symbol": instrument_symbol,
                "actor": actor,
            },
        )

    def get_pnl_analytics(
        self,
        *,
        start_date: str | None = None,
        end_date: str | None = None,
        strategy: str = "ALL",
        instrument: str = "ALL",
        direction: str = "ALL",
        exit_reason: str = "ALL",
        outcome: str = "ALL",
    ) -> ApiResult:
        params: dict[str, Any] = {
            "strategy": strategy,
            "instrument": instrument,
            "direction": direction,
            "exit_reason": exit_reason,
            "outcome": outcome,
        }
        if start_date:
            params["start_date"] = start_date
        if end_date:
            params["end_date"] = end_date
        return self._request("GET", "/api/v1/pnl/analytics", params=params)

    def list_reconciliation_runs(self, *, limit: int = 30) -> ApiResult:
        return self._request("GET", "/api/v1/reconciliation/runs", params={"limit": limit})

    def run_deterministic_reconciliation(
        self, *, apply_repair: bool = False, actor: str = "desk.operator"
    ) -> ApiResult:
        return self._request(
            "POST",
            "/api/v1/reconciliation/run",
            json_body={"apply_repair": apply_repair, "actor": actor},
        )

    def get_guided_demo_status(self) -> ApiResult:
        return self._request("GET", "/api/v1/demo/status")

    def run_demo_showcase(self, *, actor: str = "consulting.presenter") -> ApiResult:
        return self._request(
            "POST",
            "/api/v1/demo/run-showcase",
            json_body={"actor": actor},
        )

    def trigger_demo_drill(
        self, drill_name: str, *, actor: str = "consulting.presenter"
    ) -> ApiResult:
        return self._request(
            "POST",
            f"/api/v1/demo/drill/{drill_name}",
            json_body={"actor": actor},
        )

    # --- Historical Market Data (FYERS Historical, CSV Upload/Export, Synthetic) ---

    def get_historical_providers(self) -> ApiResult:
        return self._request("GET", "/api/v1/historical-data/providers")

    def get_historical_instruments(self) -> ApiResult:
        return self._request("GET", "/api/v1/historical-data/instruments")

    def trigger_fyers_download(self, payload: dict[str, Any]) -> ApiResult:
        return self._request(
            "POST",
            "/api/v1/historical-data/downloads",
            json_body=payload,
            timeout_override=max(self.timeout, 90.0),
        )

    def get_historical_download_run(self, download_id: str) -> ApiResult:
        return self._request("GET", f"/api/v1/historical-data/downloads/{download_id}")

    def list_historical_datasets(
        self,
        *,
        instrument: str | None = None,
        timeframe: str | None = None,
        source_type: str | None = None,
        provider: str | None = None,
        quality_status: str | None = None,
        include_synthetic: bool = True,
    ) -> ApiResult:
        params: dict[str, Any] = {"include_synthetic": "true" if include_synthetic else "false"}
        if instrument and instrument != "ALL":
            params["instrument"] = instrument
        if timeframe and timeframe != "ALL":
            params["timeframe"] = timeframe
        if source_type and source_type != "ALL":
            params["source_type"] = source_type
        if provider and provider != "ALL":
            params["provider"] = provider
        if quality_status and quality_status != "ALL":
            params["quality_status"] = quality_status
        return self._request("GET", "/api/v1/historical-data/datasets", params=params)

    def get_historical_dataset_detail(self, dataset_id: str) -> ApiResult:
        return self._request("GET", f"/api/v1/historical-data/datasets/{dataset_id}")

    def get_historical_dataset_quality(self, dataset_id: str) -> ApiResult:
        return self._request("GET", f"/api/v1/historical-data/datasets/{dataset_id}/quality")

    def upload_historical_csv(
        self,
        *,
        name: str,
        instrument: str,
        timeframe: str,
        csv_text: str,
        actor: str = "desk.operator",
    ) -> ApiResult:
        return self._request(
            "POST",
            "/api/v1/historical-data/uploads",
            json_body={
                "name": name,
                "instrument": instrument,
                "timeframe": timeframe,
                "csv_text": csv_text,
                "actor": actor,
            },
        )

    def delete_historical_dataset(
        self, dataset_id: str, *, actor: str = "desk.operator"
    ) -> ApiResult:
        return self._request(
            "DELETE",
            f"/api/v1/historical-data/datasets/{dataset_id}",
            params={"actor": actor},
        )

    def export_historical_dataset_csv(
        self, dataset_id: str, *, actor: str = "desk.operator"
    ) -> ApiResult:
        """Download normalized CSV text and deterministic filename from FastAPI."""
        cid = str(uuid.uuid4())
        url = f"{self.base_url}/api/v1/historical-data/datasets/{dataset_id}/csv"
        is_loopback = "localhost" in self.base_url or "127.0.0.1" in self.base_url
        try:
            with httpx.Client(timeout=self.timeout, trust_env=not is_loopback) as client:
                resp = client.get(
                    url,
                    params={"actor": actor},
                    headers={"X-Correlation-ID": cid, "Accept": "text/csv"},
                )
            if 200 <= resp.status_code < 300:
                fname = resp.headers.get("X-Dataset-Filename") or f"{dataset_id}.csv"
                return ApiResult(
                    ok=True,
                    status_code=resp.status_code,
                    data={"filename": fname, "csv_text": resp.text},
                    correlation_id=resp.headers.get("X-Correlation-ID", cid),
                )
            err_msg = f"HTTP {resp.status_code}"
            try:
                body = resp.json()
                if isinstance(body, dict) and body.get("message"):
                    err_msg = str(body["message"])
            except ValueError:
                pass
            return ApiResult(
                ok=False,
                status_code=resp.status_code,
                data=None,
                error=err_msg,
                correlation_id=cid,
            )
        except httpx.RequestError as exc:
            return ApiResult(
                ok=False,
                status_code=0,
                data=None,
                error=f"Unable to download CSV ({type(exc).__name__}).",
                correlation_id=cid,
            )


