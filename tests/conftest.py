"""Shared pytest fixtures for isolated database and FastAPI client testing."""

from __future__ import annotations

import os
from collections.abc import Iterator
from pathlib import Path

import pytest
from alembic import command
from alembic.config import Config

from core.config import clear_settings_cache
from core.persistence.db import reset_engine

PROJECT_ROOT = Path(__file__).resolve().parent.parent


@pytest.fixture()
def migrated_db_url(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[str]:
    """Provide an isolated migrated database URL (uses TEST_DATABASE_URL if set, else temp SQLite)."""
    override_pg = os.getenv("TEST_DATABASE_URL", "").strip()
    if override_pg:
        db_url = override_pg
    else:
        db_file = tmp_path / "phase1_test.sqlite3"
        db_url = f"sqlite:///{db_file.as_posix()}"

    monkeypatch.setenv("DATABASE_URL", db_url)
    monkeypatch.setenv("EXECUTION_MODE", "PAPER_ONLY")
    monkeypatch.setenv("TIMEZONE", "Asia/Kolkata")
    monkeypatch.setenv("AUTO_SEED_REFERENCE_DATA", "true")
    monkeypatch.setenv("LOG_JSON", "true")

    clear_settings_cache()
    reset_engine()

    alembic_cfg = Config(str(PROJECT_ROOT / "alembic.ini"))
    alembic_cfg.set_main_option("script_location", str(PROJECT_ROOT / "migrations"))
    alembic_cfg.set_main_option("sqlalchemy.url", db_url)
    command.upgrade(alembic_cfg, "head")

    yield db_url

    reset_engine()
    clear_settings_cache()
