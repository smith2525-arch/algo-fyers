"""Phase 3B Paper Trading & OMS schema: strategy_instances, paper_orders,
order_state_events, paper_fills, paper_positions, position_events, and signal linkage.

Revision ID: 0005_phase3b_paper_trading_oms
Revises: 0004_phase3a_market_replay
Create Date: 2026-11-20 17:10:00+05:30
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0005_phase3b_paper_trading_oms"
down_revision: str | None = "0004_phase3a_market_replay"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    bind = op.get_bind()

    # 1. strategy_instances
    op.create_table(
        "strategy_instances",
        sa.Column("id", sa.Uuid(as_uuid=True), nullable=False),
        sa.Column("instance_code", sa.String(length=64), nullable=False),
        sa.Column("account_id", sa.Uuid(as_uuid=True), nullable=False),
        sa.Column("strategy_version_id", sa.Uuid(as_uuid=True), nullable=False),
        sa.Column("strategy_version_label", sa.String(length=96), nullable=False),
        sa.Column("instrument_symbol", sa.String(length=32), nullable=False),
        sa.Column("timeframe", sa.String(length=16), nullable=False, server_default="5m"),
        sa.Column("status", sa.String(length=32), nullable=False),
        sa.Column("order_type", sa.String(length=16), nullable=False, server_default="MARKET"),
        sa.Column(
            "limit_offset_pts", sa.Numeric(precision=18, scale=4), nullable=False, server_default="0"
        ),
        sa.Column("execution_latency_ms", sa.Integer(), nullable=False, server_default="120"),
        sa.Column("latency_bars", sa.Integer(), nullable=False, server_default="1"),
        sa.Column(
            "slippage_bps", sa.Numeric(precision=10, scale=4), nullable=False, server_default="2.0"
        ),
        sa.Column(
            "partial_fill_enabled", sa.Boolean(), nullable=False, server_default=sa.text("false")
        ),
        sa.Column(
            "max_fill_ratio_per_bar",
            sa.Numeric(precision=6, scale=4),
            nullable=False,
            server_default="1.0",
        ),
        sa.Column("valid_for_bars", sa.Integer(), nullable=True),
        sa.Column(
            "auto_execute_signals", sa.Boolean(), nullable=False, server_default=sa.text("true")
        ),
        sa.Column("signals_emitted", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("orders_created", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("created_by", sa.String(length=128), nullable=False),
        sa.Column("started_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("paused_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("stopped_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(["account_id"], ["paper_accounts.id"], ondelete="RESTRICT"),
        sa.ForeignKeyConstraint(
            ["strategy_version_id"], ["strategy_versions.id"], ondelete="RESTRICT"
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("instance_code"),
    )
    op.create_index(
        "ix_strategy_instances_instance_code", "strategy_instances", ["instance_code"], unique=True
    )
    op.create_index(
        "ix_strategy_instances_account_id", "strategy_instances", ["account_id"], unique=False
    )
    op.create_index(
        "ix_strategy_instances_strategy_version_id",
        "strategy_instances",
        ["strategy_version_id"],
        unique=False,
    )
    op.create_index(
        "ix_strategy_instances_instrument_symbol",
        "strategy_instances",
        ["instrument_symbol"],
        unique=False,
    )
    op.create_index("ix_strategy_instances_status", "strategy_instances", ["status"], unique=False)
    op.create_index(
        "ix_strategy_instances_inst_status",
        "strategy_instances",
        ["instrument_symbol", "status"],
        unique=False,
    )

    # 2. Extend signals with strategy_instance_id and nullable replay_session_id
    if bind.dialect.name == "postgresql":
        op.alter_column(
            "signals", "replay_session_id", existing_type=sa.Uuid(as_uuid=True), nullable=True
        )
        op.add_column(
            "signals", sa.Column("strategy_instance_id", sa.Uuid(as_uuid=True), nullable=True)
        )
        op.create_index(
            "ix_signals_strategy_instance_id", "signals", ["strategy_instance_id"], unique=False
        )
        op.create_foreign_key(
            "fk_signals_strategy_instance_id",
            "signals",
            "strategy_instances",
            ["strategy_instance_id"],
            ["id"],
            ondelete="RESTRICT",
        )
    else:
        with op.batch_alter_table("signals") as batch_op:
            batch_op.alter_column(
                "replay_session_id", existing_type=sa.Uuid(as_uuid=True), nullable=True
            )
            batch_op.add_column(
                sa.Column("strategy_instance_id", sa.Uuid(as_uuid=True), nullable=True)
            )
            batch_op.create_index(
                "ix_signals_strategy_instance_id", ["strategy_instance_id"], unique=False
            )
            batch_op.create_foreign_key(
                "fk_signals_strategy_instance_id",
                "strategy_instances",
                ["strategy_instance_id"],
                ["id"],
                ondelete="RESTRICT",
            )

    # 3. paper_orders
    op.create_table(
        "paper_orders",
        sa.Column("id", sa.Uuid(as_uuid=True), nullable=False),
        sa.Column("order_code", sa.String(length=64), nullable=False),
        sa.Column("client_order_key", sa.String(length=160), nullable=False),
        sa.Column("correlation_id", sa.String(length=64), nullable=False),
        sa.Column("account_id", sa.Uuid(as_uuid=True), nullable=False),
        sa.Column("strategy_instance_id", sa.Uuid(as_uuid=True), nullable=True),
        sa.Column("strategy_version_id", sa.Uuid(as_uuid=True), nullable=True),
        sa.Column("strategy_version_label", sa.String(length=96), nullable=True),
        sa.Column("signal_id", sa.Uuid(as_uuid=True), nullable=True),
        sa.Column("replay_session_id", sa.Uuid(as_uuid=True), nullable=True),
        sa.Column("order_source", sa.String(length=32), nullable=False),
        sa.Column("reduce_only", sa.Boolean(), nullable=False, server_default=sa.text("false")),
        sa.Column("exit_reason", sa.String(length=64), nullable=True),
        sa.Column("instrument_symbol", sa.String(length=32), nullable=False),
        sa.Column("timeframe", sa.String(length=16), nullable=False, server_default="5m"),
        sa.Column("side", sa.String(length=16), nullable=False),
        sa.Column("order_type", sa.String(length=16), nullable=False),
        sa.Column("quantity", sa.Integer(), nullable=False),
        sa.Column("filled_quantity", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("leaves_quantity", sa.Integer(), nullable=False),
        sa.Column("requested_reference_price", sa.Numeric(precision=18, scale=4), nullable=True),
        sa.Column("limit_price", sa.Numeric(precision=18, scale=4), nullable=True),
        sa.Column("stop_price", sa.Numeric(precision=18, scale=4), nullable=True),
        sa.Column("stop_triggered", sa.Boolean(), nullable=False, server_default=sa.text("false")),
        sa.Column("stop_triggered_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("average_fill_price", sa.Numeric(precision=18, scale=4), nullable=True),
        sa.Column(
            "total_slippage_cost",
            sa.Numeric(precision=18, scale=4),
            nullable=False,
            server_default="0",
        ),
        sa.Column(
            "total_charges",
            sa.Numeric(precision=18, scale=4),
            nullable=False,
            server_default="0",
        ),
        sa.Column("execution_latency_ms", sa.Integer(), nullable=False, server_default="120"),
        sa.Column("latency_bars", sa.Integer(), nullable=False, server_default="1"),
        sa.Column("created_bar_timestamp", sa.DateTime(timezone=True), nullable=True),
        sa.Column("bars_waited", sa.Integer(), nullable=False, server_default="0"),
        sa.Column(
            "slippage_bps", sa.Numeric(precision=10, scale=4), nullable=False, server_default="2.0"
        ),
        sa.Column(
            "charges_bps", sa.Numeric(precision=10, scale=4), nullable=False, server_default="1.5"
        ),
        sa.Column(
            "per_order_fee_inr",
            sa.Numeric(precision=12, scale=4),
            nullable=False,
            server_default="20.0",
        ),
        sa.Column(
            "max_fill_ratio_per_bar",
            sa.Numeric(precision=6, scale=4),
            nullable=False,
            server_default="1.0",
        ),
        sa.Column(
            "simulate_rejection", sa.Boolean(), nullable=False, server_default=sa.text("false")
        ),
        sa.Column("simulated_rejection_reason", sa.String(length=255), nullable=True),
        sa.Column("valid_for_bars", sa.Integer(), nullable=True),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("target_stop_loss_price", sa.Numeric(precision=18, scale=4), nullable=True),
        sa.Column("target_profit_price", sa.Numeric(precision=18, scale=4), nullable=True),
        sa.Column("status", sa.String(length=32), nullable=False),
        sa.Column("risk_check_passed", sa.Boolean(), nullable=True),
        sa.Column("risk_check_details_json", sa.JSON(), nullable=False),
        sa.Column("rejection_reason", sa.Text(), nullable=True),
        sa.Column("cancel_reason", sa.Text(), nullable=True),
        sa.Column("submitted_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("acknowledged_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("completed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("created_by", sa.String(length=128), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint("quantity > 0", name="ck_paper_orders_qty_pos"),
        sa.CheckConstraint("filled_quantity >= 0", name="ck_paper_orders_filled_nonneg"),
        sa.CheckConstraint("leaves_quantity >= 0", name="ck_paper_orders_leaves_nonneg"),
        sa.ForeignKeyConstraint(["account_id"], ["paper_accounts.id"], ondelete="RESTRICT"),
        sa.ForeignKeyConstraint(
            ["strategy_instance_id"], ["strategy_instances.id"], ondelete="RESTRICT"
        ),
        sa.ForeignKeyConstraint(
            ["strategy_version_id"], ["strategy_versions.id"], ondelete="RESTRICT"
        ),
        sa.ForeignKeyConstraint(["signal_id"], ["signals.id"], ondelete="RESTRICT"),
        sa.ForeignKeyConstraint(["replay_session_id"], ["replay_sessions.id"], ondelete="RESTRICT"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("order_code"),
        sa.UniqueConstraint("client_order_key", name="uq_paper_orders_client_order_key"),
        sa.UniqueConstraint("signal_id", name="uq_paper_orders_signal_id"),
    )
    op.create_index("ix_paper_orders_order_code", "paper_orders", ["order_code"], unique=True)
    op.create_index(
        "ix_paper_orders_correlation_id", "paper_orders", ["correlation_id"], unique=False
    )
    op.create_index("ix_paper_orders_account_id", "paper_orders", ["account_id"], unique=False)
    op.create_index(
        "ix_paper_orders_strategy_instance_id",
        "paper_orders",
        ["strategy_instance_id"],
        unique=False,
    )
    op.create_index(
        "ix_paper_orders_strategy_version_id",
        "paper_orders",
        ["strategy_version_id"],
        unique=False,
    )
    op.create_index("ix_paper_orders_signal_id", "paper_orders", ["signal_id"], unique=True)
    op.create_index(
        "ix_paper_orders_replay_session_id", "paper_orders", ["replay_session_id"], unique=False
    )
    op.create_index(
        "ix_paper_orders_instrument_symbol", "paper_orders", ["instrument_symbol"], unique=False
    )
    op.create_index("ix_paper_orders_status", "paper_orders", ["status"], unique=False)
    op.create_index(
        "ix_paper_orders_inst_status",
        "paper_orders",
        ["instrument_symbol", "status"],
        unique=False,
    )
    op.create_index("ix_paper_orders_created_desc", "paper_orders", ["created_at"], unique=False)

    # 4. order_state_events
    op.create_table(
        "order_state_events",
        sa.Column("id", sa.Uuid(as_uuid=True), nullable=False),
        sa.Column("order_id", sa.Uuid(as_uuid=True), nullable=False),
        sa.Column("previous_status", sa.String(length=32), nullable=True),
        sa.Column("new_status", sa.String(length=32), nullable=False),
        sa.Column("occurred_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("actor", sa.String(length=128), nullable=False),
        sa.Column("reason", sa.Text(), nullable=False),
        sa.Column("details_json", sa.JSON(), nullable=False),
        sa.Column("correlation_id", sa.String(length=64), nullable=False),
        sa.ForeignKeyConstraint(["order_id"], ["paper_orders.id"], ondelete="RESTRICT"),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(
        "ix_order_state_events_order_id", "order_state_events", ["order_id"], unique=False
    )
    op.create_index(
        "ix_order_state_events_correlation_id",
        "order_state_events",
        ["correlation_id"],
        unique=False,
    )
    op.create_index(
        "ix_order_state_events_order_time",
        "order_state_events",
        ["order_id", "occurred_at"],
        unique=False,
    )

    # 5. paper_fills
    op.create_table(
        "paper_fills",
        sa.Column("id", sa.Uuid(as_uuid=True), nullable=False),
        sa.Column("fill_code", sa.String(length=64), nullable=False),
        sa.Column("fill_idempotency_key", sa.String(length=160), nullable=False),
        sa.Column("order_id", sa.Uuid(as_uuid=True), nullable=False),
        sa.Column("account_id", sa.Uuid(as_uuid=True), nullable=False),
        sa.Column("strategy_instance_id", sa.Uuid(as_uuid=True), nullable=True),
        sa.Column("strategy_version_id", sa.Uuid(as_uuid=True), nullable=True),
        sa.Column("strategy_version_label", sa.String(length=96), nullable=True),
        sa.Column("instrument_symbol", sa.String(length=32), nullable=False),
        sa.Column("side", sa.String(length=16), nullable=False),
        sa.Column("fill_quantity", sa.Integer(), nullable=False),
        sa.Column("raw_market_price", sa.Numeric(precision=18, scale=4), nullable=False),
        sa.Column("fill_price", sa.Numeric(precision=18, scale=4), nullable=False),
        sa.Column(
            "slippage_cost", sa.Numeric(precision=18, scale=4), nullable=False, server_default="0"
        ),
        sa.Column("charges", sa.Numeric(precision=18, scale=4), nullable=False, server_default="0"),
        sa.Column("simulated_latency_ms", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("bar_timestamp", sa.DateTime(timezone=True), nullable=False),
        sa.Column("executed_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("market_event_id", sa.String(length=64), nullable=True),
        sa.Column("liquidity_note", sa.String(length=160), nullable=False),
        sa.Column("correlation_id", sa.String(length=64), nullable=False),
        sa.CheckConstraint("fill_quantity > 0", name="ck_paper_fills_qty_pos"),
        sa.ForeignKeyConstraint(["order_id"], ["paper_orders.id"], ondelete="RESTRICT"),
        sa.ForeignKeyConstraint(["account_id"], ["paper_accounts.id"], ondelete="RESTRICT"),
        sa.ForeignKeyConstraint(
            ["strategy_instance_id"], ["strategy_instances.id"], ondelete="RESTRICT"
        ),
        sa.ForeignKeyConstraint(
            ["strategy_version_id"], ["strategy_versions.id"], ondelete="RESTRICT"
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("fill_code"),
        sa.UniqueConstraint("fill_idempotency_key", name="uq_paper_fills_idempotency_key"),
    )
    op.create_index("ix_paper_fills_fill_code", "paper_fills", ["fill_code"], unique=True)
    op.create_index("ix_paper_fills_order_id", "paper_fills", ["order_id"], unique=False)
    op.create_index("ix_paper_fills_account_id", "paper_fills", ["account_id"], unique=False)
    op.create_index(
        "ix_paper_fills_strategy_instance_id",
        "paper_fills",
        ["strategy_instance_id"],
        unique=False,
    )
    op.create_index(
        "ix_paper_fills_instrument_symbol", "paper_fills", ["instrument_symbol"], unique=False
    )
    op.create_index(
        "ix_paper_fills_correlation_id", "paper_fills", ["correlation_id"], unique=False
    )
    op.create_index(
        "ix_paper_fills_order_exec", "paper_fills", ["order_id", "executed_at"], unique=False
    )

    # 6. paper_positions
    op.create_table(
        "paper_positions",
        sa.Column("id", sa.Uuid(as_uuid=True), nullable=False),
        sa.Column("position_code", sa.String(length=64), nullable=False),
        sa.Column("account_id", sa.Uuid(as_uuid=True), nullable=False),
        sa.Column("strategy_instance_id", sa.Uuid(as_uuid=True), nullable=True),
        sa.Column("strategy_version_id", sa.Uuid(as_uuid=True), nullable=True),
        sa.Column("strategy_version_label", sa.String(length=96), nullable=False),
        sa.Column("instrument_symbol", sa.String(length=32), nullable=False),
        sa.Column("timeframe", sa.String(length=16), nullable=False, server_default="5m"),
        sa.Column("side", sa.String(length=16), nullable=False),
        sa.Column("quantity", sa.Integer(), nullable=False),
        sa.Column("opened_quantity", sa.Integer(), nullable=False),
        sa.Column("closed_quantity", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("average_entry_price", sa.Numeric(precision=18, scale=4), nullable=False),
        sa.Column("average_exit_price", sa.Numeric(precision=18, scale=4), nullable=True),
        sa.Column("last_price", sa.Numeric(precision=18, scale=4), nullable=False),
        sa.Column("highest_price_since_entry", sa.Numeric(precision=18, scale=4), nullable=False),
        sa.Column("lowest_price_since_entry", sa.Numeric(precision=18, scale=4), nullable=False),
        sa.Column("bars_held", sa.Integer(), nullable=False, server_default="0"),
        sa.Column(
            "realized_pnl", sa.Numeric(precision=18, scale=4), nullable=False, server_default="0"
        ),
        sa.Column(
            "unrealized_pnl", sa.Numeric(precision=18, scale=4), nullable=False, server_default="0"
        ),
        sa.Column(
            "unallocated_entry_charges",
            sa.Numeric(precision=18, scale=4),
            nullable=False,
            server_default="0",
        ),
        sa.Column(
            "total_charges", sa.Numeric(precision=18, scale=4), nullable=False, server_default="0"
        ),
        sa.Column("stop_loss_price", sa.Numeric(precision=18, scale=4), nullable=True),
        sa.Column("target_price", sa.Numeric(precision=18, scale=4), nullable=True),
        sa.Column("status", sa.String(length=32), nullable=False),
        sa.Column("opened_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("last_mark_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("closed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("exit_reason", sa.String(length=64), nullable=True),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint("quantity >= 0", name="ck_paper_positions_qty_nonneg"),
        sa.ForeignKeyConstraint(["account_id"], ["paper_accounts.id"], ondelete="RESTRICT"),
        sa.ForeignKeyConstraint(
            ["strategy_instance_id"], ["strategy_instances.id"], ondelete="RESTRICT"
        ),
        sa.ForeignKeyConstraint(
            ["strategy_version_id"], ["strategy_versions.id"], ondelete="RESTRICT"
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("position_code"),
    )
    op.create_index(
        "ix_paper_positions_position_code", "paper_positions", ["position_code"], unique=True
    )
    op.create_index(
        "ix_paper_positions_account_id", "paper_positions", ["account_id"], unique=False
    )
    op.create_index(
        "ix_paper_positions_strategy_instance_id",
        "paper_positions",
        ["strategy_instance_id"],
        unique=False,
    )
    op.create_index(
        "ix_paper_positions_strategy_version_id",
        "paper_positions",
        ["strategy_version_id"],
        unique=False,
    )
    op.create_index(
        "ix_paper_positions_instrument_symbol",
        "paper_positions",
        ["instrument_symbol"],
        unique=False,
    )
    op.create_index("ix_paper_positions_status", "paper_positions", ["status"], unique=False)
    op.create_index(
        "ix_paper_positions_book_status",
        "paper_positions",
        ["account_id", "instrument_symbol", "status"],
        unique=False,
    )

    # 7. position_events
    op.create_table(
        "position_events",
        sa.Column("id", sa.Uuid(as_uuid=True), nullable=False),
        sa.Column("event_idempotency_key", sa.String(length=160), nullable=False),
        sa.Column("fill_id", sa.Uuid(as_uuid=True), nullable=False),
        sa.Column("position_id", sa.Uuid(as_uuid=True), nullable=False),
        sa.Column("event_type", sa.String(length=32), nullable=False),
        sa.Column("delta_quantity", sa.Integer(), nullable=False),
        sa.Column("resulting_quantity", sa.Integer(), nullable=False),
        sa.Column("fill_price", sa.Numeric(precision=18, scale=4), nullable=False),
        sa.Column(
            "realized_pnl_delta",
            sa.Numeric(precision=18, scale=4),
            nullable=False,
            server_default="0",
        ),
        sa.Column(
            "charges_delta", sa.Numeric(precision=18, scale=4), nullable=False, server_default="0"
        ),
        sa.Column("occurred_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("correlation_id", sa.String(length=64), nullable=False),
        sa.ForeignKeyConstraint(["fill_id"], ["paper_fills.id"], ondelete="RESTRICT"),
        sa.ForeignKeyConstraint(["position_id"], ["paper_positions.id"], ondelete="RESTRICT"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "event_idempotency_key", name="uq_position_events_idempotency_key"
        ),
        sa.UniqueConstraint("fill_id", name="uq_position_events_fill_id"),
    )
    op.create_index("ix_position_events_fill_id", "position_events", ["fill_id"], unique=True)
    op.create_index(
        "ix_position_events_position_id", "position_events", ["position_id"], unique=False
    )
    op.create_index(
        "ix_position_events_correlation_id", "position_events", ["correlation_id"], unique=False
    )
    op.create_index(
        "ix_position_events_pos_time",
        "position_events",
        ["position_id", "occurred_at"],
        unique=False,
    )

    # 8. PostgreSQL PL/pgSQL immutability triggers for append-only OMS/Execution/Position ledgers
    if bind.dialect.name == "postgresql":
        op.execute(
            """
            CREATE OR REPLACE FUNCTION enforce_execution_ledger_immutability()
            RETURNS TRIGGER AS $$
            BEGIN
                RAISE EXCEPTION 'Simulated execution fills, order state events, and position events are append-only immutable records.';
            END;
            $$ LANGUAGE plpgsql;

            CREATE TRIGGER trg_paper_fills_immutable
            BEFORE UPDATE OR DELETE ON paper_fills
            FOR EACH ROW EXECUTE FUNCTION enforce_execution_ledger_immutability();

            CREATE TRIGGER trg_order_state_events_immutable
            BEFORE UPDATE OR DELETE ON order_state_events
            FOR EACH ROW EXECUTE FUNCTION enforce_execution_ledger_immutability();

            CREATE TRIGGER trg_position_events_immutable
            BEFORE UPDATE OR DELETE ON position_events
            FOR EACH ROW EXECUTE FUNCTION enforce_execution_ledger_immutability();
            """
        )


def downgrade() -> None:
    bind = op.get_bind()
    if bind.dialect.name == "postgresql":
        op.execute(
            """
            DROP TRIGGER IF EXISTS trg_position_events_immutable ON position_events;
            DROP TRIGGER IF EXISTS trg_order_state_events_immutable ON order_state_events;
            DROP TRIGGER IF EXISTS trg_paper_fills_immutable ON paper_fills;
            DROP FUNCTION IF EXISTS enforce_execution_ledger_immutability();
            """
        )

    op.drop_table("position_events")
    op.drop_table("paper_positions")
    op.drop_table("paper_fills")
    op.drop_table("order_state_events")
    op.drop_table("paper_orders")

    if bind.dialect.name == "postgresql":
        op.drop_constraint("fk_signals_strategy_instance_id", "signals", type_="foreignkey")
        op.drop_index("ix_signals_strategy_instance_id", table_name="signals")
        op.drop_column("signals", "strategy_instance_id")
        op.alter_column(
            "signals", "replay_session_id", existing_type=sa.Uuid(as_uuid=True), nullable=False
        )
    else:
        with op.batch_alter_table("signals") as batch_op:
            batch_op.drop_constraint("fk_signals_strategy_instance_id", type_="foreignkey")
            batch_op.drop_index("ix_signals_strategy_instance_id")
            batch_op.drop_column("strategy_instance_id")
            batch_op.alter_column(
                "replay_session_id", existing_type=sa.Uuid(as_uuid=True), nullable=False
            )

    op.drop_table("strategy_instances")
