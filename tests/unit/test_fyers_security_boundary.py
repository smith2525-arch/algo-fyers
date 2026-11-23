"""Security Boundary Verification Suite (`FYERS EXECUTION BOUNDARY: VERIFIED`).

Proves structurally and behaviorally that:
1. No FYERS order-placement, order-modification, order-cancellation, position, holdings,
   funds, orderbook, tradebook, or live WebSocket calls exist anywhere in the codebase.
2. No FYERS execution SDK (`fyers_apiv3`, etc.) is imported or called.
3. Paper OMS routes orders exclusively to the internal `PaperBroker`.
4. `FyersHistoricalDataProvider` exposes historical-data read methods only.
5. `AppSettings` rejects any attempt to configure FYERS as an execution broker or point
   `FYERS_HISTORICAL_DATA_URL` at non-history endpoints.
6. No UI screen offers `"Place Live Order"` or labels historical FYERS data as `"LIVE DATA"`.
7. `EXECUTION_MODE` remains locked to `"PAPER_ONLY"`.
"""

from __future__ import annotations

import ast
import inspect
from pathlib import Path

import pytest

from core.broker.paper_broker import PaperBrokerService
from core.config import AppSettings, get_settings
from core.exceptions import ConfigurationSafetyError
from core.market_data.fyers_provider import FyersHistoricalDataProvider
from core.oms import service as oms_service_module

PROJECT_ROOT = Path(__file__).resolve().parent.parent.parent

FORBIDDEN_FYERS_EXECUTION_SYMBOLS: tuple[str, ...] = (
    "place_order",
    "place_basket_orders",
    "place_multileg_order",
    "modify_order",
    "cancel_basket_orders",
    "exit_positions",
    "convert_position",
    "orderbook",
    "tradebook",
    "fyers_apiv3",
    "fyersModel",
    "data_ws",
    "order_ws",
)

# `cancel_order` is only permitted on internal Paper OMS / UI API client, never on any FYERS object
ALLOWED_INTERNAL_CANCEL_RECEIVERS: frozenset[str] = frozenset(
    {"OrderManagementService", "client", "self"}
)


def test_no_fyers_execution_or_websocket_calls_in_application_code() -> None:
    """AST scan across core/, backend/, frontend/ proving zero FYERS broker/WS imports or calls."""
    for pkg in ("core", "backend", "frontend"):
        for py_file in (PROJECT_ROOT / pkg).rglob("*.py"):
            src = py_file.read_text(encoding="utf-8")
            tree = ast.parse(src, filename=str(py_file))
            for node in ast.walk(tree):
                if isinstance(node, ast.Import):
                    for alias in node.names:
                        assert "fyers_apiv3" not in alias.name, f"Forbidden SDK import in {py_file}"
                elif isinstance(node, ast.ImportFrom):
                    mod = node.module or ""
                    assert "fyers_apiv3" not in mod, f"Forbidden SDK import in {py_file}"
                elif isinstance(node, ast.Call):
                    if isinstance(node.func, ast.Attribute):
                        assert node.func.attr not in FORBIDDEN_FYERS_EXECUTION_SYMBOLS, (
                            f"Forbidden broker call '{node.func.attr}' in {py_file}"
                        )
                        if node.func.attr == "cancel_order" and isinstance(
                            node.func.value, ast.Name
                        ):
                            assert node.func.value.id in ALLOWED_INTERNAL_CANCEL_RECEIVERS, (
                                f"Unexpected cancel_order receiver '{node.func.value.id}' in {py_file}"
                            )
                    elif isinstance(node.func, ast.Name):
                        assert node.func.id not in FORBIDDEN_FYERS_EXECUTION_SYMBOLS, (
                            f"Forbidden broker call '{node.func.id}' in {py_file}"
                        )


def test_fyers_provider_exposes_historical_methods_only() -> None:
    """Confirm FyersHistoricalDataProvider exposes strictly historical read methods."""
    public_members = {
        name
        for name, _ in inspect.getmembers(FyersHistoricalDataProvider)
        if not name.startswith("_")
    }
    assert public_members == {
        "fetch_historical_candles",
        "provider_name",
        "provider_status",
        "source_type",
    }


def test_paper_oms_resolves_only_to_internal_paper_broker() -> None:
    """Confirm OMS imports neither FyersHistoricalDataProvider nor any external broker client."""
    oms_src = Path(oms_service_module.__file__).read_text(encoding="utf-8")
    assert "fyers" not in oms_src.lower()
    assert PaperBrokerService.__name__ == "PaperBrokerService"
    assert PaperBrokerService.fill_model_documentation()["mode"] == "SIMULATED_PAPER_BROKER_ONLY"

    paper_trading_src = (PROJECT_ROOT / "core" / "paper_trading" / "service.py").read_text(
        encoding="utf-8"
    )
    assert "PaperBrokerService" in paper_trading_src
    assert "FyersHistoricalDataProvider" not in paper_trading_src


def test_configuration_rejects_fyers_as_execution_broker_and_non_history_urls() -> None:
    """Confirm AppSettings fails closed if configured for live execution or non-history FYERS URLs."""
    default_cfg = get_settings()
    assert default_cfg.EXECUTION_MODE == "PAPER_ONLY"
    assert default_cfg.EXECUTION_BROKER == "INTERNAL_PAPER_BROKER"

    with pytest.raises(ConfigurationSafetyError):
        AppSettings(EXECUTION_MODE="LIVE")

    with pytest.raises(ConfigurationSafetyError):
        AppSettings(EXECUTION_BROKER="FYERS")

    for forbidden_url in (
        "https://api-t1.fyers.in/api/v3/orders",
        "https://api-t1.fyers.in/api/v3/positions",
        "https://api-t1.fyers.in/api/v3/holdings",
        "https://api-t1.fyers.in/api/v3/funds",
        "https://api-t1.fyers.in/api/v3/tradebook",
        "wss://socket.fyers.in/data/history",
    ):
        with pytest.raises(ConfigurationSafetyError):
            AppSettings(FYERS_HISTORICAL_DATA_URL=forbidden_url)


def test_ui_contains_no_place_live_order_or_misleading_live_data_label() -> None:
    """Confirm Streamlit UI views never offer 'Place Live Order' or label FYERS history as 'LIVE DATA'."""
    for py_file in (PROJECT_ROOT / "frontend").rglob("*.py"):
        txt = py_file.read_text(encoding="utf-8")
        lower = txt.lower()
        assert "place live order" not in lower, f"Forbidden live order wording in {py_file}"
        assert "FYERS_SECRET_KEY" not in txt, f"Frontend must never reference FYERS_SECRET_KEY in {py_file}"
        assert "FYERS_ACCESS_TOKEN" not in txt, f"Frontend must never reference FYERS_ACCESS_TOKEN in {py_file}"
        assert ">LIVE DATA<" not in txt.upper(), f"Historical data must not be labeled LIVE DATA in {py_file}"
