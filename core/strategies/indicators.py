"""Deterministic Decimal-precision technical indicators (EMA and Wilder RSI)."""

from __future__ import annotations

from collections.abc import Sequence
from decimal import Decimal, getcontext

# Ensure ample intermediate precision during EMA/RSI smoothing
getcontext().prec = 28


def compute_ema_series(closes: Sequence[Decimal], period: int) -> list[Decimal | None]:
    """Compute Exponential Moving Average series seeded with SMA of the first `period` bars.

    Returns a list of identical length to `closes`, with `None` for warmup indices `< period - 1`.
    """
    if period < 1:
        raise ValueError(f"EMA period must be >= 1, got {period}")

    n = len(closes)
    if n == 0:
        return []
    if n < period:
        return [None] * n

    result: list[Decimal | None] = [None] * n
    p_dec = Decimal(period)
    multiplier = Decimal(2) / Decimal(period + 1)

    sma_seed = sum(closes[:period], Decimal(0)) / p_dec
    result[period - 1] = sma_seed
    prev_ema = sma_seed

    for idx in range(period, n):
        ema = (closes[idx] - prev_ema) * multiplier + prev_ema
        result[idx] = ema
        prev_ema = ema

    return result


def compute_rsi_series(closes: Sequence[Decimal], period: int) -> list[Decimal | None]:
    """Compute Wilder's Relative Strength Index (0..100) over `closes`.

    Returns a list of identical length to `closes`, with `None` for warmup indices `< period`.
    """
    if period < 1:
        raise ValueError(f"RSI period must be >= 1, got {period}")

    n = len(closes)
    if n == 0:
        return []
    if n <= period:
        return [None] * n

    result: list[Decimal | None] = [None] * n
    p_dec = Decimal(period)
    p_minus_1 = Decimal(period - 1)
    hundred = Decimal(100)

    gains = Decimal(0)
    losses = Decimal(0)
    for idx in range(1, period + 1):
        delta = closes[idx] - closes[idx - 1]
        if delta > 0:
            gains += delta
        elif delta < 0:
            losses += -delta

    avg_gain = gains / p_dec
    avg_loss = losses / p_dec
    result[period] = _rsi_from_averages(avg_gain, avg_loss, hundred)

    for idx in range(period + 1, n):
        delta = closes[idx] - closes[idx - 1]
        gain = delta if delta > 0 else Decimal(0)
        loss = -delta if delta < 0 else Decimal(0)

        avg_gain = (avg_gain * p_minus_1 + gain) / p_dec
        avg_loss = (avg_loss * p_minus_1 + loss) / p_dec
        result[idx] = _rsi_from_averages(avg_gain, avg_loss, hundred)

    return result


def _rsi_from_averages(avg_gain: Decimal, avg_loss: Decimal, hundred: Decimal) -> Decimal:
    if avg_loss == 0:
        return Decimal("50") if avg_gain == 0 else hundred
    rs = avg_gain / avg_loss
    return hundred - (hundred / (Decimal(1) + rs))
