"""CSV Historical Market Data Provider for user-uploaded OHLCV files."""

from __future__ import annotations

from datetime import datetime
from typing import Any

from core.backtesting.data_adapter import parse_csv_text_to_raw_rows
from core.clock import ensure_ist, now_ist
from core.market_data.models import (
    DataSourceType,
    HistoricalProviderName,
    get_instrument_provider_spec,
    get_timeframe_spec,
)
from core.market_data.normalization import RawCandleCandidate
from core.market_data.provider import HistoricalDataProvider, HistoricalProviderFetchResult
from core.market_data.validation import validate_and_normalize_candles


class CsvHistoricalDataProvider(HistoricalDataProvider):
    """Normalizes and validates user-uploaded OHLCV CSV files (`IMPORTED_CSV`)."""

    @property
    def provider_name(self) -> str:
        return HistoricalProviderName.CSV.value

    @property
    def source_type(self) -> str:
        return DataSourceType.IMPORTED_CSV.value

    def provider_status(self) -> dict[str, Any]:
        return {
            "provider": self.provider_name,
            "source_type": self.source_type,
            "enabled": True,
            "credentials_configured": True,
            "status": "AVAILABLE",
            "message": "User OHLCV CSV upload & validation ready (persisted to PostgreSQL).",
            "execution_capability": "NONE_HISTORICAL_READ_ONLY",
        }

    def parse_and_validate_csv(
        self,
        *,
        csv_text: str,
        instrument: str,
        timeframe: str,
        session_open_ist: str = "09:15:00",
        session_close_ist: str = "15:30:00",
        reference_now: datetime | None = None,
    ) -> HistoricalProviderFetchResult:
        inst_spec = get_instrument_provider_spec(instrument)
        tf_spec = get_timeframe_spec(timeframe)
        raw_rows = parse_csv_text_to_raw_rows(csv_text)

        candidates = [
            RawCandleCandidate(
                row_index=r.row_index,
                timestamp_raw=r.timestamp_raw,
                open_raw=r.open_raw,
                high_raw=r.high_raw,
                low_raw=r.low_raw,
                close_raw=r.close_raw,
                volume_raw=r.volume_raw,
                is_epoch_timestamp=False,
            )
            for r in raw_rows
        ]
        retr_ts = ensure_ist(reference_now or now_ist())
        candles, quality = validate_and_normalize_candles(
            candidates,
            instrument=inst_spec.internal_symbol,
            timeframe=tf_spec.code,
            source=self.source_type,
            provider_symbol=None,
            retrieval_timestamp=retr_ts,
            session_open_ist=session_open_ist,
            session_close_ist=session_close_ist,
            # Strict CSV validation: any duplicate or out-of-order row in an uploaded file is flagged
            sort_and_deduplicate_exact=False,
            protect_partial_current_candle=False,
            reference_now=retr_ts,
        )
        return HistoricalProviderFetchResult(
            provider=self.provider_name,
            source_type=self.source_type,
            instrument=inst_spec.internal_symbol,
            provider_symbol=None,
            timeframe=tf_spec.code,
            candles=candles,
            quality=quality,
            retrieved_at=retr_ts,
        )
