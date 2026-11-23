"""Phase 1 foundation schema: users, paper_accounts, instruments, strategies, audit & health.

Revision ID: 0001_phase1_foundation
Revises: None
Create Date: 2026-11-18 20:05:00+05:30
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0001_phase1_foundation"
down_revision: str | None = None
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    # 1. users
    op.create_table(
        "users",
        sa.Column("id", sa.Uuid(as_uuid=True), nullable=False),
        sa.Column("username", sa.String(length=64), nullable=False),
        sa.Column("email", sa.String(length=255), nullable=False),
        sa.Column("display_name", sa.String(length=128), nullable=False),
        sa.Column("role", sa.String(length=32), nullable=False),
        sa.Column("is_active", sa.Boolean(), nullable=False, server_default=sa.true()),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("email"),
        sa.UniqueConstraint("username"),
    )
    op.create_index("ix_users_username", "users", ["username"], unique=True)

    # 2. paper_accounts
    op.create_table(
        "paper_accounts",
        sa.Column("id", sa.Uuid(as_uuid=True), nullable=False),
        sa.Column("account_code", sa.String(length=64), nullable=False),
        sa.Column("account_name", sa.String(length=128), nullable=False),
        sa.Column("owner_user_id", sa.Uuid(as_uuid=True), nullable=True),
        sa.Column("is_demo_capital", sa.Boolean(), nullable=False, server_default=sa.true()),
        sa.Column("account_designation", sa.String(length=32), nullable=False),
        sa.Column("currency", sa.String(length=8), nullable=False),
        sa.Column("initial_capital", sa.Numeric(precision=18, scale=4), nullable=False),
        sa.Column("available_capital", sa.Numeric(precision=18, scale=4), nullable=False),
        sa.Column("realized_pnl_today", sa.Numeric(precision=18, scale=4), nullable=False),
        sa.Column("unrealized_pnl", sa.Numeric(precision=18, scale=4), nullable=False),
        sa.Column("paper_trading_halted", sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.Column("halt_reason", sa.Text(), nullable=True),
        sa.Column("halted_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("halted_by", sa.String(length=128), nullable=True),
        sa.Column("status", sa.String(length=32), nullable=False),
        sa.Column("version", sa.Integer(), nullable=False, server_default="1"),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint(
            "initial_capital >= 0", name="ck_paper_accounts_initial_capital_nonneg"
        ),
        sa.CheckConstraint("is_demo_capital = true", name="ck_paper_accounts_demo_only_v1"),
        sa.ForeignKeyConstraint(["owner_user_id"], ["users.id"], ondelete="SET NULL"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("account_code"),
    )
    op.create_index("ix_paper_accounts_account_code", "paper_accounts", ["account_code"], unique=True)

    # 3. instruments
    op.create_table(
        "instruments",
        sa.Column("id", sa.Uuid(as_uuid=True), nullable=False),
        sa.Column("symbol", sa.String(length=32), nullable=False),
        sa.Column("display_name", sa.String(length=128), nullable=False),
        sa.Column("exchange", sa.String(length=32), nullable=False),
        sa.Column("segment", sa.String(length=64), nullable=False),
        sa.Column("instrument_type", sa.String(length=64), nullable=False),
        sa.Column("currency", sa.String(length=8), nullable=False),
        sa.Column("lot_size", sa.Integer(), nullable=False),
        sa.Column("tick_size", sa.Numeric(precision=10, scale=4), nullable=False),
        sa.Column("session_open_ist", sa.String(length=8), nullable=False),
        sa.Column("session_close_ist", sa.String(length=8), nullable=False),
        sa.Column("metadata_json", sa.JSON(), nullable=False),
        sa.Column("is_active", sa.Boolean(), nullable=False, server_default=sa.true()),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint("lot_size >= 1", name="ck_instruments_lot_size_positive"),
        sa.CheckConstraint("tick_size > 0", name="ck_instruments_tick_size_positive"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("symbol"),
    )
    op.create_index("ix_instruments_symbol", "instruments", ["symbol"], unique=True)

    # 4. strategy_definitions
    op.create_table(
        "strategy_definitions",
        sa.Column("id", sa.Uuid(as_uuid=True), nullable=False),
        sa.Column("code", sa.String(length=64), nullable=False),
        sa.Column("name", sa.String(length=128), nullable=False),
        sa.Column("category", sa.String(length=64), nullable=False),
        sa.Column("description", sa.Text(), nullable=False),
        sa.Column("parameter_schema_json", sa.JSON(), nullable=False),
        sa.Column("default_parameters_json", sa.JSON(), nullable=False),
        sa.Column("supported_symbols_json", sa.JSON(), nullable=False),
        sa.Column("is_enabled", sa.Boolean(), nullable=False, server_default=sa.true()),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("code"),
    )
    op.create_index("ix_strategy_definitions_code", "strategy_definitions", ["code"], unique=True)

    # 5. strategy_versions (immutable parameter versions)
    op.create_table(
        "strategy_versions",
        sa.Column("id", sa.Uuid(as_uuid=True), nullable=False),
        sa.Column("strategy_definition_id", sa.Uuid(as_uuid=True), nullable=False),
        sa.Column("version_number", sa.Integer(), nullable=False),
        sa.Column("parameters_json", sa.JSON(), nullable=False),
        sa.Column("parameter_hash", sa.String(length=64), nullable=False),
        sa.Column("notes", sa.Text(), nullable=True),
        sa.Column("created_by", sa.String(length=128), nullable=False),
        sa.Column("status", sa.String(length=32), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(
            ["strategy_definition_id"], ["strategy_definitions.id"], ondelete="RESTRICT"
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "strategy_definition_id", "version_number", name="uq_strategy_versions_def_ver"
        ),
    )
    op.create_index(
        "ix_strategy_versions_strategy_definition_id",
        "strategy_versions",
        ["strategy_definition_id"],
        unique=False,
    )
    op.create_index(
        "ix_strategy_versions_parameter_hash",
        "strategy_versions",
        ["parameter_hash"],
        unique=False,
    )

    # 6. audit_events (append-only)
    op.create_table(
        "audit_events",
        sa.Column("id", sa.Uuid(as_uuid=True), nullable=False),
        sa.Column("occurred_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("correlation_id", sa.String(length=64), nullable=False),
        sa.Column("actor", sa.String(length=128), nullable=False),
        sa.Column("actor_type", sa.String(length=32), nullable=False),
        sa.Column("category", sa.String(length=32), nullable=False),
        sa.Column("action", sa.String(length=128), nullable=False),
        sa.Column("severity", sa.String(length=32), nullable=False),
        sa.Column("entity_type", sa.String(length=64), nullable=True),
        sa.Column("entity_id", sa.String(length=128), nullable=True),
        sa.Column("summary", sa.Text(), nullable=False),
        sa.Column("details_json", sa.JSON(), nullable=False),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_audit_events_occurred_at_desc", "audit_events", ["occurred_at"], unique=False)
    op.create_index("ix_audit_events_correlation_id", "audit_events", ["correlation_id"], unique=False)
    op.create_index("ix_audit_events_action", "audit_events", ["action"], unique=False)
    op.create_index("ix_audit_events_cat_sev", "audit_events", ["category", "severity"], unique=False)
    op.create_index("ix_audit_events_entity", "audit_events", ["entity_type", "entity_id"], unique=False)

    # 7. system_health_events
    op.create_table(
        "system_health_events",
        sa.Column("id", sa.Uuid(as_uuid=True), nullable=False),
        sa.Column("recorded_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("component", sa.String(length=32), nullable=False),
        sa.Column("status", sa.String(length=32), nullable=False),
        sa.Column("latency_ms", sa.Numeric(precision=10, scale=2), nullable=True),
        sa.Column("message", sa.Text(), nullable=False),
        sa.Column("details_json", sa.JSON(), nullable=False),
        sa.Column("correlation_id", sa.String(length=64), nullable=True),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(
        "ix_system_health_events_recorded_at", "system_health_events", ["recorded_at"], unique=False
    )
    op.create_index(
        "ix_system_health_events_comp_time",
        "system_health_events",
        ["component", "recorded_at"],
        unique=False,
    )
    op.create_index(
        "ix_system_health_events_correlation_id",
        "system_health_events",
        ["correlation_id"],
        unique=False,
    )

    # PostgreSQL database-level immutability triggers for audit_events & strategy_versions
    bind = op.get_bind()
    if bind.dialect.name == "postgresql":
        op.execute(
            """
            CREATE OR REPLACE FUNCTION forbid_audit_event_mutation()
            RETURNS TRIGGER AS $$
            BEGIN
                RAISE EXCEPTION 'audit_events table is append-only; UPDATE and DELETE are prohibited.';
            END;
            $$ LANGUAGE plpgsql;

            CREATE TRIGGER trg_audit_events_immutable
            BEFORE UPDATE OR DELETE ON audit_events
            FOR EACH ROW EXECUTE FUNCTION forbid_audit_event_mutation();

            CREATE OR REPLACE FUNCTION enforce_strategy_version_immutability()
            RETURNS TRIGGER AS $$
            BEGIN
                IF TG_OP = 'DELETE' THEN
                    RAISE EXCEPTION 'strategy_versions cannot be deleted; mark status as RETIRED.';
                END IF;
                IF NEW.parameter_hash IS DISTINCT FROM OLD.parameter_hash
                   OR NEW.version_number IS DISTINCT FROM OLD.version_number
                   OR NEW.strategy_definition_id IS DISTINCT FROM OLD.strategy_definition_id THEN
                    RAISE EXCEPTION 'strategy_versions parameters and lineage are immutable.';
                END IF;
                RETURN NEW;
            END;
            $$ LANGUAGE plpgsql;

            CREATE TRIGGER trg_strategy_versions_immutable
            BEFORE UPDATE OR DELETE ON strategy_versions
            FOR EACH ROW EXECUTE FUNCTION enforce_strategy_version_immutability();
            """
        )


def downgrade() -> None:
    bind = op.get_bind()
    if bind.dialect.name == "postgresql":
        op.execute(
            """
            DROP TRIGGER IF EXISTS trg_strategy_versions_immutable ON strategy_versions;
            DROP FUNCTION IF EXISTS enforce_strategy_version_immutability();
            DROP TRIGGER IF EXISTS trg_audit_events_immutable ON audit_events;
            DROP FUNCTION IF EXISTS forbid_audit_event_mutation();
            """
        )

    op.drop_table("system_health_events")
    op.drop_table("audit_events")
    op.drop_table("strategy_versions")
    op.drop_table("strategy_definitions")
    op.drop_table("instruments")
    op.drop_table("paper_accounts")
    op.drop_table("users")
