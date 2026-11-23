"""Phase 4 Risk Engine & Safety Controls schema: account_risk_configs,
strategy_risk_configs, and immutable risk_decisions ledger.

Revision ID: 0006_phase4_risk_engine_and_controls
Revises: 0005_phase3b_paper_trading_oms
Create Date: 2026-11-20 18:45:00+05:30
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0006_phase4_risk_engine_and_controls"
down_revision: str | None = "0005_phase3b_paper_trading_oms"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    bind = op.get_bind()

    # 1. account_risk_configs
    op.create_table(
        "account_risk_configs",
        sa.Column("id", sa.Uuid(as_uuid=True), nullable=False),
        sa.Column("account_id", sa.Uuid(as_uuid=True), nullable=False),
        sa.Column("is_demo_settings", sa.Boolean(), nullable=False, server_default=sa.text("true")),
        sa.Column(
            "max_daily_loss_inr",
            sa.Numeric(precision=18, scale=4),
            nullable=False,
            server_default="10000.00",
        ),
        sa.Column("max_open_positions", sa.Integer(), nullable=False, server_default="4"),
        sa.Column(
            "max_gross_exposure_inr",
            sa.Numeric(precision=18, scale=4),
            nullable=False,
            server_default="6000000.00",
        ),
        sa.Column(
            "max_order_value_inr",
            sa.Numeric(precision=18, scale=4),
            nullable=False,
            server_default="3000000.00",
        ),
        sa.Column("max_trades_per_day", sa.Integer(), nullable=False, server_default="15"),
        sa.Column("max_consecutive_losses", sa.Integer(), nullable=False, server_default="3"),
        sa.Column(
            "trading_session_enabled", sa.Boolean(), nullable=False, server_default=sa.text("true")
        ),
        sa.Column(
            "session_start_ist", sa.String(length=16), nullable=False, server_default="09:15:00"
        ),
        sa.Column(
            "session_end_ist", sa.String(length=16), nullable=False, server_default="15:30:00"
        ),
        sa.Column(
            "global_kill_switch_active",
            sa.Boolean(),
            nullable=False,
            server_default=sa.text("false"),
        ),
        sa.Column("kill_switch_reason", sa.Text(), nullable=True),
        sa.Column("kill_switch_activated_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("kill_switch_activated_by", sa.String(length=128), nullable=True),
        sa.Column(
            "market_data_stale_threshold_seconds",
            sa.Integer(),
            nullable=False,
            server_default="10",
        ),
        sa.Column("max_signal_age_seconds", sa.Integer(), nullable=False, server_default="15"),
        sa.Column(
            "enforce_system_health_gate",
            sa.Boolean(),
            nullable=False,
            server_default=sa.text("true"),
        ),
        sa.Column(
            "simulated_system_degraded",
            sa.Boolean(),
            nullable=False,
            server_default=sa.text("false"),
        ),
        sa.Column(
            "enforce_position_reconciliation_gate",
            sa.Boolean(),
            nullable=False,
            server_default=sa.text("true"),
        ),
        sa.Column("default_max_quantity", sa.Integer(), nullable=False, server_default="300"),
        sa.Column(
            "default_max_risk_per_trade_inr",
            sa.Numeric(precision=18, scale=4),
            nullable=False,
            server_default="2500.00",
        ),
        sa.Column(
            "default_strategy_daily_loss_inr",
            sa.Numeric(precision=18, scale=4),
            nullable=False,
            server_default="6000.00",
        ),
        sa.Column(
            "default_max_strategy_drawdown_inr",
            sa.Numeric(precision=18, scale=4),
            nullable=False,
            server_default="8000.00",
        ),
        sa.Column(
            "default_max_trades_per_strategy_day",
            sa.Integer(),
            nullable=False,
            server_default="5",
        ),
        sa.Column(
            "default_cooldown_seconds_after_loss",
            sa.Integer(),
            nullable=False,
            server_default="300",
        ),
        sa.Column(
            "default_max_holding_duration_seconds",
            sa.Integer(),
            nullable=False,
            server_default="14400",
        ),
        sa.Column("default_allowed_instruments_json", sa.JSON(), nullable=False),
        sa.Column("default_allowed_order_types_json", sa.JSON(), nullable=False),
        sa.Column("updated_by", sa.String(length=128), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(["account_id"], ["paper_accounts.id"], ondelete="RESTRICT"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("account_id", name="uq_account_risk_configs_account_id"),
    )
    op.create_index(
        "ix_account_risk_configs_account_id", "account_risk_configs", ["account_id"], unique=True
    )
    op.create_index(
        "ix_account_risk_configs_global_kill_switch_active",
        "account_risk_configs",
        ["global_kill_switch_active"],
        unique=False,
    )

    # 2. strategy_risk_configs
    op.create_table(
        "strategy_risk_configs",
        sa.Column("id", sa.Uuid(as_uuid=True), nullable=False),
        sa.Column("account_id", sa.Uuid(as_uuid=True), nullable=False),
        sa.Column("strategy_version_id", sa.Uuid(as_uuid=True), nullable=False),
        sa.Column("strategy_version_label", sa.String(length=96), nullable=False),
        sa.Column("max_quantity", sa.Integer(), nullable=False, server_default="300"),
        sa.Column(
            "max_risk_per_trade_inr",
            sa.Numeric(precision=18, scale=4),
            nullable=False,
            server_default="2500.00",
        ),
        sa.Column(
            "strategy_daily_loss_inr",
            sa.Numeric(precision=18, scale=4),
            nullable=False,
            server_default="6000.00",
        ),
        sa.Column(
            "max_strategy_drawdown_inr",
            sa.Numeric(precision=18, scale=4),
            nullable=False,
            server_default="8000.00",
        ),
        sa.Column("max_trades_per_day", sa.Integer(), nullable=False, server_default="5"),
        sa.Column("cooldown_seconds_after_loss", sa.Integer(), nullable=False, server_default="300"),
        sa.Column(
            "max_holding_duration_seconds", sa.Integer(), nullable=False, server_default="14400"
        ),
        sa.Column("allowed_instruments_json", sa.JSON(), nullable=False),
        sa.Column("allowed_order_types_json", sa.JSON(), nullable=False),
        sa.Column("is_enabled", sa.Boolean(), nullable=False, server_default=sa.text("true")),
        sa.Column("updated_by", sa.String(length=128), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(["account_id"], ["paper_accounts.id"], ondelete="RESTRICT"),
        sa.ForeignKeyConstraint(
            ["strategy_version_id"], ["strategy_versions.id"], ondelete="RESTRICT"
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "account_id", "strategy_version_id", name="uq_strategy_risk_configs_acct_ver"
        ),
    )
    op.create_index(
        "ix_strategy_risk_configs_account_id",
        "strategy_risk_configs",
        ["account_id"],
        unique=False,
    )
    op.create_index(
        "ix_strategy_risk_configs_strategy_version_id",
        "strategy_risk_configs",
        ["strategy_version_id"],
        unique=False,
    )

    # 3. risk_decisions (append-only immutable risk decision audit ledger)
    op.create_table(
        "risk_decisions",
        sa.Column("id", sa.Uuid(as_uuid=True), nullable=False),
        sa.Column("decision_code", sa.String(length=64), nullable=False),
        sa.Column("decision", sa.String(length=16), nullable=False),
        sa.Column("primary_rule", sa.String(length=64), nullable=False),
        sa.Column("current_value", sa.String(length=128), nullable=False),
        sa.Column("limit_value", sa.String(length=128), nullable=False),
        sa.Column("reason", sa.Text(), nullable=False),
        sa.Column("account_id", sa.Uuid(as_uuid=True), nullable=False),
        sa.Column("order_id", sa.Uuid(as_uuid=True), nullable=True),
        sa.Column("client_order_key", sa.String(length=160), nullable=True),
        sa.Column("signal_id", sa.Uuid(as_uuid=True), nullable=True),
        sa.Column("strategy_instance_id", sa.Uuid(as_uuid=True), nullable=True),
        sa.Column("strategy_version_id", sa.Uuid(as_uuid=True), nullable=True),
        sa.Column("strategy_label", sa.String(length=96), nullable=False),
        sa.Column("instrument_symbol", sa.String(length=32), nullable=False),
        sa.Column("side", sa.String(length=16), nullable=False),
        sa.Column("order_type", sa.String(length=16), nullable=False),
        sa.Column("proposed_quantity", sa.Integer(), nullable=False),
        sa.Column("proposed_price", sa.Numeric(precision=18, scale=4), nullable=True),
        sa.Column(
            "proposed_notional_inr",
            sa.Numeric(precision=18, scale=4),
            nullable=False,
            server_default="0",
        ),
        sa.Column("checks_json", sa.JSON(), nullable=False),
        sa.Column("correlation_id", sa.String(length=64), nullable=False),
        sa.Column("evaluated_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(["account_id"], ["paper_accounts.id"], ondelete="RESTRICT"),
        sa.ForeignKeyConstraint(["order_id"], ["paper_orders.id"], ondelete="RESTRICT"),
        sa.ForeignKeyConstraint(["signal_id"], ["signals.id"], ondelete="RESTRICT"),
        sa.ForeignKeyConstraint(
            ["strategy_instance_id"], ["strategy_instances.id"], ondelete="RESTRICT"
        ),
        sa.ForeignKeyConstraint(
            ["strategy_version_id"], ["strategy_versions.id"], ondelete="RESTRICT"
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("decision_code"),
    )
    op.create_index(
        "ix_risk_decisions_decision_code", "risk_decisions", ["decision_code"], unique=True
    )
    op.create_index("ix_risk_decisions_decision", "risk_decisions", ["decision"], unique=False)
    op.create_index(
        "ix_risk_decisions_primary_rule", "risk_decisions", ["primary_rule"], unique=False
    )
    op.create_index(
        "ix_risk_decisions_account_id", "risk_decisions", ["account_id"], unique=False
    )
    op.create_index("ix_risk_decisions_order_id", "risk_decisions", ["order_id"], unique=False)
    op.create_index(
        "ix_risk_decisions_client_order_key",
        "risk_decisions",
        ["client_order_key"],
        unique=False,
    )
    op.create_index("ix_risk_decisions_signal_id", "risk_decisions", ["signal_id"], unique=False)
    op.create_index(
        "ix_risk_decisions_instrument_symbol",
        "risk_decisions",
        ["instrument_symbol"],
        unique=False,
    )
    op.create_index(
        "ix_risk_decisions_correlation_id", "risk_decisions", ["correlation_id"], unique=False
    )
    op.create_index(
        "ix_risk_decisions_evaluated_at", "risk_decisions", ["evaluated_at"], unique=False
    )
    op.create_index(
        "ix_risk_decisions_evaluated_desc", "risk_decisions", ["evaluated_at"], unique=False
    )
    op.create_index(
        "ix_risk_decisions_decision_rule",
        "risk_decisions",
        ["decision", "primary_rule"],
        unique=False,
    )

    if bind.dialect.name == "postgresql":
        op.execute(
            """
            CREATE OR REPLACE FUNCTION enforce_risk_decision_immutability()
            RETURNS TRIGGER AS $$
            BEGIN
                RAISE EXCEPTION 'RiskDecision records are append-only audit artifacts and cannot be modified or deleted.';
            END;
            $$ LANGUAGE plpgsql;

            CREATE TRIGGER trg_risk_decisions_immutable
            BEFORE UPDATE OR DELETE ON risk_decisions
            FOR EACH ROW EXECUTE FUNCTION enforce_risk_decision_immutability();
            """
        )


def downgrade() -> None:
    bind = op.get_bind()
    if bind.dialect.name == "postgresql":
        op.execute(
            """
            DROP TRIGGER IF EXISTS trg_risk_decisions_immutable ON risk_decisions;
            DROP FUNCTION IF EXISTS enforce_risk_decision_immutability();
            """
        )
    op.drop_table("risk_decisions")
    op.drop_table("strategy_risk_configs")
    op.drop_table("account_risk_configs")
