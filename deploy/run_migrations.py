"""Controlled Database Migration & Seed Runner (`deploy/run_migrations.py`).

Executes Alembic schema migrations (`upgrade head`) outside application instance
startup so multiple Cloud Run containers never race DDL changes against Neon PostgreSQL.

Features:
- Acquires a PostgreSQL session advisory lock (`pg_advisory_lock`) when connected
  to PostgreSQL / Neon (use Neon's DIRECT connection string for DDL migrations).
- Optionally seeds initial idempotent reference data (`--seed`: NIFTY, BANKNIFTY,
  DEMO paper account, and v1 strategies) and/or deterministic demo showcase (`--demo-showcase`).

Usage:
    python -m deploy.run_migrations --seed
    python -m deploy.run_migrations --seed --demo-showcase
"""

from __future__ import annotations

import argparse
import logging
from pathlib import Path

from alembic import command
from alembic.config import Config
from sqlalchemy import text

from core.config import get_settings
from core.demo.service import GuidedDemoService
from core.logging import configure_logging
from core.persistence.db import build_engine, reset_engine
from core.persistence.seed import seed_foundation_data
from core.persistence.uow import unit_of_work

PROJECT_ROOT = Path(__file__).resolve().parent.parent
MIGRATION_ADVISORY_LOCK_KEY = 84729104

logger = logging.getLogger("deploy.run_migrations")


def execute_controlled_migrations(
    *,
    revision: str = "head",
    seed_reference: bool = False,
    run_demo_showcase: bool = False,
) -> str:
    """Run Alembic migrations under a PostgreSQL advisory lock and return current head revision."""
    cfg = get_settings()
    configure_logging(
        level=cfg.LOG_LEVEL,
        json_logs=cfg.LOG_JSON,
        service_name="atcc-migration-runner",
    )
    if not (cfg.DATABASE_URL or "").strip():
        raise RuntimeError(
            "DATABASE_URL is required to run controlled migrations (never hardcode credentials)."
        )

    alembic_cfg = Config(str(PROJECT_ROOT / "alembic.ini"))
    alembic_cfg.set_main_option("script_location", str(PROJECT_ROOT / "migrations"))
    alembic_cfg.set_main_option("sqlalchemy.url", cfg.DATABASE_URL)

    engine = build_engine(cfg.DATABASE_URL, cfg)
    is_postgres = engine.dialect.name == "postgresql"

    logger.info(
        "Starting controlled schema migration",
        extra={"target_revision": revision, "database": cfg.masked_database_url},
    )

    try:
        if is_postgres:
            with engine.connect() as lock_conn:
                lock_conn.execute(
                    text("SELECT pg_advisory_lock(:lock_id)"),
                    {"lock_id": MIGRATION_ADVISORY_LOCK_KEY},
                )
                lock_conn.commit()
                try:
                    command.upgrade(alembic_cfg, revision)
                finally:
                    lock_conn.execute(
                        text("SELECT pg_advisory_unlock(:lock_id)"),
                        {"lock_id": MIGRATION_ADVISORY_LOCK_KEY},
                    )
                    lock_conn.commit()
        else:
            command.upgrade(alembic_cfg, revision)

        with engine.connect() as verify_conn:
            applied_rev = str(
                verify_conn.execute(
                    text("SELECT version_num FROM alembic_version LIMIT 1")
                ).scalar()
                or "unknown"
            )
    finally:
        engine.dispose()
        reset_engine()

    if seed_reference:
        with unit_of_work(settings=cfg) as session:
            seed_foundation_data(session, cfg)
        logger.info("Verified idempotent reference data seed (NIFTY, BANKNIFTY, DEMO account, v1 strategies).")

    if run_demo_showcase:
        with unit_of_work(settings=cfg) as session:
            GuidedDemoService.run_full_demo_showcase(
                session, actor="deploy.migration_runner", settings=cfg
            )
        logger.info("Populated deterministic 17-step consulting demo showcase.")

    reset_engine()
    logger.info("Controlled migration completed cleanly", extra={"alembic_version": applied_rev})
    return applied_rev


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Controlled Alembic migration & reference-seed runner for Neon PostgreSQL."
    )
    parser.add_argument(
        "--revision",
        default="head",
        help="Target Alembic revision (default: head).",
    )
    parser.add_argument(
        "--seed",
        action="store_true",
        help="Idempotently seed NIFTY/BANKNIFTY instruments, DEMO paper account, and v1 strategies.",
    )
    parser.add_argument(
        "--demo-showcase",
        action="store_true",
        help="Also populate the deterministic 17-step consulting demo showcase.",
    )
    args = parser.parse_args()
    rev = execute_controlled_migrations(
        revision=args.revision,
        seed_reference=bool(args.seed),
        run_demo_showcase=bool(args.demo_showcase),
    )
    print(f"MIGRATION_OK alembic_version={rev}")


if __name__ == "__main__":
    main()
