"""Market Replay Engine service, normalized event dispatch, signal persistence, and worker."""

from __future__ import annotations

import asyncio
import logging
import uuid
from collections import defaultdict
from datetime import datetime, timedelta
from decimal import Decimal
from pathlib import Path
from typing import Any

from sqlalchemy import desc, func, select
from sqlalchemy.orm import Session

from core.audit.service import AuditService
from core.backtesting.data_adapter import CompositeHistoricalDataAdapter
from core.backtesting.validator import validate_ohlcv_rows
from core.clock import ensure_ist, format_ist_iso, now_ist
from core.config import AppSettings, get_settings
from core.domain.enums import (
    REPLAY_SESSION_TRANSITIONS,
    REPLAY_SPEED_MULTIPLIERS,
    ActorType,
    AuditCategory,
    AuditEventType,
    AuditSeverity,
    MarketDataFreshnessStatus,
    MarketFeedSource,
    ReplaySessionStatus,
    ReplaySpeed,
    SignalDirection,
    SignalStatus,
    StrategyEvaluationState,
    StrategyInstanceStatus,
    validate_state_transition,
)
from core.exceptions import ControlCenterError, EntityNotFoundError
from core.logging import get_correlation_id, reset_correlation_id, set_correlation_id
from core.market_data.events import CandleEvent, HistoricalCandleReplayFeed
from core.market_data.freshness import (
    MarketDataFreshnessSnapshot,
    enforce_fresh_market_data_for_new_order,
    evaluate_market_data_freshness,
)
from core.market_data.models import DataSourceType, classify_data_source
from core.persistence.models import (
    AccountRiskConfigModel,
    InstrumentModel,
    PaperAccountModel,
    ReplaySessionModel,
    ReplaySignalModel,
    StrategyInstanceModel,
    StrategyVersionModel,
)
from core.persistence.uow import unit_of_work
from core.strategies.base import OHLCVBar
from core.strategies.config_schema import EmaRsiStrategyConfig, parse_and_validate_ema_rsi_config
from core.strategies.ema_rsi import EmaRsiCrossoverStrategy
from core.strategies.indicators import compute_ema_series, compute_rsi_series

logger = logging.getLogger(__name__)


def _parse_replay_speed(raw_speed: str) -> ReplaySpeed:
    try:
        return ReplaySpeed(raw_speed.strip().lower())
    except ValueError as exc:
        allowed = ", ".join(s.value for s in ReplaySpeed)
        raise ControlCenterError(
            f"Unsupported replay speed {raw_speed!r}. Allowed speeds: {allowed}.",
            code="INVALID_REPLAY_SPEED",
        ) from exc


class MarketReplayService:
    """Stateless domain service driving Market Replay sessions and Strategy signal generation."""

    @staticmethod
    def _load_validated_dataset_bars(
        session: Session,
        *,
        dataset_id: str,
        instrument: str,
        timeframe: str,
        bundled_dir: Path | None = None,
    ) -> tuple[Any, list[OHLCVBar]]:
        inst = session.scalar(
            select(InstrumentModel).where(InstrumentModel.symbol == instrument.strip().upper())
        )
        open_ist = inst.session_open_ist if inst else "09:15:00"
        close_ist = inst.session_close_ist if inst else "15:30:00"

        adapter = CompositeHistoricalDataAdapter(session, bundled_dir=bundled_dir)
        raw_ds = adapter.load_dataset(dataset_id)
        if raw_ds.descriptor.instrument.upper() != instrument.strip().upper():
            raise ControlCenterError(
                f"Dataset '{dataset_id}' belongs to '{raw_ds.descriptor.instrument}', "
                f"expected '{instrument}'.",
                code="REPLAY_INSTRUMENT_MISMATCH",
            )
        if raw_ds.descriptor.quality_status in (
            "INVALID",
            "DOWNLOAD_FAILED",
            "AUTHENTICATION_REQUIRED",
        ):
            raise ControlCenterError(
                f"Dataset '{dataset_id}' has quality status '{raw_ds.descriptor.quality_status}' "
                "and cannot be replayed.",
                code="REPLAY_DATASET_INVALID",
            )
        bars, dq = validate_ohlcv_rows(
            raw_ds.rows,
            instrument=instrument,
            timeframe=timeframe,
            session_open_ist=open_ist,
            session_close_ist=close_ist,
        )
        if not dq.passed_validation or not bars:
            raise ControlCenterError(
                f"Dataset '{dataset_id}' failed structural validation or contains 0 valid candles.",
                code="REPLAY_DATASET_INVALID",
            )
        return raw_ds.descriptor, bars

    @staticmethod
    def _partition_warmup_and_session_bars(
        all_bars: list[OHLCVBar],
        *,
        historical_session_date: str,
        warmup_cap: int,
    ) -> tuple[list[OHLCVBar], list[OHLCVBar]]:
        clean_date = (historical_session_date or "ALL_SESSIONS").strip()
        if clean_date.upper() == "ALL_SESSIONS":
            return [], all_bars

        prior_bars: list[OHLCVBar] = []
        session_bars: list[OHLCVBar] = []
        for b in all_bars:
            d_str = b.bar_timestamp.date().isoformat()
            if d_str < clean_date:
                prior_bars.append(b)
            elif d_str == clean_date:
                session_bars.append(b)

        if not session_bars:
            raise ControlCenterError(
                f"Historical session date '{clean_date}' has 0 candles in the selected dataset.",
                code="EMPTY_HISTORICAL_SESSION",
            )
        warmup_slice = prior_bars[-warmup_cap:] if warmup_cap > 0 else []
        return warmup_slice, session_bars

    @staticmethod
    def get_replay_catalog(
        session: Session,
        *,
        instrument: str = "NIFTY",
        bundled_dir: Path | None = None,
    ) -> dict[str, Any]:
        clean_inst = instrument.strip().upper()
        versions = list(
            session.scalars(
                select(StrategyVersionModel)
                .where(StrategyVersionModel.instrument_symbol == clean_inst)
                .order_by(StrategyVersionModel.version_number.desc())
            ).all()
        )

        adapter = CompositeHistoricalDataAdapter(session, bundled_dir=bundled_dir)
        datasets = adapter.list_datasets(instrument=clean_inst)
        catalog_datasets: list[dict[str, Any]] = []

        for ds in datasets:
            sessions_out: list[dict[str, Any]] = []
            try:
                _, bars = MarketReplayService._load_validated_dataset_bars(
                    session,
                    dataset_id=ds.dataset_id,
                    instrument=clean_inst,
                    timeframe=ds.timeframe,
                    bundled_dir=bundled_dir,
                )
                by_date: dict[str, list[OHLCVBar]] = defaultdict(list)
                for b in bars:
                    by_date[b.bar_timestamp.date().isoformat()].append(b)

                sessions_out.append(
                    {
                        "session_key": "ALL_SESSIONS",
                        "label": f"All Trading Sessions ({len(bars)} candles • {min(by_date)} .. {max(by_date)})",
                        "candle_count": len(bars),
                        "has_prior_warmup": False,
                    }
                )
                sorted_dates = sorted(by_date.keys())
                for idx, d_key in enumerate(sorted_dates):
                    day_bars = by_date[d_key]
                    t0 = day_bars[0].bar_timestamp.strftime("%H:%M")
                    t1 = day_bars[-1].bar_timestamp.strftime("%H:%M")
                    sessions_out.append(
                        {
                            "session_key": d_key,
                            "label": f"{d_key} IST Session ({len(day_bars)} candles • {t0}–{t1} IST)",
                            "candle_count": len(day_bars),
                            "has_prior_warmup": idx > 0,
                        }
                    )
            except ControlCenterError:
                continue

            labels = classify_data_source(ds.source_type, ds.provider)
            catalog_datasets.append(
                {
                    "dataset_id": ds.dataset_id,
                    "name": ds.name,
                    "instrument": ds.instrument,
                    "timeframe": ds.timeframe,
                    "source_type": ds.source_type,
                    "provider": ds.provider,
                    "provider_symbol": ds.provider_symbol,
                    "quality_status": ds.quality_status,
                    "is_complete": ds.is_complete,
                    "row_count": ds.row_count,
                    "data_version_hash": ds.data_version_hash,
                    "retrieved_at": format_ist_iso(ds.retrieved_at) if ds.retrieved_at else None,
                    "labels": labels.to_dict(),
                    "historical_sessions": sessions_out,
                }
            )

        return {
            "instrument": clean_inst,
            "supported_instruments": ["NIFTY", "BANKNIFTY"],
            "supported_speeds": [s.value for s in ReplaySpeed],
            "strategy_versions": [
                {
                    "id": str(v.id),
                    "version_label": v.version_label,
                    "instrument_symbol": v.instrument_symbol,
                    "timeframe": v.timeframe,
                    "status": v.status,
                    "parameters": v.parameters_json,
                }
                for v in versions
            ],
            "datasets": catalog_datasets,
        }

    @staticmethod
    def start_session(
        session: Session,
        *,
        instrument: str,
        strategy_version_id: uuid.UUID,
        dataset_id: str,
        historical_session_date: str = "ALL_SESSIONS",
        replay_speed: str = "1x",
        actor: str = "desk.operator",
        settings: AppSettings | None = None,
        bundled_dir: Path | None = None,
        initial_step_bars: int = 1,
    ) -> dict[str, Any]:
        cfg = settings or get_settings()
        speed_enum = _parse_replay_speed(replay_speed)
        clean_inst = instrument.strip().upper()

        strat_ver = session.get(StrategyVersionModel, strategy_version_id)
        if strat_ver is None:
            raise EntityNotFoundError(f"StrategyVersion '{strategy_version_id}' not found.")
        if strat_ver.instrument_symbol.upper() != clean_inst:
            raise ControlCenterError(
                f"StrategyVersion '{strat_ver.version_label}' targets '{strat_ver.instrument_symbol}', "
                f"not '{clean_inst}'.",
                code="REPLAY_STRATEGY_INSTRUMENT_MISMATCH",
            )

        strategy_cfg = parse_and_validate_ema_rsi_config(strat_ver.parameters_json)
        strategy = EmaRsiCrossoverStrategy(strategy_cfg)

        desc_meta, all_bars = MarketReplayService._load_validated_dataset_bars(
            session,
            dataset_id=dataset_id,
            instrument=clean_inst,
            timeframe=strat_ver.timeframe,
            bundled_dir=bundled_dir,
        )
        warmup_bars, session_bars = MarketReplayService._partition_warmup_and_session_bars(
            all_bars,
            historical_session_date=historical_session_date,
            warmup_cap=max(60, strategy.warmup_bars_required + 10),
        )

        # Cleanly stop any previously active replay session so only 1 stream owns the desk
        active_others = list(
            session.scalars(
                select(ReplaySessionModel).where(
                    ReplaySessionModel.status.in_(
                        [
                            ReplaySessionStatus.RUNNING.value,
                            ReplaySessionStatus.PAUSED.value,
                            ReplaySessionStatus.PAUSED_RECOVERY.value,
                            ReplaySessionStatus.CONFIGURED.value,
                        ]
                    )
                )
            ).all()
        )
        ts_now = now_ist()
        for prev in active_others:
            prev.status = ReplaySessionStatus.STOPPED.value
            prev.stopped_at = ts_now
            prev.updated_at = ts_now

        existing_cnt = int(
            session.scalar(select(func.count()).select_from(ReplaySessionModel)) or 0
        )
        session_code = (
            f"RPL-{clean_inst}-{ts_now.strftime('%Y%m%d')}-{existing_cnt + 1:04d}-"
            f"{uuid.uuid4().hex[:4].upper()}"
        )

        risk_cfg_row = session.scalar(select(AccountRiskConfigModel).limit(1))
        eff_stale_sec = (
            int(risk_cfg_row.market_data_stale_threshold_seconds)
            if risk_cfg_row is not None
            else cfg.MARKET_DATA_STALE_THRESHOLD_SECONDS
        )
        acct_row = session.scalar(
            select(PaperAccountModel).where(
                PaperAccountModel.account_code == cfg.DEFAULT_PAPER_ACCOUNT_CODE
            )
        )
        halted_now = bool(acct_row.paper_trading_halted) if acct_row is not None else False
        running_inst_id = session.scalar(
            select(StrategyInstanceModel.id).where(
                StrategyInstanceModel.instrument_symbol == clean_inst,
                StrategyInstanceModel.strategy_version_id == strat_ver.id,
                StrategyInstanceModel.status == StrategyInstanceStatus.RUNNING.value,
            )
        )
        src_labels = classify_data_source(desc_meta.source_type, desc_meta.provider)

        model = ReplaySessionModel(
            session_code=session_code,
            instrument_symbol=clean_inst,
            timeframe=strat_ver.timeframe,
            strategy_version_id=strat_ver.id,
            strategy_version_label=strat_ver.version_label,
            dataset_id=desc_meta.dataset_id,
            dataset_name=desc_meta.name,
            dataset_source=desc_meta.source_type,
            dataset_provider=desc_meta.provider,
            data_version_hash=desc_meta.data_version_hash,
            strategy_instance_id=running_inst_id,
            correlation_id=get_correlation_id(),
            paper_trading_halted_snapshot=halted_now,
            historical_session_date=historical_session_date.strip() or "ALL_SESSIONS",
            warmup_bars_count=len(warmup_bars),
            total_session_candles=len(session_bars),
            candles_processed=0,
            signals_emitted=0,
            replay_speed=speed_enum.value,
            status=ReplaySessionStatus.RUNNING.value,
            stale_threshold_seconds=eff_stale_sec,
            started_at=ts_now,
            last_step_wall_clock=ts_now,
            fractional_bar_carry=Decimal("0"),
            created_by=actor,
            created_at=ts_now,
            updated_at=ts_now,
        )
        session.add(model)
        session.flush()

        replay_audit_details = {
            "session_code": session_code,
            "instrument": clean_inst,
            "strategy_version_label": strat_ver.version_label,
            "dataset_id": desc_meta.dataset_id,
            "dataset_source": desc_meta.source_type,
            "historical_session_date": model.historical_session_date,
            "replay_speed": speed_enum.value,
            "total_session_candles": len(session_bars),
            "warmup_bars_count": len(warmup_bars),
            "data_provider": src_labels.audit_provider,
            "data_type": src_labels.audit_data_type,
            "execution_type": src_labels.audit_execution_type,
        }

        AuditService.record(
            session,
            action=AuditEventType.HISTORICAL_DATASET_SELECTED_FOR_REPLAY.value,
            event_type=AuditEventType.HISTORICAL_DATASET_SELECTED_FOR_REPLAY.value,
            summary=(
                f"Selected dataset '{desc_meta.dataset_id}' ({src_labels.ui_headline}) "
                f"for Market Replay {session_code}."
            ),
            actor=actor,
            actor_type=ActorType.OPERATOR,
            category=AuditCategory.MARKET_DATA,
            severity=AuditSeverity.INFO,
            entity_type="historical_dataset",
            entity_id=desc_meta.dataset_id,
            source="market_replay.engine",
            details=replay_audit_details,
        )

        AuditService.record(
            session,
            action="REPLAY_SESSION_STARTED",
            summary=(
                f"Started Market Replay {session_code} on {clean_inst} "
                f"({model.historical_session_date}, {speed_enum.value}, {strat_ver.version_label})."
            ),
            actor=actor,
            actor_type=ActorType.OPERATOR,
            category=AuditCategory.MARKET_DATA,
            severity=AuditSeverity.INFO,
            entity_type="replay_session",
            entity_id=str(model.id),
            details=replay_audit_details,
        )

        if desc_meta.source_type == DataSourceType.FYERS_HISTORICAL.value:
            AuditService.record(
                session,
                action=AuditEventType.REAL_DATA_REPLAY_STARTED.value,
                event_type=AuditEventType.REAL_DATA_REPLAY_STARTED.value,
                summary=(
                    f"Started Real Historical FYERS Market Replay {session_code} on {clean_inst} "
                    "(Execution: SIMULATED PAPER)."
                ),
                actor=actor,
                actor_type=ActorType.OPERATOR,
                category=AuditCategory.MARKET_DATA,
                severity=AuditSeverity.INFO,
                entity_type="replay_session",
                entity_id=str(model.id),
                source="market_replay.engine",
                details=replay_audit_details,
            )

        if initial_step_bars > 0:
            MarketReplayService._advance_bars_internal(
                session,
                model=model,
                strategy_cfg=strategy_cfg,
                warmup_bars=warmup_bars,
                session_bars=session_bars,
                bars_to_step=initial_step_bars,
                cfg=cfg,
            )

        return MarketReplayService.get_replay_state(
            session, session_id=model.id, settings=cfg, bundled_dir=bundled_dir
        )

    @staticmethod
    def pause_session(
        session: Session,
        session_id: uuid.UUID,
        *,
        actor: str = "desk.operator",
        settings: AppSettings | None = None,
        bundled_dir: Path | None = None,
    ) -> dict[str, Any]:
        cfg = settings or get_settings()
        model = MarketReplayService._get_session_or_404(session, session_id)
        validate_state_transition(
            "ReplaySession",
            ReplaySessionStatus(model.status),
            ReplaySessionStatus.PAUSED,
            REPLAY_SESSION_TRANSITIONS,
        )
        ts_now = now_ist()
        model.status = ReplaySessionStatus.PAUSED.value
        model.paused_at = ts_now
        model.fractional_bar_carry = Decimal("0")
        model.updated_at = ts_now
        session.flush()

        src_labels = classify_data_source(model.dataset_source, model.dataset_provider)
        pause_details = {
            "candles_processed": model.candles_processed,
            "dataset_id": model.dataset_id,
            "data_provider": src_labels.audit_provider,
            "data_type": src_labels.audit_data_type,
            "execution_type": src_labels.audit_execution_type,
        }
        AuditService.record(
            session,
            action="REPLAY_SESSION_PAUSED",
            summary=f"Paused Market Replay {model.session_code} at candle {model.candles_processed}/{model.total_session_candles}.",
            actor=actor,
            actor_type=ActorType.OPERATOR,
            category=AuditCategory.MARKET_DATA,
            severity=AuditSeverity.INFO,
            entity_type="replay_session",
            entity_id=str(model.id),
            details=pause_details,
        )
        if model.dataset_source == DataSourceType.FYERS_HISTORICAL.value:
            AuditService.record(
                session,
                action=AuditEventType.REAL_DATA_REPLAY_PAUSED.value,
                event_type=AuditEventType.REAL_DATA_REPLAY_PAUSED.value,
                summary=f"Paused Real Historical FYERS Market Replay {model.session_code}.",
                actor=actor,
                actor_type=ActorType.OPERATOR,
                category=AuditCategory.MARKET_DATA,
                severity=AuditSeverity.INFO,
                entity_type="replay_session",
                entity_id=str(model.id),
                source="market_replay.engine",
                details=pause_details,
            )
        AuditService.record(
            session,
            action="MARKET_DATA_STALE",
            event_type="MARKET_DATA_STALE",
            summary=f"Market Replay {model.session_code} paused — feed marked STALE for pre-trade execution safety.",
            actor="market_data.monitor",
            actor_type=ActorType.SYSTEM,
            category=AuditCategory.MARKET_DATA,
            severity=AuditSeverity.WARNING,
            entity_type="replay_session",
            entity_id=str(model.id),
            source="market_data.monitor",
            details={"status": "STALE", "session_code": model.session_code},
        )
        return MarketReplayService.get_replay_state(
            session, session_id=model.id, settings=cfg, bundled_dir=bundled_dir
        )

    @staticmethod
    def resume_session(
        session: Session,
        session_id: uuid.UUID,
        *,
        actor: str = "desk.operator",
        settings: AppSettings | None = None,
        bundled_dir: Path | None = None,
    ) -> dict[str, Any]:
        cfg = settings or get_settings()
        model = MarketReplayService._get_session_or_404(session, session_id)
        curr_status = ReplaySessionStatus(model.status)
        validate_state_transition(
            "ReplaySession",
            curr_status,
            ReplaySessionStatus.RUNNING,
            REPLAY_SESSION_TRANSITIONS,
        )
        ts_now = now_ist()
        model.status = ReplaySessionStatus.RUNNING.value
        model.last_step_wall_clock = ts_now
        model.fractional_bar_carry = Decimal("0")
        model.updated_at = ts_now
        session.flush()

        # Emit 1 candle on resume if remaining so market-data freshness immediately returns to HEALTHY
        if model.candles_processed < model.total_session_candles:
            MarketReplayService.step_session(
                session,
                model.id,
                bars=1,
                actor=actor,
                settings=cfg,
                bundled_dir=bundled_dir,
            )

        AuditService.record(
            session,
            action="REPLAY_SESSION_RESUMED",
            summary=f"Resumed Market Replay {model.session_code} at {model.replay_speed}.",
            actor=actor,
            actor_type=ActorType.OPERATOR,
            category=AuditCategory.MARKET_DATA,
            severity=AuditSeverity.INFO,
            entity_type="replay_session",
            entity_id=str(model.id),
            details={"replay_speed": model.replay_speed, "candles_processed": model.candles_processed},
        )
        AuditService.record(
            session,
            action="MARKET_DATA_RECOVERED",
            event_type="MARKET_DATA_RECOVERED",
            summary=f"Market Replay {model.session_code} resumed — feed recovered to HEALTHY.",
            actor="market_data.monitor",
            actor_type=ActorType.SYSTEM,
            category=AuditCategory.MARKET_DATA,
            severity=AuditSeverity.INFO,
            entity_type="replay_session",
            entity_id=str(model.id),
            source="market_data.monitor",
            details={"status": "HEALTHY", "session_code": model.session_code},
        )
        return MarketReplayService.get_replay_state(
            session, session_id=model.id, settings=cfg, bundled_dir=bundled_dir
        )

    @staticmethod
    def stop_session(
        session: Session,
        session_id: uuid.UUID,
        *,
        actor: str = "desk.operator",
        settings: AppSettings | None = None,
        bundled_dir: Path | None = None,
    ) -> dict[str, Any]:
        cfg = settings or get_settings()
        model = MarketReplayService._get_session_or_404(session, session_id)
        curr = ReplaySessionStatus(model.status)
        if curr != ReplaySessionStatus.STOPPED:
            if curr == ReplaySessionStatus.COMPLETED:
                # Completed is already terminal; keep truthful state
                pass
            else:
                validate_state_transition(
                    "ReplaySession",
                    curr,
                    ReplaySessionStatus.STOPPED,
                    REPLAY_SESSION_TRANSITIONS,
                )
                ts_now = now_ist()
                model.status = ReplaySessionStatus.STOPPED.value
                model.stopped_at = ts_now
                model.fractional_bar_carry = Decimal("0")
                model.updated_at = ts_now
                session.flush()

                AuditService.record(
                    session,
                    action="REPLAY_SESSION_STOPPED",
                    summary=f"Stopped Market Replay {model.session_code} ({model.candles_processed}/{model.total_session_candles} candles).",
                    actor=actor,
                    actor_type=ActorType.OPERATOR,
                    category=AuditCategory.MARKET_DATA,
                    severity=AuditSeverity.INFO,
                    entity_type="replay_session",
                    entity_id=str(model.id),
                    details={"candles_processed": model.candles_processed},
                )
        return MarketReplayService.get_replay_state(
            session, session_id=model.id, settings=cfg, bundled_dir=bundled_dir
        )

    @staticmethod
    def reset_session(
        session: Session,
        session_id: uuid.UUID,
        *,
        actor: str = "desk.operator",
        settings: AppSettings | None = None,
        bundled_dir: Path | None = None,
    ) -> dict[str, Any]:
        """Reset replay cursor cleanly to 0 (`CONFIGURED`) while preserving prior audit/signals."""
        cfg = settings or get_settings()
        old = MarketReplayService._get_session_or_404(session, session_id)
        ts_now = now_ist()

        if old.status not in (
            ReplaySessionStatus.STOPPED.value,
            ReplaySessionStatus.COMPLETED.value,
        ):
            old.status = ReplaySessionStatus.STOPPED.value
            old.stopped_at = ts_now
            old.updated_at = ts_now

        existing_cnt = int(
            session.scalar(select(func.count()).select_from(ReplaySessionModel)) or 0
        )
        new_code = (
            f"RPL-{old.instrument_symbol}-{ts_now.strftime('%Y%m%d')}-{existing_cnt + 1:04d}-"
            f"{uuid.uuid4().hex[:4].upper()}"
        )
        fresh = ReplaySessionModel(
            session_code=new_code,
            instrument_symbol=old.instrument_symbol,
            timeframe=old.timeframe,
            strategy_version_id=old.strategy_version_id,
            strategy_version_label=old.strategy_version_label,
            dataset_id=old.dataset_id,
            dataset_name=old.dataset_name,
            dataset_source=old.dataset_source,
            dataset_provider=old.dataset_provider,
            data_version_hash=old.data_version_hash,
            strategy_instance_id=old.strategy_instance_id,
            correlation_id=get_correlation_id(),
            paper_trading_halted_snapshot=old.paper_trading_halted_snapshot,
            historical_session_date=old.historical_session_date,
            warmup_bars_count=old.warmup_bars_count,
            total_session_candles=old.total_session_candles,
            candles_processed=0,
            signals_emitted=0,
            replay_speed=old.replay_speed,
            status=ReplaySessionStatus.CONFIGURED.value,
            stale_threshold_seconds=old.stale_threshold_seconds,
            fractional_bar_carry=Decimal("0"),
            created_by=actor,
            created_at=ts_now,
            updated_at=ts_now,
        )
        session.add(fresh)
        session.flush()

        AuditService.record(
            session,
            action="REPLAY_SESSION_RESET",
            summary=(
                f"Reset Market Replay {old.session_code} -> {new_code} "
                f"(0/{fresh.total_session_candles} candles, CONFIGURED)."
            ),
            actor=actor,
            actor_type=ActorType.OPERATOR,
            category=AuditCategory.MARKET_DATA,
            severity=AuditSeverity.INFO,
            entity_type="replay_session",
            entity_id=str(fresh.id),
            details={
                "previous_session_id": str(old.id),
                "new_session_code": new_code,
            },
        )
        return MarketReplayService.get_replay_state(
            session, session_id=fresh.id, settings=cfg, bundled_dir=bundled_dir
        )

    @staticmethod
    def update_speed(
        session: Session,
        session_id: uuid.UUID,
        *,
        replay_speed: str,
        actor: str = "desk.operator",
        settings: AppSettings | None = None,
        bundled_dir: Path | None = None,
    ) -> dict[str, Any]:
        cfg = settings or get_settings()
        speed_enum = _parse_replay_speed(replay_speed)
        model = MarketReplayService._get_session_or_404(session, session_id)
        prev_speed = model.replay_speed
        ts_now = now_ist()
        model.replay_speed = speed_enum.value
        model.last_step_wall_clock = ts_now
        model.fractional_bar_carry = Decimal("0")
        model.updated_at = ts_now
        session.flush()

        AuditService.record(
            session,
            action="REPLAY_SPEED_UPDATED",
            summary=f"Changed Market Replay {model.session_code} speed {prev_speed} -> {speed_enum.value}.",
            actor=actor,
            actor_type=ActorType.OPERATOR,
            category=AuditCategory.MARKET_DATA,
            severity=AuditSeverity.INFO,
            entity_type="replay_session",
            entity_id=str(model.id),
            details={"previous_speed": prev_speed, "new_speed": speed_enum.value},
        )
        return MarketReplayService.get_replay_state(
            session, session_id=model.id, settings=cfg, bundled_dir=bundled_dir
        )

    @staticmethod
    def step_session(
        session: Session,
        session_id: uuid.UUID,
        *,
        bars: int = 1,
        actor: str = "desk.operator",
        settings: AppSettings | None = None,
        bundled_dir: Path | None = None,
    ) -> dict[str, Any]:
        """Deterministically emit `bars` normalized CandleEvents through the Strategy Engine."""
        if bars < 1:
            raise ControlCenterError("Step count must be >= 1.", code="INVALID_STEP_COUNT")
        cfg = settings or get_settings()
        model = MarketReplayService._get_session_or_404(session, session_id)

        if model.status in (
            ReplaySessionStatus.STOPPED.value,
            ReplaySessionStatus.COMPLETED.value,
        ):
            raise ControlCenterError(
                f"Cannot step ReplaySession in terminal state '{model.status}'.",
                code="REPLAY_SESSION_TERMINAL",
            )
        if model.status == ReplaySessionStatus.CONFIGURED.value:
            model.status = ReplaySessionStatus.PAUSED.value
            model.started_at = model.started_at or now_ist()

        strat_ver = session.get(StrategyVersionModel, model.strategy_version_id)
        if strat_ver is None:
            raise EntityNotFoundError("Associated StrategyVersion not found.")
        strategy_cfg = parse_and_validate_ema_rsi_config(strat_ver.parameters_json)
        strategy = EmaRsiCrossoverStrategy(strategy_cfg)

        _, all_bars = MarketReplayService._load_validated_dataset_bars(
            session,
            dataset_id=model.dataset_id,
            instrument=model.instrument_symbol,
            timeframe=model.timeframe,
            bundled_dir=bundled_dir,
        )
        warmup_bars, session_bars = MarketReplayService._partition_warmup_and_session_bars(
            all_bars,
            historical_session_date=model.historical_session_date,
            warmup_cap=max(60, strategy.warmup_bars_required + 10),
        )

        MarketReplayService._advance_bars_internal(
            session,
            model=model,
            strategy_cfg=strategy_cfg,
            warmup_bars=warmup_bars,
            session_bars=session_bars,
            bars_to_step=bars,
            cfg=cfg,
        )
        return MarketReplayService.get_replay_state(
            session, session_id=model.id, settings=cfg, bundled_dir=bundled_dir
        )

    @staticmethod
    def _advance_bars_internal(
        session: Session,
        *,
        model: ReplaySessionModel,
        strategy_cfg: EmaRsiStrategyConfig,
        warmup_bars: list[OHLCVBar],
        session_bars: list[OHLCVBar],
        bars_to_step: int,
        cfg: AppSettings,
    ) -> int:
        strategy = EmaRsiCrossoverStrategy(strategy_cfg)
        feed = HistoricalCandleReplayFeed(
            session_bars,
            symbol=model.instrument_symbol,
            timeframe=model.timeframe,
            initial_cursor=model.candles_processed,
            data_source_type=model.dataset_source,
            data_provider=model.dataset_provider,
            dataset_id=model.dataset_id,
        )
        if not feed.has_next():
            if model.status == ReplaySessionStatus.RUNNING.value:
                ts_done = now_ist()
                model.status = ReplaySessionStatus.COMPLETED.value
                model.completed_at = ts_done
                model.updated_at = ts_done
                session.flush()
            return 0

        acct = session.scalar(
            select(PaperAccountModel).where(
                PaperAccountModel.account_code == cfg.DEFAULT_PAPER_ACCOUNT_CODE
            )
        )
        paper_halted = bool(acct.paper_trading_halted) if acct is not None else False
        model.paper_trading_halted_snapshot = paper_halted
        model.correlation_id = get_correlation_id()

        # Reconstruct normalized warmup + already-emitted session CandleEvents
        warmup_events = [
            CandleEvent.from_ohlcv_bar(
                wb,
                source=MarketFeedSource.HISTORICAL_REPLAY,
                sequence_number=0,
                received_at=now_ist(),
                data_source_type=model.dataset_source,
                data_provider=model.dataset_provider,
                dataset_id=model.dataset_id,
            )
            for wb in warmup_bars
        ]
        stream_buffer: list[CandleEvent] = warmup_events + feed.emitted_events_history()

        # Track daily emitted entry signals for max_trades_per_day gate
        existing_entry_signals = list(
            session.scalars(
                select(ReplaySignalModel).where(
                    ReplaySignalModel.replay_session_id == model.id,
                    ReplaySignalModel.direction.in_(
                        [SignalDirection.ENTRY_LONG.value, SignalDirection.ENTRY_SHORT.value]
                    ),
                )
            ).all()
        )
        signals_by_day: dict[str, int] = defaultdict(int)
        for s in existing_entry_signals:
            signals_by_day[ensure_ist(s.bar_timestamp).date().isoformat()] += 1

        from core.domain.enums import StrategyInstanceStatus
        from core.paper_trading.service import PaperTradingService

        stepped = 0
        for _ in range(bars_to_step):
            recv_ts = now_ist()
            candle_ev = feed.next_candle_event(received_at=recv_ts)
            if candle_ev is None:
                break

            stream_buffer.append(candle_ev)
            bar_day_str = candle_ev.exchange_timestamp.date().isoformat()
            eval_result = strategy.inspect_candle_events(
                stream_buffer,
                open_position=None,
                trades_taken_today=signals_by_day[bar_day_str],
                paper_trading_halted=paper_halted,
            )

            model.candles_processed = feed.cursor
            model.replay_clock_ist = candle_ev.exchange_timestamp
            model.last_candle_received_at = recv_ts
            model.last_step_wall_clock = recv_ts
            model.last_candle_json = candle_ev.to_dict()
            model.latest_evaluation_json = eval_result.to_dict()
            model.updated_at = recv_ts

            live_fresh = evaluate_market_data_freshness(
                session_status=model.status,
                last_candle_received_at=recv_ts,
                last_exchange_timestamp=candle_ev.exchange_timestamp,
                stale_threshold_seconds=model.stale_threshold_seconds,
                reference_time=recv_ts,
            )
            pt_out = PaperTradingService.on_normalized_candle_event(
                session,
                candle=candle_ev,
                stream_history=stream_buffer,
                replay_session=model,
                freshness=live_fresh,
                settings=cfg,
            )
            if pt_out["instance_signals"] > 0:
                model.signals_emitted += int(pt_out["instance_signals"])
                latest_inst_sig = session.scalar(
                    select(ReplaySignalModel)
                    .where(ReplaySignalModel.replay_session_id == model.id)
                    .order_by(desc(ReplaySignalModel.emitted_at))
                    .limit(1)
                )
                if latest_inst_sig is not None:
                    model.latest_signal_json = MarketReplayService._serialize_signal(
                        latest_inst_sig
                    )

            matching_running_inst = session.scalar(
                select(StrategyInstanceModel.id).where(
                    StrategyInstanceModel.instrument_symbol == model.instrument_symbol,
                    StrategyInstanceModel.strategy_version_id == model.strategy_version_id,
                    StrategyInstanceModel.status == StrategyInstanceStatus.RUNNING.value,
                )
            )

            if matching_running_inst is None and eval_result.signal is not None:
                sig = eval_result.signal
                dedup_key = (
                    f"{model.id}:{model.strategy_version_id}:{sig.symbol}:"
                    f"{format_ist_iso(sig.bar_timestamp)}:{sig.direction.value}"
                )
                already = session.scalar(
                    select(ReplaySignalModel).where(
                        ReplaySignalModel.deduplication_key == dedup_key
                    )
                )
                if already is None:
                    sig_code = f"SIG-{sig.symbol}-{recv_ts.strftime('%H%M%S')}-{uuid.uuid4().hex[:5].upper()}"
                    expires_at = recv_ts + timedelta(seconds=model.stale_threshold_seconds)
                    sig_row = ReplaySignalModel(
                        signal_code=sig_code,
                        deduplication_key=dedup_key,
                        replay_session_id=model.id,
                        strategy_version_id=model.strategy_version_id,
                        strategy_version_label=model.strategy_version_label,
                        instrument_symbol=sig.symbol,
                        timeframe=sig.timeframe,
                        side=sig.side.value,
                        direction=sig.direction.value,
                        exit_reason=sig.exit_reason.value if sig.exit_reason else None,
                        bar_timestamp=sig.bar_timestamp,
                        emitted_at=recv_ts,
                        expires_at=expires_at,
                        reference_price=sig.reference_price,
                        status=SignalStatus.GENERATED.value,
                        rationale=sig.rationale,
                        indicators_json=sig.indicators,
                        market_event_id=str(candle_ev.event_id),
                        correlation_id=get_correlation_id(),
                    )
                    session.add(sig_row)
                    session.flush()

                    model.signals_emitted += 1
                    if sig.direction in (SignalDirection.ENTRY_LONG, SignalDirection.ENTRY_SHORT):
                        signals_by_day[bar_day_str] += 1

                    model.latest_signal_json = MarketReplayService._serialize_signal(sig_row)

                    AuditService.record(
                        session,
                        action="STRATEGY_SIGNAL_GENERATED",
                        summary=(
                            f"Replay {model.session_code} emitted {sig.direction.value} on "
                            f"{sig.symbol} @ {sig.reference_price:.2f} ({format_ist_iso(sig.bar_timestamp)})."
                        ),
                        actor="strategy.engine",
                        actor_type=ActorType.SYSTEM,
                        category=AuditCategory.STRATEGY,
                        severity=AuditSeverity.INFO,
                        entity_type="signal",
                        entity_id=str(sig_row.id),
                        details={
                            "signal_code": sig_code,
                            "replay_session_code": model.session_code,
                            "strategy_version_label": model.strategy_version_label,
                            "direction": sig.direction.value,
                            "side": sig.side.value,
                            "reference_price": str(sig.reference_price),
                            "bar_timestamp": format_ist_iso(sig.bar_timestamp),
                            "market_event_id": str(candle_ev.event_id),
                        },
                    )

            stepped += 1

        if not feed.has_next() and model.status == ReplaySessionStatus.RUNNING.value:
            done_ts = now_ist()
            model.status = ReplaySessionStatus.COMPLETED.value
            model.completed_at = done_ts
            model.fractional_bar_carry = Decimal("0")
            model.updated_at = done_ts
            src_labels = classify_data_source(model.dataset_source, model.dataset_provider)
            comp_details = {
                "candles_processed": model.candles_processed,
                "signals_emitted": model.signals_emitted,
                "dataset_id": model.dataset_id,
                "data_provider": src_labels.audit_provider,
                "data_type": src_labels.audit_data_type,
                "execution_type": src_labels.audit_execution_type,
            }
            AuditService.record(
                session,
                action="REPLAY_SESSION_COMPLETED",
                summary=(
                    f"Completed Market Replay {model.session_code} "
                    f"({model.candles_processed}/{model.total_session_candles} candles, "
                    f"{model.signals_emitted} signals)."
                ),
                actor="market_replay.engine",
                actor_type=ActorType.SYSTEM,
                category=AuditCategory.MARKET_DATA,
                severity=AuditSeverity.INFO,
                entity_type="replay_session",
                entity_id=str(model.id),
                details=comp_details,
            )
            if model.dataset_source == DataSourceType.FYERS_HISTORICAL.value:
                AuditService.record(
                    session,
                    action=AuditEventType.REAL_DATA_REPLAY_COMPLETED.value,
                    event_type=AuditEventType.REAL_DATA_REPLAY_COMPLETED.value,
                    summary=(
                        f"Completed Real Historical FYERS Market Replay {model.session_code} "
                        f"({model.candles_processed}/{model.total_session_candles} candles)."
                    ),
                    actor="market_replay.engine",
                    actor_type=ActorType.SYSTEM,
                    category=AuditCategory.MARKET_DATA,
                    severity=AuditSeverity.INFO,
                    entity_type="replay_session",
                    entity_id=str(model.id),
                    source="market_replay.engine",
                    details=comp_details,
                )

        session.flush()
        return stepped

    @staticmethod
    def advance_running_sessions_by_wall_clock(
        session: Session,
        *,
        settings: AppSettings | None = None,
        bundled_dir: Path | None = None,
    ) -> int:
        """Advance any RUNNING replay sessions based on elapsed wall-clock time & speed."""
        cfg = settings or get_settings()
        running_list = list(
            session.scalars(
                select(ReplaySessionModel).where(
                    ReplaySessionModel.status == ReplaySessionStatus.RUNNING.value
                )
            ).all()
        )
        total_stepped = 0
        ts_now = now_ist()

        for model in running_list:
            last_wall = (
                ensure_ist(model.last_step_wall_clock)
                if model.last_step_wall_clock is not None
                else ts_now
            )
            elapsed_sec = max(0.0, (ts_now - last_wall).total_seconds())
            speed_enum = _parse_replay_speed(model.replay_speed)
            mult = REPLAY_SPEED_MULTIPLIERS[speed_enum]
            rate = Decimal(str(cfg.REPLAY_BASE_CANDLES_PER_SECOND)) * Decimal(mult)
            exact_bars = Decimal(str(elapsed_sec)) * rate + Decimal(
                str(model.fractional_bar_carry or 0)
            )
            whole_bars = int(exact_bars)
            if whole_bars < 1:
                continue

            carry = exact_bars - Decimal(whole_bars)
            capped_bars = min(whole_bars, 25)  # Smooth bounded batch per tick
            model.fractional_bar_carry = carry.quantize(Decimal("0.000001"))
            model.last_step_wall_clock = ts_now

            strat_ver = session.get(StrategyVersionModel, model.strategy_version_id)
            if strat_ver is None:
                continue
            strategy_cfg = parse_and_validate_ema_rsi_config(strat_ver.parameters_json)
            strategy = EmaRsiCrossoverStrategy(strategy_cfg)

            _, all_bars = MarketReplayService._load_validated_dataset_bars(
                session,
                dataset_id=model.dataset_id,
                instrument=model.instrument_symbol,
                timeframe=model.timeframe,
                bundled_dir=bundled_dir,
            )
            warmup_bars, session_bars = MarketReplayService._partition_warmup_and_session_bars(
                all_bars,
                historical_session_date=model.historical_session_date,
                warmup_cap=max(60, strategy.warmup_bars_required + 10),
            )
            total_stepped += MarketReplayService._advance_bars_internal(
                session,
                model=model,
                strategy_cfg=strategy_cfg,
                warmup_bars=warmup_bars,
                session_bars=session_bars,
                bars_to_step=capped_bars,
                cfg=cfg,
            )

        return total_stepped

    @staticmethod
    def recover_interrupted_sessions_on_startup(session: Session) -> int:
        """Transition any orphaned RUNNING replay sessions to PAUSED_RECOVERY on startup."""
        orphans = list(
            session.scalars(
                select(ReplaySessionModel).where(
                    ReplaySessionModel.status == ReplaySessionStatus.RUNNING.value
                )
            ).all()
        )
        acct = session.scalar(select(PaperAccountModel).limit(1))
        halted_now = bool(acct.paper_trading_halted) if acct is not None else False
        ts_now = now_ist()
        for m in orphans:
            m.status = ReplaySessionStatus.PAUSED_RECOVERY.value
            m.paused_at = ts_now
            m.fractional_bar_carry = Decimal("0")
            m.paper_trading_halted_snapshot = halted_now
            m.updated_at = ts_now
            src_labels = classify_data_source(m.dataset_source, m.dataset_provider)
            AuditService.record(
                session,
                action="REPLAY_PAUSED_ON_RECOVERY",
                summary=(
                    f"Startup recovery moved ReplaySession {m.session_code} from RUNNING "
                    f"to PAUSED_RECOVERY at candle {m.candles_processed}/{m.total_session_candles}."
                ),
                actor="system.recovery",
                actor_type=ActorType.SYSTEM,
                category=AuditCategory.MARKET_DATA,
                severity=AuditSeverity.WARNING,
                entity_type="replay_session",
                entity_id=str(m.id),
                details={
                    "candles_processed": m.candles_processed,
                    "dataset_id": m.dataset_id,
                    "paper_trading_halted_snapshot": halted_now,
                    "data_provider": src_labels.audit_provider,
                    "data_type": src_labels.audit_data_type,
                    "execution_type": src_labels.audit_execution_type,
                },
            )
        if orphans:
            session.flush()
        return len(orphans)

    @staticmethod
    def get_current_freshness(
        session: Session,
        *,
        settings: AppSettings | None = None,
        reference_time: datetime | None = None,
    ) -> tuple[ReplaySessionModel | None, MarketDataFreshnessSnapshot]:
        """Return the latest replay session and its evaluated MarketDataFreshnessSnapshot."""
        cfg = settings or get_settings()
        latest = session.scalar(
            select(ReplaySessionModel).order_by(desc(ReplaySessionModel.created_at)).limit(1)
        )
        if latest is None:
            snap = evaluate_market_data_freshness(
                session_status=None,
                last_candle_received_at=None,
                stale_threshold_seconds=cfg.MARKET_DATA_STALE_THRESHOLD_SECONDS,
                reference_time=reference_time,
            )
            return None, snap

        snap = evaluate_market_data_freshness(
            session_status=latest.status,
            last_candle_received_at=latest.last_candle_received_at,
            last_exchange_timestamp=latest.replay_clock_ist,
            stale_threshold_seconds=latest.stale_threshold_seconds,
            reference_time=reference_time,
        )
        return latest, snap

    @staticmethod
    def assert_new_paper_orders_allowed_by_market_data(
        session: Session,
        *,
        settings: AppSettings | None = None,
        reference_time: datetime | None = None,
    ) -> MarketDataFreshnessSnapshot:
        """Downstream safety check refusing NEW executable paper orders if market data is STALE/STOPPED."""
        _, snap = MarketReplayService.get_current_freshness(
            session, settings=settings, reference_time=reference_time
        )
        enforce_fresh_market_data_for_new_order(snap)
        return snap

    @staticmethod
    def get_replay_state(
        session: Session,
        *,
        session_id: uuid.UUID | None = None,
        settings: AppSettings | None = None,
        bundled_dir: Path | None = None,
        reference_time: datetime | None = None,
    ) -> dict[str, Any]:
        cfg = settings or get_settings()
        if session_id is not None:
            model = MarketReplayService._get_session_or_404(session, session_id)
        else:
            model = session.scalar(
                select(ReplaySessionModel).order_by(desc(ReplaySessionModel.created_at)).limit(1)
            )

        if model is None:
            fresh = evaluate_market_data_freshness(
                session_status=None,
                last_candle_received_at=None,
                stale_threshold_seconds=cfg.MARKET_DATA_STALE_THRESHOLD_SECONDS,
                reference_time=reference_time,
            )
            return {
                "has_session": False,
                "session": None,
                "freshness": fresh.to_dict(),
                "last_candle": None,
                "strategy_evaluation": {
                    "evaluation_status": StrategyEvaluationState.STANDBY.value,
                    "condition_headline": "No active Market Replay session — configure instrument, historical session, and speed above, then click START.",
                    "fast_ema": None,
                    "slow_ema": None,
                    "ema_spread": None,
                    "rsi": None,
                    "trend_regime": "STANDBY",
                    "crossover_state": "NONE",
                    "rule_checks": [],
                },
                "latest_signal": None,
                "recent_signals": [],
                "chart_candles": [],
            }

        fresh = evaluate_market_data_freshness(
            session_status=model.status,
            last_candle_received_at=model.last_candle_received_at,
            last_exchange_timestamp=model.replay_clock_ist,
            stale_threshold_seconds=model.stale_threshold_seconds,
            reference_time=reference_time,
        )

        recent_sig_rows = list(
            session.scalars(
                select(ReplaySignalModel)
                .where(ReplaySignalModel.replay_session_id == model.id)
                .order_by(desc(ReplaySignalModel.emitted_at))
                .limit(30)
            ).all()
        )
        recent_signals = [MarketReplayService._serialize_signal(r) for r in recent_sig_rows]

        # Build pre-computed chart window (last 75 replayed candles + Fast/Slow EMA + RSI)
        chart_candles: list[dict[str, Any]] = []
        if model.candles_processed > 0:
            try:
                strat_ver = session.get(StrategyVersionModel, model.strategy_version_id)
                if strat_ver is not None:
                    s_cfg = parse_and_validate_ema_rsi_config(strat_ver.parameters_json)
                    strat = EmaRsiCrossoverStrategy(s_cfg)
                    _, all_bars = MarketReplayService._load_validated_dataset_bars(
                        session,
                        dataset_id=model.dataset_id,
                        instrument=model.instrument_symbol,
                        timeframe=model.timeframe,
                        bundled_dir=bundled_dir,
                    )
                    warmup_bars, session_bars = MarketReplayService._partition_warmup_and_session_bars(
                        all_bars,
                        historical_session_date=model.historical_session_date,
                        warmup_cap=max(60, strat.warmup_bars_required + 10),
                    )
                    visible_session = session_bars[: model.candles_processed]
                    combined = warmup_bars + visible_session
                    closes = [b.close for b in combined]
                    f_ema = compute_ema_series(closes, s_cfg.fast_ema_period)
                    s_ema = compute_ema_series(closes, s_cfg.slow_ema_period)
                    rsi_ser = compute_rsi_series(closes, s_cfg.rsi_period)

                    offset = len(warmup_bars)
                    tail_count = min(len(visible_session), 75)
                    start_idx = len(visible_session) - tail_count
                    for rel_idx in range(start_idx, len(visible_session)):
                        bar = visible_session[rel_idx]
                        abs_i = offset + rel_idx
                        fv = f_ema[abs_i]
                        sv = s_ema[abs_i]
                        rv = rsi_ser[abs_i]
                        chart_candles.append(
                            {
                                "timestamp": format_ist_iso(bar.bar_timestamp),
                                "open": str(bar.open.quantize(Decimal("0.01"))),
                                "high": str(bar.high.quantize(Decimal("0.01"))),
                                "low": str(bar.low.quantize(Decimal("0.01"))),
                                "close": str(bar.close.quantize(Decimal("0.01"))),
                                "volume": bar.volume,
                                "fast_ema": str(fv.quantize(Decimal("0.01")))
                                if fv is not None
                                else None,
                                "slow_ema": str(sv.quantize(Decimal("0.01")))
                                if sv is not None
                                else None,
                                "rsi": str(rv.quantize(Decimal("0.01")))
                                if rv is not None
                                else None,
                            }
                        )
            except Exception:  # noqa: BLE001
                chart_candles = []

        progress_pct = (
            round((model.candles_processed / model.total_session_candles) * 100.0, 1)
            if model.total_session_candles > 0
            else 0.0
        )

        default_eval = {
            "evaluation_status": StrategyEvaluationState.STANDBY.value,
            "condition_headline": "Session configured and ready — click START or +1 BAR STEP to stream normalized CandleEvents.",
            "fast_ema": None,
            "slow_ema": None,
            "ema_spread": None,
            "rsi": None,
            "trend_regime": "STANDBY",
            "crossover_state": "NONE",
            "rule_checks": [],
        }

        src_labels = classify_data_source(model.dataset_source, model.dataset_provider)
        return {
            "has_session": True,
            "session": {
                "id": str(model.id),
                "session_code": model.session_code,
                "instrument_symbol": model.instrument_symbol,
                "timeframe": model.timeframe,
                "strategy_version_id": str(model.strategy_version_id),
                "strategy_version_label": model.strategy_version_label,
                "dataset_id": model.dataset_id,
                "dataset_name": model.dataset_name,
                "dataset_source": model.dataset_source,
                "dataset_provider": model.dataset_provider,
                "labels": src_labels.to_dict(),
                "data_version_hash": model.data_version_hash,
                "strategy_instance_id": str(model.strategy_instance_id)
                if model.strategy_instance_id
                else None,
                "correlation_id": model.correlation_id,
                "paper_trading_halted_snapshot": bool(model.paper_trading_halted_snapshot),
                "historical_session_date": model.historical_session_date,
                "warmup_bars_count": model.warmup_bars_count,
                "total_session_candles": model.total_session_candles,
                "candles_processed": model.candles_processed,
                "progress_pct": progress_pct,
                "signals_emitted": model.signals_emitted,
                "replay_speed": model.replay_speed,
                "status": model.status,
                "replay_clock_ist": format_ist_iso(model.replay_clock_ist)
                if model.replay_clock_ist
                else None,
                "started_at": format_ist_iso(model.started_at) if model.started_at else None,
                "paused_at": format_ist_iso(model.paused_at) if model.paused_at else None,
                "stopped_at": format_ist_iso(model.stopped_at) if model.stopped_at else None,
                "completed_at": format_ist_iso(model.completed_at)
                if model.completed_at
                else None,
            },
            "freshness": fresh.to_dict(),
            "last_candle": model.last_candle_json,
            "strategy_evaluation": model.latest_evaluation_json or default_eval,
            "latest_signal": model.latest_signal_json,
            "recent_signals": recent_signals,
            "chart_candles": chart_candles,
        }

    @staticmethod
    def _get_session_or_404(session: Session, session_id: uuid.UUID) -> ReplaySessionModel:
        model = session.get(ReplaySessionModel, session_id)
        if model is None:
            raise EntityNotFoundError(f"ReplaySession '{session_id}' not found.")
        return model

    @staticmethod
    def _serialize_signal(row: ReplaySignalModel) -> dict[str, Any]:
        return {
            "id": str(row.id),
            "signal_code": row.signal_code,
            "correlation_id": row.correlation_id,
            "replay_session_id": str(row.replay_session_id) if row.replay_session_id else None,
            "strategy_instance_id": str(row.strategy_instance_id)
            if row.strategy_instance_id
            else None,
            "strategy_version_id": str(row.strategy_version_id),
            "strategy_version_label": row.strategy_version_label,
            "instrument_symbol": row.instrument_symbol,
            "timeframe": row.timeframe,
            "side": row.side,
            "direction": row.direction,
            "exit_reason": row.exit_reason,
            "bar_timestamp": format_ist_iso(row.bar_timestamp),
            "emitted_at": format_ist_iso(row.emitted_at),
            "expires_at": format_ist_iso(row.expires_at),
            "reference_price": str(row.reference_price.quantize(Decimal("0.01"))),
            "status": row.status,
            "reason": row.rationale,
            "rationale": row.rationale,
            "indicators": row.indicators_json or {},
            "market_event_id": row.market_event_id,
        }


async def run_replay_background_worker(stop_event: asyncio.Event) -> None:
    """Lightweight asyncio loop advancing RUNNING replay sessions in real time."""
    while not stop_event.is_set():
        try:
            cfg = get_settings()
            if cfg.REPLAY_WORKER_ENABLED and cfg.DATABASE_URL:
                token = set_correlation_id()
                try:
                    with unit_of_work(settings=cfg) as session:
                        MarketReplayService.advance_running_sessions_by_wall_clock(
                            session, settings=cfg
                        )
                finally:
                    reset_correlation_id(token)
        except Exception as exc:  # noqa: BLE001
            logger.debug("Replay background tick skipped", extra={"error": str(exc)})

        try:
            await asyncio.wait_for(stop_event.wait(), timeout=0.20)
        except TimeoutError:
            continue
