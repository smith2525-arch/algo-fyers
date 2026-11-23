"""Deterministic, zero-lookahead historical Backtesting Engine.

Key Engineering Guarantees:
1. ZERO LOOK-AHEAD BIAS:
   - At bar index `i`, the strategy receives a sliced tuple `bars[: i + 1]`. Future
     bars (`j > i`) are never accessible during signal calculation.
   - Any entry or closed-bar exit signal emitted from completed candle `i` is queued
     with `execution_delay_bars >= 1` and fills at `bars[i + delay].open` (never on
     candle `i` itself).
   - Intrabar stop-loss vs. profit-target ambiguity on the same candle always resolves
     pessimistically to `STOP_LOSS`, and gap-opens fill at the actual `bar.open`.
2. EXACT ACCOUNTING RECONCILIATION:
   - `gross_pnl == (exit_price - entry_price) * qty` (for LONG; inverted for SHORT)
   - `raw_pnl - slippage_cost == gross_pnl`
   - `gross_pnl - charges == net_pnl`
   - `initial_capital + sum(trade.net_pnl) == ending_equity`
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from datetime import date, datetime
from decimal import Decimal, ROUND_FLOOR

from core.backtesting.metrics import compute_backtest_analytics
from core.backtesting.models import (
    BacktestEngineOutput,
    BacktestExecutionInputs,
    BacktestTradeRecord,
    RawCandleRow,
)
from core.backtesting.validator import validate_ohlcv_rows
from core.clock import format_ist_iso, parse_hms_time
from core.domain.enums import (
    BacktestStatus,
    ExitReason,
    ExitTargetUnit,
    PositionSizingMode,
    Side,
    SignalDirection,
    TradeDirection,
)
from core.strategies.base import OHLCVBar, OpenPositionSnapshot
from core.strategies.config_schema import EmaRsiStrategyConfig
from core.strategies.ema_rsi import EmaRsiCrossoverStrategy

BACKTEST_ENGINE_VERSION = "backtest-engine-1.0.0"
_BPS_DIVISOR = Decimal("10000")
_PRICE_QUANT = Decimal("0.0001")
_INR_QUANT = Decimal("0.0001")


@dataclass
class _PendingOrder:
    signal_time: datetime
    due_bar_index: int
    side: Side
    direction: SignalDirection
    exit_reason: ExitReason | None = None


@dataclass
class _ActivePosition:
    direction: TradeDirection
    side: Side
    quantity: int
    entry_signal_time: datetime
    entry_time: datetime
    entry_bar_index: int
    raw_entry_price: Decimal
    entry_price: Decimal
    entry_slippage_cost: Decimal
    highest_since_entry: Decimal
    lowest_since_entry: Decimal


def _apply_adverse_slippage(raw_price: Decimal, order_side: Side, slippage_bps: Decimal) -> Decimal:
    """Apply adverse execution slippage (`BUY` fills higher; `SELL` fills lower)."""
    rate = slippage_bps / _BPS_DIVISOR
    if order_side == Side.BUY:
        slipped = raw_price * (Decimal("1") + rate)
    else:
        slipped = raw_price * (Decimal("1") - rate)
    return slipped.quantize(_PRICE_QUANT)


def _compute_order_charges(
    entry_price: Decimal,
    exit_price: Decimal,
    quantity: int,
    charges_bps: Decimal,
    per_order_fee_inr: Decimal,
) -> Decimal:
    turnover = (entry_price + exit_price) * Decimal(quantity)
    variable_fee = turnover * (charges_bps / _BPS_DIVISOR)
    flat_fees = per_order_fee_inr * Decimal(2)  # 1 entry order + 1 exit order
    return (variable_fee + flat_fees).quantize(_INR_QUANT)


class BacktestEngine:
    """Executes an immutable EmaRsiStrategyConfig deterministically over historical bars."""

    def __init__(
        self,
        *,
        strategy_config: EmaRsiStrategyConfig,
        inputs: BacktestExecutionInputs,
        session_open_ist: str = "09:15:00",
        session_close_ist: str = "15:30:00",
        deterministic_namespace: uuid.UUID | None = None,
    ) -> None:
        if inputs.execution_delay_bars < 1:
            raise ValueError("execution_delay_bars must be >= 1 to prevent look-ahead bias.")
        self._strategy_cfg = strategy_config
        self._strategy = EmaRsiCrossoverStrategy(strategy_config)
        self._inputs = inputs
        self._session_open_ist = session_open_ist
        self._session_close_ist = session_close_ist
        self._uuid_ns = deterministic_namespace or inputs.strategy_version_id

    def run(self, raw_rows: list[RawCandleRow]) -> BacktestEngineOutput:
        # 1. Pre-Backtest Data Validation
        bars, dq_report = validate_ohlcv_rows(
            raw_rows,
            instrument=self._inputs.instrument,
            timeframe=self._inputs.timeframe,
            start_date=self._inputs.start_date,
            end_date=self._inputs.end_date,
            session_open_ist=self._session_open_ist,
            session_close_ist=self._session_close_ist,
        )

        if not dq_report.passed_validation:
            empty_metrics, _, _, _ = compute_backtest_analytics(
                initial_capital=self._inputs.initial_capital,
                trades=[],
                raw_equity_points=[],
                total_evaluated_bars=0,
                bars_in_position=0,
            )
            return BacktestEngineOutput(
                status=BacktestStatus.FAILED_VALIDATION,
                engine_version=BACKTEST_ENGINE_VERSION,
                data_quality_report=dq_report,
                validated_bars=[],
                trades=[],
                metrics=empty_metrics,
                equity_curve=[],
                drawdown_curve=[],
                monthly_returns=[],
            )

        min_bars_needed = self._strategy.warmup_bars_required + self._inputs.execution_delay_bars
        if len(bars) < min_bars_needed:
            empty_metrics, _, _, _ = compute_backtest_analytics(
                initial_capital=self._inputs.initial_capital,
                trades=[],
                raw_equity_points=[],
                total_evaluated_bars=len(bars),
                bars_in_position=0,
            )
            return BacktestEngineOutput(
                status=BacktestStatus.INSUFFICIENT_DATA,
                engine_version=BACKTEST_ENGINE_VERSION,
                data_quality_report=dq_report,
                validated_bars=bars,
                trades=[],
                metrics=empty_metrics,
                equity_curve=[],
                drawdown_curve=[],
                monthly_returns=[],
            )

        # 2. Bar-by-Bar Zero-Lookahead Simulation Loop
        cash = self._inputs.initial_capital.quantize(_INR_QUANT)
        trades: list[BacktestTradeRecord] = []
        raw_equity_points: list[dict[str, object]] = []
        active_pos: _ActivePosition | None = None
        pending_order: _PendingOrder | None = None
        unfilled_end_signals = 0
        bars_in_position = 0

        current_day: date | None = None
        entries_today = 0
        sq_off_time = parse_hms_time(self._strategy_cfg.mandatory_square_off_time)

        def _close_position(
            pos: _ActivePosition,
            *,
            exit_bar_idx: int,
            exit_time: datetime,
            raw_exit_px: Decimal,
            reason: ExitReason,
        ) -> BacktestTradeRecord:
            nonlocal cash
            exit_order_side = Side.SELL if pos.direction == TradeDirection.LONG else Side.BUY
            exit_fill_px = _apply_adverse_slippage(
                raw_exit_px, exit_order_side, self._inputs.slippage_bps
            )
            exit_slip_cost = abs(raw_exit_px - exit_fill_px) * Decimal(pos.quantity)
            total_slip_cost = (pos.entry_slippage_cost + exit_slip_cost).quantize(_INR_QUANT)

            if pos.direction == TradeDirection.LONG:
                gross = ((exit_fill_px - pos.entry_price) * Decimal(pos.quantity)).quantize(
                    _INR_QUANT
                )
            else:
                gross = ((pos.entry_price - exit_fill_px) * Decimal(pos.quantity)).quantize(
                    _INR_QUANT
                )

            charges = _compute_order_charges(
                pos.entry_price,
                exit_fill_px,
                pos.quantity,
                self._inputs.estimated_charges_bps,
                self._inputs.per_order_fee_inr,
            )
            net = (gross - charges).quantize(_INR_QUANT)
            cash = (cash + net).quantize(_INR_QUANT)

            trade_num = len(trades) + 1
            det_trade_id = uuid.uuid5(
                self._uuid_ns,
                f"trade-{trade_num}-{format_ist_iso(pos.entry_time)}-{format_ist_iso(exit_time)}",
            )
            return BacktestTradeRecord(
                trade_id=det_trade_id,
                trade_number=trade_num,
                strategy_version_id=self._inputs.strategy_version_id,
                strategy_version_label=self._inputs.strategy_version_label,
                instrument=self._inputs.instrument,
                direction=pos.direction,
                entry_signal_time=pos.entry_signal_time,
                entry_time=pos.entry_time,
                raw_entry_price=pos.raw_entry_price,
                entry_price=pos.entry_price,
                quantity=pos.quantity,
                exit_time=exit_time,
                raw_exit_price=raw_exit_px.quantize(_PRICE_QUANT),
                exit_price=exit_fill_px,
                exit_reason=reason,
                gross_pnl=gross,
                charges=charges,
                slippage_cost=total_slip_cost,
                net_pnl=net,
                bars_held=max(1, exit_bar_idx - pos.entry_bar_index),
            )

        for i, bar in enumerate(bars):
            bar_day = bar.bar_timestamp.date()
            if bar_day != current_day:
                current_day = bar_day
                entries_today = 0

            bar_time = bar.bar_timestamp.timetz().replace(tzinfo=None)

            # Step A: Execute pending delayed orders due at `bars[i].open`
            if pending_order is not None and i >= pending_order.due_bar_index:
                order = pending_order
                pending_order = None

                if order.direction in (SignalDirection.ENTRY_LONG, SignalDirection.ENTRY_SHORT):
                    # Do not open a new intraday position at or after mandatory square-off
                    can_enter = active_pos is None and (
                        (not self._strategy_cfg.eod_square_off_enabled) or (bar_time < sq_off_time)
                    )
                    if can_enter and entries_today < self._strategy_cfg.max_trades_per_day:
                        qty = self._compute_order_quantity(
                            current_equity=cash, reference_price=bar.open
                        )
                        if qty > 0:
                            fill_px = _apply_adverse_slippage(
                                bar.open, order.side, self._inputs.slippage_bps
                            )
                            entry_slip = (abs(fill_px - bar.open) * Decimal(qty)).quantize(
                                _INR_QUANT
                            )
                            t_dir = (
                                TradeDirection.LONG
                                if order.direction == SignalDirection.ENTRY_LONG
                                else TradeDirection.SHORT
                            )
                            active_pos = _ActivePosition(
                                direction=t_dir,
                                side=order.side,
                                quantity=qty,
                                entry_signal_time=order.signal_time,
                                entry_time=bar.bar_timestamp,
                                entry_bar_index=i,
                                raw_entry_price=bar.open.quantize(_PRICE_QUANT),
                                entry_price=fill_px,
                                entry_slippage_cost=entry_slip,
                                highest_since_entry=bar.open,
                                lowest_since_entry=bar.open,
                            )
                            entries_today += 1
                elif active_pos is not None and order.direction in (
                    SignalDirection.EXIT_LONG,
                    SignalDirection.EXIT_SHORT,
                ):
                    closed = _close_position(
                        active_pos,
                        exit_bar_idx=i,
                        exit_time=bar.bar_timestamp,
                        raw_exit_px=bar.open,
                        reason=order.exit_reason or ExitReason.OPPOSITE_SIGNAL,
                    )
                    trades.append(closed)
                    active_pos = None

            # Step B: Check Mandatory EOD Square-Off & Protective Stops/Targets on open position
            if active_pos is not None:
                if self._strategy_cfg.eod_square_off_enabled and bar_time >= sq_off_time:
                    closed = _close_position(
                        active_pos,
                        exit_bar_idx=i,
                        exit_time=bar.bar_timestamp,
                        raw_exit_px=bar.open,
                        reason=ExitReason.EOD_SQUARE_OFF,
                    )
                    trades.append(closed)
                    active_pos = None
                    pending_order = None
                else:
                    stop_or_target = self._check_intrabar_protective_exit(active_pos, bar)
                    if stop_or_target is not None:
                        raw_exit_px, exit_reason = stop_or_target
                        closed = _close_position(
                            active_pos,
                            exit_bar_idx=i,
                            exit_time=bar.bar_timestamp,
                            raw_exit_px=raw_exit_px,
                            reason=exit_reason,
                        )
                        trades.append(closed)
                        active_pos = None
                        pending_order = None
                    else:
                        # Update extreme excursion at bar close for future trailing-stop bars
                        active_pos.highest_since_entry = max(
                            active_pos.highest_since_entry, bar.high
                        )
                        active_pos.lowest_since_entry = min(
                            active_pos.lowest_since_entry, bar.low
                        )

            # Step C: Mark-to-Market Equity Snapshot at Close of Bar `i`
            unrealized = Decimal("0")
            if active_pos is not None:
                bars_in_position += 1
                if active_pos.direction == TradeDirection.LONG:
                    unrealized = (bar.close - active_pos.entry_price) * Decimal(
                        active_pos.quantity
                    )
                else:
                    unrealized = (active_pos.entry_price - bar.close) * Decimal(
                        active_pos.quantity
                    )
            raw_equity_points.append(
                {
                    "timestamp": format_ist_iso(bar.bar_timestamp),
                    "cash": cash,
                    "unrealized_pnl": unrealized,
                    "equity": cash + unrealized,
                }
            )

            # Step D: Closed-Bar Strategy Evaluation (STRICT SLICE `bars[: i + 1]` -> NO FUTURE BARS!)
            if pending_order is None:
                pos_snapshot: OpenPositionSnapshot | None = None
                if active_pos is not None:
                    pos_snapshot = OpenPositionSnapshot(
                        symbol=bar.symbol,
                        side=active_pos.side,
                        entry_price=active_pos.entry_price,
                        entry_bar_timestamp=active_pos.entry_time,
                        bars_held=(i - active_pos.entry_bar_index) + 1,
                        highest_price_since_entry=active_pos.highest_since_entry,
                        lowest_price_since_entry=active_pos.lowest_since_entry,
                    )

                visible_history = bars[: i + 1]
                candidate = self._strategy.evaluate_closed_bars(
                    visible_history,
                    open_position=pos_snapshot,
                    trades_taken_today=entries_today,
                )
                if candidate is not None:
                    # Only queue closed-bar exits (OPPOSITE_SIGNAL / MAX_HOLDING_PERIOD) or entries;
                    # price stops/targets/EOD are handled directly in Step B without bar-close delay.
                    is_entry = candidate.direction in (
                        SignalDirection.ENTRY_LONG,
                        SignalDirection.ENTRY_SHORT,
                    )
                    is_delayed_exit = (
                        candidate.direction
                        in (SignalDirection.EXIT_LONG, SignalDirection.EXIT_SHORT)
                        and candidate.exit_reason
                        in (ExitReason.OPPOSITE_SIGNAL, ExitReason.MAX_HOLDING_PERIOD)
                    )
                    if is_entry or is_delayed_exit:
                        due_idx = i + self._inputs.execution_delay_bars
                        if due_idx < len(bars):
                            pending_order = _PendingOrder(
                                signal_time=bar.bar_timestamp,
                                due_bar_index=due_idx,
                                side=candidate.side,
                                direction=candidate.direction,
                                exit_reason=candidate.exit_reason,
                            )
                        else:
                            unfilled_end_signals += 1

        # Step E: Final Backtest Boundary Sweep (if EOD square-off was disabled and position left open)
        if active_pos is not None and bars:
            final_bar = bars[-1]
            closed = _close_position(
                active_pos,
                exit_bar_idx=len(bars) - 1,
                exit_time=final_bar.bar_timestamp,
                raw_exit_px=final_bar.close,
                reason=ExitReason.END_OF_BACKTEST,
            )
            trades.append(closed)
            active_pos = None
            raw_equity_points[-1] = {
                "timestamp": format_ist_iso(final_bar.bar_timestamp),
                "cash": cash,
                "unrealized_pnl": Decimal("0"),
                "equity": cash,
            }

        metrics, equity_curve, drawdown_curve, monthly_returns = compute_backtest_analytics(
            initial_capital=self._inputs.initial_capital,
            trades=trades,
            raw_equity_points=raw_equity_points,
            total_evaluated_bars=len(bars),
            bars_in_position=bars_in_position,
        )

        final_status = (
            BacktestStatus.COMPLETED_WITH_WARNINGS
            if dq_report.has_warnings
            else BacktestStatus.COMPLETED
        )

        return BacktestEngineOutput(
            status=final_status,
            engine_version=BACKTEST_ENGINE_VERSION,
            data_quality_report=dq_report,
            validated_bars=bars,
            trades=trades,
            metrics=metrics,
            equity_curve=equity_curve,
            drawdown_curve=drawdown_curve,
            monthly_returns=monthly_returns,
            unfilled_end_signals=unfilled_end_signals,
        )

    def _distance_delta(self, ref_price: Decimal, configured_dist: Decimal) -> Decimal:
        if self._strategy_cfg.stop_target_unit == ExitTargetUnit.PERCENT:
            return (ref_price * configured_dist) / Decimal("100")
        return configured_dist

    def _compute_order_quantity(self, *, current_equity: Decimal, reference_price: Decimal) -> int:
        mode = self._inputs.position_sizing_mode
        val = self._inputs.position_size_value
        lot = max(1, self._inputs.lot_size)

        if mode == PositionSizingMode.FIXED_LOTS:
            return int(val) * lot
        if mode == PositionSizingMode.FIXED_QUANTITY:
            return int(val)
        if mode == PositionSizingMode.RISK_PER_TRADE_PCT:
            if current_equity <= 0:
                return 0
            risk_budget = current_equity * (val / Decimal("100"))
            sl_delta = self._distance_delta(reference_price, self._strategy_cfg.stop_loss)
            if sl_delta <= 0:
                return 0
            raw_units = (risk_budget / sl_delta).to_integral_value(rounding=ROUND_FLOOR)
            lots = int(raw_units) // lot
            return max(lot, lots * lot) if int(raw_units) > 0 else 0
        return lot

    def _check_intrabar_protective_exit(
        self, pos: _ActivePosition, bar: OHLCVBar
    ) -> tuple[Decimal, ExitReason] | None:
        """Evaluate Stop Loss, Trailing Stop, and Profit Target without optimistic lookahead.

        Rules:
        - Gap-through at `bar.open` fills at `bar.open`.
        - If both Stop Loss and Profit Target fall inside `[bar.low, bar.high]`,
          pessimistic tie-breaking ALWAYS triggers `STOP_LOSS` first.
        - Trailing stop uses `highest_since_entry` / `lowest_since_entry` established
          prior to `bar.high`/`bar.low` (plus `bar.open`) so intrabar high-vs-low
          order is never optimistically assumed.
        """
        is_long = pos.direction == TradeDirection.LONG
        entry_px = pos.entry_price
        sl_dist = self._distance_delta(entry_px, self._strategy_cfg.stop_loss)
        tp_dist = self._distance_delta(entry_px, self._strategy_cfg.profit_target)
        ts_dist = self._distance_delta(entry_px, self._strategy_cfg.trailing_stop)

        stop_px = entry_px - sl_dist if is_long else entry_px + sl_dist
        target_px = entry_px + tp_dist if is_long else entry_px - tp_dist

        # Compute effective trailing stop from prior completed excursion + current open
        trail_px: Decimal | None = None
        if self._strategy_cfg.trailing_stop_enabled:
            if is_long:
                prior_peak = max(pos.highest_since_entry, bar.open)
                if prior_peak > entry_px:
                    trail_px = prior_peak - ts_dist
            else:
                prior_trough = min(pos.lowest_since_entry, bar.open)
                if prior_trough < entry_px:
                    trail_px = prior_trough + ts_dist

        # 1. Check Open Gap first
        if is_long:
            if bar.open <= stop_px:
                return bar.open, ExitReason.STOP_LOSS
            if trail_px is not None and bar.open <= trail_px:
                return bar.open, ExitReason.TRAILING_STOP
            if self._strategy_cfg.profit_target_enabled and bar.open >= target_px:
                return bar.open, ExitReason.PROFIT_TARGET
        else:
            if bar.open >= stop_px:
                return bar.open, ExitReason.STOP_LOSS
            if trail_px is not None and bar.open >= trail_px:
                return bar.open, ExitReason.TRAILING_STOP
            if self._strategy_cfg.profit_target_enabled and bar.open <= target_px:
                return bar.open, ExitReason.PROFIT_TARGET

        # 2. Check Intrabar [low, high] with pessimistic Stop-First priority
        if is_long:
            if bar.low <= stop_px:
                return stop_px, ExitReason.STOP_LOSS
            if trail_px is not None and bar.low <= trail_px:
                return trail_px, ExitReason.TRAILING_STOP
            if self._strategy_cfg.profit_target_enabled and bar.high >= target_px:
                return target_px, ExitReason.PROFIT_TARGET
        else:
            if bar.high >= stop_px:
                return stop_px, ExitReason.STOP_LOSS
            if trail_px is not None and bar.high >= trail_px:
                return trail_px, ExitReason.TRAILING_STOP
            if self._strategy_cfg.profit_target_enabled and bar.low <= target_px:
                return target_px, ExitReason.PROFIT_TARGET

        return None
