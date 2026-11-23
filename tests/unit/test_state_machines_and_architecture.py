"""Unit tests for lifecycle state machines and strict package import boundaries."""

from __future__ import annotations

import ast
from pathlib import Path

import pytest

from core.domain.enums import (
    ORDER_TRANSITIONS,
    PAPER_ACCOUNT_TRANSITIONS,
    REPLAY_SESSION_TRANSITIONS,
    SIGNAL_TRANSITIONS,
    OrderStatus,
    PaperAccountStatus,
    ReplaySessionStatus,
    SignalStatus,
    SystemComponentStatus,
    validate_state_transition,
)
from core.exceptions import InvalidStateTransitionError

PROJECT_ROOT = Path(__file__).resolve().parent.parent.parent


def test_system_component_status_matches_locked_values() -> None:
    assert {s.value for s in SystemComponentStatus} == {
        "HEALTHY",
        "DEGRADED",
        "STALE",
        "BLOCKED",
        "STOPPED",
    }


def test_valid_and_invalid_state_transitions() -> None:
    assert (
        validate_state_transition(
            "Order", OrderStatus.CREATED, OrderStatus.RISK_APPROVED, ORDER_TRANSITIONS
        )
        == OrderStatus.RISK_APPROVED
    )
    assert (
        validate_state_transition(
            "Order", OrderStatus.RISK_APPROVED, OrderStatus.SUBMITTED, ORDER_TRANSITIONS
        )
        == OrderStatus.SUBMITTED
    )
    assert (
        validate_state_transition(
            "Order", OrderStatus.SUBMITTED, OrderStatus.ACKNOWLEDGED, ORDER_TRANSITIONS
        )
        == OrderStatus.ACKNOWLEDGED
    )
    assert (
        validate_state_transition(
            "Order", OrderStatus.ACKNOWLEDGED, OrderStatus.PARTIALLY_FILLED, ORDER_TRANSITIONS
        )
        == OrderStatus.PARTIALLY_FILLED
    )
    assert (
        validate_state_transition(
            "Order", OrderStatus.PARTIALLY_FILLED, OrderStatus.FILLED, ORDER_TRANSITIONS
        )
        == OrderStatus.FILLED
    )
    # Bypassing RISK_APPROVED directly to SUBMITTED or FILLED is prohibited
    with pytest.raises(InvalidStateTransitionError):
        validate_state_transition(
            "Order", OrderStatus.CREATED, OrderStatus.SUBMITTED, ORDER_TRANSITIONS
        )
    with pytest.raises(InvalidStateTransitionError):
        validate_state_transition(
            "Order", OrderStatus.CREATED, OrderStatus.FILLED, ORDER_TRANSITIONS
        )
    with pytest.raises(InvalidStateTransitionError):
        validate_state_transition(
            "Order", OrderStatus.FILLED, OrderStatus.CANCELLED, ORDER_TRANSITIONS
        )

    with pytest.raises(InvalidStateTransitionError):
        validate_state_transition(
            "Signal", SignalStatus.EXPIRED, SignalStatus.ORDER_CREATED, SIGNAL_TRANSITIONS
        )

    assert (
        validate_state_transition(
            "PaperAccount",
            PaperAccountStatus.ACTIVE,
            PaperAccountStatus.HALTED,
            PAPER_ACCOUNT_TRANSITIONS,
        )
        == PaperAccountStatus.HALTED
    )

    assert (
        validate_state_transition(
            "ReplaySession",
            ReplaySessionStatus.RUNNING,
            ReplaySessionStatus.PAUSED_RECOVERY,
            REPLAY_SESSION_TRANSITIONS,
        )
        == ReplaySessionStatus.PAUSED_RECOVERY
    )


def _imported_top_packages(py_file: Path) -> set[str]:
    tree = ast.parse(py_file.read_text(encoding="utf-8"), filename=str(py_file))
    roots: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                roots.add(alias.name.split(".")[0])
        elif isinstance(node, ast.ImportFrom) and node.module:
            roots.add(node.module.split(".")[0])
    return roots


def test_frontend_never_imports_core_backend_or_sqlalchemy() -> None:
    frontend_dir = PROJECT_ROOT / "frontend"
    py_files = list(frontend_dir.rglob("*.py"))
    assert py_files, "Expected Python files in frontend/"

    forbidden = {"core", "backend", "sqlalchemy", "alembic", "psycopg"}
    for path in py_files:
        imports = _imported_top_packages(path)
        overlap = imports & forbidden
        assert not overlap, f"{path.relative_to(PROJECT_ROOT)} illegally imports {overlap}"


def test_core_never_imports_fastapi_or_streamlit() -> None:
    core_dir = PROJECT_ROOT / "core"
    py_files = list(core_dir.rglob("*.py"))
    assert py_files, "Expected Python files in core/"

    forbidden = {"fastapi", "streamlit", "backend", "frontend"}
    for path in py_files:
        imports = _imported_top_packages(path)
        overlap = imports & forbidden
        assert not overlap, f"{path.relative_to(PROJECT_ROOT)} illegally imports {overlap}"
