"""Historical Market Data Dataset Service.

Responsibilities:
- Orchestrate historical downloads via `FyersHistoricalDataProvider` with sanitized error
  mapping, chunk telemetry, deduplication, immutable dataset versioning, and audit logging.
- Orchestrate user CSV uploads via `CsvHistoricalDataProvider`.
- Expose unified dataset catalog, metadata, 21-point quality reports, and candle loading
  across `FYERS_HISTORICAL`, `IMPORTED_CSV`, and `BUNDLED_CSV` (synthetic demo).
- Export normalized datasets to deterministic, credential-free UTF-8 CSV files.
- Enforce referential retention: block deleting datasets referenced by BacktestRuns or ReplaySessions.
"""

from __future__ import annotations

import uuid
from datetime import date, datetime
from decimal import Decimal
from pathlib import Path
from typing import Any

import httpx
from sqlalchemy import desc, func, select
from sqlalchemy.orm import Session

from core.audit.service import AuditService
from core.backtesting.data_adapter import (
    compute_canonical_csv_sha256,
    parse_csv_text_to_raw_rows,
)
from core.clock import ensure_ist, format_ist_iso, now_ist
from core.config import AppSettings, get_settings
from core.domain.enums import (
    ActorType,
    AuditCategory,
    AuditEventType,
    AuditSeverity,
)
from core.exceptions import (
    DatasetInUseError,
    EntityNotFoundError,
    FyersAuthenticationRequiredError,
    HistoricalDataError,
    HistoricalProviderUnavailableError,
    ImmutabilityViolationError,
)
from core.logging import get_correlation_id
from core.market_data.csv_provider import CsvHistoricalDataProvider
from core.market_data.fyers_provider import (
    FYERS_AUTH_RENEWAL_UI_MESSAGE,
    FyersHistoricalDataProvider,
)
from core.market_data.models import (
    INSTRUMENT_PROVIDER_REGISTRY,
    DataQualityStatus,
    DataSourceType,
    HistoricalDatasetMetadata,
    HistoricalProviderName,
    NormalizedCandle,
    classify_data_source,
    get_instrument_provider_spec,
    get_timeframe_spec,
    list_supported_timeframes,
)
from core.market_data.normalization import (
    RawCandleCandidate,
    build_deterministic_csv_filename,
    compute_candles_fingerprint,
    ist_day_end,
    ist_day_start,
    render_normalized_csv,
)
from core.market_data.provider import sanitize_provider_error_text
from core.market_data.synthetic_provider import SyntheticHistoricalDataProvider
from core.market_data.validation import validate_and_normalize_candles
from core.persistence.models import (
    BacktestRunModel,
    HistoricalCandleModel,
    HistoricalDatasetModel,
    HistoricalDownloadRunModel,
    InstrumentModel,
    ReplaySessionModel,
)
from core.strategies.base import OHLCVBar


class HistoricalDatasetService:
    """Stateless domain service managing real FYERS, CSV, and synthetic historical datasets."""

    @staticmethod
    def list_providers(
        settings: AppSettings | None = None,
        *,
        bundled_dir: Path | None = None,
    ) -> list[dict[str, Any]]:
        cfg = settings or get_settings()
        fyers_prov = FyersHistoricalDataProvider(settings=cfg)
        csv_prov = CsvHistoricalDataProvider()
        syn_prov = SyntheticHistoricalDataProvider(directory=bundled_dir)
        return [
            fyers_prov.provider_status(),
            csv_prov.provider_status(),
            syn_prov.provider_status(),
        ]

    @staticmethod
    def list_supported_instruments_and_timeframes(
        session: Session,
        settings: AppSettings | None = None,
    ) -> dict[str, Any]:
        cfg = settings or get_settings()
        rows = list(
            session.scalars(select(InstrumentModel).order_by(InstrumentModel.symbol.asc())).all()
        )
        by_sym = {r.symbol.upper(): r for r in rows}

        instruments_out: list[dict[str, Any]] = []
        for sym, spec in INSTRUMENT_PROVIDER_REGISTRY.items():
            row = by_sym.get(sym)
            if row is not None:
                prov = (row.data_provider or "").strip() or spec.data_provider
                prov_sym = (row.provider_symbol or "").strip() or spec.provider_symbol
                instruments_out.append(
                    {
                        "instrument_id": str(row.id),
                        "internal_symbol": row.symbol,
                        "display_name": row.display_name,
                        "data_provider": prov,
                        "provider_symbol": prov_sym,
                        "exchange": row.exchange,
                        "instrument_type": row.instrument_type,
                        "session_open_ist": row.session_open_ist,
                        "session_close_ist": row.session_close_ist,
                        "is_active": bool(row.is_active),
                    }
                )
            else:
                instruments_out.append(
                    {
                        "instrument_id": None,
                        "internal_symbol": spec.internal_symbol,
                        "display_name": spec.display_name,
                        "data_provider": spec.data_provider,
                        "provider_symbol": spec.provider_symbol,
                        "exchange": spec.exchange,
                        "instrument_type": spec.instrument_type,
                        "session_open_ist": cfg.DEFAULT_SESSION_OPEN_IST,
                        "session_close_ist": cfg.DEFAULT_SESSION_CLOSE_IST,
                        "is_active": True,
                    }
                )

        return {
            "instruments": instruments_out,
            "timeframes": [tf.to_dict() for tf in list_supported_timeframes()],
            "limits": {
                "max_date_range_days": cfg.FYERS_MAX_DATE_RANGE_DAYS,
                "default_date_range_days": cfg.FYERS_DEFAULT_DATE_RANGE_DAYS,
                "default_timeframe": "5m",
                "default_instrument": "NIFTY",
                "fyers_enabled": bool(cfg.FYERS_HISTORICAL_DATA_ENABLED),
            },
        }

    @staticmethod
    def _reference_counts(session: Session) -> tuple[dict[str, int], dict[str, int]]:
        bt_rows = session.execute(
            select(BacktestRunModel.dataset_id, func.count()).group_by(BacktestRunModel.dataset_id)
        ).all()
        rpl_rows = session.execute(
            select(ReplaySessionModel.dataset_id, func.count()).group_by(
                ReplaySessionModel.dataset_id
            )
        ).all()
        return (
            {str(k): int(v) for k, v in bt_rows if k},
            {str(k): int(v) for k, v in rpl_rows if k},
        )

    @staticmethod
    def _model_to_metadata(
        model: HistoricalDatasetModel,
        *,
        bt_counts: dict[str, int],
        rpl_counts: dict[str, int],
    ) -> HistoricalDatasetMetadata:
        code = model.dataset_code
        uid = str(model.id)
        bt_ref = bt_counts.get(code, 0) + bt_counts.get(uid, 0)
        rpl_ref = rpl_counts.get(code, 0) + rpl_counts.get(uid, 0)

        dq = model.data_quality_report_json or {}
        meta = model.metadata_json or {}
        missing_cnt = int(dq.get("missing_candle_count", meta.get("missing_candle_count", 0)) or 0)
        dup_cnt = int(
            dq.get("duplicate_timestamp_count", meta.get("duplicate_candle_count", 0)) or 0
        )
        zero_vol = bool(
            int(dq.get("zero_volume_candle_count", 0) or 0) > 0
            or meta.get("zero_volume_warning", False)
        )

        if bt_ref > 0 or rpl_ref > 0:
            can_del = False
            block_reason = (
                f"Referenced by {bt_ref} backtest run(s) and {rpl_ref} replay session(s); "
                "protected for audit reproducibility."
            )
        else:
            can_del = True
            block_reason = None

        prov = (model.provider or "").strip() or (
            HistoricalProviderName.FYERS.value
            if model.source_type == DataSourceType.FYERS_HISTORICAL.value
            else HistoricalProviderName.CSV.value
        )

        return HistoricalDatasetMetadata(
            dataset_id=model.dataset_code,
            display_name=model.name,
            instrument=model.instrument_symbol,
            timeframe=model.timeframe,
            source_type=model.source_type,
            provider=prov,
            provider_symbol=model.provider_symbol,
            quality_status=model.quality_status or DataQualityStatus.VALID.value,
            is_complete=bool(model.is_complete),
            data_version_hash=model.data_version_hash,
            candle_count=model.row_count,
            requested_from=ensure_ist(model.requested_from) if model.requested_from else None,
            requested_to=ensure_ist(model.requested_to) if model.requested_to else None,
            actual_from=ensure_ist(model.start_timestamp) if model.start_timestamp else None,
            actual_to=ensure_ist(model.end_timestamp) if model.end_timestamp else None,
            retrieved_at=ensure_ist(model.retrieved_at) if model.retrieved_at else None,
            created_at=ensure_ist(model.created_at) if model.created_at else None,
            created_by=model.uploaded_by,
            missing_candle_count=missing_cnt,
            duplicate_candle_count=dup_cnt,
            zero_volume_warning=zero_vol,
            referenced_backtests_count=bt_ref,
            referenced_replays_count=rpl_ref,
            can_delete=can_del,
            delete_block_reason=block_reason,
            metadata=dict(meta),
        )

    @staticmethod
    def _find_db_dataset(session: Session, dataset_id: str) -> HistoricalDatasetModel | None:
        model = session.scalar(
            select(HistoricalDatasetModel).where(HistoricalDatasetModel.dataset_code == dataset_id)
        )
        if model is None:
            try:
                as_uuid = uuid.UUID(str(dataset_id))
                model = session.get(HistoricalDatasetModel, as_uuid)
            except ValueError:
                model = None
        return model

    @staticmethod
    def list_datasets(
        session: Session,
        *,
        instrument: str | None = None,
        timeframe: str | None = None,
        source_type: str | None = None,
        provider: str | None = None,
        quality_status: str | None = None,
        include_synthetic: bool = True,
        bundled_dir: Path | None = None,
    ) -> list[dict[str, Any]]:
        bt_counts, rpl_counts = HistoricalDatasetService._reference_counts(session)

        stmt = select(HistoricalDatasetModel).order_by(desc(HistoricalDatasetModel.created_at))
        if instrument and instrument.upper() != "ALL":
            stmt = stmt.where(
                HistoricalDatasetModel.instrument_symbol == instrument.strip().upper()
            )
        if timeframe and timeframe.upper() != "ALL":
            tf_spec = get_timeframe_spec(timeframe)
            stmt = stmt.where(HistoricalDatasetModel.timeframe == tf_spec.code)
        if source_type and source_type.upper() != "ALL":
            stmt = stmt.where(HistoricalDatasetModel.source_type == source_type.strip().upper())
        if provider and provider.upper() != "ALL":
            stmt = stmt.where(HistoricalDatasetModel.provider == provider.strip().upper())
        if quality_status and quality_status.upper() != "ALL":
            stmt = stmt.where(
                HistoricalDatasetModel.quality_status == quality_status.strip().upper()
            )

        db_models = list(session.scalars(stmt).all())
        db_items = [
            HistoricalDatasetService._model_to_metadata(
                m, bt_counts=bt_counts, rpl_counts=rpl_counts
            ).to_dict()
            for m in db_models
        ]

        syn_items: list[dict[str, Any]] = []
        if include_synthetic:
            want_syn_src = not source_type or source_type.upper() in {
                "ALL",
                DataSourceType.BUNDLED_CSV.value,
            }
            want_syn_prov = not provider or provider.upper() in {
                "ALL",
                HistoricalProviderName.SYNTHETIC.value,
            }
            want_syn_qual = not quality_status or quality_status.upper() in {
                "ALL",
                DataQualityStatus.VALID.value,
            }
            if want_syn_src and want_syn_prov and want_syn_qual:
                syn_prov = SyntheticHistoricalDataProvider(directory=bundled_dir)
                tf_filter = get_timeframe_spec(timeframe).code if (timeframe and timeframe.upper() != "ALL") else None
                inst_filter = instrument.strip().upper() if (instrument and instrument.upper() != "ALL") else None
                syn_items = [
                    m.to_dict()
                    for m in syn_prov.list_metadata(
                        instrument=inst_filter,
                        timeframe=tf_filter,
                        referenced_backtests=bt_counts,
                        referenced_replays=rpl_counts,
                    )
                ]

        # Keep bundled datasets first for backwards-compatible catalog index[0], or return all
        return syn_items + db_items

    @staticmethod
    def get_dataset_detail(
        session: Session,
        dataset_id: str,
        *,
        bundled_dir: Path | None = None,
    ) -> dict[str, Any]:
        bt_counts, rpl_counts = HistoricalDatasetService._reference_counts(session)
        if dataset_id.startswith("BUNDLED_"):
            syn = SyntheticHistoricalDataProvider(directory=bundled_dir)
            meta, res = syn.load_normalized(dataset_id)
            out = meta.to_dict()
            out["referenced_backtests_count"] = bt_counts.get(dataset_id, 0)
            out["referenced_replays_count"] = rpl_counts.get(dataset_id, 0)
            out["data_quality_report"] = res.quality.to_report_dict()
            return out

        model = HistoricalDatasetService._find_db_dataset(session, dataset_id)
        if model is None:
            # Try synthetic fallback by filename
            syn = SyntheticHistoricalDataProvider(directory=bundled_dir)
            meta, res = syn.load_normalized(dataset_id)
            out = meta.to_dict()
            out["data_quality_report"] = res.quality.to_report_dict()
            return out

        meta_obj = HistoricalDatasetService._model_to_metadata(
            model, bt_counts=bt_counts, rpl_counts=rpl_counts
        )
        out = meta_obj.to_dict()
        out["data_quality_report"] = HistoricalDatasetService.get_dataset_quality(
            session, dataset_id, bundled_dir=bundled_dir
        )
        return out

    @staticmethod
    def get_dataset_quality(
        session: Session,
        dataset_id: str,
        *,
        bundled_dir: Path | None = None,
    ) -> dict[str, Any]:
        if dataset_id.startswith("BUNDLED_"):
            syn = SyntheticHistoricalDataProvider(directory=bundled_dir)
            _, res = syn.load_normalized(dataset_id)
            return res.quality.to_report_dict()

        model = HistoricalDatasetService._find_db_dataset(session, dataset_id)
        if model is None:
            raise EntityNotFoundError(f"Historical dataset '{dataset_id}' not found.")

        if model.data_quality_report_json:
            rep = dict(model.data_quality_report_json)
            rep.setdefault("quality_status", model.quality_status or DataQualityStatus.VALID.value)
            rep.setdefault("is_complete", bool(model.is_complete))
            return rep

        # Legacy imported CSV without cached report: compute on the fly
        candles = HistoricalDatasetService._load_normalized_candles_from_db_model(session, model)
        if candles:
            return {
                "quality_status": model.quality_status or DataQualityStatus.VALID.value,
                "passed_validation": True,
                "has_warnings": False,
                "is_complete": bool(model.is_complete),
                "total_rows_inspected": len(candles),
                "valid_candle_count": len(candles),
                "null_value_count": 0,
                "timezone_issue_count": 0,
                "out_of_order_count": 0,
                "duplicate_timestamp_count": 0,
                "exact_duplicate_removed_count": 0,
                "conflicting_duplicate_count": 0,
                "invalid_ohlc_count": 0,
                "missing_candle_count": 0,
                "unexpected_session_gap_count": 0,
                "zero_volume_candle_count": sum(1 for c in candles if c.volume == 0),
                "partial_current_candle_excluded": False,
                "missing_intervals": [],
                "first_timestamp_ist": format_ist_iso(candles[0].timestamp),
                "last_timestamp_ist": format_ist_iso(candles[-1].timestamp),
                "checked_timezone": "Asia/Kolkata",
                "issues": [],
            }

        raw_rows = parse_csv_text_to_raw_rows(model.raw_csv_text)
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
        _, qual = validate_and_normalize_candles(
            candidates,
            instrument=model.instrument_symbol,
            timeframe=model.timeframe,
            source=model.source_type,
            sort_and_deduplicate_exact=False,
            protect_partial_current_candle=False,
        )
        return qual.to_report_dict()

    @staticmethod
    def _load_normalized_candles_from_db_model(
        session: Session, model: HistoricalDatasetModel
    ) -> list[NormalizedCandle]:
        rows = list(
            session.scalars(
                select(HistoricalCandleModel)
                .where(HistoricalCandleModel.dataset_id == model.id)
                .order_by(HistoricalCandleModel.bar_timestamp.asc())
            ).all()
        )
        retr = ensure_ist(model.retrieved_at or model.created_at)
        return [
            NormalizedCandle(
                timestamp=ensure_ist(r.bar_timestamp),
                open=Decimal(r.open),
                high=Decimal(r.high),
                low=Decimal(r.low),
                close=Decimal(r.close),
                volume=int(r.volume),
                instrument=r.instrument_symbol,
                timeframe=r.timeframe,
                source=model.source_type,
                provider_symbol=model.provider_symbol,
                retrieval_timestamp=retr,
                dataset_id=model.dataset_code,
            )
            for r in rows
        ]

    @staticmethod
    def load_normalized_candles(
        session: Session,
        dataset_id: str,
        *,
        bundled_dir: Path | None = None,
    ) -> tuple[HistoricalDatasetMetadata, list[NormalizedCandle]]:
        bt_counts, rpl_counts = HistoricalDatasetService._reference_counts(session)
        if dataset_id.startswith("BUNDLED_"):
            syn = SyntheticHistoricalDataProvider(directory=bundled_dir)
            meta, res = syn.load_normalized(dataset_id)
            return meta, res.candles

        model = HistoricalDatasetService._find_db_dataset(session, dataset_id)
        if model is None:
            syn = SyntheticHistoricalDataProvider(directory=bundled_dir)
            meta, res = syn.load_normalized(dataset_id)
            return meta, res.candles

        meta = HistoricalDatasetService._model_to_metadata(
            model, bt_counts=bt_counts, rpl_counts=rpl_counts
        )
        candles = HistoricalDatasetService._load_normalized_candles_from_db_model(session, model)
        return meta, candles

    @staticmethod
    def load_bars_for_execution(
        session: Session,
        dataset_id: str,
        *,
        bundled_dir: Path | None = None,
    ) -> tuple[HistoricalDatasetMetadata, list[OHLCVBar]]:
        meta, candles = HistoricalDatasetService.load_normalized_candles(
            session, dataset_id, bundled_dir=bundled_dir
        )
        return meta, [c.to_ohlcv_bar() for c in candles]

    @staticmethod
    def export_dataset_csv(
        session: Session,
        dataset_id: str,
        *,
        actor: str = "desk.operator",
        bundled_dir: Path | None = None,
    ) -> tuple[str, str]:
        """Export normalized dataset to `(deterministic_filename, csv_text)` and record audit event."""
        meta, candles = HistoricalDatasetService.load_normalized_candles(
            session, dataset_id, bundled_dir=bundled_dir
        )
        if not candles:
            raise HistoricalDataError(
                f"Dataset '{dataset_id}' contains 0 validated candles and cannot be exported as CSV.",
                code="EMPTY_DATASET_CSV_EXPORT",
            )

        csv_text = render_normalized_csv(candles)
        filename = build_deterministic_csv_filename(
            instrument=meta.instrument,
            timeframe=meta.timeframe,
            provider=meta.provider,
            start_date=meta.actual_from or candles[0].timestamp,
            end_date=meta.actual_to or candles[-1].timestamp,
        )
        labels = classify_data_source(meta.source_type, meta.provider)
        AuditService.record(
            session,
            action=AuditEventType.HISTORICAL_DATASET_CSV_EXPORTED.value,
            event_type=AuditEventType.HISTORICAL_DATASET_CSV_EXPORTED.value,
            summary=(
                f"Exported normalized CSV '{filename}' ({len(candles)} candles) from dataset '{meta.dataset_id}'."
            ),
            actor=actor,
            actor_type=ActorType.OPERATOR,
            category=AuditCategory.MARKET_DATA,
            severity=AuditSeverity.INFO,
            entity_type="historical_dataset",
            entity_id=meta.dataset_id,
            source="historical_data.service",
            details={
                "dataset_id": meta.dataset_id,
                "filename": filename,
                "candle_count": len(candles),
                "data_provider": labels.audit_provider,
                "data_type": labels.audit_data_type,
                "execution_type": labels.audit_execution_type,
            },
        )
        return filename, csv_text

    @staticmethod
    def delete_dataset(
        session: Session,
        dataset_id: str,
        *,
        actor: str = "desk.operator",
    ) -> dict[str, Any]:
        """Delete an unreferenced persisted dataset; block if synthetic or referenced by history."""
        if dataset_id.startswith("BUNDLED_"):
            raise ImmutabilityViolationError(
                "Bundled synthetic demonstration datasets are read-only and cannot be deleted."
            )

        model = HistoricalDatasetService._find_db_dataset(session, dataset_id)
        if model is None:
            raise EntityNotFoundError(f"Historical dataset '{dataset_id}' not found.")

        bt_counts, rpl_counts = HistoricalDatasetService._reference_counts(session)
        bt_ref = bt_counts.get(model.dataset_code, 0) + bt_counts.get(str(model.id), 0)
        rpl_ref = rpl_counts.get(model.dataset_code, 0) + rpl_counts.get(str(model.id), 0)

        if bt_ref > 0 or rpl_ref > 0:
            raise DatasetInUseError(
                f"Cannot delete dataset '{model.dataset_code}': referenced by {bt_ref} completed "
                f"backtest(s) and {rpl_ref} market replay session(s)."
            )

        deleted_code = model.dataset_code
        labels = classify_data_source(model.source_type, model.provider)
        session.delete(model)
        session.flush()

        AuditService.record(
            session,
            action="HISTORICAL_DATASET_DELETED",
            summary=f"Deleted unreferenced historical dataset '{deleted_code}'.",
            actor=actor,
            actor_type=ActorType.OPERATOR,
            category=AuditCategory.MARKET_DATA,
            severity=AuditSeverity.WARNING,
            entity_type="historical_dataset",
            entity_id=deleted_code,
            source="historical_data.service",
            details={
                "dataset_id": deleted_code,
                "data_provider": labels.audit_provider,
                "data_type": labels.audit_data_type,
                "execution_type": labels.audit_execution_type,
            },
        )
        return {"deleted": True, "dataset_id": deleted_code}

    @staticmethod
    def upload_csv_dataset(
        session: Session,
        *,
        name: str,
        instrument: str,
        timeframe: str,
        csv_text: str,
        actor: str = "desk.operator",
    ) -> dict[str, Any]:
        """Validate and persist a user-uploaded OHLCV CSV dataset (`IMPORTED_CSV`)."""
        inst_spec = get_instrument_provider_spec(instrument)
        tf_spec = get_timeframe_spec(timeframe)
        inst_row = session.scalar(
            select(InstrumentModel).where(InstrumentModel.symbol == inst_spec.internal_symbol)
        )
        open_ist = inst_row.session_open_ist if inst_row else "09:15:00"
        close_ist = inst_row.session_close_ist if inst_row else "15:30:00"

        csv_prov = CsvHistoricalDataProvider()
        fetch_res = csv_prov.parse_and_validate_csv(
            csv_text=csv_text,
            instrument=inst_spec.internal_symbol,
            timeframe=tf_spec.code,
            session_open_ist=open_ist,
            session_close_ist=close_ist,
        )

        normalized_csv = csv_text.replace("\r\n", "\n").replace("\r", "\n").strip()
        sha = (
            compute_candles_fingerprint(fetch_res.candles)
            if fetch_res.candles
            else compute_canonical_csv_sha256(normalized_csv)
        )
        stamp = now_ist().strftime("%Y%m%d%H%M%S")
        short_id = uuid.uuid4().hex[:6].upper()
        dataset_code = f"IMPORTED_{inst_spec.internal_symbol}_{tf_spec.code.upper()}_{stamp}_{short_id}"

        start_ts = fetch_res.candles[0].timestamp if fetch_res.candles else None
        end_ts = fetch_res.candles[-1].timestamp if fetch_res.candles else None
        dq_dict = fetch_res.quality.to_report_dict()

        ds_model = HistoricalDatasetModel(
            dataset_code=dataset_code,
            name=name.strip() or f"{inst_spec.internal_symbol} {tf_spec.code} Uploaded CSV ({short_id})",
            instrument_id=inst_row.id if inst_row else None,
            instrument_symbol=inst_spec.internal_symbol,
            provider=HistoricalProviderName.CSV.value,
            provider_symbol=None,
            timeframe=tf_spec.code,
            source_type=DataSourceType.IMPORTED_CSV.value,
            quality_status=fetch_res.quality.quality_status.value,
            is_complete=fetch_res.quality.is_complete,
            data_version_hash=sha,
            row_count=len(fetch_res.candles) if fetch_res.quality.passed_validation else fetch_res.quality.total_rows_inspected,
            requested_from=start_ts,
            requested_to=end_ts,
            start_timestamp=start_ts,
            end_timestamp=end_ts,
            retrieved_at=fetch_res.retrieved_at,
            raw_csv_text=normalized_csv,
            data_quality_report_json=dq_dict,
            metadata_json={
                "missing_candle_count": fetch_res.quality.missing_candle_count,
                "duplicate_candle_count": fetch_res.quality.duplicate_timestamp_count,
                "zero_volume_warning": fetch_res.quality.zero_volume_candle_count > 0,
            },
            uploaded_by=actor.strip() or "desk.operator",
            created_at=now_ist(),
        )
        session.add(ds_model)
        session.flush()

        if fetch_res.quality.passed_validation and fetch_res.candles:
            session.add_all(
                [
                    HistoricalCandleModel(
                        dataset_id=ds_model.id,
                        instrument_symbol=inst_spec.internal_symbol,
                        timeframe=tf_spec.code,
                        bar_timestamp=c.timestamp,
                        open=c.open,
                        high=c.high,
                        low=c.low,
                        close=c.close,
                        volume=c.volume,
                    )
                    for c in fetch_res.candles
                ]
            )
            session.flush()

        labels = classify_data_source(ds_model.source_type, ds_model.provider)
        sev = (
            AuditSeverity.WARNING
            if (not fetch_res.quality.passed_validation or fetch_res.quality.has_warnings)
            else AuditSeverity.INFO
        )
        AuditService.record(
            session,
            action=AuditEventType.HISTORICAL_DATASET_VALIDATED.value,
            event_type=AuditEventType.HISTORICAL_DATASET_VALIDATED.value,
            summary=(
                f"Uploaded & validated CSV dataset '{dataset_code}' "
                f"(status={fetch_res.quality.quality_status.value}, candles={len(fetch_res.candles)})."
            ),
            actor=actor,
            actor_type=ActorType.OPERATOR,
            category=AuditCategory.MARKET_DATA,
            severity=sev,
            entity_type="historical_dataset",
            entity_id=dataset_code,
            source="historical_data.service",
            details={
                "dataset_id": dataset_code,
                "quality_status": fetch_res.quality.quality_status.value,
                "candle_count": len(fetch_res.candles),
                "data_version_hash": sha,
                "data_provider": labels.audit_provider,
                "data_type": labels.audit_data_type,
                "execution_type": labels.audit_execution_type,
            },
        )

        bt_counts, rpl_counts = HistoricalDatasetService._reference_counts(session)
        meta_out = HistoricalDatasetService._model_to_metadata(
            ds_model, bt_counts=bt_counts, rpl_counts=rpl_counts
        ).to_dict()
        return {
            "dataset": meta_out,
            "data_quality_report": dq_dict,
        }

    @staticmethod
    def _serialize_download_run(run: HistoricalDownloadRunModel) -> dict[str, Any]:
        return {
            "id": str(run.id),
            "run_code": run.run_code,
            "provider": run.provider,
            "instrument_symbol": run.instrument_symbol,
            "provider_symbol": run.provider_symbol,
            "timeframe": run.timeframe,
            "fyers_resolution": run.fyers_resolution,
            "requested_from": format_ist_iso(run.requested_from),
            "requested_to": format_ist_iso(run.requested_to),
            "status": run.status,
            "quality_status": run.quality_status,
            "dataset_id": run.dataset_code,
            "dataset_uuid": str(run.dataset_id) if run.dataset_id else None,
            "chunks_total": run.chunks_total,
            "chunks_succeeded": run.chunks_succeeded,
            "chunks_failed": run.chunks_failed,
            "candles_fetched": run.candles_fetched,
            "candles_stored": run.candles_stored,
            "error_code": run.error_code,
            "error_message": run.error_message,
            "details": dict(run.details_json or {}),
            "actor": run.actor,
            "correlation_id": run.correlation_id,
            "started_at": format_ist_iso(run.started_at),
            "completed_at": format_ist_iso(run.completed_at) if run.completed_at else None,
        }

    @staticmethod
    def get_download_run(session: Session, download_id: str) -> dict[str, Any]:
        run = session.scalar(
            select(HistoricalDownloadRunModel).where(
                HistoricalDownloadRunModel.run_code == download_id
            )
        )
        if run is None:
            try:
                as_uuid = uuid.UUID(str(download_id))
                run = session.get(HistoricalDownloadRunModel, as_uuid)
            except ValueError:
                run = None
        if run is None:
            raise EntityNotFoundError(f"Historical download run '{download_id}' not found.")
        return HistoricalDatasetService._serialize_download_run(run)

    @staticmethod
    def download_fyers_dataset(
        session: Session,
        *,
        instrument: str,
        timeframe: str,
        from_date: date,
        to_date: date,
        display_name: str | None = None,
        force_refresh: bool = False,
        allow_partial_current_candle: bool = False,
        actor: str = "desk.operator",
        settings: AppSettings | None = None,
        http_transport: httpx.BaseTransport | None = None,
        reference_now: datetime | None = None,
    ) -> dict[str, Any]:
        """Execute bounded FYERS historical download, persisting run & audit trail safely.

        Returns a structured outcome dictionary (`ok`, `http_status`, `error_code`,
        `message`, `download_run`, `dataset`, `data_quality_report`) so the caller can
        commit audit/telemetry rows before returning appropriate HTTP status codes.
        """
        cfg = settings or get_settings()
        inst_spec = get_instrument_provider_spec(instrument)
        tf_spec = get_timeframe_spec(timeframe)

        inst_row = session.scalar(
            select(InstrumentModel).where(InstrumentModel.symbol == inst_spec.internal_symbol)
        )
        prov_symbol = (
            (inst_row.provider_symbol or "").strip()
            if inst_row is not None and (inst_row.provider_symbol or "").strip()
            else inst_spec.provider_symbol
        )
        open_ist = inst_row.session_open_ist if inst_row else cfg.DEFAULT_SESSION_OPEN_IST
        close_ist = inst_row.session_close_ist if inst_row else cfg.DEFAULT_SESSION_CLOSE_IST

        ts_start = now_ist()
        req_from_dt = ist_day_start(from_date)
        req_to_dt = ist_day_end(to_date)
        run_code = (
            f"HDL-{inst_spec.internal_symbol}-{tf_spec.csv_filename_tag}-"
            f"{ts_start.strftime('%Y%m%d%H%M%S')}-{uuid.uuid4().hex[:5].upper()}"
        )
        audit_base = {
            "run_code": run_code,
            "instrument": inst_spec.internal_symbol,
            "provider_symbol": prov_symbol,
            "timeframe": tf_spec.code,
            "requested_from": from_date.isoformat(),
            "requested_to": to_date.isoformat(),
            "data_provider": "FYERS",
            "data_type": "REAL HISTORICAL",
            "execution_type": "SIMULATED PAPER",
        }

        run_row = HistoricalDownloadRunModel(
            run_code=run_code,
            provider=HistoricalProviderName.FYERS.value,
            instrument_symbol=inst_spec.internal_symbol,
            provider_symbol=prov_symbol,
            timeframe=tf_spec.code,
            fyers_resolution=tf_spec.fyers_resolution,
            requested_from=req_from_dt,
            requested_to=req_to_dt,
            status="REQUESTED",
            quality_status=DataQualityStatus.VALID.value,
            actor=actor,
            correlation_id=get_correlation_id(),
            started_at=ts_start,
            details_json={},
        )
        session.add(run_row)
        session.flush()

        AuditService.record(
            session,
            action=AuditEventType.HISTORICAL_DOWNLOAD_REQUESTED.value,
            event_type=AuditEventType.HISTORICAL_DOWNLOAD_REQUESTED.value,
            summary=(
                f"Requested FYERS historical download {run_code} for {inst_spec.internal_symbol} "
                f"({prov_symbol}, {tf_spec.display_name}, {from_date}..{to_date})."
            ),
            actor=actor,
            actor_type=ActorType.OPERATOR,
            category=AuditCategory.MARKET_DATA,
            severity=AuditSeverity.INFO,
            entity_type="historical_download_run",
            entity_id=run_code,
            source="fyers.historical_provider",
            details=audit_base,
        )

        # Deduplication guard: reuse existing complete validated dataset for identical range unless force_refresh
        if not force_refresh:
            existing_same_req = session.scalar(
                select(HistoricalDatasetModel)
                .where(
                    HistoricalDatasetModel.instrument_symbol == inst_spec.internal_symbol,
                    HistoricalDatasetModel.timeframe == tf_spec.code,
                    HistoricalDatasetModel.source_type == DataSourceType.FYERS_HISTORICAL.value,
                    HistoricalDatasetModel.requested_from == req_from_dt,
                    HistoricalDatasetModel.requested_to == req_to_dt,
                    HistoricalDatasetModel.is_complete.is_(True),
                    HistoricalDatasetModel.quality_status.in_(
                        [DataQualityStatus.VALID.value, DataQualityStatus.VALID_WITH_WARNINGS.value]
                    ),
                )
                .order_by(desc(HistoricalDatasetModel.created_at))
                .limit(1)
            )
            if existing_same_req is not None:
                run_row.status = "DEDUPLICATED"
                run_row.quality_status = existing_same_req.quality_status
                run_row.dataset_id = existing_same_req.id
                run_row.dataset_code = existing_same_req.dataset_code
                run_row.candles_fetched = 0
                run_row.candles_stored = existing_same_req.row_count
                run_row.completed_at = now_ist()
                run_row.details_json = {"deduplicated_existing_dataset": existing_same_req.dataset_code}
                session.flush()

                bt_counts, rpl_counts = HistoricalDatasetService._reference_counts(session)
                ds_dict = HistoricalDatasetService._model_to_metadata(
                    existing_same_req, bt_counts=bt_counts, rpl_counts=rpl_counts
                ).to_dict()
                return {
                    "ok": True,
                    "http_status": 200,
                    "error_code": None,
                    "message": f"Reused existing validated FYERS dataset '{existing_same_req.dataset_code}'.",
                    "download_run": HistoricalDatasetService._serialize_download_run(run_row),
                    "dataset": ds_dict,
                    "data_quality_report": existing_same_req.data_quality_report_json or {},
                }

        run_row.status = "RUNNING"
        session.flush()
        AuditService.record(
            session,
            action=AuditEventType.HISTORICAL_DOWNLOAD_STARTED.value,
            event_type=AuditEventType.HISTORICAL_DOWNLOAD_STARTED.value,
            summary=f"Started FYERS historical download {run_code} ({prov_symbol} @ {tf_spec.fyers_resolution}).",
            actor=actor,
            actor_type=ActorType.OPERATOR,
            category=AuditCategory.MARKET_DATA,
            severity=AuditSeverity.INFO,
            entity_type="historical_download_run",
            entity_id=run_code,
            source="fyers.historical_provider",
            details=audit_base,
        )

        provider = FyersHistoricalDataProvider(
            settings=cfg,
            http_transport=http_transport,
            sleep_fn=(lambda _s: None) if http_transport is not None else __import__("time").sleep,
        )

        def _on_chunk_ok(chunk_info: dict[str, Any]) -> None:
            AuditService.record(
                session,
                action=AuditEventType.HISTORICAL_DOWNLOAD_CHUNK_COMPLETED.value,
                event_type=AuditEventType.HISTORICAL_DOWNLOAD_CHUNK_COMPLETED.value,
                summary=(
                    f"Completed FYERS chunk {chunk_info['chunk_index']}/{chunk_info['chunks_total']} "
                    f"({chunk_info['from_date']}..{chunk_info['to_date']}: {chunk_info['candles_returned']} bars)."
                ),
                actor="fyers.historical_provider",
                actor_type=ActorType.SYSTEM,
                category=AuditCategory.MARKET_DATA,
                severity=AuditSeverity.INFO,
                entity_type="historical_download_run",
                entity_id=run_code,
                source="fyers.historical_provider",
                details={**audit_base, **chunk_info},
            )

        try:
            fetch_res = provider.fetch_historical_candles(
                instrument=inst_spec.internal_symbol,
                timeframe=tf_spec.code,
                from_date=from_date,
                to_date=to_date,
                provider_symbol=prov_symbol,
                session_open_ist=open_ist,
                session_close_ist=close_ist,
                allow_partial_current_candle=allow_partial_current_candle,
                reference_now=reference_now,
                on_chunk_completed=_on_chunk_ok,
            )
        except FyersAuthenticationRequiredError as exc:
            raw_diag = getattr(exc, "diagnostic", None)
            safe_diag = (
                sanitize_provider_error_text(
                    str(raw_diag),
                    extra_secrets=(
                        cfg.FYERS_ACCESS_TOKEN,
                        cfg.FYERS_SECRET_KEY,
                        cfg.FYERS_CLIENT_ID,
                        cfg.FYERS_USER_ID,
                        cfg.FYERS_TOTP_SECRET,
                        cfg.FYERS_PIN,
                    ),
                )
                if raw_diag
                else None
            )
            run_row.status = DataQualityStatus.AUTHENTICATION_REQUIRED.value
            run_row.quality_status = DataQualityStatus.AUTHENTICATION_REQUIRED.value
            run_row.error_code = "FYERS_AUTHENTICATION_REQUIRED"
            run_row.error_message = FYERS_AUTH_RENEWAL_UI_MESSAGE
            run_row.details_json = {"auth_diagnostic": safe_diag} if safe_diag else {}
            run_row.completed_at = now_ist()
            session.flush()

            AuditService.record(
                session,
                action=AuditEventType.HISTORICAL_DATA_AUTHENTICATION_REQUIRED.value,
                event_type=AuditEventType.HISTORICAL_DATA_AUTHENTICATION_REQUIRED.value,
                summary=FYERS_AUTH_RENEWAL_UI_MESSAGE,
                actor=actor,
                actor_type=ActorType.OPERATOR,
                category=AuditCategory.MARKET_DATA,
                severity=AuditSeverity.WARNING,
                entity_type="historical_download_run",
                entity_id=run_code,
                source="fyers.historical_provider",
                details={
                    **audit_base,
                    "status": "FYERS_AUTHENTICATION_REQUIRED",
                    "auth_diagnostic": safe_diag,
                },
            )
            return {
                "ok": False,
                "http_status": 401,
                "error_code": "FYERS_AUTHENTICATION_REQUIRED",
                "message": FYERS_AUTH_RENEWAL_UI_MESSAGE,
                "auth_diagnostic": safe_diag,
                "download_run": HistoricalDatasetService._serialize_download_run(run_row),
                "dataset": None,
                "data_quality_report": None,
            }
        except HistoricalProviderUnavailableError as exc:
            safe_msg = sanitize_provider_error_text(
                str(exc),
                extra_secrets=(
                    cfg.FYERS_ACCESS_TOKEN,
                    cfg.FYERS_SECRET_KEY,
                    cfg.FYERS_CLIENT_ID,
                    cfg.FYERS_USER_ID,
                    cfg.FYERS_TOTP_SECRET,
                    cfg.FYERS_PIN,
                ),
            )
            run_row.status = "FAILED"
            run_row.quality_status = DataQualityStatus.DOWNLOAD_FAILED.value
            run_row.error_code = exc.code
            run_row.error_message = safe_msg
            run_row.completed_at = now_ist()
            session.flush()

            AuditService.record(
                session,
                action=AuditEventType.HISTORICAL_DOWNLOAD_FAILED.value,
                event_type=AuditEventType.HISTORICAL_DOWNLOAD_FAILED.value,
                summary=f"FYERS download {run_code} unavailable ({exc.code}): {safe_msg}",
                actor=actor,
                actor_type=ActorType.OPERATOR,
                category=AuditCategory.MARKET_DATA,
                severity=AuditSeverity.WARNING,
                entity_type="historical_download_run",
                entity_id=run_code,
                source="fyers.historical_provider",
                details={**audit_base, "error_code": exc.code},
            )
            return {
                "ok": False,
                "http_status": 503,
                "error_code": exc.code,
                "message": safe_msg,
                "download_run": HistoricalDatasetService._serialize_download_run(run_row),
                "dataset": None,
                "data_quality_report": None,
            }
        except HistoricalDataError as exc:
            safe_msg = sanitize_provider_error_text(
                str(exc),
                extra_secrets=(
                    cfg.FYERS_ACCESS_TOKEN,
                    cfg.FYERS_SECRET_KEY,
                    cfg.FYERS_CLIENT_ID,
                    cfg.FYERS_USER_ID,
                    cfg.FYERS_TOTP_SECRET,
                    cfg.FYERS_PIN,
                ),
            )
            run_row.status = "FAILED"
            run_row.quality_status = DataQualityStatus.DOWNLOAD_FAILED.value
            run_row.error_code = exc.code
            run_row.error_message = safe_msg
            run_row.completed_at = now_ist()
            session.flush()

            AuditService.record(
                session,
                action=AuditEventType.HISTORICAL_DOWNLOAD_FAILED.value,
                event_type=AuditEventType.HISTORICAL_DOWNLOAD_FAILED.value,
                summary=f"FYERS download {run_code} failed ({exc.code}): {safe_msg}",
                actor=actor,
                actor_type=ActorType.OPERATOR,
                category=AuditCategory.MARKET_DATA,
                severity=AuditSeverity.WARNING,
                entity_type="historical_download_run",
                entity_id=run_code,
                source="fyers.historical_provider",
                details={**audit_base, "error_code": exc.code},
            )
            http_code = 403 if exc.code == "FYERS_PERMISSION_DENIED" else 400
            return {
                "ok": False,
                "http_status": http_code,
                "error_code": exc.code,
                "message": safe_msg,
                "download_run": HistoricalDatasetService._serialize_download_run(run_row),
                "dataset": None,
                "data_quality_report": None,
            }

        # Validated / Partial / Invalid result returned with parsed candidates
        qual = fetch_res.quality
        dq_dict = qual.to_report_dict()

        AuditService.record(
            session,
            action=AuditEventType.HISTORICAL_DATASET_VALIDATED.value,
            event_type=AuditEventType.HISTORICAL_DATASET_VALIDATED.value,
            summary=(
                f"Validated FYERS historical candles for {run_code}: "
                f"quality={qual.quality_status.value}, valid_bars={qual.valid_candle_count}."
            ),
            actor="historical_data.validator",
            actor_type=ActorType.SYSTEM,
            category=AuditCategory.MARKET_DATA,
            severity=AuditSeverity.INFO if qual.passed_validation else AuditSeverity.WARNING,
            entity_type="historical_download_run",
            entity_id=run_code,
            source="historical_data.validator",
            details={
                **audit_base,
                "quality_status": qual.quality_status.value,
                "valid_candle_count": qual.valid_candle_count,
                "missing_candle_count": qual.missing_candle_count,
                "duplicate_timestamp_count": qual.duplicate_timestamp_count,
            },
        )

        if not qual.passed_validation or not fetch_res.candles:
            err_msg = (
                "Downloaded FYERS candles failed structural data-quality validation; "
                "dataset rejected and not marked READY."
            )
            run_row.status = "FAILED"
            run_row.quality_status = DataQualityStatus.INVALID.value
            run_row.chunks_total = fetch_res.chunks_total
            run_row.chunks_succeeded = fetch_res.chunks_succeeded
            run_row.chunks_failed = fetch_res.chunks_failed
            run_row.candles_fetched = qual.total_rows_inspected
            run_row.candles_stored = 0
            run_row.error_code = "FYERS_DATA_QUALITY_INVALID"
            run_row.error_message = err_msg
            run_row.details_json = {"chunk_summaries": fetch_res.chunk_summaries, "quality": dq_dict}
            run_row.completed_at = now_ist()
            session.flush()

            AuditService.record(
                session,
                action=AuditEventType.HISTORICAL_DOWNLOAD_FAILED.value,
                event_type=AuditEventType.HISTORICAL_DOWNLOAD_FAILED.value,
                summary=err_msg,
                actor=actor,
                actor_type=ActorType.OPERATOR,
                category=AuditCategory.MARKET_DATA,
                severity=AuditSeverity.WARNING,
                entity_type="historical_download_run",
                entity_id=run_code,
                source="historical_data.validator",
                details={**audit_base, "quality_status": "INVALID"},
            )
            return {
                "ok": False,
                "http_status": 422,
                "error_code": "FYERS_DATA_QUALITY_INVALID",
                "message": err_msg,
                "download_run": HistoricalDatasetService._serialize_download_run(run_row),
                "dataset": None,
                "data_quality_report": dq_dict,
            }

        fingerprint = compute_candles_fingerprint(fetch_res.candles)

        # Content-addressed deduplication: if exact same candle fingerprint already stored for this symbol/tf,
        # reuse immutable dataset rather than duplicating rows; if changed, mint a new immutable dataset!
        existing_by_hash = session.scalar(
            select(HistoricalDatasetModel).where(
                HistoricalDatasetModel.instrument_symbol == inst_spec.internal_symbol,
                HistoricalDatasetModel.timeframe == tf_spec.code,
                HistoricalDatasetModel.source_type == DataSourceType.FYERS_HISTORICAL.value,
                HistoricalDatasetModel.data_version_hash == fingerprint,
            )
        )

        if existing_by_hash is not None:
            ds_model = existing_by_hash
        else:
            d_from_compact = from_date.strftime("%Y%m%d")
            d_to_compact = to_date.strftime("%Y%m%d")
            base_code = (
                f"FYERS_{inst_spec.internal_symbol}_{tf_spec.csv_filename_tag}_"
                f"{d_from_compact}_{d_to_compact}_{fingerprint[:8].upper()}"
            )
            if session.scalar(
                select(HistoricalDatasetModel.id).where(
                    HistoricalDatasetModel.dataset_code == base_code
                )
            ):
                base_code = f"{base_code}_{uuid.uuid4().hex[:4].upper()}"

            default_title = (
                f"{inst_spec.internal_symbol} {tf_spec.display_name} FYERS Historical "
                f"({from_date.isoformat()} .. {to_date.isoformat()})"
            )
            ds_model = HistoricalDatasetModel(
                dataset_code=base_code,
                name=(display_name or "").strip() or default_title,
                instrument_id=inst_row.id if inst_row else None,
                instrument_symbol=inst_spec.internal_symbol,
                provider=HistoricalProviderName.FYERS.value,
                provider_symbol=prov_symbol,
                timeframe=tf_spec.code,
                source_type=DataSourceType.FYERS_HISTORICAL.value,
                quality_status=qual.quality_status.value,
                is_complete=qual.is_complete,
                data_version_hash=fingerprint,
                row_count=len(fetch_res.candles),
                requested_from=req_from_dt,
                requested_to=req_to_dt,
                start_timestamp=fetch_res.candles[0].timestamp,
                end_timestamp=fetch_res.candles[-1].timestamp,
                retrieved_at=fetch_res.retrieved_at,
                # Omit redundant raw_csv_text blob in Postgres to conserve Neon Free storage;
                # canonical CSV is rendered deterministically on export from historical_candles!
                raw_csv_text="",
                data_quality_report_json=dq_dict,
                metadata_json={
                    "chunks_total": fetch_res.chunks_total,
                    "chunks_succeeded": fetch_res.chunks_succeeded,
                    "chunks_failed": fetch_res.chunks_failed,
                    "missing_candle_count": qual.missing_candle_count,
                    "duplicate_candle_count": qual.duplicate_timestamp_count,
                    "exact_duplicate_removed_count": qual.exact_duplicate_removed_count,
                    "zero_volume_warning": qual.zero_volume_candle_count > 0,
                    "partial_current_candle_excluded": qual.partial_current_candle_excluded,
                    "fyers_resolution": tf_spec.fyers_resolution,
                },
                uploaded_by=actor.strip() or "desk.operator",
                created_at=now_ist(),
            )
            session.add(ds_model)
            session.flush()

            session.add_all(
                [
                    HistoricalCandleModel(
                        dataset_id=ds_model.id,
                        instrument_symbol=inst_spec.internal_symbol,
                        timeframe=tf_spec.code,
                        bar_timestamp=c.timestamp,
                        open=c.open,
                        high=c.high,
                        low=c.low,
                        close=c.close,
                        volume=c.volume,
                    )
                    for c in fetch_res.candles
                ]
            )
            session.flush()

        is_partial = qual.quality_status == DataQualityStatus.PARTIAL
        run_row.status = "PARTIAL" if is_partial else "COMPLETED"
        run_row.quality_status = qual.quality_status.value
        run_row.dataset_id = ds_model.id
        run_row.dataset_code = ds_model.dataset_code
        run_row.chunks_total = fetch_res.chunks_total
        run_row.chunks_succeeded = fetch_res.chunks_succeeded
        run_row.chunks_failed = fetch_res.chunks_failed
        run_row.candles_fetched = qual.total_rows_inspected
        run_row.candles_stored = len(fetch_res.candles)
        run_row.details_json = {"chunk_summaries": fetch_res.chunk_summaries}
        run_row.completed_at = now_ist()
        session.flush()

        term_event = (
            AuditEventType.HISTORICAL_DOWNLOAD_PARTIAL.value
            if is_partial
            else AuditEventType.HISTORICAL_DOWNLOAD_COMPLETED.value
        )
        term_msg = (
            "The historical download completed partially. Review the data-quality report before use."
            if is_partial
            else f"Completed FYERS historical download '{ds_model.dataset_code}' ({len(fetch_res.candles)} candles)."
        )
        AuditService.record(
            session,
            action=term_event,
            event_type=term_event,
            summary=term_msg,
            actor=actor,
            actor_type=ActorType.OPERATOR,
            category=AuditCategory.MARKET_DATA,
            severity=AuditSeverity.WARNING if is_partial else AuditSeverity.INFO,
            entity_type="historical_dataset",
            entity_id=ds_model.dataset_code,
            source="fyers.historical_provider",
            details={
                **audit_base,
                "dataset_id": ds_model.dataset_code,
                "candle_count": len(fetch_res.candles),
                "quality_status": qual.quality_status.value,
                "is_complete": qual.is_complete,
                "data_version_hash": fingerprint,
            },
        )

        bt_counts, rpl_counts = HistoricalDatasetService._reference_counts(session)
        ds_out = HistoricalDatasetService._model_to_metadata(
            ds_model, bt_counts=bt_counts, rpl_counts=rpl_counts
        ).to_dict()
        return {
            "ok": True,
            "http_status": 201,
            "error_code": None,
            "message": term_msg,
            "download_run": HistoricalDatasetService._serialize_download_run(run_row),
            "dataset": ds_out,
            "data_quality_report": dq_dict,
        }

