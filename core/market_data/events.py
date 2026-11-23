"""Normalized Market-Event abstraction decoupling feeds from the Strategy Engine.

Conceptual Pipeline:
    Historical Replay  |  FYERS WebSocket  |  Other Broker Feed
                              ↓
             Normalized MarketEvent / CandleEvent
                              ↓
                     Same Strategy Engine
"""

from __future__ import annotations

import uuid
from abc import ABC, abstractmethod
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal
from typing import Any

from core.clock import ensure_ist, format_ist_iso, now_ist
from core.domain.enums import MarketEventType, MarketFeedSource
from core.strategies.base import OHLCVBar


@dataclass(frozen=True)
class MarketEvent:
    """Normalized market event envelope independent of transport or vendor."""

    event_id: uuid.UUID
    event_type: MarketEventType
    source: MarketFeedSource
    symbol: str
    exchange_timestamp: datetime
    received_at: datetime
    sequence_number: int

    def __post_init__(self) -> None:
        object.__setattr__(self, "symbol", self.symbol.strip().upper())
        object.__setattr__(self, "exchange_timestamp", ensure_ist(self.exchange_timestamp))
        object.__setattr__(self, "received_at", ensure_ist(self.received_at))


@dataclass(frozen=True)
class CandleEvent(MarketEvent):
    """Normalized closed/intraday OHLCV candle event consumed by Strategy Engines."""

    timeframe: str = "5m"
    open: Decimal = Decimal("0")
    high: Decimal = Decimal("0")
    low: Decimal = Decimal("0")
    close: Decimal = Decimal("0")
    volume: int = 0
    is_closed: bool = True
    data_source_type: str = "BUNDLED_CSV"
    data_provider: str = "SYNTHETIC"
    dataset_id: str | None = None

    def to_ohlcv_bar(self) -> OHLCVBar:
        """Convert normalized CandleEvent into the strategy OHLCVBar value object."""
        return OHLCVBar(
            symbol=self.symbol,
            timeframe=self.timeframe,
            bar_timestamp=self.exchange_timestamp,
            open=self.open,
            high=self.high,
            low=self.low,
            close=self.close,
            volume=self.volume,
        )

    @classmethod
    def from_ohlcv_bar(
        cls,
        bar: OHLCVBar,
        *,
        source: MarketFeedSource = MarketFeedSource.HISTORICAL_REPLAY,
        sequence_number: int = 1,
        received_at: datetime | None = None,
        event_id: uuid.UUID | None = None,
        data_source_type: str = "BUNDLED_CSV",
        data_provider: str = "SYNTHETIC",
        dataset_id: str | None = None,
    ) -> CandleEvent:
        """Construct a normalized CandleEvent from an OHLCVBar."""
        recv = ensure_ist(received_at) if received_at is not None else now_ist()
        eid = event_id or uuid.uuid5(
            uuid.NAMESPACE_OID,
            f"{source.value}:{bar.symbol}:{bar.timeframe}:{format_ist_iso(bar.bar_timestamp)}:{sequence_number}",
        )
        return cls(
            event_id=eid,
            event_type=MarketEventType.CANDLE,
            source=source,
            symbol=bar.symbol,
            exchange_timestamp=bar.bar_timestamp,
            received_at=recv,
            sequence_number=sequence_number,
            timeframe=bar.timeframe,
            open=bar.open,
            high=bar.high,
            low=bar.low,
            close=bar.close,
            volume=bar.volume,
            is_closed=True,
            data_source_type=data_source_type,
            data_provider=data_provider,
            dataset_id=dataset_id,
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "event_id": str(self.event_id),
            "event_type": self.event_type.value,
            "source": self.source.value,
            "data_source_type": self.data_source_type,
            "data_provider": self.data_provider,
            "dataset_id": self.dataset_id,
            "symbol": self.symbol,
            "timeframe": self.timeframe,
            "exchange_timestamp": format_ist_iso(self.exchange_timestamp),
            "received_at": format_ist_iso(self.received_at),
            "sequence_number": self.sequence_number,
            "open": str(self.open.quantize(Decimal("0.01"))),
            "high": str(self.high.quantize(Decimal("0.01"))),
            "low": str(self.low.quantize(Decimal("0.01"))),
            "close": str(self.close.quantize(Decimal("0.01"))),
            "volume": self.volume,
            "is_closed": self.is_closed,
        }


class MarketEventFeed(ABC):
    """Source-agnostic market stream interface (Replay, FYERS WebSocket, or Broker Feed)."""

    @property
    @abstractmethod
    def source(self) -> MarketFeedSource:
        """Originator source identifier."""

    @property
    @abstractmethod
    def symbol(self) -> str:
        """Instrument symbol served by this stream."""

    @property
    @abstractmethod
    def timeframe(self) -> str:
        """Bar timeframe served by this stream."""

    @abstractmethod
    def has_next(self) -> bool:
        """Return True when further market events can be emitted."""

    @abstractmethod
    def next_candle_event(self, *, received_at: datetime | None = None) -> CandleEvent | None:
        """Emit the next normalized CandleEvent or None if exhausted."""

    @abstractmethod
    def reset(self) -> None:
        """Reset stream cursor to initial state."""


class HistoricalCandleReplayFeed(MarketEventFeed):
    """Emits historical OHLCV bars sequentially as normalized `CandleEvent` objects."""

    def __init__(
        self,
        bars: Sequence[OHLCVBar],
        *,
        symbol: str,
        timeframe: str = "5m",
        initial_cursor: int = 0,
        data_source_type: str = "BUNDLED_CSV",
        data_provider: str = "SYNTHETIC",
        dataset_id: str | None = None,
    ) -> None:
        self._bars = list(bars)
        self._symbol = symbol.strip().upper()
        self._timeframe = timeframe.strip()
        self._cursor = max(0, min(initial_cursor, len(self._bars)))
        self._data_source_type = data_source_type
        self._data_provider = data_provider
        self._dataset_id = dataset_id

    @property
    def source(self) -> MarketFeedSource:
        return MarketFeedSource.HISTORICAL_REPLAY

    @property
    def symbol(self) -> str:
        return self._symbol

    @property
    def timeframe(self) -> str:
        return self._timeframe

    @property
    def cursor(self) -> int:
        return self._cursor

    @property
    def total_candles(self) -> int:
        return len(self._bars)

    @property
    def remaining_candles(self) -> int:
        return max(0, len(self._bars) - self._cursor)

    def has_next(self) -> bool:
        return self._cursor < len(self._bars)

    def emitted_events_history(self, *, received_at: datetime | None = None) -> list[CandleEvent]:
        """Return normalized CandleEvents up to current cursor for stateless reconstruction."""
        recv = received_at or now_ist()
        return [
            CandleEvent.from_ohlcv_bar(
                bar,
                source=MarketFeedSource.HISTORICAL_REPLAY,
                sequence_number=idx + 1,
                received_at=recv,
                data_source_type=self._data_source_type,
                data_provider=self._data_provider,
                dataset_id=self._dataset_id,
            )
            for idx, bar in enumerate(self._bars[: self._cursor])
        ]

    def next_candle_event(self, *, received_at: datetime | None = None) -> CandleEvent | None:
        if not self.has_next():
            return None
        bar = self._bars[self._cursor]
        self._cursor += 1
        return CandleEvent.from_ohlcv_bar(
            bar,
            source=MarketFeedSource.HISTORICAL_REPLAY,
            sequence_number=self._cursor,
            received_at=received_at or now_ist(),
            data_source_type=self._data_source_type,
            data_provider=self._data_provider,
            dataset_id=self._dataset_id,
        )

    def reset(self) -> None:
        self._cursor = 0
