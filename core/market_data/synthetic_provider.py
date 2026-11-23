"""Synthetic Demonstration Historical Data Provider (`BUNDLED_CSV`).

Preserves the deterministic bundled NIFTY and BANKNIFTY demonstration datasets
as an always-available fallback when FYERS credentials are absent or markets are closed.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from core.backtesting.data_adapter import BundledCsvDataAdapter
from core.clock import now_ist
from core.market_data.models import (
    DataQualityStatus,
    DataSourceType,
    HistoricalDatasetMetadata,
    HistoricalProviderName,
    NormalizedCandle,
)
from core.market_data.normalization import RawCandleCandidate
from core.market_data.provider import HistoricalDataProvider, HistoricalProviderFetchResult
from core.market_data.validation import validate_and_normalize_candles


class SyntheticHistoricalDataProvider(HistoricalDataProvider):
    """Provides deterministic synthetic demo datasets from `demo_data/*.csv`."""

    def __init__(self, directory: Path | None = None) -> None:
        self._adapter = BundledCsvDataAdapter(directory=directory)

    @property
    def provider_name(self) -> str:
        return HistoricalProviderName.SYNTHETIC.value

    @property
    def source_type(self) -> str:
        return DataSourceType.BUNDLED_CSV.value

    def provider_status(self) -> dict[str, Any]:
        datasets = self._adapter.list_datasets()
        return {
            "provider": self.provider_name,
            "source_type": self.source_type,
            "enabled": True,
            "credentials_configured": True,
            "status": "AVAILABLE",
            "dataset_count": len(datasets),
            "message": "Deterministic synthetic demonstration datasets available offline.",
            "execution_capability": "NONE_HISTORICAL_READ_ONLY",
        }

    def list_metadata(
        self,
        *,
        instrument: str | None = None,
        timeframe: str | None = None,
        referenced_backtests: dict[str, int] | None = None,
        referenced_replays: dict[str, int] | None = None,
    ) -> list[HistoricalDatasetMetadata]:
        descriptors = self._adapter.list_datasets(instrument=instrument, timeframe=timeframe)
        bt_map = referenced_backtests or {}
        rpl_map = referenced_replays or {}
        out: list[HistoricalDatasetMetadata] = []
        for d in descriptors:
            out.append(
                HistoricalDatasetMetadata(
                    dataset_id=d.dataset_id,
                    display_name=d.name,
                    instrument=d.instrument,
                    timeframe=d.timeframe,
                    source_type=DataSourceType.BUNDLED_CSV.value,
                    provider=HistoricalProviderName.SYNTHETIC.value,
                    provider_symbol=f"SYNTHETIC:{d.instrument}-DEMO",
                    quality_status=DataQualityStatus.VALID.value,
                    is_complete=True,
                    data_version_hash=d.data_version_hash,
                    candle_count=d.row_count,
                    requested_from=d.start_timestamp,
                    requested_to=d.end_timestamp,
                    actual_from=d.start_timestamp,
                    actual_to=d.end_timestamp,
                    retrieved_at=d.start_timestamp,
                    created_at=d.start_timestamp,
                    created_by="system.synthetic",
                    missing_candle_count=0,
                    duplicate_candle_count=0,
                    zero_volume_warning=False,
                    referenced_backtests_count=bt_map.get(d.dataset_id, 0),
                    referenced_replays_count=rpl_map.get(d.dataset_id, 0),
                    can_delete=False,
                    delete_block_reason="Bundled synthetic demonstration dataset is read-only.",
                    metadata={"deterministic_demo": True},
                )
            )
        return out

    def load_normalized(self, dataset_id: str) -> tuple[HistoricalDatasetMetadata, HistoricalProviderFetchResult]:
        raw_ds = self._adapter.load_dataset(dataset_id)
        d = raw_ds.descriptor
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
            for r in raw_ds.rows
        ]
        retr_ts = d.end_timestamp or now_ist()
        candles, quality = validate_and_normalize_candles(
            candidates,
            instrument=d.instrument,
            timeframe=d.timeframe,
            source=self.source_type,
            provider_symbol=f"SYNTHETIC:{d.instrument}-DEMO",
            retrieval_timestamp=retr_ts,
            dataset_id=d.dataset_id,
            sort_and_deduplicate_exact=False,
            protect_partial_current_candle=False,
        )
        meta = HistoricalDatasetMetadata(
            dataset_id=d.dataset_id,
            display_name=d.name,
            instrument=d.instrument,
            timeframe=d.timeframe,
            source_type=DataSourceType.BUNDLED_CSV.value,
            provider=HistoricalProviderName.SYNTHETIC.value,
            provider_symbol=f"SYNTHETIC:{d.instrument}-DEMO",
            quality_status=quality.quality_status.value,
            is_complete=quality.is_complete,
            data_version_hash=d.data_version_hash,
            candle_count=len(candles),
            requested_from=d.start_timestamp,
            requested_to=d.end_timestamp,
            actual_from=d.start_timestamp,
            actual_to=d.end_timestamp,
            retrieved_at=d.start_timestamp,
            created_at=d.start_timestamp,
            created_by="system.synthetic",
            can_delete=False,
            delete_block_reason="Bundled synthetic demonstration dataset is read-only.",
            metadata={"deterministic_demo": True},
        )
        result = HistoricalProviderFetchResult(
            provider=self.provider_name,
            source_type=self.source_type,
            instrument=d.instrument,
            provider_symbol=f"SYNTHETIC:{d.instrument}-DEMO",
            timeframe=d.timeframe,
            candles=list(candles),
            quality=quality,
            retrieved_at=retr_ts,
        )
        return meta, result
