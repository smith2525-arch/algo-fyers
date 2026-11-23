"""Market-data freshness tracking and stale-feed order-blocking policy.

Freshness Statuses:
- HEALTHY : Active running stream with recent event (`age_seconds <= stale_threshold_seconds`).
- STALE   : Stream event age exceeds `stale_threshold_seconds` (or session paused/stalled).
            Downstream Risk/OMS MUST refuse NEW executable paper orders while STALE.
- STOPPED : No active stream running (`CONFIGURED`, `STOPPED`, `COMPLETED`, or None).
            Downstream Risk/OMS MUST refuse NEW executable paper orders while STOPPED.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal
from typing import Any

from core.clock import ensure_ist, format_ist_iso, now_ist
from core.domain.enums import MarketDataFreshnessStatus, ReplaySessionStatus
from core.exceptions import StaleMarketDataError


@dataclass(frozen=True)
class MarketDataFreshnessSnapshot:
    """Evaluated freshness state of the market-data stream."""

    status: MarketDataFreshnessStatus
    market_data_age_seconds: Decimal | None
    stale_threshold_seconds: int
    last_candle_received_at: datetime | None
    last_exchange_timestamp: datetime | None
    new_orders_permitted: bool
    reason: str

    def to_dict(self) -> dict[str, Any]:
        return {
            "status": self.status.value,
            "market_data_age_seconds": (
                str(self.market_data_age_seconds.quantize(Decimal("0.1")))
                if self.market_data_age_seconds is not None
                else None
            ),
            "stale_threshold_seconds": self.stale_threshold_seconds,
            "last_candle_received_at": (
                format_ist_iso(self.last_candle_received_at)
                if self.last_candle_received_at is not None
                else None
            ),
            "last_exchange_timestamp": (
                format_ist_iso(self.last_exchange_timestamp)
                if self.last_exchange_timestamp is not None
                else None
            ),
            "new_orders_permitted": self.new_orders_permitted,
            "reason": self.reason,
        }


def evaluate_market_data_freshness(
    *,
    session_status: ReplaySessionStatus | str | None,
    last_candle_received_at: datetime | None,
    last_exchange_timestamp: datetime | None = None,
    stale_threshold_seconds: int = 15,
    reference_time: datetime | None = None,
) -> MarketDataFreshnessSnapshot:
    """Compute canonical HEALTHY / STALE / STOPPED freshness telemetry."""
    ref_now = ensure_ist(reference_time) if reference_time is not None else now_ist()
    norm_status: ReplaySessionStatus | None = None
    if isinstance(session_status, ReplaySessionStatus):
        norm_status = session_status
    elif isinstance(session_status, str) and session_status.strip():
        try:
            norm_status = ReplaySessionStatus(session_status.strip().upper())
        except ValueError:
            norm_status = None

    if norm_status in (
        None,
        ReplaySessionStatus.CONFIGURED,
        ReplaySessionStatus.STOPPED,
        ReplaySessionStatus.COMPLETED,
    ):
        age = None
        if last_candle_received_at is not None:
            raw_sec = max(0.0, (ref_now - ensure_ist(last_candle_received_at)).total_seconds())
            age = Decimal(str(round(raw_sec, 2)))
        state_lbl = norm_status.value if norm_status else "INACTIVE"
        return MarketDataFreshnessSnapshot(
            status=MarketDataFreshnessStatus.STOPPED,
            market_data_age_seconds=age,
            stale_threshold_seconds=stale_threshold_seconds,
            last_candle_received_at=ensure_ist(last_candle_received_at)
            if last_candle_received_at
            else None,
            last_exchange_timestamp=ensure_ist(last_exchange_timestamp)
            if last_exchange_timestamp
            else None,
            new_orders_permitted=False,
            reason=f"Market data stream is {state_lbl}; new executable paper orders refused.",
        )

    # Session is RUNNING, PAUSED, or PAUSED_RECOVERY
    if last_candle_received_at is None:
        if norm_status == ReplaySessionStatus.RUNNING:
            return MarketDataFreshnessSnapshot(
                status=MarketDataFreshnessStatus.HEALTHY,
                market_data_age_seconds=Decimal("0.0"),
                stale_threshold_seconds=stale_threshold_seconds,
                last_candle_received_at=None,
                last_exchange_timestamp=None,
                new_orders_permitted=False,
                reason="Market replay starting — awaiting first normalized CandleEvent.",
            )
        return MarketDataFreshnessSnapshot(
            status=MarketDataFreshnessStatus.STALE,
            market_data_age_seconds=None,
            stale_threshold_seconds=stale_threshold_seconds,
            last_candle_received_at=None,
            last_exchange_timestamp=None,
            new_orders_permitted=False,
            reason=f"Replay session {norm_status.value} with no fresh candle; new orders refused.",
        )

    recv_ist = ensure_ist(last_candle_received_at)
    exch_ist = ensure_ist(last_exchange_timestamp) if last_exchange_timestamp else None
    age_float = max(0.0, (ref_now - recv_ist).total_seconds())
    age_dec = Decimal(str(round(age_float, 2)))

    if age_float > float(stale_threshold_seconds):
        return MarketDataFreshnessSnapshot(
            status=MarketDataFreshnessStatus.STALE,
            market_data_age_seconds=age_dec,
            stale_threshold_seconds=stale_threshold_seconds,
            last_candle_received_at=recv_ist,
            last_exchange_timestamp=exch_ist,
            new_orders_permitted=False,
            reason=(
                f"Market data is STALE: age {age_dec:.1f}s exceeds {stale_threshold_seconds}s "
                f"threshold — downstream new executable paper orders refused."
            ),
        )

    if norm_status in (ReplaySessionStatus.PAUSED, ReplaySessionStatus.PAUSED_RECOVERY):
        return MarketDataFreshnessSnapshot(
            status=MarketDataFreshnessStatus.STALE,
            market_data_age_seconds=age_dec,
            stale_threshold_seconds=stale_threshold_seconds,
            last_candle_received_at=recv_ist,
            last_exchange_timestamp=exch_ist,
            new_orders_permitted=False,
            reason=(
                f"Market feed is {norm_status.value} (age {age_dec:.1f}s) — "
                f"marked STALE for execution safety; new executable paper orders refused."
            ),
        )

    return MarketDataFreshnessSnapshot(
        status=MarketDataFreshnessStatus.HEALTHY,
        market_data_age_seconds=age_dec,
        stale_threshold_seconds=stale_threshold_seconds,
        last_candle_received_at=recv_ist,
        last_exchange_timestamp=exch_ist,
        new_orders_permitted=True,
        reason=(
            f"Market stream HEALTHY (age {age_dec:.1f}s <= {stale_threshold_seconds}s threshold)."
        ),
    )


def enforce_fresh_market_data_for_new_order(
    snapshot: MarketDataFreshnessSnapshot,
) -> None:
    """Gate called by downstream Risk/OMS to refuse NEW executable paper orders when STALE/STOPPED."""
    if snapshot.status != MarketDataFreshnessStatus.HEALTHY or not snapshot.new_orders_permitted:
        raise StaleMarketDataError(snapshot.reason)
