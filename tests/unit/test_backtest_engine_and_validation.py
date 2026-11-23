"""Deterministic unit tests for BacktestEngine, OHLCV Data Validation, and Metrics.

Covers:
1. Pre-backtest validation: ordering, duplicates, missing candles, invalid OHLC, nulls,
   timezone normalization, session gaps, COMPLETED / COMPLETED_WITH_WARNINGS /
   FAILED_VALIDATION / INSUFFICIENT_DATA.
2. Exact hand-calculated signal timing, next-bar open fills, execution delay, adverse
   slippage, flat + bps charges, Gross P&L, and Net P&L reconciliation.
3. Exit mechanisms: Stop Loss (including gap-open & pessimistic Stop-First tie-break),
   Profit Target, Opposite Signal, and Mandatory EOD Square-Off.
4. Peak-to-trough Maximum Drawdown & Undefined Metrics returning `None` (`N/A`).
5. Strict Zero Look-Ahead Bias proof.
"""

from __future__ import annotations

import uuid
from datetime import datetime, timedelta
from decimal import Decimal
from zoneinfo import ZoneInfo

import pytest

from core.backtesting.engine import BacktestEngine
from core.backtesting.models import BacktestExecutionInputs, RawCandleRow
from core.backtesting.validator import validate_ohlcv_rows
from core.domain.enums import (
    BacktestStatus,
    BarTimeframe,
    ExitReason,
    ExitTargetUnit,
    PositionSizingMode,
    TradeDirection,
)
from core.strategies.config_schema import EmaRsiStrategyConfig

IST = ZoneInfo("Asia/Kolkata")
STRAT_VER_UUID = uuid.UUID("11111111-2222-3333-4444-555555555555")


def _make_raw_rows(
    specs: list[tuple[str, str, str, str, str, int]],
) -> list[RawCandleRow]:
    return [
        RawCandleRow(
            row_index=idx,
            timestamp_raw=ts,
            open_raw=o,
            high_raw=h,
            low_raw=low,
            close_raw=c,
            volume_raw=vol,
        )
        for idx, (ts, o, h, low, c, vol) in enumerate(specs, start=1)
    ]


def _make_5m_day_rows(
    closes_and_ranges: list[tuple[str, str, str, str]],
    *,
    start_iso: str = "2026-11-02T09:15:00+05:30",
) -> list[RawCandleRow]:
    dt = datetime.fromisoformat(start_iso)
    rows: list[RawCandleRow] = []
    for idx, (o, h, low, c) in enumerate(closes_and_ranges, start=1):
        rows.append(
            RawCandleRow(
                row_index=idx,
                timestamp_raw=dt.isoformat(),
                open_raw=o,
                high_raw=h,
                low_raw=low,
                close_raw=c,
                volume_raw=10000,
            )
        )
        dt += timedelta(minutes=5)
    return rows


def _base_config(**overrides: object) -> EmaRsiStrategyConfig:
    params: dict[str, object] = {
        "instrument": "NIFTY",
        "timeframe": BarTimeframe.M5,
        "fast_ema_period": 2,
        "slow_ema_period": 4,
        "rsi_period": 2,
        "long_rsi_threshold": Decimal("55.0"),
        "short_rsi_threshold": Decimal("45.0"),
        "trading_start_time": "09:15:00",
        "last_entry_time": "14:45:00",
        "mandatory_square_off_time": "15:20:00",
        "max_trades_per_day": 5,
        "stop_target_unit": ExitTargetUnit.POINTS,
        "stop_loss": Decimal("20.0"),
        "profit_target_enabled": True,
        "profit_target": Decimal("40.0"),
        "trailing_stop_enabled": False,
        "trailing_stop": Decimal("15.0"),
        "exit_on_opposite_signal": True,
        "max_holding_enabled": False,
        "max_holding_bars": 20,
        "eod_square_off_enabled": True,
        "position_sizing_mode": PositionSizingMode.FIXED_QUANTITY,
        "position_size_value": Decimal("10"),
        "slippage_bps": Decimal("10.0"),        # 0.10% = 0.001
        "estimated_charges_bps": Decimal("5.0"),  # 0.05% = 0.0005
        "per_order_fee_inr": Decimal("20.0"),
    }
    params.update(overrides)
    return EmaRsiStrategyConfig(**params)  # type: ignore[arg-type]


def _base_inputs(**overrides: object) -> BacktestExecutionInputs:
    data: dict[str, object] = {
        "strategy_version_id": STRAT_VER_UUID,
        "strategy_version_label": "NIFTY_EMA_RSI_v1",
        "instrument": "NIFTY",
        "timeframe": "5m",
        "start_date": None,
        "end_date": None,
        "initial_capital": Decimal("500000.00"),
        "lot_size": 25,
        "slippage_bps": Decimal("10.0"),
        "estimated_charges_bps": Decimal("5.0"),
        "per_order_fee_inr": Decimal("20.0"),
        "position_sizing_mode": PositionSizingMode.FIXED_QUANTITY,
        "position_size_value": Decimal("10"),
        "execution_delay_bars": 1,
    }
    data.update(overrides)
    return BacktestExecutionInputs(**data)  # type: ignore[arg-type]


# ==============================================================================
# 1. DATA VALIDATION TESTS
# ==============================================================================


def test_validator_rejects_duplicate_unsorted_null_and_invalid_ohlc() -> None:
    # Duplicate timestamp -> FAILED_VALIDATION
    dup_rows = _make_raw_rows(
        [
            ("2026-11-02T09:15:00+05:30", "100", "105", "99", "102", 1000),
            ("2026-11-02T09:15:00+05:30", "102", "106", "101", "104", 1000),
        ]
    )
    _, rep_dup = validate_ohlcv_rows(dup_rows, instrument="NIFTY", timeframe="5m")
    assert rep_dup.passed_validation is False
    assert rep_dup.duplicate_timestamp_count == 1

    # Out-of-order timestamp -> FAILED_VALIDATION
    ooo_rows = _make_raw_rows(
        [
            ("2026-11-02T09:20:00+05:30", "100", "105", "99", "102", 1000),
            ("2026-11-02T09:15:00+05:30", "102", "106", "101", "104", 1000),
        ]
    )
    _, rep_ooo = validate_ohlcv_rows(ooo_rows, instrument="NIFTY", timeframe="5m")
    assert rep_ooo.passed_validation is False
    assert rep_ooo.out_of_order_count == 1

    # Null value -> FAILED_VALIDATION
    null_rows = _make_raw_rows(
        [
            ("2026-11-02T09:15:00+05:30", "100", "", "99", "102", 1000),
        ]
    )
    _, rep_null = validate_ohlcv_rows(null_rows, instrument="NIFTY", timeframe="5m")
    assert rep_null.passed_validation is False
    assert rep_null.null_value_count == 1

    # Invalid OHLC (high < open and low > close) -> FAILED_VALIDATION
    bad_ohlc_rows = _make_raw_rows(
        [
            ("2026-11-02T09:15:00+05:30", "105", "103", "100", "102", 1000),
        ]
    )
    _, rep_ohlc = validate_ohlcv_rows(bad_ohlc_rows, instrument="NIFTY", timeframe="5m")
    assert rep_ohlc.passed_validation is False
    assert rep_ohlc.invalid_ohlc_count == 1

    # Engine produces FAILED_VALIDATION status
    engine = BacktestEngine(strategy_config=_base_config(), inputs=_base_inputs())
    out = engine.run(bad_ohlc_rows)
    assert out.status == BacktestStatus.FAILED_VALIDATION
    assert out.trades == []


def test_validator_flags_missing_candles_session_gaps_and_timezone_warnings() -> None:
    # Naive timestamp + 1 missing 5m bar (09:20 missing) + 30m session gap -> COMPLETED_WITH_WARNINGS
    rows = _make_raw_rows(
        [
            ("2026-11-02T09:15:00", "100", "101", "99", "100", 500),       # naive -> assumed IST warning
            ("2026-11-02T09:25:00+05:30", "100", "101", "99", "100", 500),  # 1 missing candle (09:20)
            ("2026-11-02T09:55:00+05:30", "100", "101", "99", "100", 500),  # 30m unexpected gap (5 missing)
            ("2026-11-02T04:30:00Z", "100", "101", "99", "100", 500),       # 10:00 IST from UTC -> tz warning
            ("2026-11-02T10:05:00+05:30", "100", "101", "99", "100", 500),
            ("2026-11-02T10:10:00+05:30", "100", "101", "99", "100", 500),
        ]
    )
    bars, rep = validate_ohlcv_rows(rows, instrument="NIFTY", timeframe="5m")
    assert rep.passed_validation is True
    assert rep.has_warnings is True
    assert rep.timezone_issue_count == 2
    assert rep.missing_candle_count == 6  # 1 + 5
    assert rep.unexpected_session_gap_count == 1
    assert all(b.bar_timestamp.utcoffset() == timedelta(hours=5, minutes=30) for b in bars)

    engine = BacktestEngine(strategy_config=_base_config(), inputs=_base_inputs())
    out = engine.run(rows)
    assert out.status == BacktestStatus.COMPLETED_WITH_WARNINGS


def test_engine_returns_insufficient_data_when_below_warmup() -> None:
    # slow_ema_period=4 requires warmup=5 bars + delay=1 -> 6 bars minimum; pass 4 bars
    rows = _make_5m_day_rows(
        [
            ("100", "101", "99", "100"),
            ("100", "101", "99", "100"),
            ("100", "101", "99", "100"),
            ("100", "101", "99", "100"),
        ]
    )
    engine = BacktestEngine(strategy_config=_base_config(), inputs=_base_inputs())
    out = engine.run(rows)
    assert out.status == BacktestStatus.INSUFFICIENT_DATA
    assert out.trades == []


# ==============================================================================
# 2. EXACT SIGNAL TIMING, FILLS, SLIPPAGE, CHARGES & PROFIT TARGET TEST
# ==============================================================================


def test_exact_signal_timing_delayed_fill_slippage_charges_and_profit_target() -> None:
    """Verify exact hand-calculated numbers for signal bar `i`, fill at `i+1` open,
    adverse slippage, charges, profit target fill, Gross P&L, and Net P&L.
    """
    # Bars 0..3: flat/down so Fast EMA(2) <= Slow EMA(4) at bar 3 (09:30)
    # Bar 4 (09:35): sharp rally close=110 -> Fast EMA crosses above Slow EMA & RSI(2) >= 55!
    #   Signal emitted on closed bar 4 (09:35 IST). CANNOT fill on bar 4!
    # Bar 5 (09:40): open=112.00 -> Pending BUY fills at bar 5 open + 10 bps adverse slippage:
    #   entry_price = 112.00 * 1.001 = 112.1120 (qty = 10)
    #   entry_slippage_cost = (112.1120 - 112.0000) * 10 = 1.1200
    #   stop_loss = 20 pts -> stop = 92.1120; profit_target = 30 pts -> target = 142.1120
    # Bar 6 (09:45): open=120, high=145, low=119, close=143 -> high 145 >= target 142.1120!
    #   raw_exit_price = 142.1120
    #   SELL adverse slippage 10 bps -> exit_price = 142.1120 * 0.999 = 141.9699
    #   exit_slippage_cost = (142.1120 - 141.9699) * 10 = 1.4210
    #   total_slippage_cost = 1.1200 + 1.4210 = 2.5410
    #   gross_pnl = (141.9699 - 112.1120) * 10 = 298.5790
    #   turnover = (112.1120 + 141.9699) * 10 = 2540.8190
    #   variable_charges (5 bps = 0.0005) = 2540.8190 * 0.0005 = 1.2704095
    #   flat_fees = 2 * 20 = 40.0000 -> total charges = 41.2704
    #   net_pnl = 298.5790 - 41.2704 = 257.3086
    rows = _make_5m_day_rows(
        [
            ("100", "101", "99", "100"),      # idx 0 (09:15)
            ("100", "100", "98", "99"),       # idx 1 (09:20)
            ("99", "99", "97", "98"),         # idx 2 (09:25)
            ("98", "98.5", "97", "98"),       # idx 3 (09:30): Fast(2)=98.00 <= Slow(4)=98.75
            ("98", "111", "98", "110"),       # idx 4 (09:35): Signal bar! Fast > Slow & RSI=100
            ("112", "120", "111", "118"),     # idx 5 (09:40): Entry fill @ open 112.00 + slip
            ("120", "145", "119", "143"),     # idx 6 (09:45): Profit target hit @ 142.1120
            ("143", "144", "142", "143"),     # idx 7 (09:50)
        ]
    )
    cfg = _base_config(profit_target=Decimal("30.0"), max_trades_per_day=1)
    inputs = _base_inputs()
    out = BacktestEngine(strategy_config=cfg, inputs=inputs).run(rows)

    assert out.status == BacktestStatus.COMPLETED
    assert len(out.trades) == 1
    tr = out.trades[0]

    assert tr.direction == TradeDirection.LONG
    assert tr.entry_signal_time == datetime(2026, 11, 2, 9, 35, tzinfo=IST)
    assert tr.entry_time == datetime(2026, 11, 2, 9, 40, tzinfo=IST)
    assert tr.entry_time > tr.entry_signal_time  # Never fills on signal candle!
    assert tr.raw_entry_price == Decimal("112.0000")
    assert tr.entry_price == Decimal("112.1120")
    assert tr.quantity == 10
    assert tr.exit_time == datetime(2026, 11, 2, 9, 45, tzinfo=IST)
    assert tr.exit_reason == ExitReason.PROFIT_TARGET
    assert tr.raw_exit_price == Decimal("142.1120")
    assert tr.exit_price == Decimal("141.9699")
    assert tr.slippage_cost == Decimal("2.5410")
    assert tr.gross_pnl == Decimal("298.5790")
    assert (tr.raw_exit_price - tr.raw_entry_price) * Decimal(tr.quantity) - tr.slippage_cost == tr.gross_pnl
    assert tr.charges == Decimal("41.2704")
    assert tr.net_pnl == Decimal("257.3086")

    # Verify exact P&L reconciliation & undefined metrics when 0 losing trades
    assert Decimal(out.metrics["ending_equity"]) == Decimal("500257.31")
    assert out.metrics["profit_factor"] is None  # 0 losing trades -> undefined -> None (N/A)
    assert out.metrics["average_loser_inr"] is None
    assert out.metrics["largest_loser_inr"] is None
    assert out.metrics["win_rate_pct"] == "100.00"


def test_execution_delay_two_bars_shifts_fill_to_i_plus_2_open() -> None:
    rows = _make_5m_day_rows(
        [
            ("100", "101", "99", "100"),   # idx 0 (09:15)
            ("100", "100", "98", "99"),    # idx 1 (09:20)
            ("99", "99", "97", "98"),      # idx 2 (09:25)
            ("98", "98.5", "97", "98"),    # idx 3 (09:30)
            ("98", "111", "98", "110"),    # idx 4 (09:35): Signal emitted here
            ("112", "115", "111", "114"),  # idx 5 (09:40): Delay=2 skips bar 5
            ("116", "120", "115", "118"),  # idx 6 (09:45): Entry fills @ 116.00!
            ("118", "160", "117", "155"),  # idx 7 (09:50): TP hit
        ]
    )
    cfg = _base_config(profit_target=Decimal("30.0"), slippage_bps=Decimal("0"), max_trades_per_day=1)
    inputs = _base_inputs(slippage_bps=Decimal("0"), execution_delay_bars=2)
    out = BacktestEngine(strategy_config=cfg, inputs=inputs).run(rows)

    assert len(out.trades) == 1
    assert out.trades[0].entry_signal_time == datetime(2026, 11, 2, 9, 35, tzinfo=IST)
    assert out.trades[0].entry_time == datetime(2026, 11, 2, 9, 45, tzinfo=IST)
    assert out.trades[0].entry_price == Decimal("116.0000")


# ==============================================================================
# 3. STOP LOSS, PESSIMISTIC TIE-BREAK, OPPOSITE SIGNAL & EOD SQUARE-OFF TESTS
# ==============================================================================


def test_stop_loss_and_pessimistic_intrabar_tie_breaking() -> None:
    """When a single wide bar spans BOTH Stop Loss and Profit Target, engine must
    resolve pessimistically to STOP_LOSS (never optimistic profit target).
    """
    rows = _make_5m_day_rows(
        [
            ("100", "101", "99", "100"),
            ("100", "100", "98", "99"),
            ("99", "99", "97", "98"),
            ("98", "98.5", "97", "98"),
            ("98", "111", "98", "110"),    # idx 4: LONG signal
            ("110", "112", "108", "111"),  # idx 5: Entry @ 110 (SL=90, TP=140)
            ("111", "150", "85", "120"),   # idx 6: Wide bar hits BOTH low=85<=90 AND high=150>=140!
            ("120", "121", "119", "120"),
        ]
    )
    cfg = _base_config(
        stop_loss=Decimal("20.0"),
        profit_target=Decimal("30.0"),
        slippage_bps=Decimal("0"),
        estimated_charges_bps=Decimal("0"),
        per_order_fee_inr=Decimal("0"),
        max_trades_per_day=1,
    )
    inputs = _base_inputs(
        slippage_bps=Decimal("0"),
        estimated_charges_bps=Decimal("0"),
        per_order_fee_inr=Decimal("0"),
    )
    out = BacktestEngine(strategy_config=cfg, inputs=inputs).run(rows)

    assert len(out.trades) == 1
    tr = out.trades[0]
    assert tr.exit_reason == ExitReason.STOP_LOSS
    assert tr.exit_price == Decimal("90.0000")
    assert tr.net_pnl == Decimal("-200.0000")
    assert Decimal(out.metrics["max_drawdown_inr"]) >= Decimal("200.00")


def test_opposite_signal_delayed_exit_and_eod_square_off() -> None:
    """Verify Opposite Signal exits at next bar open, and Mandatory EOD Square-Off
    closes open intraday positions at `mandatory_square_off_time` open.
    """
    # Test 1: Opposite Signal exit
    opp_rows = _make_5m_day_rows(
        [
            ("100", "101", "99", "100"),   # 09:15
            ("100", "100", "98", "99"),    # 09:20
            ("99", "99", "97", "98"),      # 09:25
            ("98", "98.5", "97", "98"),    # 09:30
            ("98", "106", "98", "105"),    # 09:35: Bullish cross -> LONG signal
            ("105", "107", "104", "106"),  # 09:40: LONG entry @ 105
            ("106", "106", "93", "94"),    # 09:45: Bearish cross -> OPPOSITE_SIGNAL queued
            ("95", "96", "94", "95"),      # 09:50: Exits at open=95!
        ]
    )
    cfg_opp = _base_config(
        stop_loss=Decimal("100.0"),
        profit_target_enabled=False,
        exit_on_opposite_signal=True,
        slippage_bps=Decimal("0"),
        estimated_charges_bps=Decimal("0"),
        per_order_fee_inr=Decimal("0"),
        max_trades_per_day=1,
    )
    inputs_zero = _base_inputs(
        slippage_bps=Decimal("0"),
        estimated_charges_bps=Decimal("0"),
        per_order_fee_inr=Decimal("0"),
    )
    out_opp = BacktestEngine(strategy_config=cfg_opp, inputs=inputs_zero).run(opp_rows)
    assert len(out_opp.trades) == 1
    assert out_opp.trades[0].exit_reason == ExitReason.OPPOSITE_SIGNAL
    assert out_opp.trades[0].exit_time == datetime(2026, 11, 2, 9, 50, tzinfo=IST)
    assert out_opp.trades[0].exit_price == Decimal("95.0000")

    # Test 2: Mandatory EOD Square-Off at 10:00 IST
    eod_rows = _make_5m_day_rows(
        [
            ("100", "101", "99", "100"),   # 09:15
            ("100", "100", "98", "99"),    # 09:20
            ("99", "99", "97", "98"),      # 09:25
            ("98", "98.5", "97", "98"),    # 09:30
            ("98", "106", "98", "105"),    # 09:35: LONG signal
            ("105", "108", "104", "107"),  # 09:40: LONG entry @ 105
            ("107", "110", "106", "109"),  # 09:45
            ("109", "112", "108", "111"),  # 09:50
            ("111", "113", "110", "112"),  # 09:55
            ("113", "115", "112", "114"),  # 10:00: EOD square-off @ open=113!
            ("114", "115", "113", "114"),  # 10:05
        ]
    )
    cfg_eod = _base_config(
        last_entry_time="09:45:00",
        mandatory_square_off_time="10:00:00",
        stop_loss=Decimal("100.0"),
        profit_target_enabled=False,
        exit_on_opposite_signal=False,
        slippage_bps=Decimal("0"),
        estimated_charges_bps=Decimal("0"),
        per_order_fee_inr=Decimal("0"),
        max_trades_per_day=1,
    )
    out_eod = BacktestEngine(strategy_config=cfg_eod, inputs=inputs_zero).run(eod_rows)
    assert len(out_eod.trades) == 1
    assert out_eod.trades[0].exit_reason == ExitReason.EOD_SQUARE_OFF
    assert out_eod.trades[0].exit_time == datetime(2026, 11, 2, 10, 0, tzinfo=IST)
    assert out_eod.trades[0].exit_price == Decimal("113.0000")
    assert out_eod.trades[0].net_pnl == Decimal("80.0000")


# ==============================================================================
# 4. ZERO LOOK-AHEAD BIAS & UNDEFINED METRICS INVARIANTS
# ==============================================================================


def test_zero_lookahead_invariant_future_candles_never_affect_prior_fills() -> None:
    """Appending extreme future crashes or rallies after bar `k` MUST NOT change
    any entry signal, fill price, or closed trade occurring at or before bar `k`.
    Also execution_delay_bars < 1 must be rejected.
    """
    with pytest.raises(ValueError, match="execution_delay_bars"):
        BacktestEngine(
            strategy_config=_base_config(),
            inputs=_base_inputs(execution_delay_bars=0),
        )

    prefix_specs = [
        ("100", "101", "99", "100"),
        ("100", "100", "98", "99"),
        ("99", "99", "97", "98"),
        ("98", "98.5", "97", "98"),
        ("98", "111", "98", "110"),    # 09:35 signal
        ("112", "120", "111", "118"),  # 09:40 entry
        ("120", "145", "119", "143"),  # 09:45 profit target exit
        ("143", "144", "142", "143"),  # 09:50 flat
    ]
    future_crash_specs = prefix_specs + [
        ("143", "143", "20", "25"),    # Massive future crash
        ("25", "30", "10", "12"),
    ]
    future_rally_specs = prefix_specs + [
        ("143", "950", "143", "900"),  # Massive future spike
        ("900", "990", "890", "980"),
    ]

    cfg = _base_config(profit_target=Decimal("30.0"), max_trades_per_day=1)
    inp = _base_inputs()

    out_prefix = BacktestEngine(strategy_config=cfg, inputs=inp).run(_make_5m_day_rows(prefix_specs))
    out_crash = BacktestEngine(strategy_config=cfg, inputs=inp).run(_make_5m_day_rows(future_crash_specs))
    out_rally = BacktestEngine(strategy_config=cfg, inputs=inp).run(_make_5m_day_rows(future_rally_specs))

    assert out_prefix.trades[0] == out_crash.trades[0] == out_rally.trades[0]


def test_zero_trade_backtest_produces_none_for_undefined_ratios() -> None:
    """When 0 trades occur, Win Rate, Profit Factor, Expectancy, Avg Winner/Loser,
    and Largest Winner/Loser must be `None` (rendered as N/A), never fabricated.
    """
    flat_rows = _make_5m_day_rows([("100", "100.5", "99.5", "100")] * 12)
    out = BacktestEngine(strategy_config=_base_config(), inputs=_base_inputs()).run(flat_rows)

    assert out.status == BacktestStatus.COMPLETED
    assert out.metrics["total_trades"] == 0
    assert out.metrics["net_pnl"] == "0.00"
    assert out.metrics["win_rate_pct"] is None
    assert out.metrics["profit_factor"] is None
    assert out.metrics["expectancy_inr"] is None
    assert out.metrics["average_winner_inr"] is None
    assert out.metrics["average_loser_inr"] is None
    assert out.metrics["largest_winner_inr"] is None
    assert out.metrics["largest_loser_inr"] is None
