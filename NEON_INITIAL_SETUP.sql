-- ============================================================================
-- ALGO TRADING CONTROL CENTER — AUTHORITATIVE NEON POSTGRESQL INITIAL SETUP
-- File: NEON_INITIAL_SETUP.sql
--
-- Authoritative Schema Equivalence:
--   SQLAlchemy 2.0 ORM (`core/persistence/models.py`)
--   + Alembic Revisions `0001_phase1_foundation` .. `0008_fyers_historical_market_data`
--   + Reference Data Seeder (`core/persistence/seed.py` / `deploy.run_migrations --seed`)
--
-- Safety & Operational Properties:
--   - Idempotent / safely rerunnable in Neon SQL Editor
--   - Wrapped in an atomic `BEGIN; ... UPDATE instruments SET data_provider = 'FYERS', provider_symbol = 'NSE:NIFTY50-INDEX' WHERE symbol = 'NIFTY' AND (provider_symbol IS NULL OR provider_symbol = '');
UPDATE instruments SET data_provider = 'FYERS', provider_symbol = 'NSE:NIFTYBANK-INDEX' WHERE symbol = 'BANKNIFTY' AND (provider_symbol IS NULL OR provider_symbol = '');

COMMIT;` transaction block
--   - Session timezone explicitly set to `Asia/Kolkata` (IST, UTC+05:30)
--   - Zero destructive operations (no DROP, TRUNCATE, or DELETE statements)
--   - Zero credentials, hostnames, or connection strings
--   - Stamps `alembic_version` to exact head `0008_fyers_historical_market_data`
-- ============================================================================

BEGIN;

SET TIME ZONE 'Asia/Kolkata';

-- ============================================================================
-- SECTION 1: ALEMBIC VERSION TABLE (WIDENED TO VARCHAR(128) FOR 34+ CHAR HEADS)
-- ============================================================================

CREATE TABLE IF NOT EXISTS alembic_version (
    version_num VARCHAR(128) NOT NULL,
    CONSTRAINT alembic_version_pkc PRIMARY KEY (version_num)
);

ALTER TABLE alembic_version
    ALTER COLUMN version_num TYPE VARCHAR(128);

-- ============================================================================
-- SECTION 2: APPLICATION TABLES & CONSTRAINTS (DEPENDENCY-SAFE ORDER)
-- ============================================================================

-- 1. users (0001_phase1_foundation)
CREATE TABLE IF NOT EXISTS users (
    id UUID NOT NULL,
    username VARCHAR(64) NOT NULL,
    email VARCHAR(255) NOT NULL,
    display_name VARCHAR(128) NOT NULL,
    role VARCHAR(32) NOT NULL,
    is_active BOOLEAN DEFAULT true NOT NULL,
    created_at TIMESTAMP WITH TIME ZONE NOT NULL,
    updated_at TIMESTAMP WITH TIME ZONE NOT NULL,
    CONSTRAINT pk_users PRIMARY KEY (id),
    CONSTRAINT uq_users_email UNIQUE (email),
    CONSTRAINT uq_users_username UNIQUE (username)
);

-- 2. paper_accounts (0001_phase1_foundation)
CREATE TABLE IF NOT EXISTS paper_accounts (
    id UUID NOT NULL,
    account_code VARCHAR(64) NOT NULL,
    account_name VARCHAR(128) NOT NULL,
    owner_user_id UUID,
    is_demo_capital BOOLEAN DEFAULT true NOT NULL,
    account_designation VARCHAR(32) NOT NULL,
    currency VARCHAR(8) NOT NULL,
    initial_capital NUMERIC(18, 4) NOT NULL,
    available_capital NUMERIC(18, 4) NOT NULL,
    realized_pnl_today NUMERIC(18, 4) NOT NULL,
    unrealized_pnl NUMERIC(18, 4) NOT NULL,
    paper_trading_halted BOOLEAN DEFAULT false NOT NULL,
    halt_reason TEXT,
    halted_at TIMESTAMP WITH TIME ZONE,
    halted_by VARCHAR(128),
    status VARCHAR(32) NOT NULL,
    version INTEGER DEFAULT 1 NOT NULL,
    created_at TIMESTAMP WITH TIME ZONE NOT NULL,
    updated_at TIMESTAMP WITH TIME ZONE NOT NULL,
    CONSTRAINT pk_paper_accounts PRIMARY KEY (id),
    CONSTRAINT ck_paper_accounts_initial_capital_nonneg CHECK (initial_capital >= 0),
    CONSTRAINT ck_paper_accounts_demo_only_v1 CHECK (is_demo_capital = true),
    CONSTRAINT fk_paper_accounts_owner_user_id FOREIGN KEY (owner_user_id) REFERENCES users (id) ON DELETE SET NULL,
    CONSTRAINT uq_paper_accounts_account_code UNIQUE (account_code)
);

-- 3. instruments (0001_phase1_foundation)
CREATE TABLE IF NOT EXISTS instruments (
    id UUID NOT NULL,
    symbol VARCHAR(32) NOT NULL,
    display_name VARCHAR(128) NOT NULL,
    exchange VARCHAR(32) NOT NULL,
    segment VARCHAR(64) NOT NULL,
    instrument_type VARCHAR(64) NOT NULL,
    currency VARCHAR(8) NOT NULL,
    lot_size INTEGER NOT NULL,
    tick_size NUMERIC(10, 4) NOT NULL,
    session_open_ist VARCHAR(8) NOT NULL,
    session_close_ist VARCHAR(8) NOT NULL,
    metadata_json JSON NOT NULL,
    is_active BOOLEAN DEFAULT true NOT NULL,
    created_at TIMESTAMP WITH TIME ZONE NOT NULL,
    updated_at TIMESTAMP WITH TIME ZONE NOT NULL,
    CONSTRAINT pk_instruments PRIMARY KEY (id),
    CONSTRAINT ck_instruments_lot_size_positive CHECK (lot_size >= 1),
    CONSTRAINT ck_instruments_tick_size_positive CHECK (tick_size > 0),
    CONSTRAINT uq_instruments_symbol UNIQUE (symbol)
);

-- 4. strategy_definitions (0001_phase1_foundation)
CREATE TABLE IF NOT EXISTS strategy_definitions (
    id UUID NOT NULL,
    code VARCHAR(64) NOT NULL,
    name VARCHAR(128) NOT NULL,
    category VARCHAR(64) NOT NULL,
    description TEXT NOT NULL,
    parameter_schema_json JSON NOT NULL,
    default_parameters_json JSON NOT NULL,
    supported_symbols_json JSON NOT NULL,
    is_enabled BOOLEAN DEFAULT true NOT NULL,
    created_at TIMESTAMP WITH TIME ZONE NOT NULL,
    updated_at TIMESTAMP WITH TIME ZONE NOT NULL,
    CONSTRAINT pk_strategy_definitions PRIMARY KEY (id),
    CONSTRAINT uq_strategy_definitions_code UNIQUE (code)
);

-- 5. strategy_versions (0001_phase1_foundation + 0002_strategy_lab_versioning)
CREATE TABLE IF NOT EXISTS strategy_versions (
    id UUID NOT NULL,
    strategy_definition_id UUID NOT NULL,
    version_number INTEGER NOT NULL,
    version_label VARCHAR(96) NOT NULL,
    instrument_symbol VARCHAR(32) NOT NULL,
    timeframe VARCHAR(16) DEFAULT '5m' NOT NULL,
    cloned_from_version_id UUID,
    parameters_json JSON NOT NULL,
    parameter_hash VARCHAR(64) NOT NULL,
    notes TEXT,
    created_by VARCHAR(128) NOT NULL,
    status VARCHAR(32) NOT NULL,
    created_at TIMESTAMP WITH TIME ZONE NOT NULL,
    CONSTRAINT pk_strategy_versions PRIMARY KEY (id),
    CONSTRAINT fk_strategy_versions_definition_id FOREIGN KEY (strategy_definition_id) REFERENCES strategy_definitions (id) ON DELETE RESTRICT,
    CONSTRAINT fk_strategy_versions_cloned_from FOREIGN KEY (cloned_from_version_id) REFERENCES strategy_versions (id) ON DELETE RESTRICT,
    CONSTRAINT uq_strategy_versions_def_inst_ver UNIQUE (strategy_definition_id, instrument_symbol, version_number),
    CONSTRAINT uq_strategy_versions_label UNIQUE (version_label)
);

-- 6. historical_datasets (0003_phase2b_backtesting)
CREATE TABLE IF NOT EXISTS historical_datasets (
    id UUID NOT NULL,
    dataset_code VARCHAR(96) NOT NULL,
    name VARCHAR(160) NOT NULL,
    instrument_symbol VARCHAR(32) NOT NULL,
    timeframe VARCHAR(16) NOT NULL,
    source_type VARCHAR(32) NOT NULL,
    data_version_hash VARCHAR(64) NOT NULL,
    row_count INTEGER DEFAULT 0 NOT NULL,
    start_timestamp TIMESTAMP WITH TIME ZONE,
    end_timestamp TIMESTAMP WITH TIME ZONE,
    raw_csv_text TEXT NOT NULL,
    uploaded_by VARCHAR(128) NOT NULL,
    created_at TIMESTAMP WITH TIME ZONE NOT NULL,
    CONSTRAINT pk_historical_datasets PRIMARY KEY (id),
    CONSTRAINT uq_historical_datasets_dataset_code UNIQUE (dataset_code)
);

-- 7. historical_candles (0003_phase2b_backtesting)
CREATE TABLE IF NOT EXISTS historical_candles (
    id UUID NOT NULL,
    dataset_id UUID NOT NULL,
    instrument_symbol VARCHAR(32) NOT NULL,
    timeframe VARCHAR(16) NOT NULL,
    bar_timestamp TIMESTAMP WITH TIME ZONE NOT NULL,
    open NUMERIC(18, 4) NOT NULL,
    high NUMERIC(18, 4) NOT NULL,
    low NUMERIC(18, 4) NOT NULL,
    close NUMERIC(18, 4) NOT NULL,
    volume INTEGER DEFAULT 0 NOT NULL,
    CONSTRAINT pk_historical_candles PRIMARY KEY (id),
    CONSTRAINT fk_historical_candles_dataset_id FOREIGN KEY (dataset_id) REFERENCES historical_datasets (id) ON DELETE CASCADE,
    CONSTRAINT uq_historical_candles_dataset_ts UNIQUE (dataset_id, bar_timestamp)
);

-- 8. backtest_runs (0003_phase2b_backtesting)
CREATE TABLE IF NOT EXISTS backtest_runs (
    id UUID NOT NULL,
    run_code VARCHAR(64) NOT NULL,
    strategy_version_id UUID NOT NULL,
    strategy_version_label VARCHAR(96) NOT NULL,
    instrument_symbol VARCHAR(32) NOT NULL,
    timeframe VARCHAR(16) NOT NULL,
    dataset_id VARCHAR(128) NOT NULL,
    dataset_name VARCHAR(160) NOT NULL,
    dataset_source VARCHAR(32) NOT NULL,
    data_version_hash VARCHAR(64) NOT NULL,
    reproducibility_fingerprint VARCHAR(64) NOT NULL,
    input_parameters_json JSON NOT NULL,
    execution_assumptions_json JSON NOT NULL,
    data_quality_report_json JSON NOT NULL,
    metrics_json JSON NOT NULL,
    equity_curve_json JSON NOT NULL,
    drawdown_curve_json JSON NOT NULL,
    monthly_returns_json JSON NOT NULL,
    data_start_timestamp TIMESTAMP WITH TIME ZONE,
    data_end_timestamp TIMESTAMP WITH TIME ZONE,
    started_at TIMESTAMP WITH TIME ZONE NOT NULL,
    completed_at TIMESTAMP WITH TIME ZONE NOT NULL,
    status VARCHAR(32) NOT NULL,
    engine_version VARCHAR(64) NOT NULL,
    created_by VARCHAR(128) NOT NULL,
    correlation_id VARCHAR(64) NOT NULL,
    CONSTRAINT pk_backtest_runs PRIMARY KEY (id),
    CONSTRAINT fk_backtest_runs_strategy_version_id FOREIGN KEY (strategy_version_id) REFERENCES strategy_versions (id) ON DELETE RESTRICT,
    CONSTRAINT uq_backtest_runs_run_code UNIQUE (run_code)
);

-- 9. backtest_trades (0003_phase2b_backtesting)
CREATE TABLE IF NOT EXISTS backtest_trades (
    id UUID NOT NULL,
    backtest_run_id UUID NOT NULL,
    deterministic_trade_id UUID NOT NULL,
    trade_number INTEGER NOT NULL,
    strategy_version_id UUID NOT NULL,
    strategy_version_label VARCHAR(96) NOT NULL,
    instrument_symbol VARCHAR(32) NOT NULL,
    direction VARCHAR(16) NOT NULL,
    entry_signal_time TIMESTAMP WITH TIME ZONE NOT NULL,
    entry_time TIMESTAMP WITH TIME ZONE NOT NULL,
    raw_entry_price NUMERIC(18, 4) NOT NULL,
    entry_price NUMERIC(18, 4) NOT NULL,
    quantity INTEGER NOT NULL,
    exit_time TIMESTAMP WITH TIME ZONE NOT NULL,
    raw_exit_price NUMERIC(18, 4) NOT NULL,
    exit_price NUMERIC(18, 4) NOT NULL,
    exit_reason VARCHAR(32) NOT NULL,
    gross_pnl NUMERIC(18, 4) NOT NULL,
    charges NUMERIC(18, 4) NOT NULL,
    slippage_cost NUMERIC(18, 4) NOT NULL,
    net_pnl NUMERIC(18, 4) NOT NULL,
    bars_held INTEGER DEFAULT 1 NOT NULL,
    CONSTRAINT pk_backtest_trades PRIMARY KEY (id),
    CONSTRAINT fk_backtest_trades_backtest_run_id FOREIGN KEY (backtest_run_id) REFERENCES backtest_runs (id) ON DELETE CASCADE,
    CONSTRAINT fk_backtest_trades_strategy_version_id FOREIGN KEY (strategy_version_id) REFERENCES strategy_versions (id) ON DELETE RESTRICT,
    CONSTRAINT uq_backtest_trades_run_num UNIQUE (backtest_run_id, trade_number)
);

-- 10. replay_sessions (0004_phase3a_market_replay)
CREATE TABLE IF NOT EXISTS replay_sessions (
    id UUID NOT NULL,
    session_code VARCHAR(64) NOT NULL,
    instrument_symbol VARCHAR(32) NOT NULL,
    timeframe VARCHAR(16) NOT NULL,
    strategy_version_id UUID NOT NULL,
    strategy_version_label VARCHAR(96) NOT NULL,
    dataset_id VARCHAR(128) NOT NULL,
    dataset_name VARCHAR(160) NOT NULL,
    data_version_hash VARCHAR(64) NOT NULL,
    historical_session_date VARCHAR(32) NOT NULL,
    warmup_bars_count INTEGER DEFAULT 0 NOT NULL,
    total_session_candles INTEGER DEFAULT 0 NOT NULL,
    candles_processed INTEGER DEFAULT 0 NOT NULL,
    signals_emitted INTEGER DEFAULT 0 NOT NULL,
    replay_speed VARCHAR(16) DEFAULT '1x' NOT NULL,
    status VARCHAR(32) NOT NULL,
    stale_threshold_seconds INTEGER DEFAULT 15 NOT NULL,
    replay_clock_ist TIMESTAMP WITH TIME ZONE,
    last_candle_received_at TIMESTAMP WITH TIME ZONE,
    last_step_wall_clock TIMESTAMP WITH TIME ZONE,
    fractional_bar_carry NUMERIC(12, 6) DEFAULT 0 NOT NULL,
    last_candle_json JSON,
    latest_evaluation_json JSON,
    latest_signal_json JSON,
    started_at TIMESTAMP WITH TIME ZONE,
    paused_at TIMESTAMP WITH TIME ZONE,
    stopped_at TIMESTAMP WITH TIME ZONE,
    completed_at TIMESTAMP WITH TIME ZONE,
    created_by VARCHAR(128) NOT NULL,
    created_at TIMESTAMP WITH TIME ZONE NOT NULL,
    updated_at TIMESTAMP WITH TIME ZONE NOT NULL,
    CONSTRAINT pk_replay_sessions PRIMARY KEY (id),
    CONSTRAINT fk_replay_sessions_strategy_version_id FOREIGN KEY (strategy_version_id) REFERENCES strategy_versions (id) ON DELETE RESTRICT,
    CONSTRAINT uq_replay_sessions_session_code UNIQUE (session_code)
);

-- 11. strategy_instances (0005_phase3b_paper_trading_oms)
CREATE TABLE IF NOT EXISTS strategy_instances (
    id UUID NOT NULL,
    instance_code VARCHAR(64) NOT NULL,
    account_id UUID NOT NULL,
    strategy_version_id UUID NOT NULL,
    strategy_version_label VARCHAR(96) NOT NULL,
    instrument_symbol VARCHAR(32) NOT NULL,
    timeframe VARCHAR(16) DEFAULT '5m' NOT NULL,
    status VARCHAR(32) NOT NULL,
    order_type VARCHAR(16) DEFAULT 'MARKET' NOT NULL,
    limit_offset_pts NUMERIC(18, 4) DEFAULT 0 NOT NULL,
    execution_latency_ms INTEGER DEFAULT 120 NOT NULL,
    latency_bars INTEGER DEFAULT 1 NOT NULL,
    slippage_bps NUMERIC(10, 4) DEFAULT 2.0 NOT NULL,
    partial_fill_enabled BOOLEAN DEFAULT false NOT NULL,
    max_fill_ratio_per_bar NUMERIC(6, 4) DEFAULT 1.0 NOT NULL,
    valid_for_bars INTEGER,
    auto_execute_signals BOOLEAN DEFAULT true NOT NULL,
    signals_emitted INTEGER DEFAULT 0 NOT NULL,
    orders_created INTEGER DEFAULT 0 NOT NULL,
    created_by VARCHAR(128) NOT NULL,
    started_at TIMESTAMP WITH TIME ZONE,
    paused_at TIMESTAMP WITH TIME ZONE,
    stopped_at TIMESTAMP WITH TIME ZONE,
    created_at TIMESTAMP WITH TIME ZONE NOT NULL,
    updated_at TIMESTAMP WITH TIME ZONE NOT NULL,
    CONSTRAINT pk_strategy_instances PRIMARY KEY (id),
    CONSTRAINT fk_strategy_instances_account_id FOREIGN KEY (account_id) REFERENCES paper_accounts (id) ON DELETE RESTRICT,
    CONSTRAINT fk_strategy_instances_strategy_version_id FOREIGN KEY (strategy_version_id) REFERENCES strategy_versions (id) ON DELETE RESTRICT,
    CONSTRAINT uq_strategy_instances_instance_code UNIQUE (instance_code)
);

-- 12. signals (0004_phase3a_market_replay + 0005_phase3b_paper_trading_oms)
CREATE TABLE IF NOT EXISTS signals (
    id UUID NOT NULL,
    signal_code VARCHAR(64) NOT NULL,
    deduplication_key VARCHAR(160) NOT NULL,
    replay_session_id UUID,
    strategy_instance_id UUID,
    strategy_version_id UUID NOT NULL,
    strategy_version_label VARCHAR(96) NOT NULL,
    instrument_symbol VARCHAR(32) NOT NULL,
    timeframe VARCHAR(16) NOT NULL,
    side VARCHAR(16) NOT NULL,
    direction VARCHAR(32) NOT NULL,
    exit_reason VARCHAR(32),
    bar_timestamp TIMESTAMP WITH TIME ZONE NOT NULL,
    emitted_at TIMESTAMP WITH TIME ZONE NOT NULL,
    expires_at TIMESTAMP WITH TIME ZONE NOT NULL,
    reference_price NUMERIC(18, 4) NOT NULL,
    status VARCHAR(32) NOT NULL,
    rationale TEXT NOT NULL,
    indicators_json JSON NOT NULL,
    market_event_id VARCHAR(64) NOT NULL,
    correlation_id VARCHAR(64) NOT NULL,
    CONSTRAINT pk_signals PRIMARY KEY (id),
    CONSTRAINT fk_signals_replay_session_id FOREIGN KEY (replay_session_id) REFERENCES replay_sessions (id) ON DELETE RESTRICT,
    CONSTRAINT fk_signals_strategy_instance_id FOREIGN KEY (strategy_instance_id) REFERENCES strategy_instances (id) ON DELETE RESTRICT,
    CONSTRAINT fk_signals_strategy_version_id FOREIGN KEY (strategy_version_id) REFERENCES strategy_versions (id) ON DELETE RESTRICT,
    CONSTRAINT uq_signals_signal_code UNIQUE (signal_code),
    CONSTRAINT uq_signals_deduplication_key UNIQUE (deduplication_key)
);

-- 13. paper_orders (0005_phase3b_paper_trading_oms)
CREATE TABLE IF NOT EXISTS paper_orders (
    id UUID NOT NULL,
    order_code VARCHAR(64) NOT NULL,
    client_order_key VARCHAR(160) NOT NULL,
    correlation_id VARCHAR(64) NOT NULL,
    account_id UUID NOT NULL,
    strategy_instance_id UUID,
    strategy_version_id UUID,
    strategy_version_label VARCHAR(96),
    signal_id UUID,
    replay_session_id UUID,
    order_source VARCHAR(32) NOT NULL,
    reduce_only BOOLEAN DEFAULT false NOT NULL,
    exit_reason VARCHAR(64),
    instrument_symbol VARCHAR(32) NOT NULL,
    timeframe VARCHAR(16) DEFAULT '5m' NOT NULL,
    side VARCHAR(16) NOT NULL,
    order_type VARCHAR(16) NOT NULL,
    quantity INTEGER NOT NULL,
    filled_quantity INTEGER DEFAULT 0 NOT NULL,
    leaves_quantity INTEGER NOT NULL,
    requested_reference_price NUMERIC(18, 4),
    limit_price NUMERIC(18, 4),
    stop_price NUMERIC(18, 4),
    stop_triggered BOOLEAN DEFAULT false NOT NULL,
    stop_triggered_at TIMESTAMP WITH TIME ZONE,
    average_fill_price NUMERIC(18, 4),
    total_slippage_cost NUMERIC(18, 4) DEFAULT 0 NOT NULL,
    total_charges NUMERIC(18, 4) DEFAULT 0 NOT NULL,
    execution_latency_ms INTEGER DEFAULT 120 NOT NULL,
    latency_bars INTEGER DEFAULT 1 NOT NULL,
    created_bar_timestamp TIMESTAMP WITH TIME ZONE,
    bars_waited INTEGER DEFAULT 0 NOT NULL,
    slippage_bps NUMERIC(10, 4) DEFAULT 2.0 NOT NULL,
    charges_bps NUMERIC(10, 4) DEFAULT 1.5 NOT NULL,
    per_order_fee_inr NUMERIC(12, 4) DEFAULT 20.0 NOT NULL,
    max_fill_ratio_per_bar NUMERIC(6, 4) DEFAULT 1.0 NOT NULL,
    simulate_rejection BOOLEAN DEFAULT false NOT NULL,
    simulated_rejection_reason VARCHAR(255),
    valid_for_bars INTEGER,
    expires_at TIMESTAMP WITH TIME ZONE,
    target_stop_loss_price NUMERIC(18, 4),
    target_profit_price NUMERIC(18, 4),
    status VARCHAR(32) NOT NULL,
    risk_check_passed BOOLEAN,
    risk_check_details_json JSON NOT NULL,
    rejection_reason TEXT,
    cancel_reason TEXT,
    submitted_at TIMESTAMP WITH TIME ZONE,
    acknowledged_at TIMESTAMP WITH TIME ZONE,
    completed_at TIMESTAMP WITH TIME ZONE,
    created_by VARCHAR(128) NOT NULL,
    created_at TIMESTAMP WITH TIME ZONE NOT NULL,
    updated_at TIMESTAMP WITH TIME ZONE NOT NULL,
    CONSTRAINT pk_paper_orders PRIMARY KEY (id),
    CONSTRAINT ck_paper_orders_qty_pos CHECK (quantity > 0),
    CONSTRAINT ck_paper_orders_filled_nonneg CHECK (filled_quantity >= 0),
    CONSTRAINT ck_paper_orders_leaves_nonneg CHECK (leaves_quantity >= 0),
    CONSTRAINT fk_paper_orders_account_id FOREIGN KEY (account_id) REFERENCES paper_accounts (id) ON DELETE RESTRICT,
    CONSTRAINT fk_paper_orders_strategy_instance_id FOREIGN KEY (strategy_instance_id) REFERENCES strategy_instances (id) ON DELETE RESTRICT,
    CONSTRAINT fk_paper_orders_strategy_version_id FOREIGN KEY (strategy_version_id) REFERENCES strategy_versions (id) ON DELETE RESTRICT,
    CONSTRAINT fk_paper_orders_signal_id FOREIGN KEY (signal_id) REFERENCES signals (id) ON DELETE RESTRICT,
    CONSTRAINT fk_paper_orders_replay_session_id FOREIGN KEY (replay_session_id) REFERENCES replay_sessions (id) ON DELETE RESTRICT,
    CONSTRAINT uq_paper_orders_order_code UNIQUE (order_code),
    CONSTRAINT uq_paper_orders_client_order_key UNIQUE (client_order_key),
    CONSTRAINT uq_paper_orders_signal_id UNIQUE (signal_id)
);

-- 14. order_state_events (0005_phase3b_paper_trading_oms — append-only)
CREATE TABLE IF NOT EXISTS order_state_events (
    id UUID NOT NULL,
    order_id UUID NOT NULL,
    previous_status VARCHAR(32),
    new_status VARCHAR(32) NOT NULL,
    occurred_at TIMESTAMP WITH TIME ZONE NOT NULL,
    actor VARCHAR(128) NOT NULL,
    reason TEXT NOT NULL,
    details_json JSON NOT NULL,
    correlation_id VARCHAR(64) NOT NULL,
    CONSTRAINT pk_order_state_events PRIMARY KEY (id),
    CONSTRAINT fk_order_state_events_order_id FOREIGN KEY (order_id) REFERENCES paper_orders (id) ON DELETE RESTRICT
);

-- 15. paper_fills (0005_phase3b_paper_trading_oms — append-only)
CREATE TABLE IF NOT EXISTS paper_fills (
    id UUID NOT NULL,
    fill_code VARCHAR(64) NOT NULL,
    fill_idempotency_key VARCHAR(160) NOT NULL,
    order_id UUID NOT NULL,
    account_id UUID NOT NULL,
    strategy_instance_id UUID,
    strategy_version_id UUID,
    strategy_version_label VARCHAR(96),
    instrument_symbol VARCHAR(32) NOT NULL,
    side VARCHAR(16) NOT NULL,
    fill_quantity INTEGER NOT NULL,
    raw_market_price NUMERIC(18, 4) NOT NULL,
    fill_price NUMERIC(18, 4) NOT NULL,
    slippage_cost NUMERIC(18, 4) DEFAULT 0 NOT NULL,
    charges NUMERIC(18, 4) DEFAULT 0 NOT NULL,
    simulated_latency_ms INTEGER DEFAULT 0 NOT NULL,
    bar_timestamp TIMESTAMP WITH TIME ZONE NOT NULL,
    executed_at TIMESTAMP WITH TIME ZONE NOT NULL,
    market_event_id VARCHAR(64),
    liquidity_note VARCHAR(160) NOT NULL,
    correlation_id VARCHAR(64) NOT NULL,
    CONSTRAINT pk_paper_fills PRIMARY KEY (id),
    CONSTRAINT ck_paper_fills_qty_pos CHECK (fill_quantity > 0),
    CONSTRAINT fk_paper_fills_order_id FOREIGN KEY (order_id) REFERENCES paper_orders (id) ON DELETE RESTRICT,
    CONSTRAINT fk_paper_fills_account_id FOREIGN KEY (account_id) REFERENCES paper_accounts (id) ON DELETE RESTRICT,
    CONSTRAINT fk_paper_fills_strategy_instance_id FOREIGN KEY (strategy_instance_id) REFERENCES strategy_instances (id) ON DELETE RESTRICT,
    CONSTRAINT fk_paper_fills_strategy_version_id FOREIGN KEY (strategy_version_id) REFERENCES strategy_versions (id) ON DELETE RESTRICT,
    CONSTRAINT uq_paper_fills_fill_code UNIQUE (fill_code),
    CONSTRAINT uq_paper_fills_idempotency_key UNIQUE (fill_idempotency_key)
);

-- 16. paper_positions (0005_phase3b_paper_trading_oms)
CREATE TABLE IF NOT EXISTS paper_positions (
    id UUID NOT NULL,
    position_code VARCHAR(64) NOT NULL,
    account_id UUID NOT NULL,
    strategy_instance_id UUID,
    strategy_version_id UUID,
    strategy_version_label VARCHAR(96) NOT NULL,
    instrument_symbol VARCHAR(32) NOT NULL,
    timeframe VARCHAR(16) DEFAULT '5m' NOT NULL,
    side VARCHAR(16) NOT NULL,
    quantity INTEGER NOT NULL,
    opened_quantity INTEGER NOT NULL,
    closed_quantity INTEGER DEFAULT 0 NOT NULL,
    average_entry_price NUMERIC(18, 4) NOT NULL,
    average_exit_price NUMERIC(18, 4),
    last_price NUMERIC(18, 4) NOT NULL,
    highest_price_since_entry NUMERIC(18, 4) NOT NULL,
    lowest_price_since_entry NUMERIC(18, 4) NOT NULL,
    bars_held INTEGER DEFAULT 0 NOT NULL,
    realized_pnl NUMERIC(18, 4) DEFAULT 0 NOT NULL,
    unrealized_pnl NUMERIC(18, 4) DEFAULT 0 NOT NULL,
    unallocated_entry_charges NUMERIC(18, 4) DEFAULT 0 NOT NULL,
    total_charges NUMERIC(18, 4) DEFAULT 0 NOT NULL,
    stop_loss_price NUMERIC(18, 4),
    target_price NUMERIC(18, 4),
    status VARCHAR(32) NOT NULL,
    opened_at TIMESTAMP WITH TIME ZONE NOT NULL,
    last_mark_at TIMESTAMP WITH TIME ZONE NOT NULL,
    closed_at TIMESTAMP WITH TIME ZONE,
    exit_reason VARCHAR(64),
    updated_at TIMESTAMP WITH TIME ZONE NOT NULL,
    CONSTRAINT pk_paper_positions PRIMARY KEY (id),
    CONSTRAINT ck_paper_positions_qty_nonneg CHECK (quantity >= 0),
    CONSTRAINT fk_paper_positions_account_id FOREIGN KEY (account_id) REFERENCES paper_accounts (id) ON DELETE RESTRICT,
    CONSTRAINT fk_paper_positions_strategy_instance_id FOREIGN KEY (strategy_instance_id) REFERENCES strategy_instances (id) ON DELETE RESTRICT,
    CONSTRAINT fk_paper_positions_strategy_version_id FOREIGN KEY (strategy_version_id) REFERENCES strategy_versions (id) ON DELETE RESTRICT,
    CONSTRAINT uq_paper_positions_position_code UNIQUE (position_code)
);

-- 17. position_events (0005_phase3b_paper_trading_oms — append-only)
CREATE TABLE IF NOT EXISTS position_events (
    id UUID NOT NULL,
    event_idempotency_key VARCHAR(160) NOT NULL,
    fill_id UUID NOT NULL,
    position_id UUID NOT NULL,
    event_type VARCHAR(32) NOT NULL,
    delta_quantity INTEGER NOT NULL,
    resulting_quantity INTEGER NOT NULL,
    fill_price NUMERIC(18, 4) NOT NULL,
    realized_pnl_delta NUMERIC(18, 4) DEFAULT 0 NOT NULL,
    charges_delta NUMERIC(18, 4) DEFAULT 0 NOT NULL,
    occurred_at TIMESTAMP WITH TIME ZONE NOT NULL,
    correlation_id VARCHAR(64) NOT NULL,
    CONSTRAINT pk_position_events PRIMARY KEY (id),
    CONSTRAINT fk_position_events_fill_id FOREIGN KEY (fill_id) REFERENCES paper_fills (id) ON DELETE RESTRICT,
    CONSTRAINT fk_position_events_position_id FOREIGN KEY (position_id) REFERENCES paper_positions (id) ON DELETE RESTRICT,
    CONSTRAINT uq_position_events_idempotency_key UNIQUE (event_idempotency_key),
    CONSTRAINT uq_position_events_fill_id UNIQUE (fill_id)
);

-- 18. account_risk_configs (0006_phase4_risk_engine_and_controls)
CREATE TABLE IF NOT EXISTS account_risk_configs (
    id UUID NOT NULL,
    account_id UUID NOT NULL,
    is_demo_settings BOOLEAN DEFAULT true NOT NULL,
    max_daily_loss_inr NUMERIC(18, 4) DEFAULT 10000.00 NOT NULL,
    max_open_positions INTEGER DEFAULT 4 NOT NULL,
    max_gross_exposure_inr NUMERIC(18, 4) DEFAULT 6000000.00 NOT NULL,
    max_order_value_inr NUMERIC(18, 4) DEFAULT 3000000.00 NOT NULL,
    max_trades_per_day INTEGER DEFAULT 15 NOT NULL,
    max_consecutive_losses INTEGER DEFAULT 3 NOT NULL,
    trading_session_enabled BOOLEAN DEFAULT true NOT NULL,
    session_start_ist VARCHAR(16) DEFAULT '09:15:00' NOT NULL,
    session_end_ist VARCHAR(16) DEFAULT '15:30:00' NOT NULL,
    global_kill_switch_active BOOLEAN DEFAULT false NOT NULL,
    kill_switch_reason TEXT,
    kill_switch_activated_at TIMESTAMP WITH TIME ZONE,
    kill_switch_activated_by VARCHAR(128),
    market_data_stale_threshold_seconds INTEGER DEFAULT 10 NOT NULL,
    max_signal_age_seconds INTEGER DEFAULT 15 NOT NULL,
    enforce_system_health_gate BOOLEAN DEFAULT true NOT NULL,
    simulated_system_degraded BOOLEAN DEFAULT false NOT NULL,
    enforce_position_reconciliation_gate BOOLEAN DEFAULT true NOT NULL,
    default_max_quantity INTEGER DEFAULT 300 NOT NULL,
    default_max_risk_per_trade_inr NUMERIC(18, 4) DEFAULT 2500.00 NOT NULL,
    default_strategy_daily_loss_inr NUMERIC(18, 4) DEFAULT 6000.00 NOT NULL,
    default_max_strategy_drawdown_inr NUMERIC(18, 4) DEFAULT 8000.00 NOT NULL,
    default_max_trades_per_strategy_day INTEGER DEFAULT 5 NOT NULL,
    default_cooldown_seconds_after_loss INTEGER DEFAULT 300 NOT NULL,
    default_max_holding_duration_seconds INTEGER DEFAULT 14400 NOT NULL,
    default_allowed_instruments_json JSON NOT NULL,
    default_allowed_order_types_json JSON NOT NULL,
    updated_by VARCHAR(128) NOT NULL,
    created_at TIMESTAMP WITH TIME ZONE NOT NULL,
    updated_at TIMESTAMP WITH TIME ZONE NOT NULL,
    CONSTRAINT pk_account_risk_configs PRIMARY KEY (id),
    CONSTRAINT fk_account_risk_configs_account_id FOREIGN KEY (account_id) REFERENCES paper_accounts (id) ON DELETE RESTRICT,
    CONSTRAINT uq_account_risk_configs_account_id UNIQUE (account_id)
);

-- 19. strategy_risk_configs (0006_phase4_risk_engine_and_controls)
CREATE TABLE IF NOT EXISTS strategy_risk_configs (
    id UUID NOT NULL,
    account_id UUID NOT NULL,
    strategy_version_id UUID NOT NULL,
    strategy_version_label VARCHAR(96) NOT NULL,
    max_quantity INTEGER DEFAULT 300 NOT NULL,
    max_risk_per_trade_inr NUMERIC(18, 4) DEFAULT 2500.00 NOT NULL,
    strategy_daily_loss_inr NUMERIC(18, 4) DEFAULT 6000.00 NOT NULL,
    max_strategy_drawdown_inr NUMERIC(18, 4) DEFAULT 8000.00 NOT NULL,
    max_trades_per_day INTEGER DEFAULT 5 NOT NULL,
    cooldown_seconds_after_loss INTEGER DEFAULT 300 NOT NULL,
    max_holding_duration_seconds INTEGER DEFAULT 14400 NOT NULL,
    allowed_instruments_json JSON NOT NULL,
    allowed_order_types_json JSON NOT NULL,
    is_enabled BOOLEAN DEFAULT true NOT NULL,
    updated_by VARCHAR(128) NOT NULL,
    created_at TIMESTAMP WITH TIME ZONE NOT NULL,
    updated_at TIMESTAMP WITH TIME ZONE NOT NULL,
    CONSTRAINT pk_strategy_risk_configs PRIMARY KEY (id),
    CONSTRAINT fk_strategy_risk_configs_account_id FOREIGN KEY (account_id) REFERENCES paper_accounts (id) ON DELETE RESTRICT,
    CONSTRAINT fk_strategy_risk_configs_strategy_version_id FOREIGN KEY (strategy_version_id) REFERENCES strategy_versions (id) ON DELETE RESTRICT,
    CONSTRAINT uq_strategy_risk_configs_acct_ver UNIQUE (account_id, strategy_version_id)
);

-- 20. risk_decisions (0006_phase4_risk_engine_and_controls — append-only)
CREATE TABLE IF NOT EXISTS risk_decisions (
    id UUID NOT NULL,
    decision_code VARCHAR(64) NOT NULL,
    decision VARCHAR(16) NOT NULL,
    primary_rule VARCHAR(64) NOT NULL,
    current_value VARCHAR(128) NOT NULL,
    limit_value VARCHAR(128) NOT NULL,
    reason TEXT NOT NULL,
    account_id UUID NOT NULL,
    order_id UUID,
    client_order_key VARCHAR(160),
    signal_id UUID,
    strategy_instance_id UUID,
    strategy_version_id UUID,
    strategy_label VARCHAR(96) NOT NULL,
    instrument_symbol VARCHAR(32) NOT NULL,
    side VARCHAR(16) NOT NULL,
    order_type VARCHAR(16) NOT NULL,
    proposed_quantity INTEGER NOT NULL,
    proposed_price NUMERIC(18, 4),
    proposed_notional_inr NUMERIC(18, 4) DEFAULT 0 NOT NULL,
    checks_json JSON NOT NULL,
    correlation_id VARCHAR(64) NOT NULL,
    evaluated_at TIMESTAMP WITH TIME ZONE NOT NULL,
    CONSTRAINT pk_risk_decisions PRIMARY KEY (id),
    CONSTRAINT fk_risk_decisions_account_id FOREIGN KEY (account_id) REFERENCES paper_accounts (id) ON DELETE RESTRICT,
    CONSTRAINT fk_risk_decisions_order_id FOREIGN KEY (order_id) REFERENCES paper_orders (id) ON DELETE RESTRICT,
    CONSTRAINT fk_risk_decisions_signal_id FOREIGN KEY (signal_id) REFERENCES signals (id) ON DELETE RESTRICT,
    CONSTRAINT fk_risk_decisions_strategy_instance_id FOREIGN KEY (strategy_instance_id) REFERENCES strategy_instances (id) ON DELETE RESTRICT,
    CONSTRAINT fk_risk_decisions_strategy_version_id FOREIGN KEY (strategy_version_id) REFERENCES strategy_versions (id) ON DELETE RESTRICT,
    CONSTRAINT uq_risk_decisions_decision_code UNIQUE (decision_code)
);

-- 21. reconciliation_runs (0008_fyers_historical_market_data — append-only)
CREATE TABLE IF NOT EXISTS reconciliation_runs (
    id UUID NOT NULL,
    run_code VARCHAR(64) NOT NULL,
    account_id UUID,
    status VARCHAR(32) NOT NULL,
    repair_applied BOOLEAN DEFAULT false NOT NULL,
    fills_checked INTEGER DEFAULT 0 NOT NULL,
    open_orders_checked INTEGER DEFAULT 0 NOT NULL,
    positions_checked INTEGER DEFAULT 0 NOT NULL,
    discrepancies_count INTEGER DEFAULT 0 NOT NULL,
    repairs_count INTEGER DEFAULT 0 NOT NULL,
    checks_json JSON NOT NULL,
    discrepancies_json JSON NOT NULL,
    repairs_json JSON NOT NULL,
    actor VARCHAR(128) NOT NULL,
    correlation_id VARCHAR(64) NOT NULL,
    started_at TIMESTAMP WITH TIME ZONE NOT NULL,
    completed_at TIMESTAMP WITH TIME ZONE NOT NULL,
    CONSTRAINT pk_reconciliation_runs PRIMARY KEY (id),
    CONSTRAINT fk_reconciliation_runs_account_id FOREIGN KEY (account_id) REFERENCES paper_accounts (id) ON DELETE RESTRICT,
    CONSTRAINT uq_reconciliation_runs_run_code UNIQUE (run_code)
);

-- 22. audit_events (0001_phase1_foundation + 0008_fyers_historical_market_data — append-only)
CREATE TABLE IF NOT EXISTS audit_events (
    id UUID NOT NULL,
    occurred_at TIMESTAMP WITH TIME ZONE NOT NULL,
    correlation_id VARCHAR(64) NOT NULL,
    actor VARCHAR(128) NOT NULL,
    actor_type VARCHAR(32) NOT NULL,
    category VARCHAR(32) NOT NULL,
    action VARCHAR(128) NOT NULL,
    event_type VARCHAR(128),
    severity VARCHAR(32) NOT NULL,
    entity_type VARCHAR(64),
    entity_id VARCHAR(128),
    strategy_id VARCHAR(64),
    signal_id VARCHAR(64),
    order_id VARCHAR(64),
    source VARCHAR(64),
    summary TEXT NOT NULL,
    details_json JSON NOT NULL,
    CONSTRAINT pk_audit_events PRIMARY KEY (id)
);

-- 23. system_health_events (0001_phase1_foundation)
CREATE TABLE IF NOT EXISTS system_health_events (
    id UUID NOT NULL,
    recorded_at TIMESTAMP WITH TIME ZONE NOT NULL,
    component VARCHAR(32) NOT NULL,
    status VARCHAR(32) NOT NULL,
    latency_ms NUMERIC(10, 2),
    message TEXT NOT NULL,
    details_json JSON NOT NULL,
    correlation_id VARCHAR(64),
    CONSTRAINT pk_system_health_events PRIMARY KEY (id)
);

-- ============================================================================
-- SECTION 3: ALL 97 AUTHORITATIVE INDEXES (IDEMPOTENT)
-- ============================================================================

-- users (1)
CREATE UNIQUE INDEX IF NOT EXISTS ix_users_username ON users (username);

-- paper_accounts (1)
CREATE UNIQUE INDEX IF NOT EXISTS ix_paper_accounts_account_code ON paper_accounts (account_code);

-- instruments (1)
CREATE UNIQUE INDEX IF NOT EXISTS ix_instruments_symbol ON instruments (symbol);

-- strategy_definitions (1)
CREATE UNIQUE INDEX IF NOT EXISTS ix_strategy_definitions_code ON strategy_definitions (code);

-- strategy_versions (4)
CREATE INDEX IF NOT EXISTS ix_strategy_versions_strategy_definition_id ON strategy_versions (strategy_definition_id);
CREATE INDEX IF NOT EXISTS ix_strategy_versions_parameter_hash ON strategy_versions (parameter_hash);
CREATE UNIQUE INDEX IF NOT EXISTS ix_strategy_versions_version_label ON strategy_versions (version_label);
CREATE INDEX IF NOT EXISTS ix_strategy_versions_instrument_symbol ON strategy_versions (instrument_symbol);

-- historical_datasets (4)
CREATE UNIQUE INDEX IF NOT EXISTS ix_historical_datasets_dataset_code ON historical_datasets (dataset_code);
CREATE INDEX IF NOT EXISTS ix_historical_datasets_instrument_symbol ON historical_datasets (instrument_symbol);
CREATE INDEX IF NOT EXISTS ix_historical_datasets_data_version_hash ON historical_datasets (data_version_hash);
CREATE INDEX IF NOT EXISTS ix_historical_datasets_inst_tf ON historical_datasets (instrument_symbol, timeframe);

-- historical_candles (2)
CREATE INDEX IF NOT EXISTS ix_historical_candles_dataset_id ON historical_candles (dataset_id);
CREATE INDEX IF NOT EXISTS ix_historical_candles_symbol_tf_ts ON historical_candles (instrument_symbol, timeframe, bar_timestamp);

-- backtest_runs (9)
CREATE UNIQUE INDEX IF NOT EXISTS ix_backtest_runs_run_code ON backtest_runs (run_code);
CREATE INDEX IF NOT EXISTS ix_backtest_runs_strategy_version_id ON backtest_runs (strategy_version_id);
CREATE INDEX IF NOT EXISTS ix_backtest_runs_instrument_symbol ON backtest_runs (instrument_symbol);
CREATE INDEX IF NOT EXISTS ix_backtest_runs_data_version_hash ON backtest_runs (data_version_hash);
CREATE INDEX IF NOT EXISTS ix_backtest_runs_reproducibility_fingerprint ON backtest_runs (reproducibility_fingerprint);
CREATE INDEX IF NOT EXISTS ix_backtest_runs_status ON backtest_runs (status);
CREATE INDEX IF NOT EXISTS ix_backtest_runs_correlation_id ON backtest_runs (correlation_id);
CREATE INDEX IF NOT EXISTS ix_backtest_runs_started_at_desc ON backtest_runs (started_at);
CREATE INDEX IF NOT EXISTS ix_backtest_runs_strategy_status ON backtest_runs (strategy_version_id, status);

-- backtest_trades (3)
CREATE INDEX IF NOT EXISTS ix_backtest_trades_backtest_run_id ON backtest_trades (backtest_run_id);
CREATE INDEX IF NOT EXISTS ix_backtest_trades_deterministic_trade_id ON backtest_trades (deterministic_trade_id);
CREATE INDEX IF NOT EXISTS ix_backtest_trades_run_entry ON backtest_trades (backtest_run_id, entry_time);

-- replay_sessions (6)
CREATE UNIQUE INDEX IF NOT EXISTS ix_replay_sessions_session_code ON replay_sessions (session_code);
CREATE INDEX IF NOT EXISTS ix_replay_sessions_instrument_symbol ON replay_sessions (instrument_symbol);
CREATE INDEX IF NOT EXISTS ix_replay_sessions_strategy_version_id ON replay_sessions (strategy_version_id);
CREATE INDEX IF NOT EXISTS ix_replay_sessions_status ON replay_sessions (status);
CREATE INDEX IF NOT EXISTS ix_replay_sessions_status_updated ON replay_sessions (status, updated_at);
CREATE INDEX IF NOT EXISTS ix_replay_sessions_inst_status ON replay_sessions (instrument_symbol, status);

-- strategy_instances (6)
CREATE UNIQUE INDEX IF NOT EXISTS ix_strategy_instances_instance_code ON strategy_instances (instance_code);
CREATE INDEX IF NOT EXISTS ix_strategy_instances_account_id ON strategy_instances (account_id);
CREATE INDEX IF NOT EXISTS ix_strategy_instances_strategy_version_id ON strategy_instances (strategy_version_id);
CREATE INDEX IF NOT EXISTS ix_strategy_instances_instrument_symbol ON strategy_instances (instrument_symbol);
CREATE INDEX IF NOT EXISTS ix_strategy_instances_status ON strategy_instances (status);
CREATE INDEX IF NOT EXISTS ix_strategy_instances_inst_status ON strategy_instances (instrument_symbol, status);

-- signals (8)
CREATE UNIQUE INDEX IF NOT EXISTS ix_signals_signal_code ON signals (signal_code);
CREATE INDEX IF NOT EXISTS ix_signals_replay_session_id ON signals (replay_session_id);
CREATE INDEX IF NOT EXISTS ix_signals_strategy_instance_id ON signals (strategy_instance_id);
CREATE INDEX IF NOT EXISTS ix_signals_strategy_version_id ON signals (strategy_version_id);
CREATE INDEX IF NOT EXISTS ix_signals_instrument_symbol ON signals (instrument_symbol);
CREATE INDEX IF NOT EXISTS ix_signals_status ON signals (status);
CREATE INDEX IF NOT EXISTS ix_signals_correlation_id ON signals (correlation_id);
CREATE INDEX IF NOT EXISTS ix_signals_session_bar ON signals (replay_session_id, bar_timestamp);

-- paper_orders (11)
CREATE UNIQUE INDEX IF NOT EXISTS ix_paper_orders_order_code ON paper_orders (order_code);
CREATE INDEX IF NOT EXISTS ix_paper_orders_correlation_id ON paper_orders (correlation_id);
CREATE INDEX IF NOT EXISTS ix_paper_orders_account_id ON paper_orders (account_id);
CREATE INDEX IF NOT EXISTS ix_paper_orders_strategy_instance_id ON paper_orders (strategy_instance_id);
CREATE INDEX IF NOT EXISTS ix_paper_orders_strategy_version_id ON paper_orders (strategy_version_id);
CREATE UNIQUE INDEX IF NOT EXISTS ix_paper_orders_signal_id ON paper_orders (signal_id);
CREATE INDEX IF NOT EXISTS ix_paper_orders_replay_session_id ON paper_orders (replay_session_id);
CREATE INDEX IF NOT EXISTS ix_paper_orders_instrument_symbol ON paper_orders (instrument_symbol);
CREATE INDEX IF NOT EXISTS ix_paper_orders_status ON paper_orders (status);
CREATE INDEX IF NOT EXISTS ix_paper_orders_inst_status ON paper_orders (instrument_symbol, status);
CREATE INDEX IF NOT EXISTS ix_paper_orders_created_desc ON paper_orders (created_at);

-- order_state_events (3)
CREATE INDEX IF NOT EXISTS ix_order_state_events_order_id ON order_state_events (order_id);
CREATE INDEX IF NOT EXISTS ix_order_state_events_correlation_id ON order_state_events (correlation_id);
CREATE INDEX IF NOT EXISTS ix_order_state_events_order_time ON order_state_events (order_id, occurred_at);

-- paper_fills (7)
CREATE UNIQUE INDEX IF NOT EXISTS ix_paper_fills_fill_code ON paper_fills (fill_code);
CREATE INDEX IF NOT EXISTS ix_paper_fills_order_id ON paper_fills (order_id);
CREATE INDEX IF NOT EXISTS ix_paper_fills_account_id ON paper_fills (account_id);
CREATE INDEX IF NOT EXISTS ix_paper_fills_strategy_instance_id ON paper_fills (strategy_instance_id);
CREATE INDEX IF NOT EXISTS ix_paper_fills_instrument_symbol ON paper_fills (instrument_symbol);
CREATE INDEX IF NOT EXISTS ix_paper_fills_correlation_id ON paper_fills (correlation_id);
CREATE INDEX IF NOT EXISTS ix_paper_fills_order_exec ON paper_fills (order_id, executed_at);

-- paper_positions (7)
CREATE UNIQUE INDEX IF NOT EXISTS ix_paper_positions_position_code ON paper_positions (position_code);
CREATE INDEX IF NOT EXISTS ix_paper_positions_account_id ON paper_positions (account_id);
CREATE INDEX IF NOT EXISTS ix_paper_positions_strategy_instance_id ON paper_positions (strategy_instance_id);
CREATE INDEX IF NOT EXISTS ix_paper_positions_strategy_version_id ON paper_positions (strategy_version_id);
CREATE INDEX IF NOT EXISTS ix_paper_positions_instrument_symbol ON paper_positions (instrument_symbol);
CREATE INDEX IF NOT EXISTS ix_paper_positions_status ON paper_positions (status);
CREATE INDEX IF NOT EXISTS ix_paper_positions_book_status ON paper_positions (account_id, instrument_symbol, status);

-- position_events (4)
CREATE UNIQUE INDEX IF NOT EXISTS ix_position_events_fill_id ON position_events (fill_id);
CREATE INDEX IF NOT EXISTS ix_position_events_position_id ON position_events (position_id);
CREATE INDEX IF NOT EXISTS ix_position_events_correlation_id ON position_events (correlation_id);
CREATE INDEX IF NOT EXISTS ix_position_events_pos_time ON position_events (position_id, occurred_at);

-- account_risk_configs (2)
CREATE UNIQUE INDEX IF NOT EXISTS ix_account_risk_configs_account_id ON account_risk_configs (account_id);
CREATE INDEX IF NOT EXISTS ix_account_risk_configs_global_kill_switch_active ON account_risk_configs (global_kill_switch_active);

-- strategy_risk_configs (2)
CREATE INDEX IF NOT EXISTS ix_strategy_risk_configs_account_id ON strategy_risk_configs (account_id);
CREATE INDEX IF NOT EXISTS ix_strategy_risk_configs_strategy_version_id ON strategy_risk_configs (strategy_version_id);

-- risk_decisions (12)
CREATE UNIQUE INDEX IF NOT EXISTS ix_risk_decisions_decision_code ON risk_decisions (decision_code);
CREATE INDEX IF NOT EXISTS ix_risk_decisions_decision ON risk_decisions (decision);
CREATE INDEX IF NOT EXISTS ix_risk_decisions_primary_rule ON risk_decisions (primary_rule);
CREATE INDEX IF NOT EXISTS ix_risk_decisions_account_id ON risk_decisions (account_id);
CREATE INDEX IF NOT EXISTS ix_risk_decisions_order_id ON risk_decisions (order_id);
CREATE INDEX IF NOT EXISTS ix_risk_decisions_client_order_key ON risk_decisions (client_order_key);
CREATE INDEX IF NOT EXISTS ix_risk_decisions_signal_id ON risk_decisions (signal_id);
CREATE INDEX IF NOT EXISTS ix_risk_decisions_instrument_symbol ON risk_decisions (instrument_symbol);
CREATE INDEX IF NOT EXISTS ix_risk_decisions_correlation_id ON risk_decisions (correlation_id);
CREATE INDEX IF NOT EXISTS ix_risk_decisions_evaluated_at ON risk_decisions (evaluated_at);
CREATE INDEX IF NOT EXISTS ix_risk_decisions_evaluated_desc ON risk_decisions (evaluated_at);
CREATE INDEX IF NOT EXISTS ix_risk_decisions_decision_rule ON risk_decisions (decision, primary_rule);

-- reconciliation_runs (6)
CREATE UNIQUE INDEX IF NOT EXISTS ix_reconciliation_runs_run_code ON reconciliation_runs (run_code);
CREATE INDEX IF NOT EXISTS ix_reconciliation_runs_account_id ON reconciliation_runs (account_id);
CREATE INDEX IF NOT EXISTS ix_reconciliation_runs_status ON reconciliation_runs (status);
CREATE INDEX IF NOT EXISTS ix_reconciliation_runs_correlation_id ON reconciliation_runs (correlation_id);
CREATE INDEX IF NOT EXISTS ix_reconciliation_runs_completed_at ON reconciliation_runs (completed_at);
CREATE INDEX IF NOT EXISTS ix_reconciliation_runs_completed_desc ON reconciliation_runs (completed_at);

-- audit_events (9)
CREATE INDEX IF NOT EXISTS ix_audit_events_occurred_at_desc ON audit_events (occurred_at);
CREATE INDEX IF NOT EXISTS ix_audit_events_correlation_id ON audit_events (correlation_id);
CREATE INDEX IF NOT EXISTS ix_audit_events_action ON audit_events (action);
CREATE INDEX IF NOT EXISTS ix_audit_events_cat_sev ON audit_events (category, severity);
CREATE INDEX IF NOT EXISTS ix_audit_events_entity ON audit_events (entity_type, entity_id);
CREATE INDEX IF NOT EXISTS ix_audit_events_event_type ON audit_events (event_type);
CREATE INDEX IF NOT EXISTS ix_audit_events_strategy_id ON audit_events (strategy_id);
CREATE INDEX IF NOT EXISTS ix_audit_events_signal_id ON audit_events (signal_id);
CREATE INDEX IF NOT EXISTS ix_audit_events_order_id ON audit_events (order_id);

-- system_health_events (3)
CREATE INDEX IF NOT EXISTS ix_system_health_events_recorded_at ON system_health_events (recorded_at);
CREATE INDEX IF NOT EXISTS ix_system_health_events_comp_time ON system_health_events (component, recorded_at);
CREATE INDEX IF NOT EXISTS ix_system_health_events_correlation_id ON system_health_events (correlation_id);

-- ============================================================================
-- SECTION 4: PL/pgSQL IMMUTABILITY FUNCTIONS & APPEND-ONLY TRIGGERS (9 TOTAL)
-- ============================================================================

-- 4.1 Audit Events Append-Only Protection (0001)
CREATE OR REPLACE FUNCTION forbid_audit_event_mutation()
RETURNS TRIGGER AS $$
BEGIN
    RAISE EXCEPTION 'audit_events table is append-only; UPDATE and DELETE are prohibited.';
END;
$$ LANGUAGE plpgsql;

-- 4.2 Strategy Versions Parameter & Lineage Immutability Protection (0002 final)
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

-- 4.3 Backtest Runs & Trades Immutability Protection (0003)
CREATE OR REPLACE FUNCTION enforce_backtest_immutability()
RETURNS TRIGGER AS $$
BEGIN
    RAISE EXCEPTION 'Backtest runs and simulated trade ledgers are immutable historical artifacts.';
END;
$$ LANGUAGE plpgsql;

-- 4.4 Execution Fills, Order State Events & Position Events Immutability Protection (0005)
CREATE OR REPLACE FUNCTION enforce_execution_ledger_immutability()
RETURNS TRIGGER AS $$
BEGIN
    RAISE EXCEPTION 'Simulated execution fills, order state events, and position events are append-only immutable records.';
END;
$$ LANGUAGE plpgsql;

-- 4.5 Risk Decisions Immutability Protection (0006)
CREATE OR REPLACE FUNCTION enforce_risk_decision_immutability()
RETURNS TRIGGER AS $$
BEGIN
    RAISE EXCEPTION 'RiskDecision records are append-only audit artifacts and cannot be modified or deleted.';
END;
$$ LANGUAGE plpgsql;

-- 4.6 Reconciliation Runs Immutability Protection (0007)
CREATE OR REPLACE FUNCTION enforce_reconciliation_run_immutability()
RETURNS TRIGGER AS $$
BEGIN
    RAISE EXCEPTION 'ReconciliationRun records are append-only operational records and cannot be modified or deleted.';
END;
$$ LANGUAGE plpgsql;

-- Attach all 9 triggers idempotently (PostgreSQL 14+ CREATE OR REPLACE TRIGGER)
CREATE OR REPLACE TRIGGER trg_audit_events_immutable
    BEFORE UPDATE OR DELETE ON audit_events
    FOR EACH ROW EXECUTE FUNCTION forbid_audit_event_mutation();

CREATE OR REPLACE TRIGGER trg_strategy_versions_immutable
    BEFORE UPDATE OR DELETE ON strategy_versions
    FOR EACH ROW EXECUTE FUNCTION enforce_strategy_version_immutability();

CREATE OR REPLACE TRIGGER trg_backtest_runs_immutable
    BEFORE UPDATE OR DELETE ON backtest_runs
    FOR EACH ROW EXECUTE FUNCTION enforce_backtest_immutability();

CREATE OR REPLACE TRIGGER trg_backtest_trades_immutable
    BEFORE UPDATE OR DELETE ON backtest_trades
    FOR EACH ROW EXECUTE FUNCTION enforce_backtest_immutability();

CREATE OR REPLACE TRIGGER trg_paper_fills_immutable
    BEFORE UPDATE OR DELETE ON paper_fills
    FOR EACH ROW EXECUTE FUNCTION enforce_execution_ledger_immutability();

CREATE OR REPLACE TRIGGER trg_order_state_events_immutable
    BEFORE UPDATE OR DELETE ON order_state_events
    FOR EACH ROW EXECUTE FUNCTION enforce_execution_ledger_immutability();

CREATE OR REPLACE TRIGGER trg_position_events_immutable
    BEFORE UPDATE OR DELETE ON position_events
    FOR EACH ROW EXECUTE FUNCTION enforce_execution_ledger_immutability();

CREATE OR REPLACE TRIGGER trg_risk_decisions_immutable
    BEFORE UPDATE OR DELETE ON risk_decisions
    FOR EACH ROW EXECUTE FUNCTION enforce_risk_decision_immutability();

CREATE OR REPLACE TRIGGER trg_reconciliation_runs_immutable
    BEFORE UPDATE OR DELETE ON reconciliation_runs
    FOR EACH ROW EXECUTE FUNCTION enforce_reconciliation_run_immutability();

-- ============================================================================
-- SECTION 4B: REVISION 0008 — FYERS HISTORICAL MARKET DATA SCHEMA EXTENSIONS
-- ============================================================================
ALTER TABLE instruments ADD COLUMN IF NOT EXISTS data_provider VARCHAR(32) NOT NULL DEFAULT 'FYERS';
ALTER TABLE instruments ADD COLUMN IF NOT EXISTS provider_symbol VARCHAR(64) NOT NULL DEFAULT '';
ALTER TABLE historical_datasets ADD COLUMN IF NOT EXISTS instrument_id UUID;
ALTER TABLE historical_datasets ADD COLUMN IF NOT EXISTS provider VARCHAR(32) NOT NULL DEFAULT 'CSV';
ALTER TABLE historical_datasets ADD COLUMN IF NOT EXISTS provider_symbol VARCHAR(64);
ALTER TABLE historical_datasets ADD COLUMN IF NOT EXISTS quality_status VARCHAR(32) NOT NULL DEFAULT 'VALID';
ALTER TABLE historical_datasets ADD COLUMN IF NOT EXISTS is_complete BOOLEAN NOT NULL DEFAULT true;
ALTER TABLE historical_datasets ADD COLUMN IF NOT EXISTS requested_from TIMESTAMPTZ;
ALTER TABLE historical_datasets ADD COLUMN IF NOT EXISTS requested_to TIMESTAMPTZ;
ALTER TABLE historical_datasets ADD COLUMN IF NOT EXISTS retrieved_at TIMESTAMPTZ;
ALTER TABLE historical_datasets ADD COLUMN IF NOT EXISTS data_quality_report_json JSON;
ALTER TABLE historical_datasets ADD COLUMN IF NOT EXISTS metadata_json JSON;
DO  BEGIN
  IF NOT EXISTS (SELECT 1 FROM pg_constraint WHERE conname = 'fk_historical_datasets_instrument_id') THEN
    ALTER TABLE historical_datasets ADD CONSTRAINT fk_historical_datasets_instrument_id FOREIGN KEY (instrument_id) REFERENCES instruments (id) ON DELETE RESTRICT;
  END IF;
END ;
CREATE INDEX IF NOT EXISTS ix_historical_datasets_instrument_id ON historical_datasets (instrument_id);
CREATE INDEX IF NOT EXISTS ix_historical_datasets_source_quality ON historical_datasets (source_type, quality_status);
ALTER TABLE historical_candles ALTER COLUMN volume TYPE BIGINT;
CREATE TABLE IF NOT EXISTS historical_download_runs (
    id UUID NOT NULL PRIMARY KEY,
    run_code VARCHAR(64) NOT NULL UNIQUE,
    provider VARCHAR(32) NOT NULL DEFAULT 'FYERS',
    instrument_symbol VARCHAR(32) NOT NULL,
    provider_symbol VARCHAR(64) NOT NULL,
    timeframe VARCHAR(16) NOT NULL,
    fyers_resolution VARCHAR(16) NOT NULL,
    requested_from TIMESTAMPTZ NOT NULL,
    requested_to TIMESTAMPTZ NOT NULL,
    status VARCHAR(32) NOT NULL DEFAULT 'REQUESTED',
    quality_status VARCHAR(32) NOT NULL DEFAULT 'VALID',
    dataset_id UUID REFERENCES historical_datasets (id) ON DELETE SET NULL,
    dataset_code VARCHAR(96),
    chunks_total INTEGER NOT NULL DEFAULT 0,
    chunks_succeeded INTEGER NOT NULL DEFAULT 0,
    chunks_failed INTEGER NOT NULL DEFAULT 0,
    candles_fetched INTEGER NOT NULL DEFAULT 0,
    candles_stored INTEGER NOT NULL DEFAULT 0,
    error_code VARCHAR(64),
    error_message TEXT,
    details_json JSON NOT NULL,
    actor VARCHAR(128) NOT NULL DEFAULT 'desk.operator',
    correlation_id VARCHAR(64) NOT NULL,
    started_at TIMESTAMPTZ NOT NULL,
    completed_at TIMESTAMPTZ
);
CREATE UNIQUE INDEX IF NOT EXISTS ix_historical_download_runs_run_code ON historical_download_runs (run_code);
CREATE INDEX IF NOT EXISTS ix_historical_download_runs_instrument_symbol ON historical_download_runs (instrument_symbol);
CREATE INDEX IF NOT EXISTS ix_historical_download_runs_dataset_id ON historical_download_runs (dataset_id);
CREATE INDEX IF NOT EXISTS ix_historical_download_runs_correlation_id ON historical_download_runs (correlation_id);
CREATE INDEX IF NOT EXISTS ix_historical_download_runs_started_desc ON historical_download_runs (started_at);
CREATE INDEX IF NOT EXISTS ix_historical_download_runs_status ON historical_download_runs (status);
ALTER TABLE replay_sessions ADD COLUMN IF NOT EXISTS dataset_source VARCHAR(32) NOT NULL DEFAULT 'BUNDLED_CSV';
ALTER TABLE replay_sessions ADD COLUMN IF NOT EXISTS dataset_provider VARCHAR(32) NOT NULL DEFAULT 'SYNTHETIC';
ALTER TABLE replay_sessions ADD COLUMN IF NOT EXISTS strategy_instance_id UUID;
ALTER TABLE replay_sessions ADD COLUMN IF NOT EXISTS correlation_id VARCHAR(64);
ALTER TABLE replay_sessions ADD COLUMN IF NOT EXISTS paper_trading_halted_snapshot BOOLEAN NOT NULL DEFAULT false;
DO  BEGIN
  IF NOT EXISTS (SELECT 1 FROM pg_constraint WHERE conname = 'fk_replay_sessions_strategy_instance_id') THEN
    ALTER TABLE replay_sessions ADD CONSTRAINT fk_replay_sessions_strategy_instance_id FOREIGN KEY (strategy_instance_id) REFERENCES strategy_instances (id) ON DELETE SET NULL;
  END IF;
END ;

-- ============================================================================
-- SECTION 5: ALEMBIC HEAD STAMP & IDEMPOTENT FOUNDATION REFERENCE DATA SEED
-- (Matches `python -m deploy.run_migrations --seed` / `seed_foundation_data`)
-- ============================================================================

-- 5.1 Stamp single authoritative Alembic head without destructive statements
DO $$
DECLARE
    v_cnt INTEGER;
BEGIN
    SELECT COUNT(*) INTO v_cnt FROM alembic_version;
    IF v_cnt = 0 THEN
        INSERT INTO alembic_version (version_num)
        VALUES ('0008_fyers_historical_market_data');
    ELSIF v_cnt = 1 THEN
        UPDATE alembic_version
        SET version_num = '0008_fyers_historical_market_data'
        WHERE version_num <> '0008_fyers_historical_market_data';
    ELSE
        RAISE EXCEPTION 'Unexpected multiple rows (%) in alembic_version table; inspect manually.', v_cnt;
    END IF;
END $$;

-- 5.2 Primary Demonstration Operator User (`desk.operator`)
INSERT INTO users (
    id,
    username,
    email,
    display_name,
    role,
    is_active,
    created_at,
    updated_at
)
VALUES (
    gen_random_uuid(),
    'desk.operator',
    'operator@demo-trading-desk.local',
    'Primary Desk Operator',
    'OPERATOR',
    true,
    NOW(),
    NOW()
)
ON CONFLICT (username) DO NOTHING;

-- 5.3 Demonstration Paper Account (`PAPER-DESK-001` — INR 5,00,000 DEMO_PAPER_ONLY)
INSERT INTO paper_accounts (
    id,
    account_code,
    account_name,
    owner_user_id,
    is_demo_capital,
    account_designation,
    currency,
    initial_capital,
    available_capital,
    realized_pnl_today,
    unrealized_pnl,
    paper_trading_halted,
    halt_reason,
    halted_at,
    halted_by,
    status,
    version,
    created_at,
    updated_at
)
SELECT
    gen_random_uuid(),
    'PAPER-DESK-001',
    'Institutional Demo Paper Account',
    u.id,
    true,
    'DEMO_PAPER_ONLY',
    'INR',
    500000.0000,
    500000.0000,
    0.0000,
    0.0000,
    false,
    NULL,
    NULL,
    NULL,
    'ACTIVE',
    1,
    NOW(),
    NOW()
FROM users u
WHERE u.username = 'desk.operator'
ON CONFLICT (account_code) DO NOTHING;

-- 5.4 Configurable Reference Instruments (`NIFTY` lot=75, `BANKNIFTY` lot=30)
INSERT INTO instruments (
    id,
    symbol,
    display_name,
    exchange,
    segment,
    instrument_type,
    currency,
    lot_size,
    tick_size,
    session_open_ist,
    session_close_ist,
    metadata_json,
    is_active,
    created_at,
    updated_at
)
VALUES
    (
        gen_random_uuid(),
        'NIFTY',
        'NIFTY 50 Index Paper Contract',
        'NSE',
        'INDEX_DERIVATIVES_PAPER',
        'PAPER_INDEX_CONTRACT',
        'INR',
        75,
        0.0500,
        '09:15:00',
        '15:30:00',
        '{"index_name":"NIFTY 50","reference_note":"Configurable paper contract spec"}'::json,
        true,
        NOW(),
        NOW()
    ),
    (
        gen_random_uuid(),
        'BANKNIFTY',
        'NIFTY Bank Index Paper Contract',
        'NSE',
        'INDEX_DERIVATIVES_PAPER',
        'PAPER_INDEX_CONTRACT',
        'INR',
        30,
        0.0500,
        '09:15:00',
        '15:30:00',
        '{"index_name":"NIFTY BANK","reference_note":"Configurable paper contract spec"}'::json,
        true,
        NOW(),
        NOW()
    )
ON CONFLICT (symbol) DO NOTHING;

-- 5.5 Strategy Definition (`EMA_RSI_CROSSOVER`)
INSERT INTO strategy_definitions (
    id,
    code,
    name,
    category,
    description,
    parameter_schema_json,
    default_parameters_json,
    supported_symbols_json,
    is_enabled,
    created_at,
    updated_at
)
VALUES (
    gen_random_uuid(),
    'EMA_RSI_CROSSOVER',
    'EMA Crossover + RSI Confirmation',
    'TREND_MOMENTUM',
    'Generates directional candidate signals on closed-bar Fast/Slow EMA crossovers confirmed by Wilder RSI thresholds inside configured IST session bounds, with configurable Stop Loss, Profit Target, Trailing Stop, Opposite Signal, Max Holding, and Mandatory EOD Square-Off exits. Emits Signals only; never directly creates fills.',
    '{"$defs":{"BarTimeframe":{"description":"Supported OHLCV bar intervals.","enum":["1m","3m","5m","15m","30m"],"title":"BarTimeframe","type":"string"},"ExitTargetUnit":{"description":"Unit convention for stop-loss, profit-target, and trailing-stop distances.","enum":["POINTS","PERCENT"],"title":"ExitTargetUnit","type":"string"},"PositionSizingMode":{"description":"Configurable position sizing method stored on immutable strategy versions.","enum":["FIXED_LOTS","FIXED_QUANTITY","RISK_PER_TRADE_PCT"],"title":"PositionSizingMode","type":"string"}},"additionalProperties":false,"description":"Complete immutable parameter specification for EMA Crossover + RSI Confirmation.","properties":{"instrument":{"default":"NIFTY","maxLength":32,"minLength":2,"title":"Instrument","type":"string"},"timeframe":{"$ref":"#/$defs/BarTimeframe","default":"5m"},"fast_ema_period":{"default":9,"maximum":200,"minimum":2,"title":"Fast Ema Period","type":"integer"},"slow_ema_period":{"default":21,"maximum":500,"minimum":3,"title":"Slow Ema Period","type":"integer"},"rsi_period":{"default":14,"maximum":100,"minimum":2,"title":"Rsi Period","type":"integer"},"long_rsi_threshold":{"anyOf":[{"exclusiveMaximum":100.0,"exclusiveMinimum":0.0,"type":"number"},{"type":"string"}],"default":"55.0","title":"Long Rsi Threshold"},"short_rsi_threshold":{"anyOf":[{"exclusiveMaximum":100.0,"exclusiveMinimum":0.0,"type":"number"},{"type":"string"}],"default":"45.0","title":"Short Rsi Threshold"},"trading_start_time":{"default":"09:20:00","pattern":"^\\d{2}:\\d{2}(:\\d{2})?$","title":"Trading Start Time","type":"string"},"last_entry_time":{"default":"14:45:00","pattern":"^\\d{2}:\\d{2}(:\\d{2})?$","title":"Last Entry Time","type":"string"},"mandatory_square_off_time":{"default":"15:20:00","pattern":"^\\d{2}:\\d{2}(:\\d{2})?$","title":"Mandatory Square Off Time","type":"string"},"max_trades_per_day":{"default":5,"exclusiveMinimum":0,"maximum":100,"title":"Max Trades Per Day","type":"integer"},"stop_target_unit":{"$ref":"#/$defs/ExitTargetUnit","default":"POINTS"},"stop_loss":{"anyOf":[{"exclusiveMinimum":0.0,"type":"number"},{"type":"string"}],"default":"30.0","title":"Stop Loss"},"profit_target_enabled":{"default":true,"title":"Profit Target Enabled","type":"boolean"},"profit_target":{"anyOf":[{"exclusiveMinimum":0.0,"type":"number"},{"type":"string"}],"default":"70.0","title":"Profit Target"},"trailing_stop_enabled":{"default":false,"title":"Trailing Stop Enabled","type":"boolean"},"trailing_stop":{"anyOf":[{"exclusiveMinimum":0.0,"type":"number"},{"type":"string"}],"default":"25.0","title":"Trailing Stop"},"exit_on_opposite_signal":{"default":true,"title":"Exit On Opposite Signal","type":"boolean"},"max_holding_enabled":{"default":true,"title":"Max Holding Enabled","type":"boolean"},"max_holding_bars":{"default":24,"maximum":500,"minimum":1,"title":"Max Holding Bars","type":"integer"},"eod_square_off_enabled":{"default":true,"title":"Eod Square Off Enabled","type":"boolean"},"position_sizing_mode":{"$ref":"#/$defs/PositionSizingMode","default":"FIXED_LOTS"},"position_size_value":{"anyOf":[{"exclusiveMinimum":0.0,"type":"number"},{"type":"string"}],"default":"1","title":"Position Size Value"},"slippage_bps":{"anyOf":[{"maximum":250.0,"minimum":0.0,"type":"number"},{"type":"string"}],"default":"2.0","title":"Slippage Bps"},"estimated_charges_bps":{"anyOf":[{"maximum":250.0,"minimum":0.0,"type":"number"},{"type":"string"}],"default":"3.5","title":"Estimated Charges Bps"},"per_order_fee_inr":{"anyOf":[{"maximum":5000.0,"minimum":0.0,"type":"number"},{"type":"string"}],"default":"20.0","title":"Per Order Fee Inr"}},"title":"EmaRsiStrategyConfig","type":"object"}'::json,
    '{"instrument":"NIFTY","timeframe":"5m","fast_ema_period":9,"slow_ema_period":21,"rsi_period":14,"long_rsi_threshold":"55.00","short_rsi_threshold":"45.00","trading_start_time":"09:20:00","last_entry_time":"14:45:00","mandatory_square_off_time":"15:20:00","max_trades_per_day":5,"stop_target_unit":"POINTS","stop_loss":"30.00","profit_target_enabled":true,"profit_target":"60.00","trailing_stop_enabled":false,"trailing_stop":"25.00","exit_on_opposite_signal":true,"max_holding_enabled":true,"max_holding_bars":24,"eod_square_off_enabled":true,"position_sizing_mode":"FIXED_LOTS","position_size_value":"1","slippage_bps":"2.00","estimated_charges_bps":"3.50","per_order_fee_inr":"20.00"}'::json,
    '["NIFTY","BANKNIFTY"]'::json,
    true,
    NOW(),
    NOW()
)
ON CONFLICT (code) DO NOTHING;

-- 5.6 Immutable Baseline Strategy Versions (`NIFTY_EMA_RSI_v1` & `BANKNIFTY_EMA_RSI_v1`)
INSERT INTO strategy_versions (
    id,
    strategy_definition_id,
    version_number,
    version_label,
    instrument_symbol,
    timeframe,
    cloned_from_version_id,
    parameters_json,
    parameter_hash,
    notes,
    created_by,
    status,
    created_at
)
SELECT
    gen_random_uuid(),
    sd.id,
    1,
    'NIFTY_EMA_RSI_v1',
    'NIFTY',
    '5m',
    NULL,
    '{"instrument":"NIFTY","timeframe":"5m","fast_ema_period":9,"slow_ema_period":21,"rsi_period":14,"long_rsi_threshold":"55.00","short_rsi_threshold":"45.00","trading_start_time":"09:20:00","last_entry_time":"14:45:00","mandatory_square_off_time":"15:20:00","max_trades_per_day":5,"stop_target_unit":"POINTS","stop_loss":"30.00","profit_target_enabled":true,"profit_target":"60.00","trailing_stop_enabled":false,"trailing_stop":"25.00","exit_on_opposite_signal":true,"max_holding_enabled":true,"max_holding_bars":24,"eod_square_off_enabled":true,"position_sizing_mode":"FIXED_LOTS","position_size_value":"1","slippage_bps":"2.00","estimated_charges_bps":"3.50","per_order_fee_inr":"20.00"}'::json,
    'cec7433d04caaf1ea26af9d3ee624f939074d4adf8a2243c5712777d998a7528',
    'Neutral engineering baseline seeded for NIFTY (non-optimized demo defaults).',
    'system.seed',
    'VALIDATED',
    NOW()
FROM strategy_definitions sd
WHERE sd.code = 'EMA_RSI_CROSSOVER'
ON CONFLICT ON CONSTRAINT uq_strategy_versions_label DO NOTHING;

INSERT INTO strategy_versions (
    id,
    strategy_definition_id,
    version_number,
    version_label,
    instrument_symbol,
    timeframe,
    cloned_from_version_id,
    parameters_json,
    parameter_hash,
    notes,
    created_by,
    status,
    created_at
)
SELECT
    gen_random_uuid(),
    sd.id,
    1,
    'BANKNIFTY_EMA_RSI_v1',
    'BANKNIFTY',
    '5m',
    NULL,
    '{"instrument":"BANKNIFTY","timeframe":"5m","fast_ema_period":9,"slow_ema_period":21,"rsi_period":14,"long_rsi_threshold":"55.00","short_rsi_threshold":"45.00","trading_start_time":"09:20:00","last_entry_time":"14:45:00","mandatory_square_off_time":"15:20:00","max_trades_per_day":5,"stop_target_unit":"POINTS","stop_loss":"80.00","profit_target_enabled":true,"profit_target":"160.00","trailing_stop_enabled":false,"trailing_stop":"55.00","exit_on_opposite_signal":true,"max_holding_enabled":true,"max_holding_bars":24,"eod_square_off_enabled":true,"position_sizing_mode":"FIXED_LOTS","position_size_value":"1","slippage_bps":"2.00","estimated_charges_bps":"3.50","per_order_fee_inr":"20.00"}'::json,
    'ea8806993f78fdbb2981f821647dfc632a922bc6f80fc597a6581842fc675ed8',
    'Neutral engineering baseline seeded for BANKNIFTY (non-optimized demo defaults).',
    'system.seed',
    'VALIDATED',
    NOW()
FROM strategy_definitions sd
WHERE sd.code = 'EMA_RSI_CROSSOVER'
ON CONFLICT ON CONSTRAINT uq_strategy_versions_label DO NOTHING;

-- 5.7 Account-Level Paper Risk Configuration (`account_risk_configs` — DEMO SETTINGS ONLY)
INSERT INTO account_risk_configs (
    id,
    account_id,
    is_demo_settings,
    max_daily_loss_inr,
    max_open_positions,
    max_gross_exposure_inr,
    max_order_value_inr,
    max_trades_per_day,
    max_consecutive_losses,
    trading_session_enabled,
    session_start_ist,
    session_end_ist,
    global_kill_switch_active,
    kill_switch_reason,
    kill_switch_activated_at,
    kill_switch_activated_by,
    market_data_stale_threshold_seconds,
    max_signal_age_seconds,
    enforce_system_health_gate,
    simulated_system_degraded,
    enforce_position_reconciliation_gate,
    default_max_quantity,
    default_max_risk_per_trade_inr,
    default_strategy_daily_loss_inr,
    default_max_strategy_drawdown_inr,
    default_max_trades_per_strategy_day,
    default_cooldown_seconds_after_loss,
    default_max_holding_duration_seconds,
    default_allowed_instruments_json,
    default_allowed_order_types_json,
    updated_by,
    created_at,
    updated_at
)
SELECT
    gen_random_uuid(),
    pa.id,
    true,
    10000.0000,
    4,
    6000000.0000,
    3000000.0000,
    15,
    3,
    true,
    '09:15:00',
    '15:30:00',
    false,
    NULL,
    NULL,
    NULL,
    10,
    15,
    true,
    false,
    true,
    300,
    2500.0000,
    6000.0000,
    8000.0000,
    5,
    300,
    14400,
    '["NIFTY","BANKNIFTY"]'::json,
    '["MARKET","LIMIT","STOP"]'::json,
    'system.seed',
    NOW(),
    NOW()
FROM paper_accounts pa
WHERE pa.account_code = 'PAPER-DESK-001'
ON CONFLICT ON CONSTRAINT uq_account_risk_configs_account_id DO NOTHING;

-- 5.8 Per-Strategy Risk Configurations (`strategy_risk_configs` for NIFTY_EMA_RSI_v1 & BANKNIFTY_EMA_RSI_v1)
INSERT INTO strategy_risk_configs (
    id,
    account_id,
    strategy_version_id,
    strategy_version_label,
    max_quantity,
    max_risk_per_trade_inr,
    strategy_daily_loss_inr,
    max_strategy_drawdown_inr,
    max_trades_per_day,
    cooldown_seconds_after_loss,
    max_holding_duration_seconds,
    allowed_instruments_json,
    allowed_order_types_json,
    is_enabled,
    updated_by,
    created_at,
    updated_at
)
SELECT
    gen_random_uuid(),
    pa.id,
    sv.id,
    sv.version_label,
    300,
    2500.0000,
    6000.0000,
    8000.0000,
    5,
    300,
    14400,
    CASE WHEN sv.instrument_symbol = 'BANKNIFTY' THEN '["BANKNIFTY"]'::json ELSE '["NIFTY"]'::json END,
    '["MARKET","LIMIT","STOP"]'::json,
    true,
    'system.seed',
    NOW(),
    NOW()
FROM paper_accounts pa
CROSS JOIN strategy_versions sv
WHERE pa.account_code = 'PAPER-DESK-001'
  AND sv.version_label IN ('NIFTY_EMA_RSI_v1', 'BANKNIFTY_EMA_RSI_v1')
ON CONFLICT ON CONSTRAINT uq_strategy_risk_configs_acct_ver DO NOTHING;

-- 5.9 Append-Only Initialization Audit Events (Inserted once when not already recorded)
INSERT INTO audit_events (
    id,
    occurred_at,
    correlation_id,
    actor,
    actor_type,
    category,
    action,
    event_type,
    severity,
    entity_type,
    entity_id,
    strategy_id,
    signal_id,
    order_id,
    source,
    summary,
    details_json
)
SELECT
    gen_random_uuid(),
    NOW(),
    'neon-sql-init-strategy-seed',
    'system.seed',
    'SYSTEM',
    'STRATEGY',
    'STRATEGY_CREATED',
    'STRATEGY_CREATED',
    'INFO',
    'strategy_definition',
    'EMA_RSI_CROSSOVER',
    'EMA_RSI_CROSSOVER',
    NULL,
    NULL,
    'system.seed',
    'Registered configurable strategy definition ''EMA_RSI_CROSSOVER'' (EMA Crossover + RSI Confirmation).',
    '{"code":"EMA_RSI_CROSSOVER","supported_symbols":["NIFTY","BANKNIFTY"]}'::json
WHERE NOT EXISTS (
    SELECT 1
    FROM audit_events
    WHERE action = 'STRATEGY_CREATED'
      AND actor = 'system.seed'
      AND entity_id = 'EMA_RSI_CROSSOVER'
);

INSERT INTO audit_events (
    id,
    occurred_at,
    correlation_id,
    actor,
    actor_type,
    category,
    action,
    event_type,
    severity,
    entity_type,
    entity_id,
    strategy_id,
    signal_id,
    order_id,
    source,
    summary,
    details_json
)
SELECT
    gen_random_uuid(),
    NOW(),
    'neon-sql-init-foundation-seed',
    'system.seed',
    'SYSTEM',
    'SYSTEM',
    'FOUNDATION_REFERENCE_DATA_SEEDED',
    NULL,
    'INFO',
    'paper_accounts',
    'PAPER-DESK-001',
    NULL,
    NULL,
    NULL,
    'core.service',
    'Initialized demonstration paper account (INR 500,000 DEMO), configurable instruments, and EMA+RSI v1.',
    '{"created_flags":{"operator_user":true,"paper_account":true,"nifty_instrument":true,"banknifty_instrument":true,"ema_rsi_strategy":true,"demo_risk_config":true,"demo_strategy_risk_config":true},"demo_capital_inr":"500000.0000","designation":"DEMO_PAPER_ONLY"}'::json
WHERE NOT EXISTS (
    SELECT 1
    FROM audit_events
    WHERE action = 'FOUNDATION_REFERENCE_DATA_SEEDED'
      AND actor = 'system.seed'
      AND entity_id = 'PAPER-DESK-001'
);

COMMIT;

-- ============================================================================
-- SECTION 6: READ-ONLY POST-INITIALIZATION VALIDATION QUERIES
-- (Modifies zero data; verifies schema, constraints, triggers & seed state)
-- ============================================================================

-- 1. PostgreSQL Server Version
SELECT version() AS postgresql_version;

-- 2. Database/Session Timezone & Current IST Timestamp (Application sets Asia/Kolkata per session)
SELECT
    current_setting('TIMEZONE') AS session_timezone,
    NOW() AS current_session_timestamp,
    (NOW() AT TIME ZONE 'Asia/Kolkata') AS current_timestamp_ist_local;

-- 3. Alembic Schema Revision Check (Expected: single row = '0008_fyers_historical_market_data')
SELECT
    COUNT(*) AS revision_row_count,
    MIN(version_num) AS current_alembic_version,
    (COUNT(*) = 1 AND MIN(version_num) = '0008_fyers_historical_market_data') AS matches_expected_head
FROM alembic_version;

SELECT version_num FROM alembic_version;

-- 4. All Application & Migration Tables in public Schema (Expected: 24 tables = 23 app + alembic_version)
SELECT
    table_name,
    (
        SELECT COUNT(*)
        FROM information_schema.columns c
        WHERE c.table_schema = t.table_schema
          AND c.table_name = t.table_name
    ) AS column_count
FROM information_schema.tables t
WHERE table_schema = 'public'
  AND table_type = 'BASE TABLE'
ORDER BY table_name;

-- 5. Explicit Application Indexes Inventory (ix_* and uq_* / PK indexes in public schema)
SELECT
    tablename AS table_name,
    indexname AS index_name,
    indexdef AS index_definition
FROM pg_indexes
WHERE schemaname = 'public'
ORDER BY tablename, indexname;

-- 6. Primary Key, Foreign Key, Unique, and Check Constraints Inventory
SELECT
    tc.table_name,
    tc.constraint_type,
    tc.constraint_name
FROM information_schema.table_constraints tc
WHERE tc.table_schema = 'public'
  AND tc.constraint_type IN ('PRIMARY KEY', 'FOREIGN KEY', 'UNIQUE', 'CHECK')
  AND tc.constraint_name NOT LIKE '%_not_null'
ORDER BY tc.table_name, tc.constraint_type, tc.constraint_name;

-- 7. Immutability & Append-Only Database Triggers (Expected: 9 triggers across 9 protected tables)
SELECT
    event_object_table AS table_name,
    trigger_name,
    string_agg(event_manipulation, ', ' ORDER BY event_manipulation) AS guarded_operations,
    action_timing,
    action_statement
FROM information_schema.triggers
WHERE trigger_schema = 'public'
GROUP BY event_object_table, trigger_name, action_timing, action_statement
ORDER BY event_object_table, trigger_name;

-- 8. NIFTY Instrument Seed Verification (Expected: symbol='NIFTY', exchange='NSE', lot_size=75, tick_size=0.0500)
SELECT
    symbol,
    display_name,
    exchange,
    segment,
    instrument_type,
    currency,
    lot_size,
    tick_size,
    session_open_ist,
    session_close_ist,
    is_active
FROM instruments
WHERE symbol = 'NIFTY';

-- 9. BANKNIFTY Instrument Seed Verification (Expected: symbol='BANKNIFTY', exchange='NSE', lot_size=30, tick_size=0.0500)
SELECT
    symbol,
    display_name,
    exchange,
    segment,
    instrument_type,
    currency,
    lot_size,
    tick_size,
    session_open_ist,
    session_close_ist,
    is_active
FROM instruments
WHERE symbol = 'BANKNIFTY';

-- 10. PAPER-DESK-001 Demo Account & Account Risk Config Seed Verification
SELECT
    pa.account_code,
    pa.account_name,
    u.username AS owner_username,
    pa.is_demo_capital,
    pa.account_designation,
    pa.currency,
    pa.initial_capital,
    pa.available_capital,
    pa.paper_trading_halted,
    pa.status,
    arc.max_daily_loss_inr,
    arc.max_open_positions,
    arc.global_kill_switch_active,
    arc.market_data_stale_threshold_seconds,
    arc.max_signal_age_seconds
FROM paper_accounts pa
LEFT JOIN users u ON u.id = pa.owner_user_id
LEFT JOIN account_risk_configs arc ON arc.account_id = pa.id
WHERE pa.account_code = 'PAPER-DESK-001';

-- 11. Strategy Definitions Seed Verification (Expected: code='EMA_RSI_CROSSOVER')
SELECT
    code,
    name,
    category,
    supported_symbols_json::text AS supported_symbols,
    is_enabled,
    created_at
FROM strategy_definitions
ORDER BY code;

-- 12. Strategy Versions & Strategy Risk Configs Verification (Expected: NIFTY_EMA_RSI_v1 & BANKNIFTY_EMA_RSI_v1)
SELECT
    sv.version_label,
    sv.version_number,
    sv.instrument_symbol,
    sv.timeframe,
    sv.parameter_hash,
    sv.status,
    sv.created_by,
    src.max_quantity AS risk_max_quantity,
    src.max_risk_per_trade_inr,
    src.is_enabled AS risk_enabled
FROM strategy_versions sv
LEFT JOIN strategy_risk_configs src ON src.strategy_version_id = sv.id
ORDER BY sv.version_label;

-- 13. Append-Only Audit & System Health Tables Inspection
SELECT
    occurred_at,
    action,
    event_type,
    category,
    severity,
    actor,
    entity_type,
    entity_id,
    summary
FROM audit_events
ORDER BY occurred_at ASC;

SELECT COUNT(*) AS system_health_events_count FROM system_health_events;

-- 14. Complete Expected Row Count Summary Across All 24 Tables
SELECT 'alembic_version' AS table_name, COUNT(*) AS actual_rows, 1 AS expected_min_rows FROM alembic_version
UNION ALL SELECT 'users', COUNT(*), 1 FROM users
UNION ALL SELECT 'paper_accounts', COUNT(*), 1 FROM paper_accounts
UNION ALL SELECT 'instruments', COUNT(*), 2 FROM instruments
UNION ALL SELECT 'strategy_definitions', COUNT(*), 1 FROM strategy_definitions
UNION ALL SELECT 'strategy_versions', COUNT(*), 2 FROM strategy_versions
UNION ALL SELECT 'account_risk_configs', COUNT(*), 1 FROM account_risk_configs
UNION ALL SELECT 'strategy_risk_configs', COUNT(*), 2 FROM strategy_risk_configs
UNION ALL SELECT 'audit_events', COUNT(*), 2 FROM audit_events
UNION ALL SELECT 'historical_datasets', COUNT(*), 0 FROM historical_datasets
UNION ALL SELECT 'historical_candles', COUNT(*), 0 FROM historical_candles
UNION ALL SELECT 'backtest_runs', COUNT(*), 0 FROM backtest_runs
UNION ALL SELECT 'backtest_trades', COUNT(*), 0 FROM backtest_trades
UNION ALL SELECT 'replay_sessions', COUNT(*), 0 FROM replay_sessions
UNION ALL SELECT 'strategy_instances', COUNT(*), 0 FROM strategy_instances
UNION ALL SELECT 'signals', COUNT(*), 0 FROM signals
UNION ALL SELECT 'paper_orders', COUNT(*), 0 FROM paper_orders
UNION ALL SELECT 'order_state_events', COUNT(*), 0 FROM order_state_events
UNION ALL SELECT 'paper_fills', COUNT(*), 0 FROM paper_fills
UNION ALL SELECT 'paper_positions', COUNT(*), 0 FROM paper_positions
UNION ALL SELECT 'position_events', COUNT(*), 0 FROM position_events
UNION ALL SELECT 'risk_decisions', COUNT(*), 0 FROM risk_decisions
UNION ALL SELECT 'reconciliation_runs', COUNT(*), 0 FROM reconciliation_runs
UNION ALL SELECT 'system_health_events', COUNT(*), 0 FROM system_health_events;
