"""Unit tests for EMA Crossover + RSI Confirmation strategy logic, validation, exits, and IST session bounds."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from decimal import Decimal

import pytest

from core.clock import IST
from core.domain.enums import (
    ExitReason,
    ExitTargetUnit,
    PositionSizingMode,
    Side,
    SignalDirection,
)
from core.exceptions import StrategyValidationError
from core.strategies.base import OHLCVBar, OpenPositionSnapshot
from core.strategies.config_schema import (
    EmaRsiStrategyConfig,
    parse_and_validate_ema_rsi_config,
)
from core.strategies.ema_rsi import EmaRsiCrossoverStrategy
from core.strategies.indicators import compute_ema_series, compute_rsi_series


def _make_bars(
    closes: list[str | float],
    *,
    symbol: str = "NIFTY",
    timeframe: str = "5m",
    start_dt: datetime | None = None,
    step_minutes: int = 5,
) -> list[OHLCVBar]:
    base = start_dt or datetime(2026, 11, 18, 9, 20, 0, tzinfo=IST)  # Wednesday IST
    bars: list[OHLCVBar] = []
    for idx, val in enumerate(closes):
        px = Decimal(str(val))
        ts = base + timedelta(minutes=idx * step_minutes)
        bars.append(
            OHLCVBar(
                symbol=symbol,
                timeframe=timeframe,
                bar_timestamp=ts,
                open=px - Decimal("2"),
                high=px + Decimal("5"),
                low=px - Decimal("5"),
                close=px,
                volume=10000,
            )
        )
    return bars


def test_ema_and_wilder_rsi_deterministic_series() -> None:
    closes = [Decimal(str(x)) for x in [100, 102, 104, 103, 106, 108, 110, 112]]
    ema3 = compute_ema_series(closes, period=3)
    assert ema3[0] is None
    assert ema3[1] is None
    assert ema3[2] == Decimal("102")  # SMA(100, 102, 104)

    rsi4 = compute_rsi_series(closes, period=4)
    assert rsi4[:4] == [None, None, None, None]
    assert rsi4[4] is not None
    assert Decimal("50") < rsi4[4] <= Decimal("100")


def test_bullish_ema_crossover_with_rsi_confirmation_emits_long_signal() -> None:
    cfg = EmaRsiStrategyConfig(
        instrument="NIFTY",
        fast_ema_period=3,
        slow_ema_period=6,
        rsi_period=4,
        long_rsi_threshold=Decimal("55"),
        short_rsi_threshold=Decimal("45"),
    )
    strat = EmaRsiCrossoverStrategy(cfg)

    # Gradual dip followed by sharp uptick on the final closed bar causing Fast EMA(3) > Slow EMA(6)
    closes = [24100, 24090, 24080, 24070, 24065, 24060, 24058, 24115]
    bars = _make_bars(closes)

    sig = strat.evaluate_closed_bars(bars)
    assert sig is not None
    assert sig.side == Side.BUY
    assert sig.direction == SignalDirection.ENTRY_LONG
    assert sig.symbol == "NIFTY"
    assert sig.bar_timestamp.utcoffset() == timedelta(hours=5, minutes=30)

    # Next bar continues higher without a fresh crossover -> must NOT emit duplicate entry signal
    bars_next = _make_bars(closes + [24130])
    assert strat.evaluate_closed_bars(bars_next) is None


def test_bearish_ema_crossover_with_rsi_confirmation_emits_short_signal() -> None:
    cfg = EmaRsiStrategyConfig(
        instrument="BANKNIFTY",
        fast_ema_period=3,
        slow_ema_period=6,
        rsi_period=4,
        long_rsi_threshold=Decimal("55"),
        short_rsi_threshold=Decimal("45"),
    )
    strat = EmaRsiCrossoverStrategy(cfg)

    closes = [51000, 51020, 51040, 51060, 51075, 51085, 51090, 50960]
    bars = _make_bars(closes, symbol="BANKNIFTY")

    sig = strat.evaluate_closed_bars(bars)
    assert sig is not None
    assert sig.side == Side.SELL
    assert sig.direction == SignalDirection.ENTRY_SHORT
    assert sig.symbol == "BANKNIFTY"


def test_rsi_filter_blocks_crossover_when_threshold_not_met() -> None:
    # Require an extreme RSI >= 95 so a modest bullish crossover is filtered out
    cfg = EmaRsiStrategyConfig(
        instrument="NIFTY",
        fast_ema_period=3,
        slow_ema_period=6,
        rsi_period=4,
        long_rsi_threshold=Decimal("95"),
        short_rsi_threshold=Decimal("45"),
    )
    strat = EmaRsiCrossoverStrategy(cfg)
    closes = [24100, 24090, 24080, 24070, 24065, 24060, 24058, 24115]
    bars = _make_bars(closes)

    assert strat.evaluate_closed_bars(bars) is None


def test_ist_timezone_conversion_session_windows_and_max_trades_gate() -> None:
    cfg = EmaRsiStrategyConfig(
        instrument="NIFTY",
        fast_ema_period=3,
        slow_ema_period=6,
        rsi_period=4,
        trading_start_time="09:20:00",
        last_entry_time="14:45:00",
        mandatory_square_off_time="15:20:00",
        max_trades_per_day=2,
    )
    strat = EmaRsiCrossoverStrategy(cfg)
    closes = [24100, 24090, 24080, 24070, 24065, 24060, 24058, 24115]

    # 1. Supply bars with UTC timestamps (04:00 UTC == 09:30 IST; 8th bar at +35m = 10:05 IST) -> valid!
    utc_start = datetime(2026, 11, 18, 4, 0, 0, tzinfo=timezone.utc)
    utc_bars = _make_bars(closes, start_dt=utc_start)
    sig = strat.evaluate_closed_bars(utc_bars)
    assert sig is not None
    assert sig.bar_timestamp.hour == 10
    assert sig.bar_timestamp.minute == 5
    assert sig.bar_timestamp.utcoffset() == timedelta(hours=5, minutes=30)

    # 2. Max trades/day reached -> blocked
    assert strat.evaluate_closed_bars(utc_bars, trades_taken_today=2) is None

    # 3. After last_entry_time (final bar at 14:55 IST > 14:45 IST) -> entry blocked
    late_start = datetime(2026, 11, 18, 14, 20, 0, tzinfo=IST)
    late_bars = _make_bars(closes, start_dt=late_start)
    assert strat.evaluate_closed_bars(late_bars) is None

    # 4. Weekend bar (Saturday 2026-11-21) -> blocked
    sat_start = datetime(2026, 11, 21, 9, 30, 0, tzinfo=IST)
    sat_bars = _make_bars(closes, start_dt=sat_start)
    assert strat.evaluate_closed_bars(sat_bars) is None


def test_configurable_exit_mechanisms() -> None:
    cfg = EmaRsiStrategyConfig(
        instrument="NIFTY",
        fast_ema_period=3,
        slow_ema_period=6,
        rsi_period=4,
        stop_target_unit=ExitTargetUnit.POINTS,
        stop_loss=Decimal("30"),
        profit_target_enabled=True,
        profit_target=Decimal("60"),
        trailing_stop_enabled=True,
        trailing_stop=Decimal("20"),
        max_holding_enabled=True,
        max_holding_bars=10,
        exit_on_opposite_signal=True,
        eod_square_off_enabled=True,
        mandatory_square_off_time="15:20:00",
    )
    strat = EmaRsiCrossoverStrategy(cfg)
    entry_ts = datetime(2026, 11, 18, 10, 0, 0, tzinfo=IST)

    # 1. Hard Stop Loss on Long (entry 24000, close 23965 <= 23970)
    pos_sl = OpenPositionSnapshot(
        symbol="NIFTY",
        side=Side.BUY,
        entry_price=Decimal("24000"),
        entry_bar_timestamp=entry_ts,
        bars_held=2,
        highest_price_since_entry=Decimal("24005"),
        lowest_price_since_entry=Decimal("23965"),
    )
    sl_sig = strat.evaluate_closed_bars(_make_bars([24000, 23965]), open_position=pos_sl)
    assert sl_sig is not None
    assert sl_sig.direction == SignalDirection.EXIT_LONG
    assert sl_sig.exit_reason == ExitReason.STOP_LOSS

    # 2. Profit Target on Long (entry 24000, close 24065 >= 24060)
    pos_tp = OpenPositionSnapshot(
        symbol="NIFTY",
        side=Side.BUY,
        entry_price=Decimal("24000"),
        entry_bar_timestamp=entry_ts,
        bars_held=3,
        highest_price_since_entry=Decimal("24065"),
        lowest_price_since_entry=Decimal("24000"),
    )
    tp_sig = strat.evaluate_closed_bars(_make_bars([24000, 24065]), open_position=pos_tp)
    assert tp_sig is not None
    assert tp_sig.exit_reason == ExitReason.PROFIT_TARGET

    # 3. Trailing Stop on Long (entry 24000, peak 24050, close 24028 <= 24030)
    pos_trail = OpenPositionSnapshot(
        symbol="NIFTY",
        side=Side.BUY,
        entry_price=Decimal("24000"),
        entry_bar_timestamp=entry_ts,
        bars_held=4,
        highest_price_since_entry=Decimal("24050"),
        lowest_price_since_entry=Decimal("24000"),
    )
    trail_sig = strat.evaluate_closed_bars(_make_bars([24000, 24028]), open_position=pos_trail)
    assert trail_sig is not None
    assert trail_sig.exit_reason == ExitReason.TRAILING_STOP

    # 4. Max Holding Period (bars_held == 10)
    pos_hold = OpenPositionSnapshot(
        symbol="NIFTY",
        side=Side.SELL,
        entry_price=Decimal("24000"),
        entry_bar_timestamp=entry_ts,
        bars_held=10,
        highest_price_since_entry=Decimal("24005"),
        lowest_price_since_entry=Decimal("23995"),
    )
    hold_sig = strat.evaluate_closed_bars(_make_bars([24000, 23998]), open_position=pos_hold)
    assert hold_sig is not None
    assert hold_sig.direction == SignalDirection.EXIT_SHORT
    assert hold_sig.exit_reason == ExitReason.MAX_HOLDING_PERIOD

    # 5. Mandatory EOD Square-Off at 15:20 IST
    eod_bars = _make_bars(
        [24000, 24005],
        start_dt=datetime(2026, 11, 18, 15, 15, 0, tzinfo=IST),
    )
    pos_eod = OpenPositionSnapshot(
        symbol="NIFTY",
        side=Side.BUY,
        entry_price=Decimal("24000"),
        entry_bar_timestamp=entry_ts,
        bars_held=2,
        highest_price_since_entry=Decimal("24005"),
        lowest_price_since_entry=Decimal("24000"),
    )
    eod_sig = strat.evaluate_closed_bars(eod_bars, open_position=pos_eod)
    assert eod_sig is not None
    assert eod_sig.exit_reason == ExitReason.EOD_SQUARE_OFF


@pytest.mark.parametrize(
    ("bad_overrides", "expected_substring"),
    [
        ({"fast_ema_period": 21, "slow_ema_period": 21}, "Fast EMA Period"),
        ({"fast_ema_period": 30, "slow_ema_period": 15}, "Fast EMA Period"),
        (
            {
                "trading_start_time": "10:00:00",
                "last_entry_time": "09:45:00",
                "mandatory_square_off_time": "15:20:00",
            },
            "Trading Start < Last Entry < Mandatory Square-Off",
        ),
        (
            {
                "trading_start_time": "09:20:00",
                "last_entry_time": "15:20:00",
                "mandatory_square_off_time": "15:20:00",
            },
            "Trading Start < Last Entry < Mandatory Square-Off",
        ),
        ({"stop_loss": 0}, "stop_loss"),
        ({"stop_loss": -10}, "stop_loss"),
        ({"max_trades_per_day": 0}, "max_trades_per_day"),
        ({"long_rsi_threshold": 45, "short_rsi_threshold": 55}, "Short RSI Threshold"),
        (
            {
                "position_sizing_mode": PositionSizingMode.FIXED_LOTS.value,
                "position_size_value": "1.5",
            },
            "whole-number",
        ),
    ],
)
def test_configuration_validation_rejects_invalid_inputs(
    bad_overrides: dict[str, object], expected_substring: str
) -> None:
    base = EmaRsiStrategyConfig().to_canonical_dict()
    base.update(bad_overrides)
    with pytest.raises(StrategyValidationError) as exc_info:
        parse_and_validate_ema_rsi_config(base)
    assert expected_substring in str(exc_info.value)


def test_exchange_session_bounds_validation() -> None:
    base = EmaRsiStrategyConfig(mandatory_square_off_time="15:40:00").to_canonical_dict()
    with pytest.raises(StrategyValidationError, match="reference exchange hours"):
        parse_and_validate_ema_rsi_config(
            base,
            exchange_open_ist="09:15:00",
            exchange_close_ist="15:30:00",
        )
