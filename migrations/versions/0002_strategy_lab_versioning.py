"""Phase 2A Strategy Lab versioning enhancements: canonical version labels & clone lineage.

Revision ID: 0002_strategy_lab_versioning
Revises: 0001_phase1_foundation
Create Date: 2026-11-18 21:05:00+05:30
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0002_strategy_lab_versioning"
down_revision: str | None = "0001_phase1_foundation"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    bind = op.get_bind()

    # Temporarily drop PostgreSQL immutability trigger during schema migration & backfill
    if bind.dialect.name == "postgresql":
        op.execute("DROP TRIGGER IF EXISTS trg_strategy_versions_immutable ON strategy_versions;")

    with op.batch_alter_table("strategy_versions") as batch_op:
        batch_op.add_column(
            sa.Column(
                "version_label",
                sa.String(length=96),
                nullable=False,
                server_default="PENDING_LABEL",
            )
        )
        batch_op.add_column(
            sa.Column(
                "instrument_symbol",
                sa.String(length=32),
                nullable=False,
                server_default="NIFTY",
            )
        )
        batch_op.add_column(
            sa.Column(
                "timeframe",
                sa.String(length=16),
                nullable=False,
                server_default="5m",
            )
        )
        batch_op.add_column(
            sa.Column("cloned_from_version_id", sa.Uuid(as_uuid=True), nullable=True)
        )

    # Backfill any existing rows with deterministic label NIFTY_EMA_RSI_v{version_number}
    op.execute(
        "UPDATE strategy_versions "
        "SET version_label = 'NIFTY_EMA_RSI_v' || CAST(version_number AS VARCHAR(16)), "
        "    instrument_symbol = 'NIFTY', "
        "    timeframe = '5m' "
        "WHERE version_label = 'PENDING_LABEL'"
    )

    with op.batch_alter_table("strategy_versions") as batch_op:
        batch_op.alter_column("version_label", server_default=None)
        batch_op.alter_column("instrument_symbol", server_default=None)
        batch_op.drop_constraint("uq_strategy_versions_def_ver", type_="unique")
        batch_op.create_unique_constraint(
            "uq_strategy_versions_def_inst_ver",
            ["strategy_definition_id", "instrument_symbol", "version_number"],
        )
        batch_op.create_unique_constraint("uq_strategy_versions_label", ["version_label"])
        batch_op.create_index(
            "ix_strategy_versions_version_label", ["version_label"], unique=True
        )
        batch_op.create_index(
            "ix_strategy_versions_instrument_symbol", ["instrument_symbol"], unique=False
        )
        batch_op.create_foreign_key(
            "fk_strategy_versions_cloned_from",
            "strategy_versions",
            ["cloned_from_version_id"],
            ["id"],
            ondelete="RESTRICT",
        )

    if bind.dialect.name == "postgresql":
        op.execute(
            """
            CREATE OR REPLACE FUNCTION enforce_strategy_version_immutability()
            RETURNS TRIGGER AS $$
            BEGIN
                IF TG_OP = 'DELETE' THEN
                    RAISE EXCEPTION 'strategy_versions cannot be deleted; mark status as RETIRED.';
                END IF;
                IF NEW.parameter_hash IS DISTINCT FROM OLD.parameter_hash
                   OR NEW.version_number IS DISTINCT FROM OLD.version_number
                   OR NEW.version_label IS DISTINCT FROM OLD.version_label
                   OR NEW.instrument_symbol IS DISTINCT FROM OLD.instrument_symbol
                   OR NEW.timeframe IS DISTINCT FROM OLD.timeframe
                   OR NEW.cloned_from_version_id IS DISTINCT FROM OLD.cloned_from_version_id
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
        op.execute("DROP TRIGGER IF EXISTS trg_strategy_versions_immutable ON strategy_versions;")

    with op.batch_alter_table("strategy_versions") as batch_op:
        batch_op.drop_constraint("fk_strategy_versions_cloned_from", type_="foreignkey")
        batch_op.drop_index("ix_strategy_versions_instrument_symbol")
        batch_op.drop_index("ix_strategy_versions_version_label")
        batch_op.drop_constraint("uq_strategy_versions_label", type_="unique")
        batch_op.drop_constraint("uq_strategy_versions_def_inst_ver", type_="unique")
        batch_op.drop_column("cloned_from_version_id")
        batch_op.drop_column("timeframe")
        batch_op.drop_column("instrument_symbol")
        batch_op.drop_column("version_label")
        batch_op.create_unique_constraint(
            "uq_strategy_versions_def_ver",
            ["strategy_definition_id", "version_number"],
        )

    if bind.dialect.name == "postgresql":
        op.execute(
            """
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
