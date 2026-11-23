"""FYERS Historical Market Data schema: instrument provider mappings, rich dataset
metadata, BigInteger candle volume, historical_download_runs, and replay recovery context.

Revision ID: 0008_fyers_historical_market_data
Revises: 0007_phase5_operational_visibility
Create Date: 2026-11-23 07:00:00+05:30
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0008_fyers_historical_market_data"
down_revision: str | None = "0007_phase5_operational_visibility"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    bind = op.get_bind()
    is_pg = bind.dialect.name == "postgresql"

    # 1. Extend instruments with authoritative data_provider & provider_symbol
    op.add_column(
        "instruments",
        sa.Column("data_provider", sa.String(length=32), nullable=False, server_default="FYERS"),
    )
    op.add_column(
        "instruments",
        sa.Column("provider_symbol", sa.String(length=64), nullable=False, server_default=""),
    )
    op.execute(
        """
        UPDATE instruments
        SET data_provider = 'FYERS', provider_symbol = 'NSE:NIFTY50-INDEX'
        WHERE symbol = 'NIFTY' AND (provider_symbol IS NULL OR provider_symbol = '')
        """
    )
    op.execute(
        """
        UPDATE instruments
        SET data_provider = 'FYERS', provider_symbol = 'NSE:NIFTYBANK-INDEX'
        WHERE symbol = 'BANKNIFTY' AND (provider_symbol IS NULL OR provider_symbol = '')
        """
    )

    # 2. Extend historical_datasets with full provider, range, quality & reproducibility metadata
    if is_pg:
        op.add_column(
            "historical_datasets",
            sa.Column("instrument_id", sa.Uuid(as_uuid=True), nullable=True),
        )
        op.create_foreign_key(
            "fk_historical_datasets_instrument_id",
            "historical_datasets",
            "instruments",
            ["instrument_id"],
            ["id"],
            ondelete="RESTRICT",
        )
        op.create_index(
            "ix_historical_datasets_instrument_id",
            "historical_datasets",
            ["instrument_id"],
            unique=False,
        )
    else:
        with op.batch_alter_table("historical_datasets") as batch_op:
            batch_op.add_column(sa.Column("instrument_id", sa.Uuid(as_uuid=True), nullable=True))
            batch_op.create_foreign_key(
                "fk_historical_datasets_instrument_id",
                "instruments",
                ["instrument_id"],
                ["id"],
                ondelete="RESTRICT",
            )
            batch_op.create_index(
                "ix_historical_datasets_instrument_id", ["instrument_id"], unique=False
            )

    op.add_column(
        "historical_datasets",
        sa.Column("provider", sa.String(length=32), nullable=False, server_default="CSV"),
    )
    op.add_column(
        "historical_datasets",
        sa.Column("provider_symbol", sa.String(length=64), nullable=True),
    )
    op.add_column(
        "historical_datasets",
        sa.Column("quality_status", sa.String(length=32), nullable=False, server_default="VALID"),
    )
    op.add_column(
        "historical_datasets",
        sa.Column(
            "is_complete",
            sa.Boolean(),
            nullable=False,
            server_default=sa.text("true") if is_pg else sa.text("1"),
        ),
    )
    op.add_column(
        "historical_datasets",
        sa.Column("requested_from", sa.DateTime(timezone=True), nullable=True),
    )
    op.add_column(
        "historical_datasets",
        sa.Column("requested_to", sa.DateTime(timezone=True), nullable=True),
    )
    op.add_column(
        "historical_datasets",
        sa.Column("retrieved_at", sa.DateTime(timezone=True), nullable=True),
    )
    op.add_column(
        "historical_datasets",
        sa.Column("data_quality_report_json", sa.JSON(), nullable=True),
    )
    op.add_column(
        "historical_datasets",
        sa.Column("metadata_json", sa.JSON(), nullable=True),
    )
    op.create_index(
        "ix_historical_datasets_source_quality",
        "historical_datasets",
        ["source_type", "quality_status"],
        unique=False,
    )

    # 3. Upgrade historical_candles.volume to BigInteger for large daily index/contract volumes
    if is_pg:
        op.alter_column(
            "historical_candles",
            "volume",
            existing_type=sa.Integer(),
            type_=sa.BigInteger(),
            existing_nullable=False,
        )

    # 4. Create historical_download_runs table
    op.create_table(
        "historical_download_runs",
        sa.Column("id", sa.Uuid(as_uuid=True), nullable=False),
        sa.Column("run_code", sa.String(length=64), nullable=False),
        sa.Column("provider", sa.String(length=32), nullable=False, server_default="FYERS"),
        sa.Column("instrument_symbol", sa.String(length=32), nullable=False),
        sa.Column("provider_symbol", sa.String(length=64), nullable=False),
        sa.Column("timeframe", sa.String(length=16), nullable=False),
        sa.Column("fyers_resolution", sa.String(length=16), nullable=False),
        sa.Column("requested_from", sa.DateTime(timezone=True), nullable=False),
        sa.Column("requested_to", sa.DateTime(timezone=True), nullable=False),
        sa.Column("status", sa.String(length=32), nullable=False, server_default="REQUESTED"),
        sa.Column("quality_status", sa.String(length=32), nullable=False, server_default="VALID"),
        sa.Column("dataset_id", sa.Uuid(as_uuid=True), nullable=True),
        sa.Column("dataset_code", sa.String(length=96), nullable=True),
        sa.Column("chunks_total", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("chunks_succeeded", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("chunks_failed", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("candles_fetched", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("candles_stored", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("error_code", sa.String(length=64), nullable=True),
        sa.Column("error_message", sa.Text(), nullable=True),
        sa.Column("details_json", sa.JSON(), nullable=False),
        sa.Column("actor", sa.String(length=128), nullable=False, server_default="desk.operator"),
        sa.Column("correlation_id", sa.String(length=64), nullable=False),
        sa.Column("started_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("completed_at", sa.DateTime(timezone=True), nullable=True),
        sa.ForeignKeyConstraint(["dataset_id"], ["historical_datasets.id"], ondelete="SET NULL"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("run_code"),
    )
    op.create_index(
        "ix_historical_download_runs_run_code",
        "historical_download_runs",
        ["run_code"],
        unique=True,
    )
    op.create_index(
        "ix_historical_download_runs_instrument_symbol",
        "historical_download_runs",
        ["instrument_symbol"],
        unique=False,
    )
    op.create_index(
        "ix_historical_download_runs_dataset_id",
        "historical_download_runs",
        ["dataset_id"],
        unique=False,
    )
    op.create_index(
        "ix_historical_download_runs_correlation_id",
        "historical_download_runs",
        ["correlation_id"],
        unique=False,
    )
    op.create_index(
        "ix_historical_download_runs_started_desc",
        "historical_download_runs",
        ["started_at"],
        unique=False,
    )
    op.create_index(
        "ix_historical_download_runs_status",
        "historical_download_runs",
        ["status"],
        unique=False,
    )

    # 5. Extend replay_sessions with source lineage & Render sleep recovery context
    if is_pg:
        op.add_column(
            "replay_sessions",
            sa.Column(
                "dataset_source",
                sa.String(length=32),
                nullable=False,
                server_default="BUNDLED_CSV",
            ),
        )
        op.add_column(
            "replay_sessions",
            sa.Column(
                "dataset_provider",
                sa.String(length=32),
                nullable=False,
                server_default="SYNTHETIC",
            ),
        )
        op.add_column(
            "replay_sessions",
            sa.Column("strategy_instance_id", sa.Uuid(as_uuid=True), nullable=True),
        )
        op.create_foreign_key(
            "fk_replay_sessions_strategy_instance_id",
            "replay_sessions",
            "strategy_instances",
            ["strategy_instance_id"],
            ["id"],
            ondelete="SET NULL",
        )
        op.add_column(
            "replay_sessions",
            sa.Column("correlation_id", sa.String(length=64), nullable=True),
        )
        op.add_column(
            "replay_sessions",
            sa.Column(
                "paper_trading_halted_snapshot",
                sa.Boolean(),
                nullable=False,
                server_default=sa.text("false"),
            ),
        )
    else:
        with op.batch_alter_table("replay_sessions") as batch_op:
            batch_op.add_column(
                sa.Column(
                    "dataset_source",
                    sa.String(length=32),
                    nullable=False,
                    server_default="BUNDLED_CSV",
                )
            )
            batch_op.add_column(
                sa.Column(
                    "dataset_provider",
                    sa.String(length=32),
                    nullable=False,
                    server_default="SYNTHETIC",
                )
            )
            batch_op.add_column(
                sa.Column("strategy_instance_id", sa.Uuid(as_uuid=True), nullable=True)
            )
            batch_op.create_foreign_key(
                "fk_replay_sessions_strategy_instance_id",
                "strategy_instances",
                ["strategy_instance_id"],
                ["id"],
                ondelete="SET NULL",
            )
            batch_op.add_column(
                sa.Column("correlation_id", sa.String(length=64), nullable=True)
            )
            batch_op.add_column(
                sa.Column(
                    "paper_trading_halted_snapshot",
                    sa.Boolean(),
                    nullable=False,
                    server_default=sa.text("0"),
                )
            )


def downgrade() -> None:
    bind = op.get_bind()
    is_pg = bind.dialect.name == "postgresql"

    # 1. Revert replay_sessions columns
    if is_pg:
        op.drop_constraint(
            "fk_replay_sessions_strategy_instance_id", "replay_sessions", type_="foreignkey"
        )
        op.drop_column("replay_sessions", "paper_trading_halted_snapshot")
        op.drop_column("replay_sessions", "correlation_id")
        op.drop_column("replay_sessions", "strategy_instance_id")
        op.drop_column("replay_sessions", "dataset_provider")
        op.drop_column("replay_sessions", "dataset_source")
    else:
        with op.batch_alter_table("replay_sessions") as batch_op:
            batch_op.drop_constraint(
                "fk_replay_sessions_strategy_instance_id", type_="foreignkey"
            )
            batch_op.drop_column("paper_trading_halted_snapshot")
            batch_op.drop_column("correlation_id")
            batch_op.drop_column("strategy_instance_id")
            batch_op.drop_column("dataset_provider")
            batch_op.drop_column("dataset_source")

    # 2. Drop historical_download_runs
    op.drop_table("historical_download_runs")

    # 3. Revert historical_candles.volume
    if is_pg:
        op.alter_column(
            "historical_candles",
            "volume",
            existing_type=sa.BigInteger(),
            type_=sa.Integer(),
            existing_nullable=False,
        )

    # 4. Revert historical_datasets columns
    if is_pg:
        op.drop_index("ix_historical_datasets_source_quality", table_name="historical_datasets")
        op.drop_index("ix_historical_datasets_instrument_id", table_name="historical_datasets")
        op.drop_constraint(
            "fk_historical_datasets_instrument_id", "historical_datasets", type_="foreignkey"
        )
        op.drop_column("historical_datasets", "metadata_json")
        op.drop_column("historical_datasets", "data_quality_report_json")
        op.drop_column("historical_datasets", "retrieved_at")
        op.drop_column("historical_datasets", "requested_to")
        op.drop_column("historical_datasets", "requested_from")
        op.drop_column("historical_datasets", "is_complete")
        op.drop_column("historical_datasets", "quality_status")
        op.drop_column("historical_datasets", "provider_symbol")
        op.drop_column("historical_datasets", "provider")
        op.drop_column("historical_datasets", "instrument_id")
    else:
        with op.batch_alter_table("historical_datasets") as batch_op:
            batch_op.drop_index("ix_historical_datasets_source_quality")
            batch_op.drop_index("ix_historical_datasets_instrument_id")
            batch_op.drop_constraint("fk_historical_datasets_instrument_id", type_="foreignkey")
            batch_op.drop_column("metadata_json")
            batch_op.drop_column("data_quality_report_json")
            batch_op.drop_column("retrieved_at")
            batch_op.drop_column("requested_to")
            batch_op.drop_column("requested_from")
            batch_op.drop_column("is_complete")
            batch_op.drop_column("quality_status")
            batch_op.drop_column("provider_symbol")
            batch_op.drop_column("provider")
            batch_op.drop_column("instrument_id")

    # 5. Revert instruments columns
    if is_pg:
        op.drop_column("instruments", "provider_symbol")
        op.drop_column("instruments", "data_provider")
    else:
        with op.batch_alter_table("instruments") as batch_op:
            batch_op.drop_column("provider_symbol")
            batch_op.drop_column("data_provider")
