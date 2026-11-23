"""Phase 3A Market Replay schema: replay_sessions and signals tables.

Revision ID: 0004_phase3a_market_replay
Revises: 0003_phase2b_backtesting
Create Date: 2026-11-19 17:05:00+05:30
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0004_phase3a_market_replay"
down_revision: str | None = "0003_phase2b_backtesting"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    # 1. replay_sessions
    op.create_table(
        "replay_sessions",
        sa.Column("id", sa.Uuid(as_uuid=True), nullable=False),
        sa.Column("session_code", sa.String(length=64), nullable=False),
        sa.Column("instrument_symbol", sa.String(length=32), nullable=False),
        sa.Column("timeframe", sa.String(length=16), nullable=False),
        sa.Column("strategy_version_id", sa.Uuid(as_uuid=True), nullable=False),
        sa.Column("strategy_version_label", sa.String(length=96), nullable=False),
        sa.Column("dataset_id", sa.String(length=128), nullable=False),
        sa.Column("dataset_name", sa.String(length=160), nullable=False),
        sa.Column("data_version_hash", sa.String(length=64), nullable=False),
        sa.Column("historical_session_date", sa.String(length=32), nullable=False),
        sa.Column("warmup_bars_count", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("total_session_candles", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("candles_processed", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("signals_emitted", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("replay_speed", sa.String(length=16), nullable=False, server_default="1x"),
        sa.Column("status", sa.String(length=32), nullable=False),
        sa.Column("stale_threshold_seconds", sa.Integer(), nullable=False, server_default="15"),
        sa.Column("replay_clock_ist", sa.DateTime(timezone=True), nullable=True),
        sa.Column("last_candle_received_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("last_step_wall_clock", sa.DateTime(timezone=True), nullable=True),
        sa.Column(
            "fractional_bar_carry",
            sa.Numeric(precision=12, scale=6),
            nullable=False,
            server_default="0",
        ),
        sa.Column("last_candle_json", sa.JSON(), nullable=True),
        sa.Column("latest_evaluation_json", sa.JSON(), nullable=True),
        sa.Column("latest_signal_json", sa.JSON(), nullable=True),
        sa.Column("started_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("paused_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("stopped_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("completed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("created_by", sa.String(length=128), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(
            ["strategy_version_id"], ["strategy_versions.id"], ondelete="RESTRICT"
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("session_code"),
    )
    op.create_index(
        "ix_replay_sessions_session_code", "replay_sessions", ["session_code"], unique=True
    )
    op.create_index(
        "ix_replay_sessions_instrument_symbol",
        "replay_sessions",
        ["instrument_symbol"],
        unique=False,
    )
    op.create_index(
        "ix_replay_sessions_strategy_version_id",
        "replay_sessions",
        ["strategy_version_id"],
        unique=False,
    )
    op.create_index("ix_replay_sessions_status", "replay_sessions", ["status"], unique=False)
    op.create_index(
        "ix_replay_sessions_status_updated",
        "replay_sessions",
        ["status", "updated_at"],
        unique=False,
    )
    op.create_index(
        "ix_replay_sessions_inst_status",
        "replay_sessions",
        ["instrument_symbol", "status"],
        unique=False,
    )

    # 2. signals
    op.create_table(
        "signals",
        sa.Column("id", sa.Uuid(as_uuid=True), nullable=False),
        sa.Column("signal_code", sa.String(length=64), nullable=False),
        sa.Column("deduplication_key", sa.String(length=160), nullable=False),
        sa.Column("replay_session_id", sa.Uuid(as_uuid=True), nullable=False),
        sa.Column("strategy_version_id", sa.Uuid(as_uuid=True), nullable=False),
        sa.Column("strategy_version_label", sa.String(length=96), nullable=False),
        sa.Column("instrument_symbol", sa.String(length=32), nullable=False),
        sa.Column("timeframe", sa.String(length=16), nullable=False),
        sa.Column("side", sa.String(length=16), nullable=False),
        sa.Column("direction", sa.String(length=32), nullable=False),
        sa.Column("exit_reason", sa.String(length=32), nullable=True),
        sa.Column("bar_timestamp", sa.DateTime(timezone=True), nullable=False),
        sa.Column("emitted_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("reference_price", sa.Numeric(precision=18, scale=4), nullable=False),
        sa.Column("status", sa.String(length=32), nullable=False),
        sa.Column("rationale", sa.Text(), nullable=False),
        sa.Column("indicators_json", sa.JSON(), nullable=False),
        sa.Column("market_event_id", sa.String(length=64), nullable=False),
        sa.Column("correlation_id", sa.String(length=64), nullable=False),
        sa.ForeignKeyConstraint(["replay_session_id"], ["replay_sessions.id"], ondelete="RESTRICT"),
        sa.ForeignKeyConstraint(
            ["strategy_version_id"], ["strategy_versions.id"], ondelete="RESTRICT"
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("signal_code"),
        sa.UniqueConstraint("deduplication_key", name="uq_signals_deduplication_key"),
    )
    op.create_index("ix_signals_signal_code", "signals", ["signal_code"], unique=True)
    op.create_index(
        "ix_signals_replay_session_id", "signals", ["replay_session_id"], unique=False
    )
    op.create_index(
        "ix_signals_strategy_version_id", "signals", ["strategy_version_id"], unique=False
    )
    op.create_index(
        "ix_signals_instrument_symbol", "signals", ["instrument_symbol"], unique=False
    )
    op.create_index("ix_signals_status", "signals", ["status"], unique=False)
    op.create_index("ix_signals_correlation_id", "signals", ["correlation_id"], unique=False)
    op.create_index(
        "ix_signals_session_bar",
        "signals",
        ["replay_session_id", "bar_timestamp"],
        unique=False,
    )


def downgrade() -> None:
    op.drop_table("signals")
    op.drop_table("replay_sessions")
