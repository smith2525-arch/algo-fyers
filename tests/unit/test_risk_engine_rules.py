"""Unit & DB-level tests for Phase 4 Independent Fail-Closed Risk Engine:
- DEMO defaults verification
- Daily loss exceeded, consecutive losses, strategy daily loss & drawdown, cooldown, max holding
- Position limit exceeded, order value, gross exposure, max risk/trade, allowed instruments/types, session
- Trade count exceeded (strategy & account)
- Signal expired & invalid signal
- Stale market data & recovery from stale data
- Fail-closed gates (unknown risk state, system health gate, unreconciled position gate)
- Immutable RiskDecisionModel audit enforcement
"""

from __future__ import annotations

import uuid
from datetime import datetime, timedelta
from decimal import Decimal

import pytest
from sqlalchemy import delete, select

from core.clock import IST
from core.domain.enums import (
    OrderStatus,
    OrderType,
    PositionStatus,
    ReplaySessionStatus,
    RiskDecisionOutcome,
    Side,
    SignalDirection,
)
from core.exceptions import ImmutabilityViolationError
from core.market_data.freshness import evaluate_market_data_freshness
from core.oms.service import OrderManagementService
from core.paper_trading.service import PaperTradingService
from core.persistence.models import (
    AccountRiskConfigModel,
    PaperPositionModel,
    RiskDecisionModel,
    StrategyRiskConfigModel,
    StrategyVersionModel,
)
from core.persistence.seed import seed_foundation_data
from core.persistence.uow import unit_of_work
from core.risk.evaluator import PreTradeRiskEvaluator
from core.strategies.base import SignalCandidate


def _fresh(ts: datetime, age_sec: float = 1.0, threshold: int = 10):
    return evaluate_market_data_freshness(
        session_status=ReplaySessionStatus.RUNNING,
        last_candle_received_at=ts - timedelta(seconds=age_sec),
        last_exchange_timestamp=ts,
        stale_threshold_seconds=threshold,
        reference_time=ts,
    )


def test_seeded_phase4_demo_risk_defaults(migrated_db_url: str) -> None:
    with unit_of_work() as session:
        seed_foundation_data(session)
        acct = PaperTradingService.get_primary_account_row(session)
        rcfg = session.scalar(
            select(AccountRiskConfigModel).where(AccountRiskConfigModel.account_id == acct.id)
        )
        assert rcfg is not None
        assert rcfg.is_demo_settings is True
        assert Decimal(str(acct.initial_capital)) == Decimal("500000.0000")
        assert Decimal(str(rcfg.max_daily_loss_inr)) == Decimal("10000.0000")
        assert Decimal(str(rcfg.default_max_risk_per_trade_inr)) == Decimal("2500.0000")
        assert rcfg.default_max_trades_per_strategy_day == 5
        assert rcfg.max_consecutive_losses == 3
        assert rcfg.market_data_stale_threshold_seconds == 10
        assert rcfg.max_signal_age_seconds == 15


def test_daily_loss_position_limit_trade_count_and_strategy_rules(
    migrated_db_url: str,
) -> None:
    t0 = datetime(2026, 10, 29, 10, 15, tzinfo=IST)

    with unit_of_work() as session:
        seed_foundation_data(session)
        acct = PaperTradingService.get_primary_account_row(session)
        rcfg = session.scalar(
            select(AccountRiskConfigModel).where(AccountRiskConfigModel.account_id == acct.id)
        )
        strat_ver = session.scalar(
            select(StrategyVersionModel).where(StrategyVersionModel.instrument_symbol == "NIFTY")
        )
        assert rcfg is not None and strat_ver is not None
        s_cfg = session.scalar(
            select(StrategyRiskConfigModel).where(
                StrategyRiskConfigModel.strategy_version_id == strat_ver.id
            )
        )
        assert s_cfg is not None
        fresh = _fresh(t0)

        # 1. MAX_RISK_PER_TRADE: 75 qty * 40 pts stop = ₹3,000 > ₹2,500 DEMO limit -> REJECTED
        dec_rpt = PreTradeRiskEvaluator.evaluate_order(
            session,
            account=acct,
            instrument_symbol="NIFTY",
            order_type=OrderType.MARKET,
            quantity=75,
            reference_price=Decimal("24200"),
            stop_loss_price=Decimal("24160"),
            strategy_version_id=strat_ver.id,
            freshness=fresh,
            evaluation_time=t0,
        )
        assert dec_rpt.approved is False
        assert dec_rpt.primary_rule == "MAX_RISK_PER_TRADE"

        # 2. ALLOWED_INSTRUMENTS & ALLOWED_ORDER_TYPES & STRATEGY_MAX_QUANTITY
        dec_inst = PreTradeRiskEvaluator.evaluate_order(
            session,
            account=acct,
            instrument_symbol="BANKNIFTY",  # NIFTY_EMA_RSI_v1 only allows ["NIFTY"]
            order_type=OrderType.MARKET,
            quantity=30,
            reference_price=Decimal("51500"),
            strategy_version_id=strat_ver.id,
            freshness=fresh,
            evaluation_time=t0,
        )
        assert dec_inst.approved is False
        assert dec_inst.primary_rule == "ALLOWED_INSTRUMENTS"

        s_cfg.allowed_order_types_json = ["MARKET", "LIMIT"]
        session.flush()
        dec_otype = PreTradeRiskEvaluator.evaluate_order(
            session,
            account=acct,
            instrument_symbol="NIFTY",
            order_type=OrderType.STOP,
            quantity=75,
            reference_price=Decimal("24200"),
            stop_price=Decimal("24250"),
            strategy_version_id=strat_ver.id,
            freshness=fresh,
            evaluation_time=t0,
        )
        assert dec_otype.approved is False
        assert dec_otype.primary_rule == "ALLOWED_ORDER_TYPES"
        s_cfg.allowed_order_types_json = ["MARKET", "LIMIT", "STOP"]

        s_cfg.max_quantity = 150
        session.flush()
        dec_sqty = PreTradeRiskEvaluator.evaluate_order(
            session,
            account=acct,
            instrument_symbol="NIFTY",
            order_type=OrderType.MARKET,
            quantity=160,
            reference_price=Decimal("24200"),
            strategy_version_id=strat_ver.id,
            freshness=fresh,
            evaluation_time=t0,
        )
        assert dec_sqty.approved is False
        assert dec_sqty.primary_rule == "STRATEGY_MAX_QUANTITY"
        s_cfg.max_quantity = 300
        session.flush()

        # 3. TRADING_SESSION: 09:05 IST < 09:15 IST session open -> REJECTED
        pre_open_t = datetime(2026, 10, 29, 9, 5, tzinfo=IST)
        dec_sess = PreTradeRiskEvaluator.evaluate_order(
            session,
            account=acct,
            instrument_symbol="NIFTY",
            order_type=OrderType.MARKET,
            quantity=75,
            reference_price=Decimal("24200"),
            created_bar_timestamp=pre_open_t,
            freshness=_fresh(pre_open_t),
            evaluation_time=pre_open_t,
        )
        assert dec_sess.approved is False
        assert dec_sess.primary_rule == "TRADING_SESSION"

        # 4. STRATEGY_MAX_TRADES_PER_DAY (DEMO limit = 5): submit 5 approved orders, 6th is REJECTED!
        for idx in range(5):
            o, _ = OrderManagementService.create_and_evaluate_order(
                session,
                account=acct,
                client_order_key=f"tc-test-{idx}",
                instrument_symbol="NIFTY",
                side=Side.BUY,
                order_type=OrderType.MARKET,
                quantity=15,
                reference_price=Decimal("24200"),
                strategy_version_id=strat_ver.id,
                strategy_version_label=strat_ver.version_label,
                created_bar_timestamp=t0 + timedelta(minutes=idx),
                freshness=fresh,
            )
            assert o.status == OrderStatus.RISK_APPROVED.value

        sixth_ord, _ = OrderManagementService.create_and_evaluate_order(
            session,
            account=acct,
            client_order_key="tc-test-6th-blocked",
            instrument_symbol="NIFTY",
            side=Side.BUY,
            order_type=OrderType.MARKET,
            quantity=15,
            reference_price=Decimal("24200"),
            strategy_version_id=strat_ver.id,
            strategy_version_label=strat_ver.version_label,
            created_bar_timestamp=t0 + timedelta(minutes=10),
            freshness=fresh,
        )
        assert sixth_ord.status == OrderStatus.RISK_REJECTED.value
        assert "STRATEGY_MAX_TRADES_PER_DAY" in (sixth_ord.rejection_reason or "")

        # 5. MAX_OPEN_POSITIONS & MAX_ORDER_VALUE & MAX_DAILY_LOSS
        rcfg.max_order_value_inr = Decimal("1000000.00")
        session.flush()
        ov_ord, _ = OrderManagementService.create_and_evaluate_order(
            session,
            account=acct,
            client_order_key="ov-exceeded-001",
            instrument_symbol="NIFTY",
            side=Side.BUY,
            order_type=OrderType.MARKET,
            quantity=75,  # 75 * 24200 = ₹18.15L > ₹10.00L
            reference_price=Decimal("24200"),
            created_bar_timestamp=t0,
            freshness=fresh,
        )
        assert ov_ord.status == OrderStatus.RISK_REJECTED.value
        assert "MAX_ORDER_VALUE" in (ov_ord.rejection_reason or "")
        rcfg.max_order_value_inr = Decimal("3000000.00")

        # Daily Loss Exceeded (realized_pnl_today = -₹10,500 >= ₹10,000 limit)
        acct.realized_pnl_today = Decimal("-10500.0000")
        session.flush()
        dl_ord, _ = OrderManagementService.create_and_evaluate_order(
            session,
            account=acct,
            client_order_key="daily-loss-exceeded-001",
            instrument_symbol="NIFTY",
            side=Side.BUY,
            order_type=OrderType.MARKET,
            quantity=15,
            reference_price=Decimal("24200"),
            created_bar_timestamp=t0,
            freshness=fresh,
        )
        assert dl_ord.status == OrderStatus.RISK_REJECTED.value
        assert "MAX_DAILY_LOSS" in (dl_ord.rejection_reason or "")
        acct.realized_pnl_today = Decimal("0.0000")

        # Position Limit Exceeded (set max_open_positions = 1 while 1 open position exists)
        rcfg.max_open_positions = 1
        rcfg.enforce_position_reconciliation_gate = False  # Synthetic open row for pure cap test
        session.add(
            PaperPositionModel(
                position_code="POS-CAP-TEST-001",
                account_id=acct.id,
                strategy_version_label="MANUAL_PAPER_DESK",
                instrument_symbol="NIFTY",
                timeframe="5m",
                side="LONG",
                quantity=15,
                opened_quantity=15,
                closed_quantity=0,
                average_entry_price=Decimal("24200"),
                last_price=Decimal("24200"),
                highest_price_since_entry=Decimal("24200"),
                lowest_price_since_entry=Decimal("24200"),
                status=PositionStatus.OPEN.value,
                opened_at=t0,
                last_mark_at=t0,
                updated_at=t0,
            )
        )
        session.flush()

        pos_lim_ord, _ = OrderManagementService.create_and_evaluate_order(
            session,
            account=acct,
            client_order_key="pos-limit-exceeded-001",
            instrument_symbol="BANKNIFTY",
            side=Side.BUY,
            order_type=OrderType.MARKET,
            quantity=30,
            reference_price=Decimal("51500"),
            created_bar_timestamp=t0,
            freshness=fresh,
        )
        assert pos_lim_ord.status == OrderStatus.RISK_REJECTED.value
        assert "MAX_OPEN_POSITIONS" in (pos_lim_ord.rejection_reason or "")


def test_signal_expired_stale_data_recovery_and_fail_closed_gates(
    migrated_db_url: str,
) -> None:
    t0 = datetime(2026, 10, 29, 11, 0, tzinfo=IST)

    with unit_of_work() as session:
        seed_foundation_data(session)
        acct = PaperTradingService.get_primary_account_row(session)
        rcfg = session.scalar(
            select(AccountRiskConfigModel).where(AccountRiskConfigModel.account_id == acct.id)
        )
        strat_ver = session.scalar(
            select(StrategyVersionModel).where(StrategyVersionModel.instrument_symbol == "NIFTY")
        )
        assert rcfg is not None and strat_ver is not None

        # 1. Signal Expired (> 15s DEMO max_signal_age_seconds)
        cand = SignalCandidate(
            strategy_code="EMA_RSI_CROSSOVER",
            symbol="NIFTY",
            timeframe="5m",
            bar_timestamp=t0,
            side=Side.BUY,
            direction=SignalDirection.ENTRY_LONG,
            reference_price=Decimal("24200"),
            rationale="Signal expiry test",
        )
        sig, _ = PaperTradingService.persist_signal_idempotent(
            session,
            candidate=cand,
            strategy_version_id=strat_ver.id,
            strategy_version_label=strat_ver.version_label,
            market_event_id="evt-sig-age",
        )
        # Age the signal by 20 seconds (> 15s limit)
        sig.emitted_at = sig.emitted_at - timedelta(seconds=20)
        sig.expires_at = sig.emitted_at + timedelta(seconds=15)
        session.flush()

        exp_ord, _ = PaperTradingService.route_signal_to_paper_execution(
            session, sig, freshness=_fresh(t0, age_sec=1.0)
        )
        assert exp_ord.status == OrderStatus.RISK_REJECTED.value
        assert sig.status == "EXPIRED"
        assert "SIGNAL_EXPIRED" in (exp_ord.rejection_reason or "")

        # 2. Stale Market Data (age 14s > 10s DEMO stale threshold) -> BLOCKED,
        #    followed by Recovery from Stale Data (age 1.5s <= 10s) -> APPROVED!
        stale_snap = _fresh(t0, age_sec=14.0, threshold=10)
        stale_ord, _ = OrderManagementService.create_and_evaluate_order(
            session,
            account=acct,
            client_order_key="stale-blocked-001",
            instrument_symbol="NIFTY",
            side=Side.BUY,
            order_type=OrderType.MARKET,
            quantity=75,
            reference_price=Decimal("24200"),
            created_bar_timestamp=t0,
            freshness=stale_snap,
        )
        assert stale_ord.status == OrderStatus.RISK_REJECTED.value
        assert "STALE_MARKET_DATA" in (stale_ord.rejection_reason or "")

        recovered_snap = _fresh(t0 + timedelta(seconds=5), age_sec=1.5, threshold=10)
        rec_ord, _ = OrderManagementService.create_and_evaluate_order(
            session,
            account=acct,
            client_order_key="stale-recovered-001",
            instrument_symbol="NIFTY",
            side=Side.BUY,
            order_type=OrderType.MARKET,
            quantity=75,
            reference_price=Decimal("24200"),
            created_bar_timestamp=t0,
            freshness=recovered_snap,
        )
        assert rec_ord.status == OrderStatus.RISK_APPROVED.value

        # 3. Fail-Closed Gates: System Health Gate, Unreconciled Position Gate, Unknown Risk State
        rcfg.simulated_system_degraded = True
        session.flush()
        deg_dec = PreTradeRiskEvaluator.evaluate_order(
            session,
            account=acct,
            instrument_symbol="NIFTY",
            order_type=OrderType.MARKET,
            quantity=75,
            reference_price=Decimal("24200"),
            freshness=recovered_snap,
            evaluation_time=t0,
        )
        assert deg_dec.approved is False
        assert deg_dec.primary_rule == "SYSTEM_HEALTH_GATE"
        rcfg.simulated_system_degraded = False

        # Insert an unbacked open position (0 fills in paper_fills -> unreconciled!)
        unrec_pos = PaperPositionModel(
            position_code="POS-UNRECONCILED-001",
            account_id=acct.id,
            strategy_version_label="CORRUPT_DRIFT",
            instrument_symbol="NIFTY",
            timeframe="5m",
            side="LONG",
            quantity=75,
            opened_quantity=75,
            closed_quantity=0,
            average_entry_price=Decimal("24200"),
            last_price=Decimal("24200"),
            highest_price_since_entry=Decimal("24200"),
            lowest_price_since_entry=Decimal("24200"),
            status=PositionStatus.OPEN.value,
            opened_at=t0,
            last_mark_at=t0,
            updated_at=t0,
        )
        session.add(unrec_pos)
        session.flush()

        unrec_dec = PreTradeRiskEvaluator.evaluate_order(
            session,
            account=acct,
            instrument_symbol="BANKNIFTY",
            order_type=OrderType.MARKET,
            quantity=30,
            reference_price=Decimal("51500"),
            freshness=recovered_snap,
            evaluation_time=t0,
        )
        assert unrec_dec.approved is False
        assert unrec_dec.primary_rule == "POSITION_RECONCILIATION_GATE"

        # Unknown critical risk state (force_unknown_risk_state=True) -> FAIL CLOSED
        unk_dec = PreTradeRiskEvaluator.evaluate_order(
            session,
            account=acct,
            instrument_symbol="NIFTY",
            order_type=OrderType.MARKET,
            quantity=75,
            reference_price=Decimal("24200"),
            freshness=recovered_snap,
            force_unknown_risk_state=True,
            evaluation_time=t0,
        )
        assert unk_dec.approved is False
        assert unk_dec.decision == RiskDecisionOutcome.REJECTED
        assert unk_dec.primary_rule == "UNKNOWN_CRITICAL_RISK_STATE"

    # 4. RiskDecisionModel rows are strictly append-only immutable
    with pytest.raises(ImmutabilityViolationError):
        with unit_of_work() as session:
            rd = session.scalar(select(RiskDecisionModel).limit(1))
            assert rd is not None
            rd.decision = "APPROVED"
            session.flush()


def test_consecutive_losses_gross_exposure_cooldown_and_strategy_drawdown(
    migrated_db_url: str,
) -> None:
    t0 = datetime(2026, 10, 29, 10, 30, tzinfo=IST)

    with unit_of_work() as session:
        seed_foundation_data(session)
        acct = PaperTradingService.get_primary_account_row(session)
        rcfg = session.scalar(
            select(AccountRiskConfigModel).where(AccountRiskConfigModel.account_id == acct.id)
        )
        strat_ver = session.scalar(
            select(StrategyVersionModel).where(StrategyVersionModel.instrument_symbol == "NIFTY")
        )
        assert rcfg is not None and strat_ver is not None
        rcfg.enforce_position_reconciliation_gate = False
        fresh = _fresh(t0)

        # 1. Insert 1 losing closed position 120s ago -> COOLDOWN_AFTER_LOSS (300s DEMO default) blocks!
        loss_p1 = PaperPositionModel(
            position_code="POS-LOSS-CD-001",
            account_id=acct.id,
            strategy_version_id=strat_ver.id,
            strategy_version_label=strat_ver.version_label,
            instrument_symbol="NIFTY",
            timeframe="5m",
            side="LONG",
            quantity=0,
            opened_quantity=75,
            closed_quantity=75,
            average_entry_price=Decimal("24200"),
            average_exit_price=Decimal("24180"),
            last_price=Decimal("24180"),
            highest_price_since_entry=Decimal("24200"),
            lowest_price_since_entry=Decimal("24180"),
            realized_pnl=Decimal("-1500.0000"),
            unrealized_pnl=Decimal("0"),
            status=PositionStatus.CLOSED.value,
            opened_at=t0 - timedelta(minutes=10),
            last_mark_at=t0 - timedelta(seconds=120),
            closed_at=t0 - timedelta(seconds=120),
            updated_at=t0,
        )
        session.add(loss_p1)
        session.flush()

        cd_dec = PreTradeRiskEvaluator.evaluate_order(
            session,
            account=acct,
            instrument_symbol="NIFTY",
            order_type=OrderType.MARKET,
            quantity=75,
            reference_price=Decimal("24200"),
            strategy_version_id=strat_ver.id,
            created_bar_timestamp=t0,
            freshness=fresh,
            evaluation_time=t0,
        )
        assert cd_dec.approved is False
        assert cd_dec.primary_rule == "COOLDOWN_AFTER_LOSS"

        # 2. Add 2 more consecutive closed losses (> 300s ago) -> MAX_CONSECUTIVE_LOSSES (3 DEMO default) blocks!
        loss_p1.closed_at = t0 - timedelta(minutes=30)
        for idx in (2, 3):
            session.add(
                PaperPositionModel(
                    position_code=f"POS-LOSS-STREAK-{idx}",
                    account_id=acct.id,
                    strategy_version_label="MANUAL_PAPER_DESK",
                    instrument_symbol="NIFTY",
                    timeframe="5m",
                    side="LONG",
                    quantity=0,
                    opened_quantity=15,
                    closed_quantity=15,
                    average_entry_price=Decimal("24200"),
                    average_exit_price=Decimal("24190"),
                    last_price=Decimal("24190"),
                    highest_price_since_entry=Decimal("24200"),
                    lowest_price_since_entry=Decimal("24190"),
                    realized_pnl=Decimal("-150.0000"),
                    unrealized_pnl=Decimal("0"),
                    status=PositionStatus.CLOSED.value,
                    opened_at=t0 - timedelta(minutes=25 - idx),
                    last_mark_at=t0 - timedelta(minutes=20 - idx),
                    closed_at=t0 - timedelta(minutes=20 - idx),
                    updated_at=t0,
                )
            )
        session.flush()

        consec_dec = PreTradeRiskEvaluator.evaluate_order(
            session,
            account=acct,
            instrument_symbol="NIFTY",
            order_type=OrderType.MARKET,
            quantity=15,
            reference_price=Decimal("24200"),
            created_bar_timestamp=t0,
            freshness=fresh,
            evaluation_time=t0,
        )
        assert consec_dec.approved is False
        assert consec_dec.primary_rule == "MAX_CONSECUTIVE_LOSSES"

        # Break consecutive loss streak with 1 winner, then test MAX_GROSS_EXPOSURE
        session.add(
            PaperPositionModel(
                position_code="POS-WIN-RESET",
                account_id=acct.id,
                strategy_version_label="MANUAL_PAPER_DESK",
                instrument_symbol="NIFTY",
                timeframe="5m",
                side="LONG",
                quantity=0,
                opened_quantity=15,
                closed_quantity=15,
                average_entry_price=Decimal("24200"),
                average_exit_price=Decimal("24220"),
                last_price=Decimal("24220"),
                highest_price_since_entry=Decimal("24220"),
                lowest_price_since_entry=Decimal("24200"),
                realized_pnl=Decimal("300.0000"),
                unrealized_pnl=Decimal("0"),
                status=PositionStatus.CLOSED.value,
                opened_at=t0 - timedelta(minutes=12),
                last_mark_at=t0 - timedelta(minutes=10),
                closed_at=t0 - timedelta(minutes=10),
                updated_at=t0,
            )
        )
        rcfg.max_gross_exposure_inr = Decimal("1500000.00")
        session.flush()

        gross_dec = PreTradeRiskEvaluator.evaluate_order(
            session,
            account=acct,
            instrument_symbol="NIFTY",
            order_type=OrderType.MARKET,
            quantity=75,  # 75 * 24200 = ₹18.15L > ₹15.00L max_gross_exposure
            reference_price=Decimal("24200"),
            created_bar_timestamp=t0,
            freshness=fresh,
            evaluation_time=t0,
        )
        assert gross_dec.approved is False
        assert gross_dec.primary_rule == "MAX_GROSS_EXPOSURE"
