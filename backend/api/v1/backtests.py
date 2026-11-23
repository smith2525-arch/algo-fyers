"""Backtesting API endpoints: historical dataset catalog/import, execution, and result attribution."""

from __future__ import annotations

from uuid import UUID

from fastapi import APIRouter, Query, status

from backend.schemas.api import (
    BacktestRunDetailOut,
    BacktestRunSummaryOut,
    DatasetDescriptorOut,
    ImportCsvDatasetOut,
    ImportCsvDatasetRequest,
    RunBacktestRequest,
)
from core.backtesting.service import BacktestService
from core.config import get_settings
from core.persistence.uow import unit_of_work

router = APIRouter(prefix="/api/v1/backtests", tags=["Backtests"])


@router.get("/datasets", response_model=list[DatasetDescriptorOut])
def list_historical_datasets(
    instrument: str | None = Query(default=None),
    timeframe: str | None = Query(default=None),
) -> list[DatasetDescriptorOut]:
    cfg = get_settings()
    with unit_of_work(settings=cfg) as session:
        items = BacktestService.list_datasets(
            session, instrument=instrument, timeframe=timeframe
        )
        return [DatasetDescriptorOut(**item) for item in items]


@router.post(
    "/datasets/import-csv",
    response_model=ImportCsvDatasetOut,
    status_code=status.HTTP_201_CREATED,
)
def import_csv_dataset(payload: ImportCsvDatasetRequest) -> ImportCsvDatasetOut:
    cfg = get_settings()
    with unit_of_work(settings=cfg) as session:
        res = BacktestService.import_csv_dataset(
            session,
            name=payload.name,
            instrument=payload.instrument,
            timeframe=payload.timeframe,
            csv_text=payload.csv_text,
            actor=payload.actor,
        )
        return ImportCsvDatasetOut(
            dataset=DatasetDescriptorOut(**res["dataset"]),
            data_quality_report=res["data_quality_report"],
        )


@router.post(
    "",
    response_model=BacktestRunDetailOut,
    status_code=status.HTTP_201_CREATED,
)
def execute_backtest_run(payload: RunBacktestRequest) -> BacktestRunDetailOut:
    cfg = get_settings()
    with unit_of_work(settings=cfg) as session:
        detail = BacktestService.execute_backtest(
            session,
            strategy_version_id=payload.strategy_version_id,
            dataset_id=payload.dataset_id,
            start_date=payload.start_date,
            end_date=payload.end_date,
            initial_capital=payload.initial_capital,
            slippage_bps=payload.slippage_bps,
            estimated_charges_bps=payload.estimated_charges_bps,
            per_order_fee_inr=payload.per_order_fee_inr,
            position_sizing_mode=payload.position_sizing_mode,
            position_size_value=payload.position_size_value,
            execution_delay_bars=payload.execution_delay_bars,
            allow_partial_dataset=payload.allow_partial_dataset,
            actor=payload.actor,
        )
        return BacktestRunDetailOut(**detail)


@router.get("", response_model=list[BacktestRunSummaryOut])
def list_backtest_runs(
    instrument: str | None = Query(default=None),
    strategy_version_id: UUID | None = Query(default=None),
    status_filter: str | None = Query(default=None, alias="status"),
    limit: int = Query(default=50, ge=1, le=200),
) -> list[BacktestRunSummaryOut]:
    cfg = get_settings()
    with unit_of_work(settings=cfg) as session:
        runs = BacktestService.list_backtests(
            session,
            instrument=instrument,
            strategy_version_id=strategy_version_id,
            status=status_filter,
            limit=limit,
        )
        return [BacktestRunSummaryOut(**r) for r in runs]


@router.get("/{backtest_id}", response_model=BacktestRunDetailOut)
def get_backtest_run_detail(backtest_id: UUID) -> BacktestRunDetailOut:
    cfg = get_settings()
    with unit_of_work(settings=cfg) as session:
        detail = BacktestService.get_backtest_detail(session, backtest_id)
        return BacktestRunDetailOut(**detail)
