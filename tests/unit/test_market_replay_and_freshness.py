"""Unit tests for Phase 3A Market Replay normalized events, feed decoupling,
market-data freshness tracking (HEALTHY / STALE / STOPPED), stale-order gate,
and live strategy stream explainability (`inspect_candle_events`).
"""

from __future__ import annotations

import uuid
from datetime import datetime, timedelta, timezone
from decimal import Decimal

import pytest

from core.clock import IST
from core.domain.enums import (
    MarketDataFreshnessStatus,
    MarketEventType,
    MarketFeedSource,
    ReplaySessionStatus,
    SignalDirection,
    StrategyEvaluationState,
)
from core.exceptions import StaleMarketDataError
from core.market_data.events import (
    CandleEvent,
    HistoricalCandleReplayFeed,
    MarketEvent,
    MarketEventFeed,
)
from core.market_data.freshness import (
    enforce_fresh_market_data_for_new_order,
    evaluate_market_data_freshness,
)
from core.strategies.base import OHLCVBar
from core.strategies.config_schema import parse_and_validate_ema_rsi_config
from core.strategies.ema_rsi import EmaRsiCrossoverStrategy


class MockWebSocketCandleFeed(MarketEventFeed):
    """Non-CSV simulated live broker WebSocket feed proving zero CSV coupling."""

    def __init__(self, payloads: list[dict[str, object]], symbol: str = "NIFTY") -> None:
        self._payloads = list(payloads)
        self._symbol = symbol
        self._idx = 0

    @property
    def source(self) -> MarketFeedSource:
        return MarketFeedSource.FYERS_WEBSOCKET

    @property
    def symbol(self) -> str:
        return self._symbol

    @property
    def timeframe(self) -> str:
        return "5m"

    def has_next(self) -> bool:
        return self._idx < len(self._payloads)

    def next_candle_event(self, *, received_at: datetime | None = None) -> CandleEvent | None:
        if not self.has_next():
            return None
        raw = self._payloads[self._idx]
        self._idx += 1
        recv = received_at or datetime(2026, 10, 29, 10, 0, tzinfo=IST)
        return CandleEvent(
            event_id=uuid.uuid4(),
            event_type=MarketEventType.CANDLE,
            source=MarketFeedSource.FYERS_WEBSOCKET,
            symbol=self._symbol,
            exchange_timestamp=raw["ts"],  # type: ignore[arg-type]
            received_at=recv,
            sequence_number=self._idx,
            timeframe="5m",
            open=Decimal(str(raw["open"])),
            high=Decimal(str(raw["high"])),
            low=Decimal(str(raw["low"])),
            close=Decimal(str(raw["close"])),
            volume=int(raw["volume"]),  # type: ignore[arg-type]
            is_closed=True,
        )

    def reset(self) -> None:
        self._idx = 0


def _build_synthetic_crossover_bars() -> list[OHLCVBar]:
    """Construct deterministic 5m IST bars that warm up and cross Fast EMA above Slow EMA."""
    base_ts = datetime(2026, 10, 29, 9, 15, tzinfo=IST)
    # Gently decline for 24 bars, then surge on bars 25-27 to trigger bullish EMA cross + RSI >= 55
    closes = [Decimal("24200") - Decimal(i * 4) for i in range(24)] + [
        Decimal("24150"),
        Decimal("24220"),
        Decimal("24290"),
    ]
    bars: list[OHLCVBar] = []
    for idx, c in enumerate(closes):
        ts = base_ts + timedelta(minutes=5 * idx)
        bars.append(
            OHLCVBar(
                symbol="NIFTY",
                timeframe="5m",
                bar_timestamp=ts,
                open=c - Decimal("5"),
                high=c + Decimal("12"),
                low=c - Decimal("10"),
                close=c,
                volume=10000 + idx * 100,
            )
        )
    return bars


def test_market_event_and_candle_event_enforce_ist_normalization() -> None:
    utc_ts = datetime(2026, 10, 29, 4, 0, tzinfo=timezone.utc)  # 09:30 IST
    ev = MarketEvent(
        event_id=uuid.uuid4(),
        event_type=MarketEventType.HEARTBEAT,
        source=MarketFeedSource.HISTORICAL_REPLAY,
        symbol=" nifty ",
        exchange_timestamp=utc_ts,
        received_at=utc_ts,
        sequence_number=1,
    )
    assert ev.symbol == "NIFTY"
    assert ev.exchange_timestamp.utcoffset() == timedelta(hours=5, minutes=30)
    assert ev.exchange_timestamp.hour == 9
    assert ev.exchange_timestamp.minute == 30

    bar = OHLCVBar(
        symbol="BANKNIFTY",
        timeframe="5m",
        bar_timestamp=utc_ts,
        open=Decimal("51500.00"),
        high=Decimal("51560.50"),
        low=Decimal("51480.25"),
        close=Decimal("51540.75"),
        volume=25000,
    )
    candle_ev = CandleEvent.from_ohlcv_bar(
        bar, source=MarketFeedSource.HISTORICAL_REPLAY, sequence_number=7, received_at=utc_ts
    )
    d = candle_ev.to_dict()
    assert d["symbol"] == "BANKNIFTY"
    assert d["exchange_timestamp"].endswith("+05:30")
    assert d["received_at"].endswith("+05:30")
    assert d["close"] == "51540.75"
    assert candle_ev.to_ohlcv_bar().close == Decimal("51540.75")


def test_strategy_consumes_replay_feed_and_mock_websocket_feed_identically() -> None:
    """Prove Strategy Engine depends ONLY on normalized CandleEvent, never on CSV files."""
    bars = _build_synthetic_crossover_bars()
    ws_payloads = [
        {
            "ts": b.bar_timestamp,
            "open": b.open,
            "high": b.high,
            "low": b.low,
            "close": b.close,
            "volume": b.volume,
        }
        for b in bars
    ]

    replay_feed = HistoricalCandleReplayFeed(bars, symbol="NIFTY", timeframe="5m")
    ws_feed = MockWebSocketCandleFeed(ws_payloads, symbol="NIFTY")

    cfg = parse_and_validate_ema_rsi_config(
        {
            "instrument": "NIFTY",
            "timeframe": "5m",
            "fast_ema_period": 9,
            "slow_ema_period": 21,
            "rsi_period": 14,
            "long_rsi_threshold": 55.0,
            "short_rsi_threshold": 45.0,
            "stop_loss": 40.0,
            "profit_target": 80.0,
        }
    )
    strategy = EmaRsiCrossoverStrategy(cfg)

    replay_stream: list[CandleEvent] = []
    ws_stream: list[CandleEvent] = []
    replay_signals: list[SignalDirection] = []
    ws_signals: list[SignalDirection] = []

    while replay_feed.has_next() and ws_feed.has_next():
        r_ev = replay_feed.next_candle_event()
        w_ev = ws_feed.next_candle_event()
        assert r_ev is not None and w_ev is not None
        assert r_ev.source == MarketFeedSource.HISTORICAL_REPLAY
        assert w_ev.source == MarketFeedSource.FYERS_WEBSOCKET

        replay_stream.append(r_ev)
        ws_stream.append(w_ev)

        r_sig = strategy.consume_candle_events(replay_stream)
        w_sig = strategy.consume_candle_events(ws_stream)
        r_eval = strategy.inspect_candle_events(replay_stream)
        w_eval = strategy.inspect_candle_events(ws_stream)

        assert r_eval.evaluation_status == w_eval.evaluation_status
        assert r_eval.fast_ema == w_eval.fast_ema
        assert r_eval.slow_ema == w_eval.slow_ema
        assert r_eval.rsi == w_eval.rsi
        assert (r_sig.direction if r_sig else None) == (w_sig.direction if w_sig else None)

        if r_sig is not None:
            replay_signals.append(r_sig.direction)
        if w_sig is not None:
            ws_signals.append(w_sig.direction)

    assert replay_signals == ws_signals
    assert SignalDirection.ENTRY_LONG in replay_signals

    # Verify reset() rewinds HistoricalCandleReplayFeed cleanly
    assert replay_feed.remaining_candles == 0
    replay_feed.reset()
    assert replay_feed.cursor == 0
    assert replay_feed.remaining_candles == len(bars)


def test_strategy_stream_evaluation_states_and_halt_awareness() -> None:
    cfg = parse_and_validate_ema_rsi_config(
        {
            "instrument": "NIFTY",
            "timeframe": "5m",
            "fast_ema_period": 9,
            "slow_ema_period": 21,
            "rsi_period": 14,
            "long_rsi_threshold": 55.0,
            "short_rsi_threshold": 45.0,
            "trading_start_time": "09:20",
            "last_entry_time": "14:45",
            "mandatory_square_off_time": "15:15",
            "stop_loss": 40.0,
            "profit_target": 80.0,
        }
    )
    strategy = EmaRsiCrossoverStrategy(cfg)

    # 1. Empty stream -> STANDBY
    empty_eval = strategy.inspect_candle_events([])
    assert empty_eval.evaluation_status == StrategyEvaluationState.STANDBY
    assert empty_eval.signal is None

    # 2. Fewer bars than warmup -> WARMING_UP
    bars = _build_synthetic_crossover_bars()
    events = [CandleEvent.from_ohlcv_bar(b, sequence_number=i + 1) for i, b in enumerate(bars)]
    warm_eval = strategy.inspect_candle_events(events[:5])
    assert warm_eval.evaluation_status == StrategyEvaluationState.WARMING_UP
    assert warm_eval.warmup_bars_buffered == 5

    # 3. Operator STOP PAPER TRADING engaged -> HALTED_BY_OPERATOR (suppresses new entry signals)
    halted_eval = strategy.inspect_candle_events(events, paper_trading_halted=True)
    assert halted_eval.evaluation_status == StrategyEvaluationState.HALTED_BY_OPERATOR
    assert halted_eval.signal is None

    # 4. Outside entry window (15:00 IST > 14:45 last_entry_time) -> OUTSIDE_TRADING_WINDOW
    late_bar = OHLCVBar(
        symbol="NIFTY",
        timeframe="5m",
        bar_timestamp=datetime(2026, 10, 29, 15, 0, tzinfo=IST),
        open=Decimal("24300"),
        high=Decimal("24310"),
        low=Decimal("24290"),
        close=Decimal("24305"),
        volume=12000,
    )
    late_events = events + [CandleEvent.from_ohlcv_bar(late_bar, sequence_number=99)]
    outside_eval = strategy.inspect_candle_events(late_events)
    assert outside_eval.evaluation_status == StrategyEvaluationState.OUTSIDE_TRADING_WINDOW
    assert outside_eval.signal is None


def test_market_data_freshness_tracking_and_stale_order_refusal() -> None:
    ref_t = datetime(2026, 10, 29, 10, 30, 0, tzinfo=IST)
    exch_t = datetime(2026, 10, 29, 10, 25, 0, tzinfo=IST)

    # 1. Inactive / STOPPED / CONFIGURED / COMPLETED -> STOPPED, new orders refused
    for st_val in (
        None,
        ReplaySessionStatus.CONFIGURED,
        ReplaySessionStatus.STOPPED,
        ReplaySessionStatus.COMPLETED,
    ):
        snap = evaluate_market_data_freshness(
            session_status=st_val,
            last_candle_received_at=ref_t - timedelta(seconds=2),
            last_exchange_timestamp=exch_t,
            stale_threshold_seconds=15,
            reference_time=ref_t,
        )
        assert snap.status == MarketDataFreshnessStatus.STOPPED
        assert snap.new_orders_permitted is False
        with pytest.raises(StaleMarketDataError):
            enforce_fresh_market_data_for_new_order(snap)

    # 2. RUNNING with recent candle (age 3.5s <= 15s) -> HEALTHY, new orders permitted
    healthy_snap = evaluate_market_data_freshness(
        session_status=ReplaySessionStatus.RUNNING,
        last_candle_received_at=ref_t - timedelta(seconds=3.5),
        last_exchange_timestamp=exch_t,
        stale_threshold_seconds=15,
        reference_time=ref_t,
    )
    assert healthy_snap.status == MarketDataFreshnessStatus.HEALTHY
    assert healthy_snap.new_orders_permitted is True
    assert healthy_snap.market_data_age_seconds == Decimal("3.5")
    enforce_fresh_market_data_for_new_order(healthy_snap)  # Must NOT raise

    # 3. RUNNING with old candle (age 19s > 15s) -> STALE, new orders refused
    stale_age_snap = evaluate_market_data_freshness(
        session_status=ReplaySessionStatus.RUNNING,
        last_candle_received_at=ref_t - timedelta(seconds=19),
        last_exchange_timestamp=exch_t,
        stale_threshold_seconds=15,
        reference_time=ref_t,
    )
    assert stale_age_snap.status == MarketDataFreshnessStatus.STALE
    assert stale_age_snap.new_orders_permitted is False
    with pytest.raises(StaleMarketDataError, match="STALE"):
        enforce_fresh_market_data_for_new_order(stale_age_snap)

    # 4. PAUSED / PAUSED_RECOVERY even at 1s age -> STALE for execution safety, new orders refused
    for paused_st in (ReplaySessionStatus.PAUSED, ReplaySessionStatus.PAUSED_RECOVERY):
        paused_snap = evaluate_market_data_freshness(
            session_status=paused_st,
            last_candle_received_at=ref_t - timedelta(seconds=1),
            last_exchange_timestamp=exch_t,
            stale_threshold_seconds=15,
            reference_time=ref_t,
        )
        assert paused_snap.status == MarketDataFreshnessStatus.STALE
        assert paused_snap.new_orders_permitted is False
        with pytest.raises(StaleMarketDataError):
            enforce_fresh_market_data_for_new_order(paused_snap)
