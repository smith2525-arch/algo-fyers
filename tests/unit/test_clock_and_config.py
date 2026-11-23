"""Unit tests for IST clock discipline, configuration safety locks, and secret hygiene."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from core.clock import (
    FixedISTClock,
    ensure_ist,
    format_ist_iso,
    is_within_ist_session,
    now_ist,
)
from core.config import AppSettings
from core.exceptions import ConfigurationSafetyError

PROJECT_ROOT = Path(__file__).resolve().parent.parent.parent


def test_now_ist_carries_plus_0530_offset() -> None:
    current = now_ist()
    assert current.utcoffset() == timedelta(hours=5, minutes=30)
    assert format_ist_iso(current).endswith("+05:30")


def test_ensure_ist_converts_utc_and_tags_naive() -> None:
    utc_dt = datetime(2026, 11, 18, 4, 0, 0, tzinfo=timezone.utc)
    converted = ensure_ist(utc_dt)
    assert converted.hour == 9
    assert converted.minute == 30
    assert converted.utcoffset() == timedelta(hours=5, minutes=30)

    naive = datetime(2026, 11, 18, 10, 15, 0)
    tagged = ensure_ist(naive)
    assert tagged.hour == 10
    assert tagged.minute == 15
    assert tagged.utcoffset() == timedelta(hours=5, minutes=30)


def test_ist_market_session_window_check() -> None:
    # Wednesday 2026-11-18 10:00 IST -> inside 09:15..15:30
    open_bar = ensure_ist(datetime(2026, 11, 18, 10, 0, 0))
    assert is_within_ist_session(open_bar) is True

    # Pre-open 09:00 IST -> outside
    pre_open = ensure_ist(datetime(2026, 11, 18, 9, 0, 0))
    assert is_within_ist_session(pre_open) is False

    # Saturday 2026-11-21 11:00 IST -> outside (weekend)
    saturday = ensure_ist(datetime(2026, 11, 21, 11, 0, 0))
    assert is_within_ist_session(saturday) is False


def test_fixed_ist_clock_advances_deterministically() -> None:
    clk = FixedISTClock(datetime(2026, 11, 18, 9, 15, 0))
    nxt = clk.advance(timedelta(minutes=5))
    assert nxt.hour == 9
    assert nxt.minute == 20
    assert nxt.utcoffset() == timedelta(hours=5, minutes=30)


def test_execution_mode_rejects_live_trading() -> None:
    with pytest.raises(ConfigurationSafetyError):
        AppSettings(EXECUTION_MODE="LIVE_BROKER")


def test_timezone_rejects_non_ist() -> None:
    with pytest.raises(ConfigurationSafetyError):
        AppSettings(TIMEZONE="UTC")


def test_masked_database_url_hides_credentials() -> None:
    cfg = AppSettings(
        DATABASE_URL="postgresql+psycopg://neon_user:super_secret_pw_123@ep-cool.neon.tech/neondb?sslmode=require"
    )
    masked = cfg.masked_database_url
    assert "super_secret_pw_123" not in masked
    assert "neon_user:***@ep-cool.neon.tech/neondb" in masked


def test_no_embedded_real_secrets_in_repo_configs() -> None:
    env_example = (PROJECT_ROOT / ".env.example").read_text(encoding="utf-8")
    assert "npg_" not in env_example  # Neon secret token prefix
    assert "sk-" not in env_example
    assert "AIza" not in env_example
