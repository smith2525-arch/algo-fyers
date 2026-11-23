"""Database engine, Neon PostgreSQL SSL/serverless pooling, IST session hook, and session management."""

from __future__ import annotations

import logging
import re
import time
from decimal import Decimal
from typing import Any
from urllib.parse import parse_qs, urlsplit

from sqlalchemy import create_engine, event, text
from sqlalchemy.engine import Engine
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.orm import Session, sessionmaker
from sqlalchemy.pool import NullPool, StaticPool

from core.config import AppSettings, get_settings
from core.exceptions import DatabaseUnavailableError

logger = logging.getLogger(__name__)

_engine: Engine | None = None
_session_factory: sessionmaker[Session] | None = None
_bound_dsn: str | None = None


def _attach_timezone_listener(engine: Engine) -> None:
    """Ensure every PostgreSQL connection explicitly sets session TimeZone to Asia/Kolkata."""
    if engine.dialect.name != "postgresql":
        return

    @event.listens_for(engine, "connect")
    def _set_postgresql_ist_timezone(dbapi_connection: Any, connection_record: Any) -> None:
        cursor = dbapi_connection.cursor()
        try:
            cursor.execute("SET TIME ZONE 'Asia/Kolkata'")
        finally:
            cursor.close()


def _resolve_postgresql_connect_args(clean_dsn: str, cfg: AppSettings) -> dict[str, Any]:
    """Build psycopg v3 connect_args with automatic SSL enforcement for Neon PostgreSQL."""
    connect_args: dict[str, Any] = {
        "connect_timeout": cfg.DB_CONNECT_TIMEOUT_SECONDS,
    }
    parsed = urlsplit(clean_dsn)
    query_params = parse_qs(parsed.query)
    has_explicit_sslmode = "sslmode" in query_params

    host_lower = (parsed.hostname or "").lower()
    is_neon_host = host_lower.endswith(".neon.tech") or ".neon." in host_lower
    configured_sslmode = (cfg.DB_SSLMODE or "prefer").strip().lower()

    if not has_explicit_sslmode:
        if is_neon_host:
            connect_args["sslmode"] = "require"
        elif configured_sslmode in {
            "disable",
            "allow",
            "prefer",
            "require",
            "verify-ca",
            "verify-full",
        } and configured_sslmode != "prefer":
            connect_args["sslmode"] = configured_sslmode

    return connect_args


def build_engine(dsn: str, settings: AppSettings | None = None) -> Engine:
    """Create a configured SQLAlchemy 2.0 Engine tuned for Cloud Run & Neon PostgreSQL."""
    cfg = settings or get_settings()
    clean_dsn = (dsn or "").strip()
    if not clean_dsn:
        raise DatabaseUnavailableError(
            "DATABASE_URL is not configured. Provide a PostgreSQL connection string via environment variables."
        )

    if clean_dsn.startswith("sqlite"):
        # Used only in isolated unit/integration tests when explicitly requested
        connect_args = {"check_same_thread": False}
        is_memory = ":memory:" in clean_dsn
        engine = create_engine(
            clean_dsn,
            connect_args=connect_args,
            poolclass=StaticPool if is_memory else None,
            future=True,
        )
        return engine

    # Normalize standard postgres:// or postgresql:// URLs to psycopg v3 driver if unspecified
    if clean_dsn.startswith("postgres://"):
        clean_dsn = "postgresql+psycopg://" + clean_dsn[len("postgres://") :]
    elif clean_dsn.startswith("postgresql://"):
        clean_dsn = "postgresql+psycopg://" + clean_dsn[len("postgresql://") :]

    pg_connect_args: dict[str, Any] = {}
    if clean_dsn.startswith("postgresql+psycopg"):
        pg_connect_args = _resolve_postgresql_connect_args(clean_dsn, cfg)

    if cfg.DB_POOL_MODE.upper() == "NULLPOOL":
        # Serverless scale-to-zero mode: holds 0 idle connections inside Cloud Run containers,
        # delegating connection pooling to Neon's built-in `-pooler` PgBouncer endpoint so
        # Neon compute can auto-suspend to 0 CU when idle.
        engine = create_engine(
            clean_dsn,
            poolclass=NullPool,
            connect_args=pg_connect_args,
            future=True,
        )
    else:
        engine = create_engine(
            clean_dsn,
            pool_pre_ping=True,
            pool_use_lifo=True,
            pool_size=cfg.DB_POOL_SIZE,
            max_overflow=cfg.DB_MAX_OVERFLOW,
            pool_recycle=cfg.DB_POOL_RECYCLE_SECONDS,
            connect_args=pg_connect_args,
            future=True,
        )

    _attach_timezone_listener(engine)
    return engine


def get_engine(settings: AppSettings | None = None) -> Engine:
    """Return singleton SQLAlchemy engine for configured DATABASE_URL."""
    global _engine, _session_factory, _bound_dsn
    cfg = settings or get_settings()
    dsn = (cfg.DATABASE_URL or "").strip()
    if not dsn:
        raise DatabaseUnavailableError(
            "DATABASE_URL is empty. Configure DATABASE_URL via environment or Secret Manager."
        )

    cache_key = f"{dsn}|mode={cfg.DB_POOL_MODE}|ssl={cfg.DB_SSLMODE}"
    if _engine is None or _bound_dsn != cache_key:
        if _engine is not None:
            _engine.dispose()
        _engine = build_engine(dsn, cfg)
        _session_factory = sessionmaker(
            bind=_engine,
            autoflush=False,
            autocommit=False,
            expire_on_commit=False,
            class_=Session,
        )
        _bound_dsn = cache_key
    return _engine


def get_session_factory(settings: AppSettings | None = None) -> sessionmaker[Session]:
    """Return configured SQLAlchemy sessionmaker."""
    get_engine(settings)
    assert _session_factory is not None
    return _session_factory


def reset_engine() -> None:
    """Dispose active engine and reset cached factory (used on graceful shutdown and tests)."""
    global _engine, _session_factory, _bound_dsn
    if _engine is not None:
        _engine.dispose()
    _engine = None
    _session_factory = None
    _bound_dsn = None


def probe_database(settings: AppSettings | None = None) -> dict[str, Any]:
    """Execute a lightweight connectivity & timezone check (with cold-wake retry for Neon)."""
    cfg = settings or get_settings()
    if not (cfg.DATABASE_URL or "").strip():
        return {
            "reachable": False,
            "configured": False,
            "latency_ms": None,
            "dialect": None,
            "db_timezone": None,
            "alembic_version": None,
            "error": "DATABASE_URL is not configured",
        }

    start = time.perf_counter()
    last_exc: Exception | None = None
    max_attempts = 2 if "neon.tech" in cfg.DATABASE_URL.lower() else 1

    for attempt in range(max_attempts):
        try:
            engine = get_engine(cfg)
            dialect = engine.dialect.name
            db_tz = "Asia/Kolkata"
            alembic_rev: str | None = None

            with engine.connect() as conn:
                conn.execute(text("SELECT 1"))
                if dialect == "postgresql":
                    tz_row = conn.execute(text("SHOW TIME ZONE")).scalar()
                    db_tz = str(tz_row) if tz_row else "unknown"
                try:
                    rev_row = conn.execute(
                        text("SELECT version_num FROM alembic_version LIMIT 1")
                    ).scalar()
                    alembic_rev = str(rev_row) if rev_row else None
                except SQLAlchemyError:
                    conn.rollback()
                    alembic_rev = None

            elapsed_ms = Decimal(str(round((time.perf_counter() - start) * 1000.0, 2)))
            return {
                "reachable": True,
                "configured": True,
                "latency_ms": elapsed_ms,
                "dialect": dialect,
                "db_timezone": db_tz,
                "alembic_version": alembic_rev,
                "error": None,
            }
        except Exception as exc:  # noqa: BLE001
            last_exc = exc
            if attempt + 1 < max_attempts:
                time.sleep(1.0)

    elapsed_ms = Decimal(str(round((time.perf_counter() - start) * 1000.0, 2)))
    sanitized_err = re.sub(
        r"(postgres(?:ql)?(?:\+\w+)?://)[^\s]+",
        r"\1<redacted>",
        str(last_exc or "Database connection failed"),
    )
    sanitized_err = re.sub(
        r'server at "[^"]+" \([^)]+\)',
        'server at "<redacted-host>"',
        sanitized_err,
    )
    logger.warning(
        "Database probe failed",
        extra={"error": sanitized_err, "latency_ms": str(elapsed_ms)},
    )
    return {
        "reachable": False,
        "configured": True,
        "latency_ms": elapsed_ms,
        "dialect": None,
        "db_timezone": None,
        "alembic_version": None,
        "error": sanitized_err,
    }
