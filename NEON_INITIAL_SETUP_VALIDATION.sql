-- ============================================================================
-- ALGO TRADING CONTROL CENTER — READ-ONLY NEON VALIDATION SUITE
-- File: NEON_INITIAL_SETUP_VALIDATION.sql
--
-- SAFETY GUARANTEE:
-- Contains strictly read-only SELECT queries only. Modifies ZERO data and
-- alters ZERO schema or session objects. Safe to run at any time in Neon SQL Editor.
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
