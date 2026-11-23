"""Deterministic generator for bundled NIFTY & BANKNIFTY 5m IST demonstration CSVs.

Note:
These synthetic historical sample series are strictly for demonstrating backtesting,
data validation, trade ledger attribution, and drawdown analytics. They contain both
trending and whipsaw intraday regimes and do NOT represent live exchange data or imply
strategy profitability.
"""

from __future__ import annotations

import csv
import math
import random
from datetime import date, datetime, time, timedelta
from decimal import Decimal
from pathlib import Path
from zoneinfo import ZoneInfo

IST = ZoneInfo("Asia/Kolkata")
OUT_DIR = Path(__file__).resolve().parent

TRADING_DAYS = [
    date(2026, 10, 28),  # Wed
    date(2026, 10, 29),  # Thu
    date(2026, 10, 30),  # Fri
    date(2026, 11, 2),   # Mon
    date(2026, 11, 3),   # Tue
    date(2026, 11, 4),   # Wed
]


def _generate_series(
    *,
    seed: int,
    base_price: float,
    bar_scale: float,
    base_volume: int,
) -> list[dict[str, str]]:
    rng = random.Random(seed)
    rows: list[dict[str, str]] = []
    price = base_price

    # Regime schedule per day (75 5m bars/day from 09:15 to 15:25 IST)
    # Mixes clean trends with intraday whipsaws so both winning and losing trades occur.
    day_regimes: list[list[tuple[int, float]]] = [
        [(18, -0.35), (28, 0.95), (29, -0.25)],   # Day 1: dip, strong rally, mild pullback
        [(22, 0.40), (20, -0.85), (33, 0.55)],    # Day 2: morning pop, sharp selloff, bounce
        [(15, -0.55), (15, 0.65), (15, -0.60), (30, 0.15)],  # Day 3: choppy whipsaw session
        [(20, 0.25), (35, -1.05), (20, -0.20)],   # Day 4: breakdown trend day
        [(16, 0.65), (18, -0.75), (20, 0.70), (21, -0.50)],  # Day 5: volatile two-way rotation
        [(24, -0.45), (32, 0.90), (19, 0.10)],    # Day 6: morning fade then sustained recovery
    ]

    for day_idx, trading_day in enumerate(TRADING_DAYS):
        regimes = day_regimes[day_idx % len(day_regimes)]
        drifts: list[float] = []
        for length, drift_mult in regimes:
            drifts.extend([drift_mult] * length)

        cur_dt = datetime.combine(trading_day, time(9, 15), tzinfo=IST)
        for bar_idx in range(75):
            drift = drifts[bar_idx] * bar_scale
            wave = math.sin(bar_idx / 4.2) * (bar_scale * 0.45)
            noise = rng.gauss(0.0, bar_scale * 0.65)

            open_px = round(price, 2)
            delta = drift + wave + noise
            close_px = round(max(100.0, open_px + delta), 2)

            wick_up = abs(rng.gauss(bar_scale * 0.55, bar_scale * 0.25))
            wick_dn = abs(rng.gauss(bar_scale * 0.55, bar_scale * 0.25))
            high_px = round(max(open_px, close_px) + wick_up, 2)
            low_px = round(max(50.0, min(open_px, close_px) - wick_dn), 2)

            vol_mult = 1.35 if (bar_idx < 6 or bar_idx > 66) else 1.0
            volume = int(max(1000, rng.gauss(base_volume * vol_mult, base_volume * 0.22)))

            rows.append(
                {
                    "timestamp": cur_dt.isoformat(),
                    "open": f"{Decimal(str(open_px)):.2f}",
                    "high": f"{Decimal(str(high_px)):.2f}",
                    "low": f"{Decimal(str(low_px)):.2f}",
                    "close": f"{Decimal(str(close_px)):.2f}",
                    "volume": str(volume),
                }
            )
            price = close_px
            cur_dt += timedelta(minutes=5)

    return rows


def main() -> None:
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    nifty_rows = _generate_series(seed=20261119, base_price=24250.0, bar_scale=14.5, base_volume=85000)
    banknifty_rows = _generate_series(seed=20261120, base_price=51800.0, bar_scale=36.0, base_volume=62000)

    for filename, rows in (
        ("nifty_5m_sample.csv", nifty_rows),
        ("banknifty_5m_sample.csv", banknifty_rows),
    ):
        target = OUT_DIR / filename
        with target.open("w", encoding="utf-8", newline="\n") as fh:
            writer = csv.DictWriter(
                fh, fieldnames=["timestamp", "open", "high", "low", "close", "volume"]
            )
            writer.writeheader()
            writer.writerows(rows)
        print(f"Generated {target.name}: {len(rows)} 5m IST candles")


if __name__ == "__main__":
    main()
