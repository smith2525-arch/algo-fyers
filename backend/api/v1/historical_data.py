"""Historical Market Data API endpoints (FYERS historical download, CSV upload/export, quality).

SECURITY INVARIANT:
- All FYERS historical API requests originate exclusively from the FastAPI backend.
- Zero credentials (`FYERS_CLIENT_ID`, `FYERS_SECRET_KEY`, `FYERS_ACCESS_TOKEN`) are
  ever returned in API responses or CSV exports.
- FYERS is used strictly as a historical OHLCV source; execution remains PAPER_ONLY.
"""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Query, Response, status
from fastapi.encoders import jsonable_encoder
from fastapi.responses import JSONResponse

from backend.schemas.api import HistoricalDownloadRequest, HistoricalUploadRequest
from core.config import get_settings
from core.logging import get_correlation_id
from core.market_data.dataset_service import HistoricalDatasetService
from core.persistence.uow import unit_of_work

router = APIRouter(prefix="/api/v1/historical-data", tags=["Historical Market Data"])


@router.get("/providers")
def list_historical_providers() -> dict[str, Any]:
    cfg = get_settings()
    providers = HistoricalDatasetService.list_providers(settings=cfg)
    return {
        "execution_mode": cfg.EXECUTION_MODE,
        "fyers_historical_data_enabled": cfg.FYERS_HISTORICAL_DATA_ENABLED,
        "providers": providers,
    }


@router.get("/instruments")
def list_historical_instruments() -> dict[str, Any]:
    cfg = get_settings()
    with unit_of_work(settings=cfg) as session:
        return HistoricalDatasetService.list_supported_instruments_and_timeframes(
            session, settings=cfg
        )


@router.post("/downloads")
def trigger_fyers_historical_download(payload: HistoricalDownloadRequest) -> Response:
    cfg = get_settings()
    with unit_of_work(settings=cfg) as session:
        outcome = HistoricalDatasetService.download_fyers_dataset(
            session,
            instrument=payload.instrument,
            timeframe=payload.timeframe,
            from_date=payload.from_date,
            to_date=payload.to_date,
            display_name=payload.display_name,
            force_refresh=payload.force_refresh,
            allow_partial_current_candle=payload.allow_partial_current_candle,
            actor=payload.actor,
            settings=cfg,
        )

    http_code = int(outcome["http_status"])
    if not outcome["ok"]:
        return JSONResponse(
            status_code=http_code,
            content=jsonable_encoder(
                {
                    "error": outcome["error_code"],
                    "message": outcome["message"],
                    "auth_diagnostic": outcome.get("auth_diagnostic"),
                    "download_run": outcome["download_run"],
                    "data_quality_report": outcome.get("data_quality_report"),
                    "correlation_id": get_correlation_id(),
                }
            ),
        )

    return JSONResponse(
        status_code=http_code,
        content=jsonable_encoder(
            {
                "status": outcome["download_run"]["status"],
                "message": outcome["message"],
                "download_run": outcome["download_run"],
                "dataset": outcome["dataset"],
                "data_quality_report": outcome["data_quality_report"],
            }
        ),
    )


@router.get("/downloads/{download_id}")
def get_historical_download_run(download_id: str) -> dict[str, Any]:
    cfg = get_settings()
    with unit_of_work(settings=cfg) as session:
        return HistoricalDatasetService.get_download_run(session, download_id)


@router.get("/datasets")
def list_historical_datasets(
    instrument: str | None = Query(default=None),
    timeframe: str | None = Query(default=None),
    source_type: str | None = Query(default=None),
    provider: str | None = Query(default=None),
    quality_status: str | None = Query(default=None),
    include_synthetic: bool = Query(default=True),
) -> dict[str, Any]:
    cfg = get_settings()
    with unit_of_work(settings=cfg) as session:
        items = HistoricalDatasetService.list_datasets(
            session,
            instrument=instrument,
            timeframe=timeframe,
            source_type=source_type,
            provider=provider,
            quality_status=quality_status,
            include_synthetic=include_synthetic,
        )
        return {"count": len(items), "datasets": items}


@router.get("/datasets/{dataset_id}")
def get_historical_dataset_detail(dataset_id: str) -> dict[str, Any]:
    cfg = get_settings()
    with unit_of_work(settings=cfg) as session:
        return HistoricalDatasetService.get_dataset_detail(session, dataset_id)


@router.get("/datasets/{dataset_id}/quality")
def get_historical_dataset_quality(dataset_id: str) -> dict[str, Any]:
    cfg = get_settings()
    with unit_of_work(settings=cfg) as session:
        return HistoricalDatasetService.get_dataset_quality(session, dataset_id)


@router.get("/datasets/{dataset_id}/csv")
def export_historical_dataset_csv(
    dataset_id: str,
    actor: str = Query(default="desk.operator"),
) -> Response:
    cfg = get_settings()
    with unit_of_work(settings=cfg) as session:
        filename, csv_text = HistoricalDatasetService.export_dataset_csv(
            session, dataset_id, actor=actor
        )
    return Response(
        content=csv_text.encode("utf-8"),
        media_type="text/csv; charset=utf-8",
        headers={
            "Content-Disposition": f'attachment; filename="{filename}"',
            "X-Dataset-Filename": filename,
        },
    )


@router.post("/uploads", status_code=status.HTTP_201_CREATED)
def upload_historical_csv_dataset(payload: HistoricalUploadRequest) -> dict[str, Any]:
    cfg = get_settings()
    with unit_of_work(settings=cfg) as session:
        return HistoricalDatasetService.upload_csv_dataset(
            session,
            name=payload.name,
            instrument=payload.instrument,
            timeframe=payload.timeframe,
            csv_text=payload.csv_text,
            actor=payload.actor,
        )


@router.delete("/datasets/{dataset_id}")
def delete_historical_dataset(
    dataset_id: str,
    actor: str = Query(default="desk.operator"),
) -> dict[str, Any]:
    cfg = get_settings()
    with unit_of_work(settings=cfg) as session:
        return HistoricalDatasetService.delete_dataset(session, dataset_id, actor=actor)

