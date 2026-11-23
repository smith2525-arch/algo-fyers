"""Safe runtime environment and configuration inspection endpoint (zero secrets)."""

from __future__ import annotations

from fastapi import APIRouter

from backend.schemas.api import RuntimeSettingsOut
from core.clock import now_ist
from core.config import get_settings
from core.persistence.db import probe_database

router = APIRouter(prefix="/api/v1/settings", tags=["Settings"])


@router.get("", response_model=RuntimeSettingsOut)
def get_runtime_settings() -> RuntimeSettingsOut:
    cfg = get_settings()
    probe = probe_database(cfg)
    return RuntimeSettingsOut(
        app_name=cfg.APP_NAME,
        app_env=cfg.APP_ENV,
        app_version=cfg.APP_VERSION,
        execution_mode=cfg.EXECUTION_MODE,
        market_mode=cfg.MARKET_MODE,
        timezone=cfg.TIMEZONE,
        masked_database_url=cfg.public_database_display,
        db_pool_size=cfg.DB_POOL_SIZE,
        db_max_overflow=cfg.DB_MAX_OVERFLOW,
        db_pool_recycle_seconds=cfg.DB_POOL_RECYCLE_SECONDS,
        api_base_url=cfg.API_BASE_URL,
        alembic_version=probe.get("alembic_version"),
        database_reachable=bool(probe.get("reachable")),
        fyers_historical_data_enabled=bool(cfg.FYERS_HISTORICAL_DATA_ENABLED),
        fyers_token_configured=bool(cfg.fyers_token_configured),
        timestamp_ist=now_ist(),
    )
