"""Deterministic Consulting Demo Setup & 17-Step Walkthrough Service (`core/demo/service.py`).

CRITICAL ENGINEERING INTEGRITY RULE:
- Zero fabricated trading rows and zero strategy parameter manipulation to fake profits.
- Every scenario executes strictly through the real Backtesting, Market Replay, Strategy,
  Risk Engine, OMS, Paper Broker, Position, and Reconciliation services.
- Reliably demonstrates:
    1. Successful automated strategy trade (Signal -> Risk -> OMS -> Fill -> Position)
    2. Profitable trade AND loss-making trade (real historical candle outcomes)
    3. Deliberate Risk Rejection (oversized order exceeding MAX_ORDER_VALUE)
    4. Stale-data protection (paused/aged feed blocking new order via MARKET_DATA_FRESHNESS)
    5. Market-data recovery (resumed replay emitting MARKET_DATA_RECOVERED -> HEALTHY)
    6. Global Kill Switch halt (open positions untouched) & Recovery + Reconciliation
    7. Complete 8-stage Audit Lifecycle Trace by Correlation ID
"""

from __future__ import annotations

import uuid
from decimal import Decimal
from typing import Any

from sqlalchemy import desc, func, select
from sqlalchemy.orm import Session

from core.backtesting.service import BacktestService
from core.config import AppSettings, get_settings
from core.logging import reset_correlation_id, set_correlation_id
from core.market_data.service import MarketReplayService
from core.paper_trading.service import PaperTradingService
from core.persistence.models import (
    AuditEventModel,
    BacktestRunModel,
    PaperFillModel,
    PaperOrderModel,
    PaperPositionModel,
    ReconciliationRunModel,
    ReplaySessionModel,
    ReplaySignalModel,
    RiskDecisionModel,
    StrategyInstanceModel,
    StrategyVersionModel,
)
from core.persistence.seed import seed_foundation_data
from core.reconciliation.service import ReconciliationService
from core.risk.service import RiskControlService

GUIDED_DEMO_STEPS: list[dict[str, Any]] = [
    {
        "step": 1,
        "title": "Dashboard Overview",
        "screen": "Dashboard",
        "duration_sec": 15,
        "talking_point": "Show persistent PAPER TRADING | NO REAL ORDERS banner, ₹5,00,000 DEMO capital, 5-subsystem health strip, and STOP PAPER TRADING safety gate.",
    },
    {
        "step": 2,
        "title": "Strategy Lab",
        "screen": "Strategy Lab",
        "duration_sec": 15,
        "talking_point": "Review EMA Crossover + RSI Confirmation rules, IST session bounds (09:20–14:45, 15:20 square-off), and explicit execution cost assumptions.",
    },
    {
        "step": 3,
        "title": "Select Immutable Strategy Version",
        "screen": "Strategy Lab",
        "duration_sec": 15,
        "talking_point": "Highlight NIFTY_EMA_RSI_v1 SHA-256 parameter hash and immutable version lineage — strategies cannot be silently edited in place.",
    },
    {
        "step": 4,
        "title": "Run Deterministic Backtest",
        "screen": "Backtests",
        "duration_sec": 20,
        "talking_point": "Execute a zero-lookahead historical simulation (t close signal -> t+1 open fill) with slippage and brokerage/statutory charges.",
    },
    {
        "step": 5,
        "title": "Inspect Backtest Results",
        "screen": "Backtests",
        "duration_sec": 20,
        "talking_point": "Show SIMULATED BACKTEST RESULTS banner, Pre-Backtest Data Quality Report, equity/drawdown curves, and both winning and losing trades.",
    },
    {
        "step": 6,
        "title": "Start Market Replay",
        "screen": "Market Replay",
        "duration_sec": 15,
        "talking_point": "Stream normalized historical CandleEvents at 1x–50x speed so the platform can be demonstrated 24/7 even when Indian markets are closed.",
    },
    {
        "step": 7,
        "title": "Deploy Strategy in Paper Mode",
        "screen": "Paper Trading",
        "duration_sec": 15,
        "talking_point": "Launch a Paper Strategy Instance consuming the normalized replay stream (strategies emit Signals only — never direct fills).",
    },
    {
        "step": 8,
        "title": "Generate Idempotent Signal",
        "screen": "Paper Trading",
        "duration_sec": 15,
        "talking_point": "Inspect emitted EMA+RSI Signals with globally unique Signal IDs and Correlation IDs preventing duplicate order creation.",
    },
    {
        "step": 9,
        "title": "Show Independent Risk Approval",
        "screen": "Risk Center",
        "duration_sec": 15,
        "talking_point": "Inspect persisted APPROVED RiskDecision records showing 25 operational, strategy, and account rules evaluated before OMS acceptance.",
    },
    {
        "step": 10,
        "title": "Show Paper Order State Machine",
        "screen": "Orders",
        "duration_sec": 15,
        "talking_point": "Trace the OMS order state transitions: CREATED -> RISK_APPROVED -> SUBMITTED -> ACKNOWLEDGED -> FILLED.",
    },
    {
        "step": 11,
        "title": "Show Simulated Execution Fill",
        "screen": "Paper Trading",
        "duration_sec": 15,
        "talking_point": "Inspect Paper Broker execution slices with modeled bar latency, adverse slippage (bps), and transaction charges.",
    },
    {
        "step": 12,
        "title": "Show Execution-Derived Position",
        "screen": "Positions",
        "duration_sec": 15,
        "talking_point": "Verify open and closed paper positions derived strictly from execution fills (direct position mutation is forbidden with HTTP 409).",
    },
    {
        "step": 13,
        "title": "Show Live & Realized P&L Analytics",
        "screen": "P&L Analytics",
        "duration_sec": 20,
        "talking_point": "Inspect Gross P&L, Slippage Cost, Charges, Realized & Unrealized P&L, Drawdown, and both profitable and loss-making trades.",
    },
    {
        "step": 14,
        "title": "Deliberately Trigger Risk Rejection",
        "screen": "Risk Center",
        "duration_sec": 20,
        "talking_point": "Trigger an oversized paper order and inspect the immutable RISK_REJECTED decision showing exact breached rule, current value, and limit.",
        "drill": "risk_rejection",
    },
    {
        "step": 15,
        "title": "Demonstrate Stale-Market-Data Blocking",
        "screen": "Market Replay",
        "duration_sec": 20,
        "talking_point": "Pause Market Replay so feed becomes STALE, attempt a paper order, and show fail-closed MARKET_DATA_FRESHNESS blocking.",
        "drill": "stale_data_block",
    },
    {
        "step": 16,
        "title": "Recover Market Data & Reconcile Ledger",
        "screen": "System Health",
        "duration_sec": 20,
        "talking_point": "Resume Market Replay (MARKET_DATA_RECOVERED -> HEALTHY), verify Kill Switch recovery, and run Deterministic Reconciliation (0 discrepancies).",
        "drill": "recover_and_reconcile",
    },
    {
        "step": 17,
        "title": "Show 8-Stage Audit Trace by Correlation ID",
        "screen": "Audit Log",
        "duration_sec": 25,
        "talking_point": "Select a trade Correlation ID to display the complete 8-stage visual trace: Market Event -> Signal -> Risk -> Order -> Fill -> Position -> Exit -> Final P&L.",
    },
]


class GuidedDemoService:
    """Orchestrates deterministic consulting demo preparation, drills, and step verification."""

    @staticmethod
    def get_demo_status(
        session: Session, *, settings: AppSettings | None = None
    ) -> dict[str, Any]:
        """Inspect live database state to report completion across all 17 guided demo steps."""
        _ = settings or get_settings()

        strat_cnt = int(
            session.scalar(select(func.count()).select_from(StrategyVersionModel)) or 0
        )
        bt_cnt = int(
            session.scalar(select(func.count()).select_from(BacktestRunModel)) or 0
        )
        rpl_cnt = int(
            session.scalar(select(func.count()).select_from(ReplaySessionModel)) or 0
        )
        inst_cnt = int(
            session.scalar(select(func.count()).select_from(StrategyInstanceModel)) or 0
        )
        sig_cnt = int(
            session.scalar(select(func.count()).select_from(ReplaySignalModel)) or 0
        )
        approved_rd = session.scalar(
            select(RiskDecisionModel)
            .where(RiskDecisionModel.decision == "APPROVED")
            .order_by(desc(RiskDecisionModel.evaluated_at))
            .limit(1)
        )
        rejected_rd = session.scalar(
            select(RiskDecisionModel)
            .where(
                RiskDecisionModel.decision == "REJECTED",
                RiskDecisionModel.primary_rule.notin_(
                    [
                        "MARKET_DATA_FRESHNESS_LIMIT",
                        "MARKET_DATA_FRESHNESS",
                        "GLOBAL_KILL_SWITCH",
                        "PAPER_TRADING_HALTED",
                    ]
                ),
            )
            .order_by(desc(RiskDecisionModel.evaluated_at))
            .limit(1)
        )
        stale_rd = session.scalar(
            select(RiskDecisionModel)
            .where(
                RiskDecisionModel.decision == "REJECTED",
                RiskDecisionModel.primary_rule.in_(
                    ["MARKET_DATA_FRESHNESS_LIMIT", "MARKET_DATA_FRESHNESS"]
                ),
            )
            .order_by(desc(RiskDecisionModel.evaluated_at))
            .limit(1)
        )
        ks_rd = session.scalar(
            select(RiskDecisionModel)
            .where(
                RiskDecisionModel.decision == "REJECTED",
                RiskDecisionModel.primary_rule.in_(["GLOBAL_KILL_SWITCH", "PAPER_TRADING_HALTED"]),
            )
            .order_by(desc(RiskDecisionModel.evaluated_at))
            .limit(1)
        )
        recovered_ev = session.scalar(
            select(AuditEventModel)
            .where(AuditEventModel.event_type == "MARKET_DATA_RECOVERED")
            .order_by(desc(AuditEventModel.occurred_at))
            .limit(1)
        )
        recon_ok = session.scalar(
            select(ReconciliationRunModel)
            .where(ReconciliationRunModel.status.in_(["COMPLETED", "REPAIRED"]))
            .order_by(desc(ReconciliationRunModel.completed_at))
            .limit(1)
        )

        ord_cnt = int(
            session.scalar(select(func.count()).select_from(PaperOrderModel)) or 0
        )
        fill_cnt = int(
            session.scalar(select(func.count()).select_from(PaperFillModel)) or 0
        )
        positions = list(
            session.scalars(
                select(PaperPositionModel).order_by(desc(PaperPositionModel.opened_at))
            ).all()
        )

        win_pos = next(
            (
                p
                for p in positions
                if (Decimal(str(p.realized_pnl)) + Decimal(str(p.unrealized_pnl))) > Decimal("0")
            ),
            None,
        )
        loss_pos = next(
            (
                p
                for p in positions
                if (Decimal(str(p.realized_pnl)) + Decimal(str(p.unrealized_pnl))) < Decimal("0")
            ),
            None,
        )

        def _corr_for_pos(pos: PaperPositionModel | None) -> str | None:
            if pos is None:
                return None
            f = session.scalar(
                select(PaperFillModel)
                .where(
                    PaperFillModel.account_id == pos.account_id,
                    PaperFillModel.instrument_symbol == pos.instrument_symbol,
                    PaperFillModel.strategy_instance_id == pos.strategy_instance_id,
                )
                .order_by(PaperFillModel.executed_at.asc())
                .limit(1)
            )
            return f.correlation_id if f else None

        win_cid = _corr_for_pos(win_pos)
        loss_cid = _corr_for_pos(loss_pos)
        primary_trace_cid = win_cid or loss_cid or (approved_rd.correlation_id if approved_rd else None)

        step_checks: dict[int, bool] = {
            1: True,
            2: strat_cnt >= 1,
            3: strat_cnt >= 1,
            4: bt_cnt >= 1,
            5: bt_cnt >= 1,
            6: rpl_cnt >= 1,
            7: inst_cnt >= 1,
            8: sig_cnt >= 1,
            9: approved_rd is not None,
            10: ord_cnt >= 1,
            11: fill_cnt >= 1,
            12: len(positions) >= 1,
            13: len(positions) >= 1 and (win_pos is not None or loss_pos is not None),
            14: rejected_rd is not None,
            15: stale_rd is not None,
            16: recovered_ev is not None and recon_ok is not None,
            17: primary_trace_cid is not None,
        }

        steps_out = []
        for s in GUIDED_DEMO_STEPS:
            s_num = int(s["step"])
            steps_out.append({**s, "completed": bool(step_checks.get(s_num, False))})

        completed_count = sum(1 for s in steps_out if s["completed"])
        return {
            "total_steps": len(steps_out),
            "completed_steps": completed_count,
            "estimated_duration": "3–5 minutes",
            "steps": steps_out,
            "showcase_artifacts": {
                "has_profitable_trade": win_pos is not None,
                "has_loss_making_trade": loss_pos is not None,
                "has_risk_rejection": rejected_rd is not None,
                "has_stale_data_block": stale_rd is not None,
                "has_kill_switch_block": ks_rd is not None,
                "has_data_recovery": recovered_ev is not None,
                "has_clean_reconciliation": recon_ok is not None,
                "correlations": {
                    "profitable_trade_cid": win_cid,
                    "loss_making_trade_cid": loss_cid,
                    "risk_rejection_cid": rejected_rd.correlation_id if rejected_rd else None,
                    "stale_data_rejection_cid": stale_rd.correlation_id if stale_rd else None,
                    "kill_switch_rejection_cid": ks_rd.correlation_id if ks_rd else None,
                    "recommended_trace_cid": primary_trace_cid,
                },
            },
        }

    @staticmethod
    def run_full_demo_showcase(
        session: Session,
        *,
        actor: str = "consulting.presenter",
        settings: AppSettings | None = None,
    ) -> dict[str, Any]:
        """Deterministically execute the full engineering showcase using real services.

        Produces:
        - Reproducible NIFTY backtest (winning & losing trades)
        - Live Market Replay + Paper Strategy Instance round-trip trades (both profitable & loss-making)
        - Open mark-to-market BANKNIFTY paper position
        - Deliberate Risk Rejection (MAX_ORDER_VALUE / MAX_STRATEGY_QUANTITY)
        - Stale-Data Protection block (MARKET_DATA_FRESHNESS) + Market-Data Recovery
        - Global Kill Switch block (preserving open position) + Deactivation + Clean Reconciliation
        """
        cfg = settings or get_settings()
        seed_foundation_data(session, cfg)

        # Ensure clean starting gate (Kill Switch off) before running automated showcase
        RiskControlService.deactivate_kill_switch(
            session,
            actor=actor,
            reason="Preparing deterministic consulting demo showcase",
            settings=cfg,
        )

        nifty_ver = session.scalar(
            select(StrategyVersionModel)
            .where(StrategyVersionModel.instrument_symbol == "NIFTY")
            .order_by(StrategyVersionModel.version_number.asc())
            .limit(1)
        )
        assert nifty_ver is not None, "Seeded NIFTY_EMA_RSI_v1 strategy version missing"

        datasets = BacktestService.list_datasets(session, instrument="NIFTY", timeframe="5m")
        nifty_ds_id = datasets[0]["dataset_id"]

        # 1. Deterministic Backtest (Steps 4 & 5)
        tok = set_correlation_id(f"demo-bt-{uuid.uuid4().hex[:10]}")
        try:
            bt_run = BacktestService.execute_backtest(
                session,
                strategy_version_id=nifty_ver.id,
                dataset_id=nifty_ds_id,
                initial_capital=Decimal("500000.00"),
                execution_delay_bars=1,
                actor=actor,
            )
        finally:
            reset_correlation_id(tok)

        # 2. Deploy Strategy in Paper Mode + Start & Step Market Replay 320 bars (Steps 6..13)
        #    Stepping 320 real 5m NIFTY candles naturally produces multiple round-trip trades
        #    including BOTH profitable trades and loss-making trades with zero parameter tampering!
        existing_running = session.scalar(
            select(StrategyInstanceModel).where(
                StrategyInstanceModel.strategy_version_id == nifty_ver.id,
                StrategyInstanceModel.status == "RUNNING",
            )
        )
        if existing_running is None:
            tok = set_correlation_id(f"demo-inst-{uuid.uuid4().hex[:10]}")
            try:
                PaperTradingService.start_strategy_instance(
                    session,
                    strategy_version_id=nifty_ver.id,
                    order_type="MARKET",
                    execution_latency_ms=100,
                    latency_bars=1,
                    slippage_bps=Decimal("2.0"),
                    partial_fill_enabled=False,
                    max_fill_ratio_per_bar=Decimal("1.0"),
                    auto_execute_signals=True,
                    actor=actor,
                    settings=cfg,
                )
            finally:
                reset_correlation_id(tok)

        tok = set_correlation_id(f"demo-rpl-{uuid.uuid4().hex[:10]}")
        try:
            rpl_state = MarketReplayService.start_session(
                session,
                instrument="NIFTY",
                strategy_version_id=nifty_ver.id,
                dataset_id=nifty_ds_id,
                historical_session_date="ALL_SESSIONS",
                replay_speed="25x",
                initial_step_bars=1,
                actor=actor,
                settings=cfg,
            )
            rpl_id = uuid.UUID(str(rpl_state["session"]["id"]))
            MarketReplayService.step_session(
                session,
                rpl_id,
                bars=200,
                actor=actor,
                settings=cfg,
            )
            MarketReplayService.step_session(
                session,
                rpl_id,
                bars=120,
                actor=actor,
                settings=cfg,
            )
        finally:
            reset_correlation_id(tok)

        # Open 1 active BANKNIFTY position so Open Positions & Unrealized MTM P&L are live on Dashboard
        tok = set_correlation_id(f"demo-bn-open-{uuid.uuid4().hex[:10]}")
        try:
            PaperTradingService.submit_manual_paper_order(
                session,
                client_order_key=f"demo-bn-live-{uuid.uuid4().hex[:8]}",
                instrument_symbol="BANKNIFTY",
                side="BUY",
                order_type="MARKET",
                quantity=30,
                reference_price=Decimal("51450.00"),
                latency_bars=0,
                slippage_bps=Decimal("2.0"),
                actor=actor,
                settings=cfg,
            )
        finally:
            reset_correlation_id(tok)

        # 3. Deliberate Risk Rejection Drill (Step 14)
        risk_drill = GuidedDemoService.trigger_demo_drill(
            session, "risk_rejection", actor=actor, settings=cfg
        )

        # 4. Stale-Market-Data Blocking Drill (Step 15)
        stale_drill = GuidedDemoService.trigger_demo_drill(
            session, "stale_data_block", actor=actor, settings=cfg
        )

        # 5. Market-Data Recovery + Kill Switch Drill + Deterministic Reconciliation (Step 16)
        ks_drill = GuidedDemoService.trigger_demo_drill(
            session, "kill_switch_drill", actor=actor, settings=cfg
        )
        rec_drill = GuidedDemoService.trigger_demo_drill(
            session, "recover_and_reconcile", actor=actor, settings=cfg
        )

        status_summary = GuidedDemoService.get_demo_status(session, settings=cfg)
        return {
            "showcase_executed": True,
            "backtest_run_code": bt_run.get("run_code"),
            "drills": {
                "risk_rejection": risk_drill,
                "stale_data_block": stale_drill,
                "kill_switch_drill": ks_drill,
                "recover_and_reconcile": rec_drill,
            },
            "status": status_summary,
        }

    @staticmethod
    def trigger_demo_drill(
        session: Session,
        drill_name: str,
        *,
        actor: str = "consulting.presenter",
        settings: AppSettings | None = None,
    ) -> dict[str, Any]:
        """Execute an individual deterministic operational safety drill for live presentation."""
        cfg = settings or get_settings()
        clean_drill = drill_name.strip().lower()

        if clean_drill == "risk_rejection":
            # Ensure replay feed is fresh so the rejection specifically demonstrates MAX_ORDER_VALUE / MAX_QUANTITY
            GuidedDemoService._ensure_healthy_replay_stream(session, actor=actor, settings=cfg)
            cid = f"demo-risk-rej-{uuid.uuid4().hex[:10]}"
            tok = set_correlation_id(cid)
            try:
                ord_dict, _ = PaperTradingService.submit_manual_paper_order(
                    session,
                    client_order_key=f"demo-oversized-{uuid.uuid4().hex[:8]}",
                    instrument_symbol="BANKNIFTY",
                    side="BUY",
                    order_type="MARKET",
                    quantity=1500,  # ~₹7.7 Cr notional >> ₹30,00,000 MAX_ORDER_VALUE limit
                    reference_price=Decimal("51500.00"),
                    latency_bars=0,
                    actor=actor,
                    settings=cfg,
                )
            finally:
                reset_correlation_id(tok)
            return {
                "drill": "risk_rejection",
                "correlation_id": cid,
                "order_code": ord_dict.get("order_code"),
                "order_status": ord_dict.get("status"),
                "rejection_reason": ord_dict.get("rejection_reason"),
            }

        if clean_drill == "stale_data_block":
            rpl = GuidedDemoService._ensure_healthy_replay_stream(session, actor=actor, settings=cfg)
            if rpl.status == "RUNNING":
                MarketReplayService.pause_session(
                    session, rpl.id, actor=actor, settings=cfg
                )
            cid = f"demo-stale-blk-{uuid.uuid4().hex[:10]}"
            tok = set_correlation_id(cid)
            try:
                ord_dict, _ = PaperTradingService.submit_manual_paper_order(
                    session,
                    client_order_key=f"demo-stale-ord-{uuid.uuid4().hex[:8]}",
                    instrument_symbol="NIFTY",
                    side="BUY",
                    order_type="MARKET",
                    quantity=75,
                    latency_bars=0,
                    require_fresh_market_data=True,
                    actor=actor,
                    settings=cfg,
                )
            finally:
                reset_correlation_id(tok)
            return {
                "drill": "stale_data_block",
                "correlation_id": cid,
                "replay_status": "PAUSED (STALE)",
                "order_code": ord_dict.get("order_code"),
                "order_status": ord_dict.get("status"),
                "rejection_reason": ord_dict.get("rejection_reason"),
            }

        if clean_drill == "kill_switch_drill":
            GuidedDemoService._ensure_healthy_replay_stream(session, actor=actor, settings=cfg)
            cid = f"demo-ks-drill-{uuid.uuid4().hex[:10]}"
            tok = set_correlation_id(cid)
            try:
                RiskControlService.activate_kill_switch(
                    session,
                    actor=actor,
                    reason="Consulting Demo Kill Switch Drill — block new orders, preserve open positions",
                    settings=cfg,
                )
                ord_dict, _ = PaperTradingService.submit_manual_paper_order(
                    session,
                    client_order_key=f"demo-ks-blocked-{uuid.uuid4().hex[:8]}",
                    instrument_symbol="NIFTY",
                    side="BUY",
                    order_type="MARKET",
                    quantity=75,
                    latency_bars=0,
                    actor=actor,
                    settings=cfg,
                )
                RiskControlService.deactivate_kill_switch(
                    session,
                    actor=actor,
                    reason="Consulting Demo Kill Switch Drill complete — safety gates verified",
                    settings=cfg,
                )
            finally:
                reset_correlation_id(tok)
            return {
                "drill": "kill_switch_drill",
                "correlation_id": cid,
                "blocked_order_code": ord_dict.get("order_code"),
                "blocked_order_status": ord_dict.get("status"),
                "rejection_reason": ord_dict.get("rejection_reason"),
                "kill_switch_restored": True,
            }

        if clean_drill in ("recover_market_data", "recover_and_reconcile"):
            rpl = session.scalar(
                select(ReplaySessionModel)
                .order_by(desc(ReplaySessionModel.updated_at))
                .limit(1)
            )
            if rpl is not None and rpl.status in ("PAUSED", "PAUSED_RECOVERY"):
                MarketReplayService.resume_session(
                    session, rpl.id, actor=actor, settings=cfg
                )
            else:
                rpl = GuidedDemoService._ensure_healthy_replay_stream(
                    session, actor=actor, settings=cfg
                )
                if rpl.candles_processed < rpl.total_session_candles:
                    MarketReplayService.step_session(
                        session, rpl.id, bars=1, actor=actor, settings=cfg
                    )

            RiskControlService.deactivate_kill_switch(
                session,
                actor=actor,
                reason="Verified market-data recovery and safety posture",
                settings=cfg,
            )
            recon = ReconciliationService.run_reconciliation(
                session, apply_repair=False, actor=actor, settings=cfg
            )
            return {
                "drill": "recover_and_reconcile",
                "market_data_status": "HEALTHY",
                "reconciliation_run_code": recon.get("run_code"),
                "reconciliation_status": recon.get("status"),
                "discrepancies_count": recon.get("discrepancies_count"),
            }

        from core.exceptions import ControlCenterError

        raise ControlCenterError(
            f"Unsupported demo drill '{drill_name}'. Supported: risk_rejection, stale_data_block, kill_switch_drill, recover_and_reconcile.",
            code="INVALID_DEMO_DRILL",
        )

    @staticmethod
    def _ensure_healthy_replay_stream(
        session: Session, *, actor: str, settings: AppSettings
    ) -> ReplaySessionModel:
        rpl = session.scalar(
            select(ReplaySessionModel)
            .order_by(desc(ReplaySessionModel.updated_at))
            .limit(1)
        )
        if rpl is not None and rpl.status == "RUNNING" and rpl.candles_processed < rpl.total_session_candles:
            MarketReplayService.step_session(
                session, rpl.id, bars=1, actor=actor, settings=settings
            )
            return rpl
        if rpl is not None and rpl.status in ("PAUSED", "PAUSED_RECOVERY") and rpl.candles_processed < rpl.total_session_candles:
            MarketReplayService.resume_session(
                session, rpl.id, actor=actor, settings=settings
            )
            return rpl

        nifty_ver = session.scalar(
            select(StrategyVersionModel)
            .where(StrategyVersionModel.instrument_symbol == "NIFTY")
            .order_by(StrategyVersionModel.version_number.asc())
            .limit(1)
        )
        assert nifty_ver is not None
        datasets = BacktestService.list_datasets(session, instrument="NIFTY", timeframe="5m")
        started = MarketReplayService.start_session(
            session,
            instrument="NIFTY",
            strategy_version_id=nifty_ver.id,
            dataset_id=datasets[0]["dataset_id"],
            historical_session_date="ALL_SESSIONS",
            replay_speed="10x",
            initial_step_bars=2,
            actor=actor,
            settings=settings,
        )
        new_id = uuid.UUID(str(started["session"]["id"]))
        res_row = session.get(ReplaySessionModel, new_id)
        assert res_row is not None
        return res_row
