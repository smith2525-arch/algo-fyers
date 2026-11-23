"""Alembic migration environment reading DATABASE_URL from AppSettings."""

from __future__ import annotations

from logging.config import fileConfig

from alembic import context
from sqlalchemy import text

from core.config import get_settings
from core.persistence.db import build_engine
from core.persistence.models import Base

config = context.config

if config.config_file_name is not None:
    fileConfig(config.config_file_name)

target_metadata = Base.metadata


def _resolve_database_url() -> str:
    override = config.get_main_option("sqlalchemy.url")
    if override:
        return override
    settings = get_settings()
    if not settings.DATABASE_URL:
        raise RuntimeError(
            "DATABASE_URL is not set. Export DATABASE_URL or configure .env before running Alembic."
        )
    return settings.DATABASE_URL


def run_migrations_offline() -> None:
    """Run migrations in 'offline' mode (SQL script emission)."""
    url = _resolve_database_url()
    context.configure(
        url=url,
        target_metadata=target_metadata,
        literal_binds=True,
        dialect_opts={"paramstyle": "named"},
        compare_type=True,
        output_buffer=config.output_buffer,
    )

    with context.begin_transaction():
        context.run_migrations()


def run_migrations_online() -> None:
    """Run migrations in 'online' mode with Asia/Kolkata session timezone."""
    url = _resolve_database_url()
    connectable = build_engine(url)

    with connectable.connect() as connection:
        if connection.dialect.name == "postgresql":
            connection.execute(text("SET TIME ZONE 'Asia/Kolkata'"))
        context.configure(
            connection=connection,
            target_metadata=target_metadata,
            compare_type=True,
        )

        with context.begin_transaction():
            context.run_migrations()

    connectable.dispose()


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()
