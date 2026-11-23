"""Application service orchestrating deterministic backtests, reproducibility, and persistence."""

from __future__ import annotations

import hashlib
import json
import uuid
from datetime import date
from decimal import Decimal
from pathlib import Path
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from core.audit.service import AuditService
from core.backtesting.data_adapter import CompositeHistoricalDataAdapter
from core.backtesting.engine import BACKTEST_ENGINE_VERSION, BacktestEngine
from core.backtesting.models import BacktestExecutionInputs, DatasetDescriptor
from core.backtesting.validator import validate_ohlcv_rows
from core.clock import ensure_ist, format_ist_iso, now_ist
from core.domain.enums import (
    ActorType,
    AuditCategory,
    AuditEventType,
    AuditSeverity,
    BacktestStatus,
    PositionSizingMode,
)
from core.exceptions import ControlCenterError, EntityNotFoundError
from core.logging import get_correlation_id
from core.market_data.models import DataSourceType, classify_data_source
from core.persistence.models import (
    BacktestRunModel,
    BacktestTradeModel,
    InstrumentModel,
    StrategyVersionModel,
)
from core.strategies.config_schema import EmaRsiStrategyConfig, parse_and_validate_ema_rsi_config
from core.strategies.indicators import compute_ema_series, compute_rsi_series
from core.strategies.ema_rsi import OHLCVBar


def _compute_reproducibility_fingerprint(payload: dict[str, Any]) -> str:
    canonical_json = json.dumps(payload, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(canonical_json.encode("utf-8")).hexdigest()


def _build_chart_bars(
    bars: list[OHLCVBar],
    strategy_cfg: EmaRsiStrategyConfig,
) -> list[dict[str, Any]]:
    if not bars:
        return []
    closes = [b.close for b in bars]
    fast_ema = compute_ema_series(closes, strategy_cfg.fast_ema_period)
    slow_ema = compute_ema_series(closes, strategy_cfg.slow_ema_period)
    rsi = compute_rsi_series(closes, strategy_cfg.rsi_period)

    out: list[dict[str, Any]] = []
    for idx, bar in enumerate(bars):
        f_val = fast_ema[idx]
        s_val = slow_ema[idx]
        r_val = rsi[idx]
        out.append(
            {
                "timestamp": format_ist_iso(bar.bar_timestamp),
                "open": str(bar.open.quantize(Decimal("0.01"))),
                "high": str(bar.high.quantize(Decimal("0.01"))),
                "low": str(bar.low.quantize(Decimal("0.01"))),
                "close": str(bar.close.quantize(Decimal("0.01"))),
                "volume": bar.volume,
                "fast_ema": str(f_val.quantize(Decimal("0.01"))) if f_val is not None else None,
                "slow_ema": str(s_val.quantize(Decimal("0.01"))) if s_val is not None else None,
                "rsi": str(r_val.quantize(Decimal("0.01"))) if r_val is not None else None,
            }
        )
    return out


class BacktestService:
    """Stateless domain service managing historical datasets and backtest runs."""

    @staticmethod
    def _descriptor_to_dict(desc: DatasetDescriptor) -> dict[str, Any]:
        labels = classify_data_source(desc.source_type, desc.provider)
        return {
            "dataset_id": desc.dataset_id,
            "name": desc.name,
            "instrument": desc.instrument,
            "timeframe": desc.timeframe,
            "source_type": desc.source_type,
            "provider": desc.provider,
            "provider_symbol": desc.provider_symbol,
            "quality_status": desc.quality_status,
            "is_complete": desc.is_complete,
            "data_version_hash": desc.data_version_hash,
            "row_count": desc.row_count,
            "start_timestamp": desc.start_timestamp,
            "end_timestamp": desc.end_timestamp,
            "retrieved_at": desc.retrieved_at,
            "labels": labels.to_dict(),
        }

    @staticmethod
    def list_datasets(
        session: Session,
        *,
        instrument: str | None = None,
        timeframe: str | None = None,
        bundled_dir: Path | None = None,
    ) -> list[dict[str, Any]]:
        adapter = CompositeHistoricalDataAdapter(session, bundled_dir=bundled_dir)
        items = adapter.list_datasets(instrument=instrument, timeframe=timeframe)
        return [BacktestService._descriptor_to_dict(d) for d in items]

    @staticmethod
    def import_csv_dataset(
        session: Session,
        *,
        name: str,
        instrument: str,
        timeframe: str,
        csv_text: str,
        actor: str = "desk.operator",
        bundled_dir: Path | None = None,
    ) -> dict[str, Any]:
        clean_symbol = instrument.strip().upper()
        inst = session.scalar(
            select(InstrumentModel).where(InstrumentModel.symbol == clean_symbol)
        )
        if inst is None:
            raise EntityNotFoundError(f"Instrument '{clean_symbol}' is not registered.")

        adapter = CompositeHistoricalDataAdapter(session, bundled_dir=bundled_dir)
        descriptor, dq_report = adapter.db_adapter.import_csv(
            name=name,
            instrument=clean_symbol,
            timeframe=timeframe.strip(),
            csv_text=csv_text,
            uploaded_by=actor,
            session_open_ist=inst.session_open_ist,
            session_close_ist=inst.session_close_ist,
        )

        sev = (
            AuditSeverity.WARNING
            if (not dq_report.passed_validation or dq_report.has_warnings)
            else AuditSeverity.INFO
        )
        AuditService.record(
            session,
            action="HISTORICAL_DATASET_IMPORTED",
            summary=(
                f"Imported CSV dataset '{descriptor.dataset_id}' ({descriptor.row_count} rows, "
                f"SHA256={descriptor.data_version_hash[:12]}) for {clean_symbol} {timeframe}."
            ),
            actor=actor,
            actor_type=ActorType.OPERATOR,
            category=AuditCategory.BACKTEST,
            severity=sev,
            entity_type="historical_dataset",
            entity_id=descriptor.dataset_id,
            details={
                "dataset_id": descriptor.dataset_id,
                "instrument": clean_symbol,
                "timeframe": timeframe,
                "row_count": descriptor.row_count,
                "data_version_hash": descriptor.data_version_hash,
                "passed_validation": dq_report.passed_validation,
                "has_warnings": dq_report.has_warnings,
            },
        )
        return {
            "dataset": BacktestService._descriptor_to_dict(descriptor),
            "data_quality_report": dq_report.to_dict(),
        }

    @staticmethod
    def execute_backtest(
        session: Session,
        *,
        strategy_version_id: uuid.UUID,
        dataset_id: str,
        start_date: date | None = None,
        end_date: date | None = None,
        initial_capital: Decimal = Decimal("500000.00"),
        slippage_bps: Decimal | None = None,
        estimated_charges_bps: Decimal | None = None,
        per_order_fee_inr: Decimal | None = None,
        position_sizing_mode: PositionSizingMode | None = None,
        position_size_value: Decimal | None = None,
        execution_delay_bars: int = 1,
        allow_partial_dataset: bool = False,
        actor: str = "desk.operator",
        bundled_dir: Path | None = None,
    ) -> dict[str, Any]:
        started_at = now_ist()

        if start_date is not None and end_date is not None and start_date > end_date:
            raise ControlCenterError(
                f"Backtest start_date ({start_date}) cannot be after end_date ({end_date}).",
                code="INVALID_BACKTEST_DATE_RANGE",
            )
        if initial_capital <= Decimal("0"):
            raise ControlCenterError(
                "Initial capital must be strictly greater than zero.",
                code="INVALID_INITIAL_CAPITAL",
            )
        if execution_delay_bars < 1:
            raise ControlCenterError(
                "execution_delay_bars must be >= 1 to prevent look-ahead fills.",
                code="LOOKAHEAD_VIOLATION_PREVENTED",
            )

        strat_ver = session.get(StrategyVersionModel, strategy_version_id)
        if strat_ver is None:
            raise EntityNotFoundError(f"StrategyVersion '{strategy_version_id}' not found.")

        strategy_cfg = parse_and_validate_ema_rsi_config(strat_ver.parameters_json)

        inst = session.scalar(
            select(InstrumentModel).where(InstrumentModel.symbol == strat_ver.instrument_symbol)
        )
        if inst is None:
            raise EntityNotFoundError(
                f"Instrument '{strat_ver.instrument_symbol}' not found in reference table."
            )

        adapter = CompositeHistoricalDataAdapter(session, bundled_dir=bundled_dir)
        raw_dataset = adapter.load_dataset(dataset_id)
        desc = raw_dataset.descriptor

        if desc.instrument.upper() != strat_ver.instrument_symbol.upper():
            raise ControlCenterError(
                f"Instrument mismatch: StrategyVersion '{strat_ver.version_label}' targets "
                f"'{strat_ver.instrument_symbol}', but dataset '{desc.dataset_id}' is '{desc.instrument}'.",
                code="BACKTEST_INSTRUMENT_MISMATCH",
            )
        if desc.timeframe != strat_ver.timeframe:
            raise ControlCenterError(
                f"Timeframe mismatch: StrategyVersion '{strat_ver.version_label}' targets "
                f"'{strat_ver.timeframe}', but dataset '{desc.dataset_id}' is '{desc.timeframe}'.",
                code="BACKTEST_TIMEFRAME_MISMATCH",
            )

        if desc.quality_status in ("DOWNLOAD_FAILED", "AUTHENTICATION_REQUIRED") or (
            desc.source_type == DataSourceType.FYERS_HISTORICAL.value
            and desc.quality_status == "INVALID"
        ):
            raise ControlCenterError(
                f"Dataset '{desc.dataset_id}' has quality status '{desc.quality_status}' and cannot be used.",
                code="BACKTEST_DATASET_INVALID",
            )

        if desc.quality_status == "PARTIAL" and not allow_partial_dataset:
            raise ControlCenterError(
                f"Dataset '{desc.dataset_id}' is PARTIAL. Explicit operator acknowledgment "
                "('allow_partial_dataset=true') is required before running a backtest.",
                code="PARTIAL_DATASET_ACKNOWLEDGMENT_REQUIRED",
            )

        src_labels = classify_data_source(desc.source_type, desc.provider)
        AuditService.record(
            session,
            action=AuditEventType.HISTORICAL_DATASET_SELECTED_FOR_BACKTEST.value,
            event_type=AuditEventType.HISTORICAL_DATASET_SELECTED_FOR_BACKTEST.value,
            summary=(
                f"Selected dataset '{desc.dataset_id}' ({src_labels.ui_headline}) "
                f"for backtest on {strat_ver.version_label}."
            ),
            actor=actor,
            actor_type=ActorType.OPERATOR,
            category=AuditCategory.BACKTEST,
            severity=AuditSeverity.WARNING if desc.quality_status == "PARTIAL" else AuditSeverity.INFO,
            entity_type="historical_dataset",
            entity_id=desc.dataset_id,
            strategy_id=strat_ver.version_label,
            source="backtest.service",
            details={
                "dataset_id": desc.dataset_id,
                "data_version_hash": desc.data_version_hash,
                "quality_status": desc.quality_status,
                "candle_count": desc.row_count,
                "retrieved_at": format_ist_iso(desc.retrieved_at) if desc.retrieved_at else None,
                "data_provider": src_labels.audit_provider,
                "data_type": src_labels.audit_data_type,
                "execution_type": src_labels.audit_execution_type,
            },
        )

        eff_slippage = (
            slippage_bps if slippage_bps is not None else strategy_cfg.slippage_bps
        )
        eff_charges = (
            estimated_charges_bps
            if estimated_charges_bps is not None
            else strategy_cfg.estimated_charges_bps
        )
        eff_flat_fee = (
            per_order_fee_inr
            if per_order_fee_inr is not None
            else strategy_cfg.per_order_fee_inr
        )
        eff_sizing_mode = (
            position_sizing_mode
            if position_sizing_mode is not None
            else strategy_cfg.position_sizing_mode
        )
        eff_sizing_val = (
            position_size_value
            if position_size_value is not None
            else strategy_cfg.position_size_value
        )

        if eff_slippage < 0 or eff_charges < 0 or eff_flat_fee < 0:
            raise ControlCenterError(
                "Slippage and charge assumptions must be non-negative.",
                code="INVALID_COST_ASSUMPTION",
            )
        if eff_sizing_val <= 0:
            raise ControlCenterError(
                "Position sizing value must be strictly positive.",
                code="INVALID_POSITION_SIZING",
            )

        AuditService.record(
            session,
            action="BACKTEST_STARTED",
            event_type="BACKTEST_STARTED",
            summary=f"Started deterministic backtest on {strat_ver.version_label} ({desc.dataset_id}).",
            actor=actor,
            actor_type=ActorType.OPERATOR,
            category=AuditCategory.BACKTEST,
            severity=AuditSeverity.INFO,
            entity_type="strategy_version",
            entity_id=str(strat_ver.id),
            strategy_id=strat_ver.version_label,
            source="backtest.service",
            details={"dataset_id": desc.dataset_id, "strategy_version": strat_ver.version_label},
        )

        inputs = BacktestExecutionInputs(
            strategy_version_id=strat_ver.id,
            strategy_version_label=strat_ver.version_label,
            instrument=strat_ver.instrument_symbol,
            timeframe=strat_ver.timeframe,
            start_date=start_date,
            end_date=end_date,
            initial_capital=initial_capital,
            lot_size=inst.lot_size,
            slippage_bps=eff_slippage,
            estimated_charges_bps=eff_charges,
            per_order_fee_inr=eff_flat_fee,
            position_sizing_mode=eff_sizing_mode,
            position_size_value=eff_sizing_val,
            execution_delay_bars=execution_delay_bars,
        )

        input_parameters_json: dict[str, Any] = {
            "strategy_version_id": str(strat_ver.id),
            "strategy_version_label": strat_ver.version_label,
            "strategy_parameter_hash": strat_ver.parameter_hash,
            "strategy_parameters": strat_ver.parameters_json,
            "instrument": inputs.instrument,
            "timeframe": inputs.timeframe,
            "start_date": start_date.isoformat() if start_date else None,
            "end_date": end_date.isoformat() if end_date else None,
            "initial_capital": str(initial_capital.quantize(Decimal("0.01"))),
            "lot_size": inst.lot_size,
            "position_sizing_mode": eff_sizing_mode.value,
            "position_size_value": str(eff_sizing_val),
        }

        execution_assumptions_json: dict[str, Any] = {
            "execution_mode": "SIMULATED_BACKTEST_ONLY",
            "execution_delay_bars": execution_delay_bars,
            "fill_model": f"NEXT_BAR_OPEN_PLUS_{execution_delay_bars}_DELAY_WITH_ADVERSE_SLIPPAGE",
            "intrabar_tie_break": "PESSIMISTIC_STOP_FIRST",
            "slippage_bps": str(eff_slippage),
            "estimated_charges_bps": str(eff_charges),
            "per_order_fee_inr": str(eff_flat_fee),
            "session_open_ist": inst.session_open_ist,
            "session_close_ist": inst.session_close_ist,
            "timezone": "Asia/Kolkata",
        }

        fingerprint = _compute_reproducibility_fingerprint(
            {
                "engine_version": BACKTEST_ENGINE_VERSION,
                "dataset_id": desc.dataset_id,
                "data_version_hash": desc.data_version_hash,
                "inputs": input_parameters_json,
                "assumptions": execution_assumptions_json,
            }
        )
        det_namespace = uuid.uuid5(uuid.NAMESPACE_OID, fingerprint)

        engine = BacktestEngine(
            strategy_config=strategy_cfg,
            inputs=inputs,
            session_open_ist=inst.session_open_ist,
            session_close_ist=inst.session_close_ist,
            deterministic_namespace=det_namespace,
        )
        output = engine.run(raw_dataset.rows)
        completed_at = now_ist()

        existing_count = int(
            session.scalar(select(func.count()).select_from(BacktestRunModel)) or 0
        )
        run_code = (
            f"BT-{started_at.strftime('%Y%m%d')}-{existing_count + 1:04d}-"
            f"{uuid.uuid4().hex[:4].upper()}"
        )

        data_start_ts = (
            output.validated_bars[0].bar_timestamp if output.validated_bars else None
        )
        data_end_ts = (
            output.validated_bars[-1].bar_timestamp if output.validated_bars else None
        )

        run_model = BacktestRunModel(
            run_code=run_code,
            strategy_version_id=strat_ver.id,
            strategy_version_label=strat_ver.version_label,
            instrument_symbol=inputs.instrument,
            timeframe=inputs.timeframe,
            dataset_id=desc.dataset_id,
            dataset_name=desc.name,
            dataset_source=desc.source_type,
            data_version_hash=desc.data_version_hash,
            reproducibility_fingerprint=fingerprint,
            input_parameters_json=input_parameters_json,
            execution_assumptions_json=execution_assumptions_json,
            data_quality_report_json=output.data_quality_report.to_dict(),
            metrics_json=output.metrics,
            equity_curve_json=output.equity_curve,
            drawdown_curve_json=output.drawdown_curve,
            monthly_returns_json=output.monthly_returns,
            data_start_timestamp=data_start_ts,
            data_end_timestamp=data_end_ts,
            started_at=started_at,
            completed_at=completed_at,
            status=output.status.value,
            engine_version=output.engine_version,
            created_by=actor,
            correlation_id=get_correlation_id(),
        )
        session.add(run_model)
        session.flush()

        trade_models: list[BacktestTradeModel] = []
        for t in output.trades:
            tm = BacktestTradeModel(
                backtest_run_id=run_model.id,
                deterministic_trade_id=t.trade_id,
                trade_number=t.trade_number,
                strategy_version_id=t.strategy_version_id,
                strategy_version_label=t.strategy_version_label,
                instrument_symbol=t.instrument,
                direction=t.direction.value,
                entry_signal_time=t.entry_signal_time,
                entry_time=t.entry_time,
                raw_entry_price=t.raw_entry_price,
                entry_price=t.entry_price,
                quantity=t.quantity,
                exit_time=t.exit_time,
                raw_exit_price=t.raw_exit_price,
                exit_price=t.exit_price,
                exit_reason=t.exit_reason.value,
                gross_pnl=t.gross_pnl,
                charges=t.charges,
                slippage_cost=t.slippage_cost,
                net_pnl=t.net_pnl,
                bars_held=t.bars_held,
            )
            trade_models.append(tm)

        if trade_models:
            session.add_all(trade_models)
            session.flush()

        audit_sev = (
            AuditSeverity.WARNING
            if output.status in (BacktestStatus.FAILED_VALIDATION, BacktestStatus.INSUFFICIENT_DATA)
            else AuditSeverity.INFO
        )
        AuditService.record(
            session,
            action="BACKTEST_EXECUTED",
            summary=(
                f"Backtest {run_code} on {strat_ver.version_label} ({desc.dataset_id}) "
                f"finished with status {output.status.value} ({len(trade_models)} simulated trades)."
            ),
            actor=actor,
            actor_type=ActorType.OPERATOR,
            category=AuditCategory.BACKTEST,
            severity=audit_sev,
            entity_type="backtest_run",
            entity_id=str(run_model.id),
            details={
                "run_code": run_code,
                "strategy_version_id": str(strat_ver.id),
                "strategy_version_label": strat_ver.version_label,
                "dataset_id": desc.dataset_id,
                "data_version_hash": desc.data_version_hash,
                "reproducibility_fingerprint": fingerprint,
                "status": output.status.value,
                "total_trades": len(trade_models),
                "net_pnl": output.metrics.get("net_pnl"),
                "engine_version": output.engine_version,
            },
        )

        chart_bars = _build_chart_bars(output.validated_bars, strategy_cfg)
        return BacktestService._serialize_run_detail(run_model, trade_models, chart_bars)

    @staticmethod
    def list_backtests(
        session: Session,
        *,
        instrument: str | None = None,
        strategy_version_id: uuid.UUID | None = None,
        status: str | None = None,
        limit: int = 50,
    ) -> list[dict[str, Any]]:
        stmt = select(BacktestRunModel).order_by(BacktestRunModel.started_at.desc()).limit(limit)
        if instrument and instrument.upper() != "ALL":
            stmt = stmt.where(BacktestRunModel.instrument_symbol == instrument.strip().upper())
        if strategy_version_id is not None:
            stmt = stmt.where(BacktestRunModel.strategy_version_id == strategy_version_id)
        if status and status.upper() != "ALL":
            stmt = stmt.where(BacktestRunModel.status == status.strip().upper())

        runs = list(session.scalars(stmt).all())
        return [BacktestService._serialize_run_summary(r) for r in runs]

    @staticmethod
    def get_backtest_detail(
        session: Session,
        backtest_id: uuid.UUID,
        *,
        bundled_dir: Path | None = None,
    ) -> dict[str, Any]:
        run_model = session.get(BacktestRunModel, backtest_id)
        if run_model is None:
            raise EntityNotFoundError(f"Backtest run '{backtest_id}' not found.")

        trades = list(run_model.trades)
        chart_bars: list[dict[str, Any]] = []
        if run_model.status != BacktestStatus.FAILED_VALIDATION.value:
            try:
                strat_ver = session.get(StrategyVersionModel, run_model.strategy_version_id)
                if strat_ver is not None:
                    strategy_cfg = parse_and_validate_ema_rsi_config(strat_ver.parameters_json)
                    adapter = CompositeHistoricalDataAdapter(session, bundled_dir=bundled_dir)
                    raw_ds = adapter.load_dataset(run_model.dataset_id)
                    in_params = run_model.input_parameters_json or {}
                    s_date = (
                        date.fromisoformat(in_params["start_date"])
                        if in_params.get("start_date")
                        else None
                    )
                    e_date = (
                        date.fromisoformat(in_params["end_date"])
                        if in_params.get("end_date")
                        else None
                    )
                    assumptions = run_model.execution_assumptions_json or {}
                    bars, _ = validate_ohlcv_rows(
                        raw_ds.rows,
                        instrument=run_model.instrument_symbol,
                        timeframe=run_model.timeframe,
                        start_date=s_date,
                        end_date=e_date,
                        session_open_ist=str(assumptions.get("session_open_ist", "09:15:00")),
                        session_close_ist=str(assumptions.get("session_close_ist", "15:30:00")),
                    )
                    chart_bars = _build_chart_bars(bars, strategy_cfg)
            except Exception:  # noqa: BLE001
                chart_bars = []

        return BacktestService._serialize_run_detail(run_model, trades, chart_bars)

    @staticmethod
    def _serialize_run_summary(run: BacktestRunModel) -> dict[str, Any]:
        m = run.metrics_json or {}
        labels = classify_data_source(run.dataset_source)
        return {
            "id": run.id,
            "run_code": run.run_code,
            "simulation_disclaimer": "SIMULATED BACKTEST RESULTS",
            "strategy_version_id": run.strategy_version_id,
            "strategy_version_label": run.strategy_version_label,
            "instrument_symbol": run.instrument_symbol,
            "timeframe": run.timeframe,
            "dataset_id": run.dataset_id,
            "dataset_name": run.dataset_name,
            "dataset_source": run.dataset_source,
            "dataset_labels": labels.to_dict(),
            "data_version_hash": run.data_version_hash,
            "reproducibility_fingerprint": run.reproducibility_fingerprint,
            "status": run.status,
            "engine_version": run.engine_version,
            "total_trades": int(m.get("total_trades", 0)),
            "net_pnl": m.get("net_pnl"),
            "return_pct": m.get("return_pct"),
            "max_drawdown_pct": m.get("max_drawdown_pct"),
            "win_rate_pct": m.get("win_rate_pct"),
            "profit_factor": m.get("profit_factor"),
            "data_start_timestamp": ensure_ist(run.data_start_timestamp)
            if run.data_start_timestamp
            else None,
            "data_end_timestamp": ensure_ist(run.data_end_timestamp)
            if run.data_end_timestamp
            else None,
            "started_at": ensure_ist(run.started_at),
            "completed_at": ensure_ist(run.completed_at),
            "created_by": run.created_by,
            "correlation_id": run.correlation_id,
        }

    @staticmethod
    def _serialize_trade(t: BacktestTradeModel) -> dict[str, Any]:
        return {
            "trade_id": t.deterministic_trade_id,
            "trade_number": t.trade_number,
            "strategy_version_id": t.strategy_version_id,
            "strategy_version_label": t.strategy_version_label,
            "instrument": t.instrument_symbol,
            "direction": t.direction,
            "entry_signal_time": ensure_ist(t.entry_signal_time),
            "entry_time": ensure_ist(t.entry_time),
            "raw_entry_price": t.raw_entry_price.quantize(Decimal("0.01")),
            "entry_price": t.entry_price.quantize(Decimal("0.01")),
            "quantity": t.quantity,
            "exit_time": ensure_ist(t.exit_time),
            "raw_exit_price": t.raw_exit_price.quantize(Decimal("0.01")),
            "exit_price": t.exit_price.quantize(Decimal("0.01")),
            "exit_reason": t.exit_reason,
            "gross_pnl": t.gross_pnl.quantize(Decimal("0.01")),
            "charges": t.charges.quantize(Decimal("0.01")),
            "slippage_cost": t.slippage_cost.quantize(Decimal("0.01")),
            "net_pnl": t.net_pnl.quantize(Decimal("0.01")),
            "bars_held": t.bars_held,
        }

    @staticmethod
    def _serialize_run_detail(
        run: BacktestRunModel,
        trades: list[BacktestTradeModel],
        chart_bars: list[dict[str, Any]],
    ) -> dict[str, Any]:
        summary = BacktestService._serialize_run_summary(run)
        return {
            **summary,
            "simulation_disclaimer": "SIMULATED BACKTEST RESULTS",
            "input_parameters": run.input_parameters_json,
            "execution_assumptions": run.execution_assumptions_json,
            "data_quality_report": run.data_quality_report_json,
            "metrics": run.metrics_json,
            "equity_curve": run.equity_curve_json,
            "drawdown_curve": run.drawdown_curve_json,
            "monthly_returns": run.monthly_returns_json,
            "trades": [BacktestService._serialize_trade(t) for t in trades],
            "chart_bars": chart_bars,
        }
