"""Dashboard aggregation endpoint serving real persisted state and truthful empty states."""

from __future__ import annotations

from decimal import Decimal

from fastapi import APIRouter

from backend.schemas.api import (
    AuditEventOut,
    ComponentHealthOut,
    DashboardKPIsOut,
    DashboardOverviewOut,
    PaperAccountOut,
    RiskUtilizationSnapshotOut,
)
from sqlalchemy import desc, func, select

from core.audit.service import AuditService
from core.clock import now_ist
from core.config import get_settings
from core.controls.service import ControlService
from core.domain.enums import (
    WORKING_ORDER_STATUSES,
    PositionStatus,
    ReplaySessionStatus,
    StrategyInstanceStatus,
    SystemComponentStatus,
)
from core.health.service import HealthService
from core.market_data.service import MarketReplayService
from core.oms.service import OrderManagementService
from core.persistence.models import (
    PaperOrderModel,
    PaperPositionModel,
    StrategyInstanceModel,
)
from core.persistence.uow import unit_of_work
from core.positions.service import PositionService

router = APIRouter(prefix="/api/v1/dashboard", tags=["Dashboard"])


@router.get("", response_model=DashboardOverviewOut)
def get_dashboard_overview() -> DashboardOverviewOut:
    """Return complete Dashboard telemetry without fabricating trading activity."""
    cfg = get_settings()
    health = HealthService.evaluate_system_health(settings=cfg, persist_probe=False)
    db_ok = health["database"].status == SystemComponentStatus.HEALTHY

    account_out: PaperAccountOut | None = None
    alerts_out: list[AuditEventOut] = []
    running_strategies_out: list[dict[str, object]] = []
    open_positions_out: list[dict[str, object]] = []
    recent_orders_out: list[dict[str, object]] = []
    active_strategy_cnt = 0
    open_pos_cnt = 0
    pending_ord_cnt = 0

    if db_ok:
        try:
            with unit_of_work(settings=cfg) as session:
                acct = ControlService.get_primary_paper_account(session, cfg)
                account_out = PaperAccountOut(**acct.__dict__)
                audit_rows = AuditService.list_events(session, limit=12)
                alerts_out = [AuditEventOut(**ev.__dict__) for ev in audit_rows]
                latest_rpl, fresh_snap = MarketReplayService.get_current_freshness(
                    session, settings=cfg
                )

                active_instances = list(
                    session.scalars(
                        select(StrategyInstanceModel)
                        .where(
                            StrategyInstanceModel.status.in_(
                                [
                                    StrategyInstanceStatus.RUNNING.value,
                                    StrategyInstanceStatus.PAUSED.value,
                                ]
                            )
                        )
                        .order_by(desc(StrategyInstanceModel.created_at))
                    ).all()
                )
                for inst in active_instances:
                    if inst.status == StrategyInstanceStatus.RUNNING.value:
                        active_strategy_cnt += 1
                    running_strategies_out.append(
                        {
                            "strategy_version": inst.strategy_version_label,
                            "instrument": inst.instrument_symbol,
                            "timeframe": inst.timeframe,
                            "mode": f"PAPER ({inst.order_type})",
                            "status": inst.status,
                            "market_data_freshness": fresh_snap.status.value,
                            "signals_emitted": inst.signals_emitted,
                        }
                    )

                if (
                    not active_instances
                    and latest_rpl is not None
                    and latest_rpl.status
                    in (
                        ReplaySessionStatus.RUNNING.value,
                        ReplaySessionStatus.PAUSED.value,
                        ReplaySessionStatus.PAUSED_RECOVERY.value,
                    )
                ):
                    if latest_rpl.status == ReplaySessionStatus.RUNNING.value:
                        active_strategy_cnt = 1
                    running_strategies_out.append(
                        {
                            "strategy_version": latest_rpl.strategy_version_label,
                            "instrument": latest_rpl.instrument_symbol,
                            "timeframe": latest_rpl.timeframe,
                            "mode": f"REPLAY ({latest_rpl.replay_speed})",
                            "status": latest_rpl.status,
                            "market_data_freshness": fresh_snap.status.value,
                            "signals_emitted": latest_rpl.signals_emitted,
                        }
                    )

                open_pos_rows = list(
                    session.scalars(
                        select(PaperPositionModel)
                        .where(PaperPositionModel.status == PositionStatus.OPEN.value)
                        .order_by(desc(PaperPositionModel.opened_at))
                    ).all()
                )
                open_pos_cnt = len(open_pos_rows)
                open_positions_out = [PositionService.serialize_position(p) for p in open_pos_rows]

                pending_ord_cnt = int(
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
                )
                recent_orders_out = OrderManagementService.list_orders(session, limit=8)
        except Exception:  # noqa: BLE001
            account_out = None

    if account_out is not None:
        deployed = max(Decimal("0"), account_out.initial_capital - account_out.available_capital)
        deployed_pct = (
            (deployed / account_out.initial_capital * Decimal("100")).quantize(Decimal("0.01"))
            if account_out.initial_capital > 0
            else Decimal("0.00")
        )
        daily_loss = (
            abs(account_out.realized_pnl_today)
            if account_out.realized_pnl_today < 0
            else Decimal("0.00")
        )
        kpis = DashboardKPIsOut(
            paper_capital=account_out.initial_capital,
            available_capital=account_out.available_capital,
            todays_realized_pnl=account_out.realized_pnl_today,
            unrealized_pnl=account_out.unrealized_pnl,
            open_positions=open_pos_cnt,
            active_strategies=active_strategy_cnt,
            pending_orders=pending_ord_cnt,
            currency=account_out.currency,
            is_demo_capital=account_out.is_demo_capital,
            account_designation=account_out.account_designation.value,
        )
        risk_util = RiskUtilizationSnapshotOut(
            paper_trading_halted=account_out.paper_trading_halted,
            new_orders_gate="BLOCKED" if account_out.paper_trading_halted else "PERMITTED",
            capital_deployed_pct=deployed_pct,
            daily_loss_used_inr=daily_loss,
            open_positions_count=open_pos_cnt,
            auto_flatten_on_stop=False,
        )
    else:
        demo_cap = cfg.DEFAULT_DEMO_CAPITAL_INR
        kpis = DashboardKPIsOut(
            paper_capital=demo_cap,
            available_capital=demo_cap,
            todays_realized_pnl=Decimal("0.00"),
            unrealized_pnl=Decimal("0.00"),
            open_positions=0,
            active_strategies=active_strategy_cnt,
            pending_orders=0,
            currency="INR",
            is_demo_capital=True,
            account_designation="DEMO_PAPER_ONLY",
        )
        risk_util = RiskUtilizationSnapshotOut(
            paper_trading_halted=False,
            new_orders_gate="DEGRADED_DB_OFFLINE",
            capital_deployed_pct=Decimal("0.00"),
            daily_loss_used_inr=Decimal("0.00"),
            open_positions_count=0,
            auto_flatten_on_stop=False,
        )

    strip = [
        ComponentHealthOut(
            component=c.component,
            status=c.status,
            message=c.message,
            checked_at=c.checked_at,
            latency_ms=c.latency_ms,
            details=c.details,
        )
        for c in health["components"]
    ]

    return DashboardOverviewOut(
        generated_at=now_ist(),
        execution_mode=cfg.EXECUTION_MODE,
        market_mode=cfg.MARKET_MODE,
        timezone=cfg.TIMEZONE,
        database_connected=db_ok,
        account=account_out,
        kpis=kpis,
        system_status_strip=strip,
        running_strategies=running_strategies_out,
        open_positions=open_positions_out,
        recent_orders=recent_orders_out,
        risk_utilization=risk_util,
        recent_alerts=alerts_out,
        daily_pnl_series=[],
    )
