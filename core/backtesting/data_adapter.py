"""Pluggable Historical Data Adapter abstraction for deterministic backtesting.

Architecture:
- `HistoricalDataAdapter` defines the clean source-agnostic contract so CSV files,
  PostgreSQL-persisted imports, or future vendor/Parquet feeds are interchangeable.
- `BundledCsvDataAdapter` loads read-only demonstration CSV datasets from `demo_data/`.
- `DatabaseCsvDataAdapter` persists and loads operator-imported CSV datasets in
  PostgreSQL (`historical_datasets` + `historical_candles`) for stateless Cloud Run
  operation.
- `CompositeHistoricalDataAdapter` combines bundled and database-backed adapters.
"""

from __future__ import annotations

import csv
import hashlib
import io
import uuid
from abc import ABC, abstractmethod
from datetime import datetime
from pathlib import Path

from sqlalchemy import select
from sqlalchemy.orm import Session

from core.backtesting.models import (
    DataQualityReport,
    DatasetDescriptor,
    RawCandleDataset,
    RawCandleRow,
)
from core.backtesting.validator import validate_ohlcv_rows
from core.clock import ensure_ist, now_ist
from core.exceptions import ControlCenterError, EntityNotFoundError
from core.persistence.models import HistoricalCandleModel, HistoricalDatasetModel

DEFAULT_DEMO_DATA_DIR = Path(__file__).resolve().parent.parent.parent / "demo_data"


def compute_canonical_csv_sha256(csv_text: str) -> str:
    """Compute cross-platform SHA-256 over LF-normalized UTF-8 CSV content."""
    normalized = csv_text.replace("\r\n", "\n").replace("\r", "\n").strip()
    return hashlib.sha256(normalized.encode("utf-8")).hexdigest()


def parse_csv_text_to_raw_rows(csv_text: str) -> list[RawCandleRow]:
    """Parse OHLCV CSV text into unvalidated `RawCandleRow` items."""
    normalized = csv_text.replace("\r\n", "\n").replace("\r", "\n").strip()
    if not normalized:
        raise ControlCenterError("CSV payload is empty.", code="EMPTY_CSV")

    reader = csv.DictReader(io.StringIO(normalized))
    if not reader.fieldnames:
        raise ControlCenterError("CSV payload is missing a header row.", code="MISSING_CSV_HEADER")

    header_map: dict[str, str] = {}
    for raw_col in reader.fieldnames:
        if raw_col is None:
            continue
        clean = raw_col.strip().lower()
        header_map[clean] = raw_col

    ts_col = (
        header_map.get("timestamp")
        or header_map.get("bar_timestamp")
        or header_map.get("datetime")
        or header_map.get("date")
    )
    open_col = header_map.get("open")
    high_col = header_map.get("high")
    low_col = header_map.get("low")
    close_col = header_map.get("close")
    vol_col = header_map.get("volume")

    missing_cols = [
        name
        for name, col in (
            ("timestamp", ts_col),
            ("open", open_col),
            ("high", high_col),
            ("low", low_col),
            ("close", close_col),
        )
        if col is None
    ]
    if missing_cols:
        raise ControlCenterError(
            f"CSV header missing required OHLCV columns: {', '.join(missing_cols)}",
            code="INVALID_CSV_COLUMNS",
        )

    rows: list[RawCandleRow] = []
    for idx, record in enumerate(reader, start=1):
        rows.append(
            RawCandleRow(
                row_index=idx,
                timestamp_raw=record.get(ts_col),  # type: ignore[arg-type]
                open_raw=record.get(open_col),  # type: ignore[arg-type]
                high_raw=record.get(high_col),  # type: ignore[arg-type]
                low_raw=record.get(low_col),  # type: ignore[arg-type]
                close_raw=record.get(close_col),  # type: ignore[arg-type]
                volume_raw=record.get(vol_col, "0") if vol_col else "0",
            )
        )
    return rows


def _extract_timestamp_bounds(rows: list[RawCandleRow]) -> tuple[datetime | None, datetime | None]:
    first_dt: datetime | None = None
    last_dt: datetime | None = None
    for r in rows:
        if not r.timestamp_raw or not isinstance(r.timestamp_raw, str):
            continue
        txt = r.timestamp_raw.strip()
        if txt.endswith("Z"):
            txt = txt[:-1] + "+00:00"
        try:
            dt = ensure_ist(datetime.fromisoformat(txt))
        except ValueError:
            continue
        if first_dt is None or dt < first_dt:
            first_dt = dt
        if last_dt is None or dt > last_dt:
            last_dt = dt
    return first_dt, last_dt


class HistoricalDataAdapter(ABC):
    """Source-agnostic interface for listing and loading historical OHLCV datasets."""

    @abstractmethod
    def list_datasets(
        self,
        *,
        instrument: str | None = None,
        timeframe: str | None = None,
    ) -> list[DatasetDescriptor]:
        """Return available historical datasets matching optional filters."""

    @abstractmethod
    def load_dataset(self, dataset_id: str) -> RawCandleDataset:
        """Load raw OHLCV rows and version metadata for `dataset_id`."""


class BundledCsvDataAdapter(HistoricalDataAdapter):
    """Loads bundled demonstration OHLCV CSV files from disk (`demo_data/`)."""

    def __init__(self, directory: Path | None = None) -> None:
        self._dir = directory or DEFAULT_DEMO_DATA_DIR

    @staticmethod
    def _infer_meta_from_filename(stem: str) -> tuple[str, str, str]:
        parts = stem.lower().split("_")
        instrument = parts[0].upper() if parts else "NIFTY"
        timeframe = parts[1] if len(parts) >= 2 and parts[1].endswith("m") else "5m"
        dataset_id = f"BUNDLED_{stem.upper()}"
        display_name = f"{instrument} {timeframe} Demonstration Sample ({stem}.csv)"
        return dataset_id, instrument, timeframe if timeframe else "5m"

    def _build_from_path(self, csv_path: Path) -> RawCandleDataset:
        csv_text = csv_path.read_text(encoding="utf-8")
        sha = compute_canonical_csv_sha256(csv_text)
        rows = parse_csv_text_to_raw_rows(csv_text)
        start_ts, end_ts = _extract_timestamp_bounds(rows)
        dataset_id, instrument, timeframe = self._infer_meta_from_filename(csv_path.stem)
        descriptor = DatasetDescriptor(
            dataset_id=dataset_id,
            name=f"{instrument} {timeframe} Historical Sample ({csv_path.name})",
            instrument=instrument,
            timeframe=timeframe,
            source_type="BUNDLED_CSV",
            data_version_hash=sha,
            row_count=len(rows),
            start_timestamp=start_ts,
            end_timestamp=end_ts,
            provider="SYNTHETIC",
            provider_symbol=f"SYNTHETIC:{instrument}-DEMO",
            quality_status="VALID",
            is_complete=True,
            retrieved_at=start_ts,
        )
        return RawCandleDataset(descriptor=descriptor, rows=rows)

    def list_datasets(
        self,
        *,
        instrument: str | None = None,
        timeframe: str | None = None,
    ) -> list[DatasetDescriptor]:
        if not self._dir.exists():
            return []
        out: list[DatasetDescriptor] = []
        for path in sorted(self._dir.glob("*.csv")):
            ds = self._build_from_path(path).descriptor
            if instrument and ds.instrument.upper() != instrument.strip().upper():
                continue
            if timeframe and ds.timeframe != timeframe.strip():
                continue
            out.append(ds)
        return out

    def load_dataset(self, dataset_id: str) -> RawCandleDataset:
        if not self._dir.exists():
            raise EntityNotFoundError(f"Historical dataset '{dataset_id}' not found.")
        for path in sorted(self._dir.glob("*.csv")):
            inferred_id, _, _ = self._infer_meta_from_filename(path.stem)
            if inferred_id == dataset_id or path.name == dataset_id:
                return self._build_from_path(path)
        raise EntityNotFoundError(f"Bundled dataset '{dataset_id}' not found.")


class DatabaseCsvDataAdapter(HistoricalDataAdapter):
    """Persists and loads operator-imported CSV datasets in PostgreSQL."""

    def __init__(self, session: Session) -> None:
        self._session = session

    @staticmethod
    def _to_descriptor(model: HistoricalDatasetModel) -> DatasetDescriptor:
        prov = (model.provider or "").strip() or (
            "FYERS" if model.source_type == "FYERS_HISTORICAL" else "CSV"
        )
        return DatasetDescriptor(
            dataset_id=model.dataset_code,
            name=model.name,
            instrument=model.instrument_symbol,
            timeframe=model.timeframe,
            source_type=model.source_type,
            data_version_hash=model.data_version_hash,
            row_count=model.row_count,
            start_timestamp=ensure_ist(model.start_timestamp) if model.start_timestamp else None,
            end_timestamp=ensure_ist(model.end_timestamp) if model.end_timestamp else None,
            provider=prov,
            provider_symbol=model.provider_symbol,
            quality_status=model.quality_status or "VALID",
            is_complete=bool(model.is_complete),
            retrieved_at=ensure_ist(model.retrieved_at) if model.retrieved_at else None,
        )

    def list_datasets(
        self,
        *,
        instrument: str | None = None,
        timeframe: str | None = None,
    ) -> list[DatasetDescriptor]:
        stmt = select(HistoricalDatasetModel).order_by(HistoricalDatasetModel.created_at.desc())
        if instrument:
            stmt = stmt.where(
                HistoricalDatasetModel.instrument_symbol == instrument.strip().upper()
            )
        if timeframe:
            stmt = stmt.where(HistoricalDatasetModel.timeframe == timeframe.strip())
        models = list(self._session.scalars(stmt).all())
        return [self._to_descriptor(m) for m in models]

    def load_dataset(self, dataset_id: str) -> RawCandleDataset:
        stmt = select(HistoricalDatasetModel).where(
            HistoricalDatasetModel.dataset_code == dataset_id
        )
        model = self._session.scalar(stmt)
        if model is None:
            try:
                as_uuid = uuid.UUID(dataset_id)
                model = self._session.get(HistoricalDatasetModel, as_uuid)
            except ValueError:
                model = None
        if model is None:
            raise EntityNotFoundError(f"Historical dataset '{dataset_id}' not found.")

        if (model.raw_csv_text or "").strip():
            rows = parse_csv_text_to_raw_rows(model.raw_csv_text)
        else:
            from core.clock import format_ist_iso

            candle_rows = list(
                self._session.scalars(
                    select(HistoricalCandleModel)
                    .where(HistoricalCandleModel.dataset_id == model.id)
                    .order_by(HistoricalCandleModel.bar_timestamp.asc())
                ).all()
            )
            rows = [
                RawCandleRow(
                    row_index=idx,
                    timestamp_raw=format_ist_iso(ensure_ist(c.bar_timestamp)),
                    open_raw=str(c.open),
                    high_raw=str(c.high),
                    low_raw=str(c.low),
                    close_raw=str(c.close),
                    volume_raw=str(c.volume),
                )
                for idx, c in enumerate(candle_rows, start=1)
            ]
        return RawCandleDataset(descriptor=self._to_descriptor(model), rows=rows)

    def import_csv(
        self,
        *,
        name: str,
        instrument: str,
        timeframe: str,
        csv_text: str,
        uploaded_by: str = "desk.operator",
        session_open_ist: str = "09:15:00",
        session_close_ist: str = "15:30:00",
    ) -> tuple[DatasetDescriptor, DataQualityReport]:
        clean_symbol = instrument.strip().upper()
        clean_tf = timeframe.strip()
        sha = compute_canonical_csv_sha256(csv_text)
        rows = parse_csv_text_to_raw_rows(csv_text)
        valid_bars, dq_report = validate_ohlcv_rows(
            rows,
            instrument=clean_symbol,
            timeframe=clean_tf,
            session_open_ist=session_open_ist,
            session_close_ist=session_close_ist,
        )
        start_ts, end_ts = _extract_timestamp_bounds(rows)
        stamp = now_ist().strftime("%Y%m%d%H%M%S")
        short_id = uuid.uuid4().hex[:6].upper()
        dataset_code = f"IMPORTED_{clean_symbol}_{clean_tf.upper()}_{stamp}_{short_id}"

        qual_status = (
            "INVALID"
            if not dq_report.passed_validation
            else ("VALID_WITH_WARNINGS" if dq_report.has_warnings else "VALID")
        )
        ts_now = now_ist()
        ds_model = HistoricalDatasetModel(
            dataset_code=dataset_code,
            name=name.strip() or f"{clean_symbol} {clean_tf} Imported CSV ({short_id})",
            instrument_symbol=clean_symbol,
            provider="CSV",
            provider_symbol=None,
            timeframe=clean_tf,
            source_type="IMPORTED_CSV",
            quality_status=qual_status,
            is_complete=dq_report.passed_validation,
            data_version_hash=sha,
            row_count=len(rows),
            requested_from=start_ts,
            requested_to=end_ts,
            start_timestamp=start_ts,
            end_timestamp=end_ts,
            retrieved_at=ts_now,
            raw_csv_text=csv_text.replace("\r\n", "\n").replace("\r", "\n").strip(),
            data_quality_report_json=dq_report.to_dict(),
            metadata_json={
                "missing_candle_count": dq_report.missing_candle_count,
                "duplicate_candle_count": dq_report.duplicate_timestamp_count,
            },
            uploaded_by=uploaded_by.strip() or "desk.operator",
            created_at=ts_now,
        )
        self._session.add(ds_model)
        self._session.flush()

        # Persist normalized candles when dataset passes structural validation
        if dq_report.passed_validation and valid_bars:
            candle_models = [
                HistoricalCandleModel(
                    dataset_id=ds_model.id,
                    instrument_symbol=clean_symbol,
                    timeframe=clean_tf,
                    bar_timestamp=b.bar_timestamp,
                    open=b.open,
                    high=b.high,
                    low=b.low,
                    close=b.close,
                    volume=b.volume,
                )
                for b in valid_bars
            ]
            self._session.add_all(candle_models)
            self._session.flush()

        return self._to_descriptor(ds_model), dq_report


class CompositeHistoricalDataAdapter(HistoricalDataAdapter):
    """Unified adapter combining bundled CSV datasets and PostgreSQL-imported datasets."""

    def __init__(
        self,
        session: Session,
        bundled_dir: Path | None = None,
    ) -> None:
        self._bundled = BundledCsvDataAdapter(directory=bundled_dir)
        self._db = DatabaseCsvDataAdapter(session)

    @property
    def db_adapter(self) -> DatabaseCsvDataAdapter:
        return self._db

    def list_datasets(
        self,
        *,
        instrument: str | None = None,
        timeframe: str | None = None,
    ) -> list[DatasetDescriptor]:
        bundled = self._bundled.list_datasets(instrument=instrument, timeframe=timeframe)
        imported = self._db.list_datasets(instrument=instrument, timeframe=timeframe)
        return bundled + imported

    def load_dataset(self, dataset_id: str) -> RawCandleDataset:
        if dataset_id.startswith("BUNDLED_"):
            return self._bundled.load_dataset(dataset_id)
        try:
            return self._db.load_dataset(dataset_id)
        except EntityNotFoundError:
            return self._bundled.load_dataset(dataset_id)
