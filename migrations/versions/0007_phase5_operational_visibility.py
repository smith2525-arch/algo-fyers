"""Phase 5 Operational Visibility schema: structured trace columns on audit_events
and immutable reconciliation_runs table.

Revision ID: 0007_phase5_operational_visibility
Revises: 0006_phase4_risk_engine_and_controls
Create Date: 2026-11-21 12:15:00+05:30
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0007_phase5_operational_visibility"
down_revision: str | None = "0006_phase4_risk_engine_and_controls"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    bind = op.get_bind()

    # 1. Extend audit_events with structured correlation & trace linkage columns
    op.add_column("audit_events", sa.Column("event_type", sa.String(length=128), nullable=True))
    op.add_column("audit_events", sa.Column("strategy_id", sa.String(length=64), nullable=True))
    op.add_column("audit_events", sa.Column("signal_id", sa.String(length=64), nullable=True))
    op.add_column("audit_events", sa.Column("order_id", sa.String(length=64), nullable=True))
    op.add_column("audit_events", sa.Column("source", sa.String(length=64), nullable=True))

    op.create_index("ix_audit_events_event_type", "audit_events", ["event_type"], unique=False)
    op.create_index("ix_audit_events_strategy_id", "audit_events", ["strategy_id"], unique=False)
    op.create_index("ix_audit_events_signal_id", "audit_events", ["signal_id"], unique=False)
    op.create_index("ix_audit_events_order_id", "audit_events", ["order_id"], unique=False)

    # 2. reconciliation_runs (append-only immutable operational reconciliation history)
    op.create_table(
        "reconciliation_runs",
        sa.Column("id", sa.Uuid(as_uuid=True), nullable=False),
        sa.Column("run_code", sa.String(length=64), nullable=False),
        sa.Column("account_id", sa.Uuid(as_uuid=True), nullable=True),
        sa.Column("status", sa.String(length=32), nullable=False),
        sa.Column("repair_applied", sa.Boolean(), nullable=False, server_default=sa.text("false")),
        sa.Column("fills_checked", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("open_orders_checked", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("positions_checked", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("discrepancies_count", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("repairs_count", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("checks_json", sa.JSON(), nullable=False),
        sa.Column("discrepancies_json", sa.JSON(), nullable=False),
        sa.Column("repairs_json", sa.JSON(), nullable=False),
        sa.Column("actor", sa.String(length=128), nullable=False),
        sa.Column("correlation_id", sa.String(length=64), nullable=False),
        sa.Column("started_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("completed_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(["account_id"], ["paper_accounts.id"], ondelete="RESTRICT"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("run_code"),
    )
    op.create_index(
        "ix_reconciliation_runs_run_code", "reconciliation_runs", ["run_code"], unique=True
    )
    op.create_index(
        "ix_reconciliation_runs_account_id", "reconciliation_runs", ["account_id"], unique=False
    )
    op.create_index(
        "ix_reconciliation_runs_status", "reconciliation_runs", ["status"], unique=False
    )
    op.create_index(
        "ix_reconciliation_runs_correlation_id",
        "reconciliation_runs",
        ["correlation_id"],
        unique=False,
    )
    op.create_index(
        "ix_reconciliation_runs_completed_at",
        "reconciliation_runs",
        ["completed_at"],
        unique=False,
    )
    op.create_index(
        "ix_reconciliation_runs_completed_desc",
        "reconciliation_runs",
        ["completed_at"],
        unique=False,
    )

    if bind.dialect.name == "postgresql":
        op.execute(
            """
            CREATE OR REPLACE FUNCTION enforce_reconciliation_run_immutability()
            RETURNS TRIGGER AS $$
            BEGIN
                RAISE EXCEPTION 'ReconciliationRun records are append-only operational records and cannot be modified or deleted.';
            END;
            $$ LANGUAGE plpgsql;

            CREATE TRIGGER trg_reconciliation_runs_immutable
            BEFORE UPDATE OR DELETE ON reconciliation_runs
            FOR EACH ROW EXECUTE FUNCTION enforce_reconciliation_run_immutability();
            """
        )


def downgrade() -> None:
    bind = op.get_bind()
    if bind.dialect.name == "postgresql":
        op.execute(
            """
            DROP TRIGGER IF EXISTS trg_reconciliation_runs_immutable ON reconciliation_runs;
            DROP FUNCTION IF EXISTS enforce_reconciliation_run_immutability();
            """
        )
    op.drop_table("reconciliation_runs")

    if bind.dialect.name == "postgresql":
        op.drop_index("ix_audit_events_order_id", table_name="audit_events")
        op.drop_index("ix_audit_events_signal_id", table_name="audit_events")
        op.drop_index("ix_audit_events_strategy_id", table_name="audit_events")
        op.drop_index("ix_audit_events_event_type", table_name="audit_events")
        op.drop_column("audit_events", "source")
        op.drop_column("audit_events", "order_id")
        op.drop_column("audit_events", "signal_id")
        op.drop_column("audit_events", "strategy_id")
        op.drop_column("audit_events", "event_type")
    else:
        with op.batch_alter_table("audit_events") as batch_op:
            batch_op.drop_index("ix_audit_events_order_id")
            batch_op.drop_index("ix_audit_events_signal_id")
            batch_op.drop_index("ix_audit_events_strategy_id")
            batch_op.drop_index("ix_audit_events_event_type")
            batch_op.drop_column("source")
            batch_op.drop_column("order_id")
            batch_op.drop_column("signal_id")
            batch_op.drop_column("strategy_id")
            batch_op.drop_column("event_type")
