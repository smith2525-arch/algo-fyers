"""Phase 2B Backtesting schema: historical_datasets, historical_candles, backtest_runs, backtest_trades.

Revision ID: 0003_phase2b_backtesting
Revises: 0002_strategy_lab_versioning
Create Date: 2026-11-19 10:25:00+05:30
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0003_phase2b_backtesting"
down_revision: str | None = "0002_strategy_lab_versioning"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    bind = op.get_bind()

    # 1. historical_datasets
    op.create_table(
        "historical_datasets",
        sa.Column("id", sa.Uuid(as_uuid=True), nullable=False),
        sa.Column("dataset_code", sa.String(length=96), nullable=False),
        sa.Column("name", sa.String(length=160), nullable=False),
        sa.Column("instrument_symbol", sa.String(length=32), nullable=False),
        sa.Column("timeframe", sa.String(length=16), nullable=False),
        sa.Column("source_type", sa.String(length=32), nullable=False),
        sa.Column("data_version_hash", sa.String(length=64), nullable=False),
        sa.Column("row_count", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("start_timestamp", sa.DateTime(timezone=True), nullable=True),
        sa.Column("end_timestamp", sa.DateTime(timezone=True), nullable=True),
        sa.Column("raw_csv_text", sa.Text(), nullable=False),
        sa.Column("uploaded_by", sa.String(length=128), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("dataset_code"),
    )
    op.create_index(
        "ix_historical_datasets_dataset_code", "historical_datasets", ["dataset_code"], unique=True
    )
    op.create_index(
        "ix_historical_datasets_instrument_symbol",
        "historical_datasets",
        ["instrument_symbol"],
        unique=False,
    )
    op.create_index(
        "ix_historical_datasets_data_version_hash",
        "historical_datasets",
        ["data_version_hash"],
        unique=False,
    )
    op.create_index(
        "ix_historical_datasets_inst_tf",
        "historical_datasets",
        ["instrument_symbol", "timeframe"],
        unique=False,
    )

    # 2. historical_candles
    op.create_table(
        "historical_candles",
        sa.Column("id", sa.Uuid(as_uuid=True), nullable=False),
        sa.Column("dataset_id", sa.Uuid(as_uuid=True), nullable=False),
        sa.Column("instrument_symbol", sa.String(length=32), nullable=False),
        sa.Column("timeframe", sa.String(length=16), nullable=False),
        sa.Column("bar_timestamp", sa.DateTime(timezone=True), nullable=False),
        sa.Column("open", sa.Numeric(precision=18, scale=4), nullable=False),
        sa.Column("high", sa.Numeric(precision=18, scale=4), nullable=False),
        sa.Column("low", sa.Numeric(precision=18, scale=4), nullable=False),
        sa.Column("close", sa.Numeric(precision=18, scale=4), nullable=False),
        sa.Column("volume", sa.Integer(), nullable=False, server_default="0"),
        sa.ForeignKeyConstraint(["dataset_id"], ["historical_datasets.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "dataset_id", "bar_timestamp", name="uq_historical_candles_dataset_ts"
        ),
    )
    op.create_index(
        "ix_historical_candles_dataset_id", "historical_candles", ["dataset_id"], unique=False
    )
    op.create_index(
        "ix_historical_candles_symbol_tf_ts",
        "historical_candles",
        ["instrument_symbol", "timeframe", "bar_timestamp"],
        unique=False,
    )

    # 3. backtest_runs
    op.create_table(
        "backtest_runs",
        sa.Column("id", sa.Uuid(as_uuid=True), nullable=False),
        sa.Column("run_code", sa.String(length=64), nullable=False),
        sa.Column("strategy_version_id", sa.Uuid(as_uuid=True), nullable=False),
        sa.Column("strategy_version_label", sa.String(length=96), nullable=False),
        sa.Column("instrument_symbol", sa.String(length=32), nullable=False),
        sa.Column("timeframe", sa.String(length=16), nullable=False),
        sa.Column("dataset_id", sa.String(length=128), nullable=False),
        sa.Column("dataset_name", sa.String(length=160), nullable=False),
        sa.Column("dataset_source", sa.String(length=32), nullable=False),
        sa.Column("data_version_hash", sa.String(length=64), nullable=False),
        sa.Column("reproducibility_fingerprint", sa.String(length=64), nullable=False),
        sa.Column("input_parameters_json", sa.JSON(), nullable=False),
        sa.Column("execution_assumptions_json", sa.JSON(), nullable=False),
        sa.Column("data_quality_report_json", sa.JSON(), nullable=False),
        sa.Column("metrics_json", sa.JSON(), nullable=False),
        sa.Column("equity_curve_json", sa.JSON(), nullable=False),
        sa.Column("drawdown_curve_json", sa.JSON(), nullable=False),
        sa.Column("monthly_returns_json", sa.JSON(), nullable=False),
        sa.Column("data_start_timestamp", sa.DateTime(timezone=True), nullable=True),
        sa.Column("data_end_timestamp", sa.DateTime(timezone=True), nullable=True),
        sa.Column("started_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("completed_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("status", sa.String(length=32), nullable=False),
        sa.Column("engine_version", sa.String(length=64), nullable=False),
        sa.Column("created_by", sa.String(length=128), nullable=False),
        sa.Column("correlation_id", sa.String(length=64), nullable=False),
        sa.ForeignKeyConstraint(
            ["strategy_version_id"], ["strategy_versions.id"], ondelete="RESTRICT"
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("run_code"),
    )
    op.create_index("ix_backtest_runs_run_code", "backtest_runs", ["run_code"], unique=True)
    op.create_index(
        "ix_backtest_runs_strategy_version_id",
        "backtest_runs",
        ["strategy_version_id"],
        unique=False,
    )
    op.create_index(
        "ix_backtest_runs_instrument_symbol",
        "backtest_runs",
        ["instrument_symbol"],
        unique=False,
    )
    op.create_index(
        "ix_backtest_runs_data_version_hash",
        "backtest_runs",
        ["data_version_hash"],
        unique=False,
    )
    op.create_index(
        "ix_backtest_runs_reproducibility_fingerprint",
        "backtest_runs",
        ["reproducibility_fingerprint"],
        unique=False,
    )
    op.create_index("ix_backtest_runs_status", "backtest_runs", ["status"], unique=False)
    op.create_index(
        "ix_backtest_runs_correlation_id", "backtest_runs", ["correlation_id"], unique=False
    )
    op.create_index(
        "ix_backtest_runs_started_at_desc", "backtest_runs", ["started_at"], unique=False
    )
    op.create_index(
        "ix_backtest_runs_strategy_status",
        "backtest_runs",
        ["strategy_version_id", "status"],
        unique=False,
    )

    # 4. backtest_trades
    op.create_table(
        "backtest_trades",
        sa.Column("id", sa.Uuid(as_uuid=True), nullable=False),
        sa.Column("backtest_run_id", sa.Uuid(as_uuid=True), nullable=False),
        sa.Column("deterministic_trade_id", sa.Uuid(as_uuid=True), nullable=False),
        sa.Column("trade_number", sa.Integer(), nullable=False),
        sa.Column("strategy_version_id", sa.Uuid(as_uuid=True), nullable=False),
        sa.Column("strategy_version_label", sa.String(length=96), nullable=False),
        sa.Column("instrument_symbol", sa.String(length=32), nullable=False),
        sa.Column("direction", sa.String(length=16), nullable=False),
        sa.Column("entry_signal_time", sa.DateTime(timezone=True), nullable=False),
        sa.Column("entry_time", sa.DateTime(timezone=True), nullable=False),
        sa.Column("raw_entry_price", sa.Numeric(precision=18, scale=4), nullable=False),
        sa.Column("entry_price", sa.Numeric(precision=18, scale=4), nullable=False),
        sa.Column("quantity", sa.Integer(), nullable=False),
        sa.Column("exit_time", sa.DateTime(timezone=True), nullable=False),
        sa.Column("raw_exit_price", sa.Numeric(precision=18, scale=4), nullable=False),
        sa.Column("exit_price", sa.Numeric(precision=18, scale=4), nullable=False),
        sa.Column("exit_reason", sa.String(length=32), nullable=False),
        sa.Column("gross_pnl", sa.Numeric(precision=18, scale=4), nullable=False),
        sa.Column("charges", sa.Numeric(precision=18, scale=4), nullable=False),
        sa.Column("slippage_cost", sa.Numeric(precision=18, scale=4), nullable=False),
        sa.Column("net_pnl", sa.Numeric(precision=18, scale=4), nullable=False),
        sa.Column("bars_held", sa.Integer(), nullable=False, server_default="1"),
        sa.ForeignKeyConstraint(["backtest_run_id"], ["backtest_runs.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(
            ["strategy_version_id"], ["strategy_versions.id"], ondelete="RESTRICT"
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "backtest_run_id", "trade_number", name="uq_backtest_trades_run_num"
        ),
    )
    op.create_index(
        "ix_backtest_trades_backtest_run_id",
        "backtest_trades",
        ["backtest_run_id"],
        unique=False,
    )
    op.create_index(
        "ix_backtest_trades_deterministic_trade_id",
        "backtest_trades",
        ["deterministic_trade_id"],
        unique=False,
    )
    op.create_index(
        "ix_backtest_trades_run_entry",
        "backtest_trades",
        ["backtest_run_id", "entry_time"],
        unique=False,
    )

    if bind.dialect.name == "postgresql":
        op.execute(
            """
            CREATE OR REPLACE FUNCTION enforce_backtest_immutability()
            RETURNS TRIGGER AS $$
            BEGIN
                RAISE EXCEPTION 'Backtest runs and simulated trade ledgers are immutable historical artifacts.';
            END;
            $$ LANGUAGE plpgsql;

            CREATE TRIGGER trg_backtest_runs_immutable
            BEFORE UPDATE OR DELETE ON backtest_runs
            FOR EACH ROW EXECUTE FUNCTION enforce_backtest_immutability();

            CREATE TRIGGER trg_backtest_trades_immutable
            BEFORE UPDATE OR DELETE ON backtest_trades
            FOR EACH ROW EXECUTE FUNCTION enforce_backtest_immutability();
            """
        )


def downgrade() -> None:
    bind = op.get_bind()
    if bind.dialect.name == "postgresql":
        op.execute("DROP TRIGGER IF EXISTS trg_backtest_trades_immutable ON backtest_trades;")
        op.execute("DROP TRIGGER IF EXISTS trg_backtest_runs_immutable ON backtest_runs;")
        op.execute("DROP FUNCTION IF EXISTS enforce_backtest_immutability();")

    op.drop_table("backtest_trades")
    op.drop_table("backtest_runs")
    op.drop_table("historical_candles")
    op.drop_table("historical_datasets")
