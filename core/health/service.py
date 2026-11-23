"""Subsystem health evaluation and health-event persistence service."""

from __future__ import annotations

import logging
from decimal import Decimal
from typing import Any

from sqlalchemy import desc, func, select
from sqlalchemy.orm import Session

from core.clock import ensure_ist, now_ist
from core.config import AppSettings, get_settings
from core.domain.entities import ComponentHealthSnapshot
from core.domain.enums import (
    WORKING_ORDER_STATUSES,
    MarketDataFreshnessStatus,
    PositionStatus,
    ReconciliationStatus,
    ReplaySessionStatus,
    StrategyInstanceStatus,
    SystemComponent,
    SystemComponentStatus,
)
from core.logging import get_correlation_id
from core.market_data.service import MarketReplayService
from core.persistence.db import probe_database
from core.persistence.models import (
    InstrumentModel,
    PaperAccountModel,
    PaperOrderModel,
    PaperPositionModel,
    ReconciliationRunModel,
    StrategyDefinitionModel,
    StrategyInstanceModel,
    SystemHealthEventModel,
)
from core.persistence.uow import unit_of_work
from core.positions.service import PositionService

logger = logging.getLogger(__name__)


class HealthService:
    """Evaluates truth-based health across all core desk subsystems."""

    @staticmethod
    def record_health_event(
        session: Session,
        *,
        component: SystemComponent,
        status: SystemComponentStatus,
        message: str,
        latency_ms: Decimal | None = None,
        details: dict[str, Any] | None = None,
        correlation_id: str | None = None,
    ) -> SystemHealthEventModel:
        row = SystemHealthEventModel(
            recorded_at=now_ist(),
            component=component.value,
            status=status.value,
            latency_ms=latency_ms,
            message=message,
            details_json=details or {},
            correlation_id=(correlation_id or "").strip() or get_correlation_id(),
        )
        session.add(row)
        session.flush()
        return row

    @classmethod
    def evaluate_system_health(
        cls,
        *,
        settings: AppSettings | None = None,
        persist_probe: bool = False,
    ) -> dict[str, Any]:
        """Evaluate current status of Market Data, Strategy, Risk, Paper Broker, and Database."""
        cfg = settings or get_settings()
        checked_at = now_ist()
        db_probe = probe_database(cfg)

        db_reachable = bool(db_probe["reachable"])
        db_configured = bool(db_probe["configured"])
        paper_halted = False
        halt_reason: str | None = None
        table_counts: dict[str, int] = {}
        latest_replay = None
        freshness_snap = None
        latest_recon_info: dict[str, Any] | None = None
        pos_recon_clean = True
        open_positions_cnt = 0

        if db_reachable:
            try:
                with unit_of_work(settings=cfg) as session:
                    acct = session.scalar(
                        select(PaperAccountModel).where(
                            PaperAccountModel.account_code == cfg.DEFAULT_PAPER_ACCOUNT_CODE
                        )
                    )
                    if acct is not None:
                        paper_halted = bool(acct.paper_trading_halted)
                        halt_reason = acct.halt_reason

                    open_positions_cnt = int(
                        session.scalar(
                            select(func.count())
                            .select_from(PaperPositionModel)
                            .where(PaperPositionModel.status == PositionStatus.OPEN.value)
                        )
                        or 0
                    )
                    pos_check = PositionService.reconcile_positions_from_fills(
                        session, heal_if_needed=False, settings=cfg
                    )
                    pos_recon_clean = pos_check.reconciled and pos_check.discrepancies_found == 0

                    latest_recon_row = session.scalar(
                        select(ReconciliationRunModel)
                        .order_by(desc(ReconciliationRunModel.completed_at))
                        .limit(1)
                    )
                    if latest_recon_row is not None:
                        latest_recon_info = {
                            "run_code": latest_recon_row.run_code,
                            "status": latest_recon_row.status,
                            "discrepancies_count": int(latest_recon_row.discrepancies_count),
                            "repairs_count": int(latest_recon_row.repairs_count),
                            "completed_at": ensure_ist(latest_recon_row.completed_at).isoformat(),
                        }

                    table_counts = {
                        "instruments": int(
                            session.scalar(select(func.count()).select_from(InstrumentModel)) or 0
                        ),
                        "strategy_definitions": int(
                            session.scalar(
                                select(func.count()).select_from(StrategyDefinitionModel)
                            )
                            or 0
                        ),
                        "running_instances": int(
                            session.scalar(
                                select(func.count())
                                .select_from(StrategyInstanceModel)
                                .where(
                                    StrategyInstanceModel.status
                                    == StrategyInstanceStatus.RUNNING.value
                                )
                            )
                            or 0
                        ),
                        "working_orders": int(
                            session.scalar(
                                select(func.count())
                                .select_from(PaperOrderModel)
                                .where(
                                    PaperOrderModel.status.in_(
                                        [s.value for s in WORKING_ORDER_STATUSES]
                                    )
                                )
                            )
                            or 0
                        ),
                        "total_orders": int(
                            session.scalar(select(func.count()).select_from(PaperOrderModel)) or 0
                        ),
                    }
                    latest_replay, freshness_snap = MarketReplayService.get_current_freshness(
                        session, settings=cfg, reference_time=checked_at
                    )
                    replay_info: dict[str, Any] | None = None
                    if latest_replay is not None:
                        replay_info = {
                            "session_code": latest_replay.session_code,
                            "instrument": latest_replay.instrument_symbol,
                            "status": latest_replay.status,
                            "speed": latest_replay.replay_speed,
                            "candles_processed": latest_replay.candles_processed,
                            "total_candles": latest_replay.total_session_candles,
                            "strategy_version_label": latest_replay.strategy_version_label,
                        }

                    if persist_probe:
                        cls.record_health_event(
                            session,
                            component=SystemComponent.DATABASE,
                            status=SystemComponentStatus.HEALTHY,
                            latency_ms=db_probe["latency_ms"],
                            message="PostgreSQL connectivity and IST session verified.",
                            details={
                                "dialect": db_probe["dialect"],
                                "db_timezone": db_probe["db_timezone"],
                                "alembic_version": db_probe["alembic_version"],
                            },
                        )
            except Exception as exc:  # noqa: BLE001
                logger.warning("Failed reading desk state during health check", extra={"error": str(exc)})
                db_reachable = False
                db_probe["error"] = str(exc)
                replay_info = None
        else:
            replay_info = None

        # 1. Database Component Status
        if db_reachable:
            db_status = SystemComponentStatus.HEALTHY
            db_msg = (
                f"Connected ({db_probe['dialect']}, {db_probe['latency_ms']} ms, "
                f"TZ={db_probe['db_timezone']})"
            )
        elif not db_configured:
            db_status = SystemComponentStatus.STOPPED
            db_msg = "DATABASE_URL not configured"
        else:
            db_status = SystemComponentStatus.DEGRADED
            db_msg = f"Database unreachable: {db_probe['error']}"

        # 2. Market Data Status (Driven by MarketDataFreshnessSnapshot: HEALTHY | STALE | STOPPED)
        if freshness_snap is not None and replay_info is not None:
            if freshness_snap.status == MarketDataFreshnessStatus.HEALTHY:
                md_status = SystemComponentStatus.HEALTHY
                md_msg = (
                    f"Replay {replay_info['session_code']} ({replay_info['instrument']} @ "
                    f"{replay_info['speed']}) — {freshness_snap.reason}"
                )
            elif freshness_snap.status == MarketDataFreshnessStatus.STALE:
                md_status = SystemComponentStatus.STALE
                md_msg = freshness_snap.reason
            else:
                md_status = SystemComponentStatus.STOPPED
                md_msg = "Idle — No active Market Replay feed running."
            market_data_snapshot = ComponentHealthSnapshot(
                component=SystemComponent.MARKET_DATA,
                status=md_status,
                message=md_msg,
                checked_at=checked_at,
                details={
                    "mode": cfg.MARKET_MODE,
                    "freshness": freshness_snap.to_dict(),
                    "replay": replay_info,
                },
            )
        else:
            market_data_snapshot = ComponentHealthSnapshot(
                component=SystemComponent.MARKET_DATA,
                status=SystemComponentStatus.STOPPED,
                message="Idle — No active Market Replay feed running.",
                checked_at=checked_at,
                details={"mode": cfg.MARKET_MODE},
            )

        # 3. Strategy Engine Status
        is_replay_running = (
            replay_info is not None
            and replay_info.get("status") == ReplaySessionStatus.RUNNING.value
        )
        if paper_halted:
            strategy_snapshot = ComponentHealthSnapshot(
                component=SystemComponent.STRATEGY_ENGINE,
                status=SystemComponentStatus.BLOCKED,
                message="STOP PAPER TRADING active — new signal dispatch halted.",
                checked_at=checked_at,
                details={"paper_trading_halted": True, "halt_reason": halt_reason},
            )
        elif is_replay_running and replay_info is not None:
            strategy_snapshot = ComponentHealthSnapshot(
                component=SystemComponent.STRATEGY_ENGINE,
                status=SystemComponentStatus.HEALTHY,
                message=(
                    f"Evaluating {replay_info['strategy_version_label']} on normalized "
                    f"{replay_info['instrument']} CandleEvents."
                ),
                checked_at=checked_at,
                details={
                    "registered_strategies": table_counts.get("strategy_definitions", 0),
                    "active_strategy_version": replay_info["strategy_version_label"],
                },
            )
        else:
            strategy_snapshot = ComponentHealthSnapshot(
                component=SystemComponent.STRATEGY_ENGINE,
                status=SystemComponentStatus.STOPPED,
                message="Standby — 0 active strategy runners.",
                checked_at=checked_at,
                details={"registered_strategies": table_counts.get("strategy_definitions", 0)},
            )

        # 4. Risk Engine Status
        if not db_reachable:
            risk_snapshot = ComponentHealthSnapshot(
                component=SystemComponent.RISK_ENGINE,
                status=SystemComponentStatus.DEGRADED,
                message="Degraded — Risk state store unavailable.",
                checked_at=checked_at,
            )
        elif paper_halted:
            risk_snapshot = ComponentHealthSnapshot(
                component=SystemComponent.RISK_ENGINE,
                status=SystemComponentStatus.BLOCKED,
                message="Pre-trade order gate locked by STOP PAPER TRADING (open positions preserved).",
                checked_at=checked_at,
                details={"paper_trading_halted": True, "auto_flatten_positions": False},
            )
        else:
            risk_snapshot = ComponentHealthSnapshot(
                component=SystemComponent.RISK_ENGINE,
                status=SystemComponentStatus.HEALTHY,
                message="Armed — PAPER_ONLY guardrails active; new orders permitted.",
                checked_at=checked_at,
                details={"execution_mode": cfg.EXECUTION_MODE, "paper_trading_halted": False},
            )

        # 5. Paper Broker Status
        if paper_halted:
            broker_snapshot = ComponentHealthSnapshot(
                component=SystemComponent.PAPER_BROKER,
                status=SystemComponentStatus.BLOCKED,
                message="Blocked — New simulated order routing halted by operator.",
                checked_at=checked_at,
                details={"paper_trading_halted": True},
            )
        elif (
            is_replay_running
            or table_counts.get("running_instances", 0) > 0
            or table_counts.get("working_orders", 0) > 0
        ):
            broker_snapshot = ComponentHealthSnapshot(
                component=SystemComponent.PAPER_BROKER,
                status=SystemComponentStatus.HEALTHY,
                message=(
                    f"Active — Simulated Paper Broker matching orders "
                    f"({table_counts.get('working_orders', 0)} working / "
                    f"{table_counts.get('total_orders', 0)} total)."
                ),
                checked_at=checked_at,
                details={
                    "execution_mode": cfg.EXECUTION_MODE,
                    "working_orders": table_counts.get("working_orders", 0),
                    "total_orders": table_counts.get("total_orders", 0),
                },
            )
        else:
            broker_snapshot = ComponentHealthSnapshot(
                component=SystemComponent.PAPER_BROKER,
                status=SystemComponentStatus.STOPPED,
                message="Standby — Simulated execution simulator idle (no real orders).",
                checked_at=checked_at,
                details={"execution_mode": cfg.EXECUTION_MODE},
            )

        db_snapshot = ComponentHealthSnapshot(
            component=SystemComponent.DATABASE,
            status=db_status,
            message=db_msg,
            checked_at=checked_at,
            latency_ms=db_probe["latency_ms"],
            details={
                "configured": db_configured,
                "reachable": db_reachable,
                "dialect": db_probe["dialect"],
                "db_timezone": db_probe["db_timezone"],
                "alembic_version": db_probe["alembic_version"],
                "masked_dsn": cfg.public_database_display,
                "error": db_probe["error"],
                "table_counts": table_counts,
            },
        )

        components = [
            market_data_snapshot,
            strategy_snapshot,
            risk_snapshot,
            broker_snapshot,
            db_snapshot,
        ]

        # Additional Phase 5 Operational Subsystems for full 9-Component System Health Matrix
        api_snapshot = ComponentHealthSnapshot(
            component=SystemComponent.API_SERVER,
            status=SystemComponentStatus.HEALTHY,
            message=f"FastAPI {cfg.APP_VERSION} serving requests ({cfg.EXECUTION_MODE}).",
            checked_at=checked_at,
            details={"version": cfg.APP_VERSION, "timezone": cfg.TIMEZONE},
        )

        if replay_info is not None:
            r_stat = str(replay_info.get("status", "STOPPED"))
            if r_stat == ReplaySessionStatus.RUNNING.value:
                mr_status = SystemComponentStatus.HEALTHY
            elif r_stat in (
                ReplaySessionStatus.PAUSED.value,
                ReplaySessionStatus.PAUSED_RECOVERY.value,
            ):
                mr_status = SystemComponentStatus.STALE
            else:
                mr_status = SystemComponentStatus.STOPPED
            replay_snapshot = ComponentHealthSnapshot(
                component=SystemComponent.MARKET_REPLAY,
                status=mr_status,
                message=(
                    f"Session {replay_info['session_code']} ({r_stat} @ {replay_info['speed']} • "
                    f"{replay_info['candles_processed']}/{replay_info['total_candles']} candles)."
                ),
                checked_at=checked_at,
                details=replay_info,
            )
        else:
            replay_snapshot = ComponentHealthSnapshot(
                component=SystemComponent.MARKET_REPLAY,
                status=SystemComponentStatus.STOPPED,
                message="Idle — No Market Replay session configured.",
                checked_at=checked_at,
            )

        if not db_reachable:
            pos_status = SystemComponentStatus.DEGRADED
            pos_msg = "Degraded — Position ledger store unreachable."
        elif not pos_recon_clean:
            pos_status = SystemComponentStatus.BLOCKED
            pos_msg = "Blocked — Unreconciled discrepancy detected between fills and positions."
        else:
            pos_status = SystemComponentStatus.HEALTHY
            pos_msg = f"Healthy — Execution-derived book verified ({open_positions_cnt} open position(s))."
        position_snapshot = ComponentHealthSnapshot(
            component=SystemComponent.POSITION_ENGINE,
            status=pos_status,
            message=pos_msg,
            checked_at=checked_at,
            details={"open_positions": open_positions_cnt, "reconciled": pos_recon_clean},
        )

        if not db_reachable:
            rec_status = SystemComponentStatus.DEGRADED
            rec_msg = "Degraded — Unable to query reconciliation_runs."
        elif latest_recon_info is None:
            rec_status = SystemComponentStatus.STOPPED
            rec_msg = "No deterministic reconciliation run recorded yet."
        elif latest_recon_info["status"] in (
            ReconciliationStatus.COMPLETED.value,
            ReconciliationStatus.REPAIRED.value,
        ) and pos_recon_clean:
            rec_status = SystemComponentStatus.HEALTHY
            rec_msg = (
                f"Run {latest_recon_info['run_code']} {latest_recon_info['status']} "
                f"({latest_recon_info['discrepancies_count']} discrepancy, "
                f"{latest_recon_info['repairs_count']} repair)."
            )
        else:
            rec_status = SystemComponentStatus.BLOCKED
            rec_msg = (
                f"Discrepancy active — Run {latest_recon_info['run_code']} status "
                f"{latest_recon_info['status']}."
            )
        recon_snapshot = ComponentHealthSnapshot(
            component=SystemComponent.LAST_RECONCILIATION,
            status=rec_status,
            message=rec_msg,
            checked_at=checked_at,
            details=latest_recon_info or {},
        )

        operational_components = [
            api_snapshot,
            db_snapshot,
            replay_snapshot,
            market_data_snapshot,
            strategy_snapshot,
            risk_snapshot,
            broker_snapshot,
            position_snapshot,
            recon_snapshot,
        ]

        overall = (
            SystemComponentStatus.HEALTHY
            if db_reachable and not paper_halted and pos_recon_clean
            else (
                SystemComponentStatus.BLOCKED
                if db_reachable and (paper_halted or not pos_recon_clean)
                else SystemComponentStatus.DEGRADED
            )
        )

        return {
            "overall_status": overall,
            "checked_at": checked_at,
            "timezone": cfg.TIMEZONE,
            "execution_mode": cfg.EXECUTION_MODE,
            "paper_trading_halted": paper_halted,
            "halt_reason": halt_reason,
            "database": db_snapshot,
            "components": components,
            "operational_components": operational_components,
        }

    @staticmethod
    def list_recent_health_events(session: Session, *, limit: int = 50) -> list[dict[str, Any]]:
        safe_limit = max(1, min(limit, 250))
        rows = session.scalars(
            select(SystemHealthEventModel)
            .order_by(desc(SystemHealthEventModel.recorded_at))
            .limit(safe_limit)
        ).all()
        return [
            {
                "id": str(row.id),
                "recorded_at": ensure_ist(row.recorded_at),
                "component": row.component,
                "status": row.status,
                "latency_ms": row.latency_ms,
                "message": row.message,
                "details": dict(row.details_json or {}),
                "correlation_id": row.correlation_id,
            }
            for row in rows
        ]
