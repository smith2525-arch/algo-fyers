"""EMA Crossover + RSI Confirmation strategy engine.

Signal Rules (Evaluated strictly on closed bars in IST):
- LONG candidate (`ENTRY_LONG`):
    prev_fast_ema <= prev_slow_ema AND curr_fast_ema > curr_slow_ema
    AND curr_rsi >= long_rsi_threshold
    AND bar timestamp within [trading_start_time, last_entry_time] IST (weekday)
    AND trades_taken_today < max_trades_per_day
- SHORT candidate (`ENTRY_SHORT`):
    prev_fast_ema >= prev_slow_ema AND curr_fast_ema < curr_slow_ema
    AND curr_rsi <= short_rsi_threshold
    AND bar timestamp within [trading_start_time, last_entry_time] IST (weekday)
    AND trades_taken_today < max_trades_per_day
- Configurable Exit candidates (`EXIT_LONG` / `EXIT_SHORT` when `open_position` exists):
    1. Mandatory End-of-Day Square-Off (`bar_time_ist >= mandatory_square_off_time`)
    2. Stop Loss (`stop_loss` in POINTS or PERCENT)
    3. Trailing Stop (`trailing_stop` from favorable excursion when enabled)
    4. Profit Target (`profit_target` when enabled)
    5. Maximum Holding Period (`bars_held >= max_holding_bars` when enabled)
    6. Opposite EMA Crossover (`exit_on_opposite_signal` when enabled)
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass, field
from decimal import Decimal
from typing import TYPE_CHECKING, Any

from core.clock import ensure_ist, format_ist_iso, parse_hms_time
from core.domain.enums import (
    ExitReason,
    ExitTargetUnit,
    Side,
    SignalDirection,
    StrategyEvaluationState,
)
from core.strategies.base import (
    BaseStrategy,
    OHLCVBar,
    OpenPositionSnapshot,
    SignalCandidate,
)
from core.strategies.config_schema import EMA_RSI_STRATEGY_CODE, EmaRsiStrategyConfig
from core.strategies.indicators import compute_ema_series, compute_rsi_series

if TYPE_CHECKING:
    from core.market_data.events import CandleEvent


@dataclass(frozen=True)
class StrategyStreamEvaluation:
    """Explainable telemetry snapshot emitted when Strategy Engine processes a CandleEvent."""

    evaluation_status: StrategyEvaluationState
    fast_ema_period: int
    slow_ema_period: int
    rsi_period: int
    fast_ema: Decimal | None
    slow_ema: Decimal | None
    ema_spread: Decimal | None
    rsi: Decimal | None
    long_rsi_threshold: Decimal
    short_rsi_threshold: Decimal
    trend_regime: str
    crossover_state: str
    session_gate_open: bool
    warmup_bars_buffered: int
    warmup_bars_required: int
    condition_headline: str
    rule_checks: list[dict[str, Any]] = field(default_factory=list)
    signal: SignalCandidate | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "evaluation_status": self.evaluation_status.value,
            "fast_ema_period": self.fast_ema_period,
            "slow_ema_period": self.slow_ema_period,
            "rsi_period": self.rsi_period,
            "fast_ema": str(self.fast_ema.quantize(Decimal("0.01")))
            if self.fast_ema is not None
            else None,
            "slow_ema": str(self.slow_ema.quantize(Decimal("0.01")))
            if self.slow_ema is not None
            else None,
            "ema_spread": str(self.ema_spread.quantize(Decimal("0.01")))
            if self.ema_spread is not None
            else None,
            "rsi": str(self.rsi.quantize(Decimal("0.01"))) if self.rsi is not None else None,
            "long_rsi_threshold": str(self.long_rsi_threshold.quantize(Decimal("0.01"))),
            "short_rsi_threshold": str(self.short_rsi_threshold.quantize(Decimal("0.01"))),
            "trend_regime": self.trend_regime,
            "crossover_state": self.crossover_state,
            "session_gate_open": self.session_gate_open,
            "warmup_bars_buffered": self.warmup_bars_buffered,
            "warmup_bars_required": self.warmup_bars_required,
            "condition_headline": self.condition_headline,
            "rule_checks": self.rule_checks,
            "signal_emitted": self.signal is not None,
            "signal": {
                "strategy_code": self.signal.strategy_code,
                "symbol": self.signal.symbol,
                "timeframe": self.signal.timeframe,
                "bar_timestamp": format_ist_iso(self.signal.bar_timestamp),
                "side": self.signal.side.value,
                "direction": self.signal.direction.value,
                "reference_price": str(self.signal.reference_price.quantize(Decimal("0.01"))),
                "exit_reason": self.signal.exit_reason.value if self.signal.exit_reason else None,
                "rationale": self.signal.rationale,
            }
            if self.signal is not None
            else None,
        }


class EmaRsiCrossoverStrategy(BaseStrategy):
    """Deterministic closed-bar EMA Crossover + RSI Confirmation signal generator."""

    def __init__(self, config: EmaRsiStrategyConfig) -> None:
        self._config = config
        self._start_t = parse_hms_time(config.trading_start_time)
        self._last_entry_t = parse_hms_time(config.last_entry_time)
        self._square_off_t = parse_hms_time(config.mandatory_square_off_time)

    @property
    def code(self) -> str:
        return EMA_RSI_STRATEGY_CODE

    @property
    def config(self) -> EmaRsiStrategyConfig:
        return self._config

    @property
    def warmup_bars_required(self) -> int:
        """Minimum closed bars required so previous & current EMA + current RSI exist."""
        return max(self._config.slow_ema_period + 1, self._config.rsi_period + 1)

    def evaluate_bar_with_precomputed_indicators(
        self,
        curr_bar: OHLCVBar,
        *,
        bars_seen_count: int,
        curr_fast: Decimal | None,
        curr_slow: Decimal | None,
        prev_fast: Decimal | None,
        prev_slow: Decimal | None,
        curr_rsi: Decimal | None,
        open_position: OpenPositionSnapshot | None = None,
        trades_taken_today: int = 0,
    ) -> SignalCandidate | None:
        """Evaluate closed-bar rules in O(1) using strictly causal indicator values at bar `i`."""
        if curr_bar.symbol.upper() != self._config.instrument.upper():
            return None

        bar_ist = ensure_ist(curr_bar.bar_timestamp)
        bar_time = bar_ist.timetz().replace(tzinfo=None)

        bullish_cross = (
            prev_fast is not None
            and prev_slow is not None
            and curr_fast is not None
            and curr_slow is not None
            and prev_fast <= prev_slow
            and curr_fast > curr_slow
        )
        bearish_cross = (
            prev_fast is not None
            and prev_slow is not None
            and curr_fast is not None
            and curr_slow is not None
            and prev_fast >= prev_slow
            and curr_fast < curr_slow
        )

        indicator_payload = {
            "fast_ema": str(round(curr_fast, 4)) if curr_fast is not None else None,
            "slow_ema": str(round(curr_slow, 4)) if curr_slow is not None else None,
            "prev_fast_ema": str(round(prev_fast, 4)) if prev_fast is not None else None,
            "prev_slow_ema": str(round(prev_slow, 4)) if prev_slow is not None else None,
            "rsi": str(round(curr_rsi, 4)) if curr_rsi is not None else None,
        }

        # 1. Evaluate configurable EXIT rules first if a position is currently open
        if open_position is not None:
            exit_signal = self._evaluate_exit(
                curr_bar=curr_bar,
                bar_time=bar_time,
                open_position=open_position,
                bullish_cross=bullish_cross,
                bearish_cross=bearish_cross,
                indicator_payload=indicator_payload,
            )
            if exit_signal is not None:
                return exit_signal
            # Do not stack additional entries while a position is already open
            return None

        # 2. Entry eligibility gates: warmup, weekday, IST entry window, max trades/day
        if bars_seen_count < self.warmup_bars_required:
            return None
        if bar_ist.weekday() >= 5:
            return None
        if not (self._start_t <= bar_time <= self._last_entry_t):
            return None
        if trades_taken_today >= self._config.max_trades_per_day:
            return None
        if curr_rsi is None or curr_fast is None or curr_slow is None:
            return None

        # 3. Evaluate LONG & SHORT entry crossover + RSI confirmation
        if bullish_cross and curr_rsi >= self._config.long_rsi_threshold:
            return SignalCandidate(
                strategy_code=self.code,
                symbol=curr_bar.symbol.upper(),
                timeframe=curr_bar.timeframe,
                bar_timestamp=bar_ist,
                side=Side.BUY,
                direction=SignalDirection.ENTRY_LONG,
                reference_price=curr_bar.close,
                rationale=(
                    f"Fast EMA({self._config.fast_ema_period})={curr_fast:.2f} crossed above "
                    f"Slow EMA({self._config.slow_ema_period})={curr_slow:.2f} with "
                    f"RSI({self._config.rsi_period})={curr_rsi:.2f} >= {self._config.long_rsi_threshold}"
                ),
                indicators=indicator_payload,
            )

        if bearish_cross and curr_rsi <= self._config.short_rsi_threshold:
            return SignalCandidate(
                strategy_code=self.code,
                symbol=curr_bar.symbol.upper(),
                timeframe=curr_bar.timeframe,
                bar_timestamp=bar_ist,
                side=Side.SELL,
                direction=SignalDirection.ENTRY_SHORT,
                reference_price=curr_bar.close,
                rationale=(
                    f"Fast EMA({self._config.fast_ema_period})={curr_fast:.2f} crossed below "
                    f"Slow EMA({self._config.slow_ema_period})={curr_slow:.2f} with "
                    f"RSI({self._config.rsi_period})={curr_rsi:.2f} <= {self._config.short_rsi_threshold}"
                ),
                indicators=indicator_payload,
            )

        return None

    def evaluate_closed_bars(
        self,
        bars: Sequence[OHLCVBar],
        *,
        open_position: OpenPositionSnapshot | None = None,
        trades_taken_today: int = 0,
    ) -> SignalCandidate | None:
        if not bars:
            return None

        closes = [b.close for b in bars]
        fast_series = compute_ema_series(closes, self._config.fast_ema_period)
        slow_series = compute_ema_series(closes, self._config.slow_ema_period)
        rsi_series = compute_rsi_series(closes, self._config.rsi_period)

        return self.evaluate_bar_with_precomputed_indicators(
            bars[-1],
            bars_seen_count=len(bars),
            curr_fast=fast_series[-1] if fast_series else None,
            curr_slow=slow_series[-1] if slow_series else None,
            prev_fast=fast_series[-2] if len(fast_series) >= 2 else None,
            prev_slow=slow_series[-2] if len(slow_series) >= 2 else None,
            curr_rsi=rsi_series[-1] if rsi_series else None,
            open_position=open_position,
            trades_taken_today=trades_taken_today,
        )

    def _distance_to_price_delta(self, reference_price: Decimal, distance: Decimal) -> Decimal:
        if self._config.stop_target_unit == ExitTargetUnit.PERCENT:
            return (reference_price * distance) / Decimal("100")
        return distance

    def _evaluate_exit(
        self,
        *,
        curr_bar: OHLCVBar,
        bar_time: object,
        open_position: OpenPositionSnapshot,
        bullish_cross: bool,
        bearish_cross: bool,
        indicator_payload: dict[str, str | None],
    ) -> SignalCandidate | None:
        is_long = open_position.side == Side.BUY
        exit_side = Side.SELL if is_long else Side.BUY
        exit_dir = SignalDirection.EXIT_LONG if is_long else SignalDirection.EXIT_SHORT
        entry_px = open_position.entry_price
        close_px = curr_bar.close

        def _build_exit(reason: ExitReason, rationale: str) -> SignalCandidate:
            return SignalCandidate(
                strategy_code=self.code,
                symbol=curr_bar.symbol.upper(),
                timeframe=curr_bar.timeframe,
                bar_timestamp=curr_bar.bar_timestamp,
                side=exit_side,
                direction=exit_dir,
                reference_price=close_px,
                exit_reason=reason,
                rationale=rationale,
                indicators=indicator_payload,
            )

        # 1. Mandatory End-of-Day Square Off
        if self._config.eod_square_off_enabled and bar_time >= self._square_off_t:  # type: ignore[operator]
            return _build_exit(
                ExitReason.EOD_SQUARE_OFF,
                f"Mandatory intraday square-off time ({self._config.mandatory_square_off_time} IST) reached.",
            )

        # 2. Hard Stop Loss
        sl_delta = self._distance_to_price_delta(entry_px, self._config.stop_loss)
        if is_long and close_px <= (entry_px - sl_delta):
            return _build_exit(
                ExitReason.STOP_LOSS,
                f"Long Stop Loss triggered: close {close_px} <= stop {entry_px - sl_delta:.2f}.",
            )
        if (not is_long) and close_px >= (entry_px + sl_delta):
            return _build_exit(
                ExitReason.STOP_LOSS,
                f"Short Stop Loss triggered: close {close_px} >= stop {entry_px + sl_delta:.2f}.",
            )

        # 3. Trailing Stop
        if self._config.trailing_stop_enabled:
            peak_high = max(open_position.highest_price_since_entry, curr_bar.high)
            trough_low = min(open_position.lowest_price_since_entry, curr_bar.low)
            ts_delta = self._distance_to_price_delta(entry_px, self._config.trailing_stop)
            if is_long and peak_high > entry_px and close_px <= (peak_high - ts_delta):
                return _build_exit(
                    ExitReason.TRAILING_STOP,
                    f"Long Trailing Stop triggered from peak {peak_high} (trail={ts_delta:.2f}).",
                )
            if (not is_long) and trough_low < entry_px and close_px >= (trough_low + ts_delta):
                return _build_exit(
                    ExitReason.TRAILING_STOP,
                    f"Short Trailing Stop triggered from trough {trough_low} (trail={ts_delta:.2f}).",
                )

        # 4. Profit Target
        if self._config.profit_target_enabled:
            tp_delta = self._distance_to_price_delta(entry_px, self._config.profit_target)
            if is_long and close_px >= (entry_px + tp_delta):
                return _build_exit(
                    ExitReason.PROFIT_TARGET,
                    f"Long Profit Target reached: close {close_px} >= target {entry_px + tp_delta:.2f}.",
                )
            if (not is_long) and close_px <= (entry_px - tp_delta):
                return _build_exit(
                    ExitReason.PROFIT_TARGET,
                    f"Short Profit Target reached: close {close_px} <= target {entry_px - tp_delta:.2f}.",
                )

        # 5. Maximum Holding Period
        if (
            self._config.max_holding_enabled
            and open_position.bars_held >= self._config.max_holding_bars
        ):
            return _build_exit(
                ExitReason.MAX_HOLDING_PERIOD,
                f"Maximum holding period ({self._config.max_holding_bars} bars) reached.",
            )

        # 6. Opposite Crossover Signal
        if self._config.exit_on_opposite_signal:
            if is_long and bearish_cross:
                return _build_exit(
                    ExitReason.OPPOSITE_SIGNAL,
                    "Opposite bearish EMA crossover detected while long.",
                )
            if (not is_long) and bullish_cross:
                return _build_exit(
                    ExitReason.OPPOSITE_SIGNAL,
                    "Opposite bullish EMA crossover detected while short.",
                )

        return None

    def inspect_candle_events(
        self,
        events: Sequence[CandleEvent],
        *,
        open_position: OpenPositionSnapshot | None = None,
        trades_taken_today: int = 0,
        paper_trading_halted: bool = False,
    ) -> StrategyStreamEvaluation:
        """Consume normalized `CandleEvent`s and produce full visual explainability telemetry."""
        closed_bars = [ev.to_ohlcv_bar() for ev in events if ev.is_closed]
        req_warmup = self.warmup_bars_required

        if not closed_bars:
            return StrategyStreamEvaluation(
                evaluation_status=StrategyEvaluationState.STANDBY,
                fast_ema_period=self._config.fast_ema_period,
                slow_ema_period=self._config.slow_ema_period,
                rsi_period=self._config.rsi_period,
                fast_ema=None,
                slow_ema=None,
                ema_spread=None,
                rsi=None,
                long_rsi_threshold=self._config.long_rsi_threshold,
                short_rsi_threshold=self._config.short_rsi_threshold,
                trend_regime="STANDBY",
                crossover_state="NONE",
                session_gate_open=False,
                warmup_bars_buffered=0,
                warmup_bars_required=req_warmup,
                condition_headline="Standby — Start Market Replay to stream normalized CandleEvents into the Strategy Engine.",
                rule_checks=[],
                signal=None,
            )

        curr_bar = closed_bars[-1]
        bar_ist = ensure_ist(curr_bar.bar_timestamp)
        bar_time = bar_ist.timetz().replace(tzinfo=None)

        closes = [b.close for b in closed_bars]
        fast_series = compute_ema_series(closes, self._config.fast_ema_period)
        slow_series = compute_ema_series(closes, self._config.slow_ema_period)
        rsi_series = compute_rsi_series(closes, self._config.rsi_period)

        curr_fast = fast_series[-1] if fast_series else None
        curr_slow = slow_series[-1] if slow_series else None
        prev_fast = fast_series[-2] if len(fast_series) >= 2 else None
        prev_slow = slow_series[-2] if len(slow_series) >= 2 else None
        curr_rsi = rsi_series[-1] if rsi_series else None
        spread = (curr_fast - curr_slow) if (curr_fast is not None and curr_slow is not None) else None

        bullish_cross = (
            prev_fast is not None
            and prev_slow is not None
            and curr_fast is not None
            and curr_slow is not None
            and prev_fast <= prev_slow
            and curr_fast > curr_slow
        )
        bearish_cross = (
            prev_fast is not None
            and prev_slow is not None
            and curr_fast is not None
            and curr_slow is not None
            and prev_fast >= prev_slow
            and curr_fast < curr_slow
        )

        if curr_fast is None or curr_slow is None:
            trend_regime = "WARMING_UP"
        elif curr_fast > curr_slow:
            trend_regime = "BULLISH (Fast > Slow)"
        elif curr_fast < curr_slow:
            trend_regime = "BEARISH (Fast < Slow)"
        else:
            trend_regime = "NEUTRAL (Fast == Slow)"

        crossover_state = (
            "BULLISH_CROSSOVER"
            if bullish_cross
            else ("BEARISH_CROSSOVER" if bearish_cross else "NO_CROSSOVER")
        )
        in_session_window = (bar_ist.weekday() < 5) and (self._start_t <= bar_time <= self._last_entry_t)
        warmup_ready = len(closed_bars) >= req_warmup
        long_rsi_ok = curr_rsi is not None and curr_rsi >= self._config.long_rsi_threshold
        short_rsi_ok = curr_rsi is not None and curr_rsi <= self._config.short_rsi_threshold

        rule_checks = [
            {
                "label": f"Indicator Warmup ({min(len(closed_bars), req_warmup)}/{req_warmup} bars)",
                "passed": warmup_ready,
                "detail": "Ready" if warmup_ready else f"Buffering {req_warmup - len(closed_bars)} more candle(s)",
            },
            {
                "label": f"IST Entry Window ({self._config.trading_start_time}..{self._config.last_entry_time})",
                "passed": in_session_window,
                "detail": f"Bar @ {bar_ist.strftime('%H:%M:%S')} IST",
            },
            {
                "label": f"EMA({self._config.fast_ema_period}) vs EMA({self._config.slow_ema_period}) Crossover",
                "passed": bullish_cross or bearish_cross,
                "detail": crossover_state.replace("_", " ")
                if (bullish_cross or bearish_cross)
                else f"No fresh cross ({trend_regime})",
            },
            {
                "label": f"RSI({self._config.rsi_period}) Confirmation (L≥{self._config.long_rsi_threshold} / S≤{self._config.short_rsi_threshold})",
                "passed": bool(long_rsi_ok or short_rsi_ok),
                "detail": f"RSI = {curr_rsi:.2f}" if curr_rsi is not None else "Warming up",
            },
        ]

        if paper_trading_halted:
            return StrategyStreamEvaluation(
                evaluation_status=StrategyEvaluationState.HALTED_BY_OPERATOR,
                fast_ema_period=self._config.fast_ema_period,
                slow_ema_period=self._config.slow_ema_period,
                rsi_period=self._config.rsi_period,
                fast_ema=curr_fast,
                slow_ema=curr_slow,
                ema_spread=spread,
                rsi=curr_rsi,
                long_rsi_threshold=self._config.long_rsi_threshold,
                short_rsi_threshold=self._config.short_rsi_threshold,
                trend_regime=trend_regime,
                crossover_state=crossover_state,
                session_gate_open=False,
                warmup_bars_buffered=len(closed_bars),
                warmup_bars_required=req_warmup,
                condition_headline="STOP PAPER TRADING active — new entry signal emission halted by operator.",
                rule_checks=rule_checks,
                signal=None,
            )

        signal = self.consume_candle_events(
            events,
            open_position=open_position,
            trades_taken_today=trades_taken_today,
        )

        if signal is not None:
            status = StrategyEvaluationState.SIGNAL_GENERATED
            headline = f"SIGNAL EMITTED ({signal.direction.value}): {signal.rationale}"
        elif not warmup_ready:
            status = StrategyEvaluationState.WARMING_UP
            headline = (
                f"Warming up indicators: {len(closed_bars)}/{req_warmup} closed candles buffered "
                f"before EMA({self._config.slow_ema_period}) & RSI({self._config.rsi_period}) activate."
            )
        elif not in_session_window:
            status = StrategyEvaluationState.OUTSIDE_TRADING_WINDOW
            headline = (
                f"Outside new-entry window [{self._config.trading_start_time}..{self._config.last_entry_time} IST] "
                f"(current candle {bar_ist.strftime('%H:%M:%S')} IST)."
            )
        elif trades_taken_today >= self._config.max_trades_per_day:
            status = StrategyEvaluationState.DAILY_LIMIT_REACHED
            headline = f"Daily trade cap ({self._config.max_trades_per_day}) reached for {bar_ist.date()}."
        elif bullish_cross and not long_rsi_ok:
            status = StrategyEvaluationState.EVALUATING_NO_SIGNAL
            headline = (
                f"Bullish EMA crossover occurred, but RSI({curr_rsi:.2f}) < Long Threshold "
                f"({self._config.long_rsi_threshold}) — entry filtered out."
            )
        elif bearish_cross and not short_rsi_ok:
            status = StrategyEvaluationState.EVALUATING_NO_SIGNAL
            headline = (
                f"Bearish EMA crossover occurred, but RSI({curr_rsi:.2f}) > Short Threshold "
                f"({self._config.short_rsi_threshold}) — entry filtered out."
            )
        else:
            status = StrategyEvaluationState.EVALUATING_NO_SIGNAL
            spread_txt = f"{spread:+.2f}" if spread is not None else "0.00"
            rsi_txt = f"{curr_rsi:.2f}" if curr_rsi is not None else "N/A"
            headline = (
                f"Monitoring stream: {trend_regime} (spread {spread_txt} pts), RSI={rsi_txt} — "
                f"awaiting confirmed EMA({self._config.fast_ema_period})/{self._config.slow_ema_period} crossover."
            )

        return StrategyStreamEvaluation(
            evaluation_status=status,
            fast_ema_period=self._config.fast_ema_period,
            slow_ema_period=self._config.slow_ema_period,
            rsi_period=self._config.rsi_period,
            fast_ema=curr_fast,
            slow_ema=curr_slow,
            ema_spread=spread,
            rsi=curr_rsi,
            long_rsi_threshold=self._config.long_rsi_threshold,
            short_rsi_threshold=self._config.short_rsi_threshold,
            trend_regime=trend_regime,
            crossover_state=crossover_state,
            session_gate_open=in_session_window,
            warmup_bars_buffered=len(closed_bars),
            warmup_bars_required=req_warmup,
            condition_headline=headline,
            rule_checks=rule_checks,
            signal=signal,
        )

