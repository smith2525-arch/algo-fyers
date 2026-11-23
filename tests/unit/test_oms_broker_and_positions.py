"""Unit & DB-level tests for Phase 3B OMS state machine, Paper Broker fill model
(MARKET / LIMIT trade-through / STOP trigger / latency / slippage / partial fills /
rejection / cancel / expiration), Idempotency (duplicate signal / order / fill /
position event), and Position & P&L math (realized & unrealized P&L).
"""

from __future__ import annotations

import uuid
from datetime import datetime, timedelta
from decimal import Decimal

import pytest
from sqlalchemy import func, select

from core.broker.paper_broker import PaperBrokerService
from core.clock import IST
from core.domain.enums import (
    MarketEventType,
    MarketFeedSource,
    OrderSource,
    OrderStatus,
    OrderType,
    PositionStatus,
    ReplaySessionStatus,
    Side,
    SignalDirection,
    SignalStatus,
)
from core.exceptions import (
    DuplicateEntityError,
    ImmutabilityViolationError,
    InvalidStateTransitionError,
)
from core.market_data.events import CandleEvent
from core.market_data.freshness import evaluate_market_data_freshness
from core.oms.service import OrderManagementService
from core.paper_trading.service import PaperTradingService
from core.persistence.models import (
    PaperFillModel,
    PaperOrderModel,
    PaperPositionModel,
    PositionEventModel,
    ReplaySignalModel,
    StrategyVersionModel,
)
from core.persistence.seed import seed_foundation_data
from core.persistence.uow import unit_of_work
from core.positions.service import PositionService
from core.strategies.base import SignalCandidate


def _make_candle(
    *,
    ts: datetime,
    open_px: str,
    high_px: str,
    low_px: str,
    close_px: str,
    seq: int = 1,
    symbol: str = "NIFTY",
) -> CandleEvent:
    return CandleEvent(
        event_id=uuid.uuid5(uuid.NAMESPACE_OID, f"{symbol}:{ts.isoformat()}:{seq}"),
        event_type=MarketEventType.CANDLE,
        source=MarketFeedSource.HISTORICAL_REPLAY,
        symbol=symbol,
        exchange_timestamp=ts,
        received_at=ts,
        sequence_number=seq,
        timeframe="5m",
        open=Decimal(open_px),
        high=Decimal(high_px),
        low=Decimal(low_px),
        close=Decimal(close_px),
        volume=15000,
        is_closed=True,
    )


def _healthy_freshness(ts: datetime):
    return evaluate_market_data_freshness(
        session_status=ReplaySessionStatus.RUNNING,
        last_candle_received_at=ts,
        last_exchange_timestamp=ts,
        stale_threshold_seconds=15,
        reference_time=ts,
    )


def test_duplicate_signal_order_fill_and_position_event_idempotency(
    migrated_db_url: str,
) -> None:
    """Verify strict duplicate prevention across signals, orders, fills, and position events."""
    t0 = datetime(2026, 10, 29, 10, 0, tzinfo=IST)
    t1 = t0 + timedelta(minutes=5)

    with unit_of_work() as session:
        seed_foundation_data(session)
        acct = PaperTradingService.get_primary_account_row(session)
        strat_ver = session.scalar(
            select(StrategyVersionModel).where(StrategyVersionModel.instrument_symbol == "NIFTY")
        )
        assert strat_ver is not None

        cand = SignalCandidate(
            strategy_code="EMA_RSI_CROSSOVER",
            symbol="NIFTY",
            timeframe="5m",
            bar_timestamp=t0,
            side=Side.BUY,
            direction=SignalDirection.ENTRY_LONG,
            reference_price=Decimal("24200.00"),
            rationale="Bullish EMA cross + RSI >= 55",
            indicators={"fast_ema": "24205", "slow_ema": "24195", "rsi": "58"},
        )

        # 1. Duplicate Signal emission returns same signal ID & never inserts twice
        sig1, dup_sig1 = PaperTradingService.persist_signal_idempotent(
            session,
            candidate=cand,
            strategy_version_id=strat_ver.id,
            strategy_version_label=strat_ver.version_label,
            market_event_id="evt-001",
        )
        sig2, dup_sig2 = PaperTradingService.persist_signal_idempotent(
            session,
            candidate=cand,
            strategy_version_id=strat_ver.id,
            strategy_version_label=strat_ver.version_label,
            market_event_id="evt-001",
        )
        assert dup_sig1 is False
        assert dup_sig2 is True
        assert sig1.id == sig2.id
        assert session.scalar(select(func.count()).select_from(ReplaySignalModel)) == 1

        # 2. Routing the same Signal twice into OMS never creates multiple orders
        fresh = _healthy_freshness(t0)
        ord1, dup_ord_sig1 = PaperTradingService.route_signal_to_paper_execution(
            session, sig1, freshness=fresh
        )
        ord2, dup_ord_sig2 = PaperTradingService.route_signal_to_paper_execution(
            session, sig1, freshness=fresh
        )
        assert dup_ord_sig1 is False
        assert dup_ord_sig2 is True
        assert ord1.id == ord2.id
        assert sig1.status == SignalStatus.ORDER_CREATED.value
        assert ord1.status == OrderStatus.ACKNOWLEDGED.value  # Does NOT auto-fill on t0 close!

        # Even if a caller supplies a brand-new client_order_key for the same signal,
        # OMS returns the original order idempotently (dup=True) and never creates a second order!
        ord3, dup_ord_sig3 = OrderManagementService.create_and_evaluate_order(
            session,
            account=acct,
            client_order_key="forced-second-order-for-same-signal",
            instrument_symbol="NIFTY",
            side=Side.BUY,
            order_type=OrderType.MARKET,
            quantity=75,
            reference_price=Decimal("24200"),
            signal=sig1,
            freshness=fresh,
        )
        assert dup_ord_sig3 is True
        assert ord3.id == ord1.id
        assert (
            session.scalar(
                select(func.count())
                .select_from(PaperOrderModel)
                .where(PaperOrderModel.signal_id == sig1.id)
            )
            == 1
        )

        # And an EXPIRED signal with 0 orders raises DuplicateEntityError if someone tries to order on it
        exp_cand = SignalCandidate(
            strategy_code="EMA_RSI_CROSSOVER",
            symbol="NIFTY",
            timeframe="5m",
            bar_timestamp=t0 + timedelta(minutes=15),
            side=Side.SELL,
            direction=SignalDirection.ENTRY_SHORT,
            reference_price=Decimal("24150.00"),
            rationale="Expired signal test",
        )
        exp_sig, _ = PaperTradingService.persist_signal_idempotent(
            session,
            candidate=exp_cand,
            strategy_version_id=strat_ver.id,
            strategy_version_label=strat_ver.version_label,
            market_event_id="evt-exp",
        )
        exp_sig.status = SignalStatus.EXPIRED.value
        session.flush()
        with pytest.raises(DuplicateEntityError):
            OrderManagementService.create_and_evaluate_order(
                session,
                account=acct,
                client_order_key="order-from-expired-signal",
                instrument_symbol="NIFTY",
                side=Side.SELL,
                order_type=OrderType.MARKET,
                quantity=75,
                reference_price=Decimal("24150"),
                signal=exp_sig,
                freshness=fresh,
            )

        # 3. Duplicate manual order request with identical client_order_key
        m_ord1, m_dup1 = OrderManagementService.create_and_evaluate_order(
            session,
            account=acct,
            client_order_key="idem-manual-001",
            instrument_symbol="BANKNIFTY",
            side=Side.BUY,
            order_type=OrderType.LIMIT,
            quantity=30,
            reference_price=Decimal("51500"),
            limit_price=Decimal("51400"),
            freshness=fresh,
        )
        m_ord2, m_dup2 = OrderManagementService.create_and_evaluate_order(
            session,
            account=acct,
            client_order_key="idem-manual-001",
            instrument_symbol="BANKNIFTY",
            side=Side.BUY,
            order_type=OrderType.LIMIT,
            quantity=30,
            reference_price=Decimal("51500"),
            limit_price=Decimal("51400"),
            freshness=fresh,
        )
        assert m_dup1 is False
        assert m_dup2 is True
        assert m_ord1.id == m_ord2.id

        # 4. Duplicate Fill & Position Event Idempotency
        fill1, f_dup1 = PaperBrokerService.record_execution_fill(
            session,
            order=ord1,
            fill_quantity=ord1.quantity,
            raw_market_price=Decimal("24210"),
            fill_price=Decimal("24212"),
            bar_timestamp=t1,
            fill_idempotency_key="idem-fill-001",
        )
        fill2, f_dup2 = PaperBrokerService.record_execution_fill(
            session,
            order=ord1,
            fill_quantity=ord1.quantity,
            raw_market_price=Decimal("24210"),
            fill_price=Decimal("24212"),
            bar_timestamp=t1,
            fill_idempotency_key="idem-fill-001",
        )
        assert f_dup1 is False
        assert f_dup2 is True
        assert fill1.id == fill2.id
        assert ord1.status == OrderStatus.FILLED.value
        assert ord1.filled_quantity == ord1.quantity

        # Re-applying the same fill to PositionService is a strict no-op
        pos1, p_dup = PositionService.apply_fill_to_position(session, fill1, order=ord1)
        assert p_dup is True
        assert pos1.quantity == ord1.quantity
        assert session.scalar(select(func.count()).select_from(PaperFillModel)) == 1
        assert session.scalar(select(func.count()).select_from(PositionEventModel)) == 1

    # 5. PaperFill records are strictly immutable once persisted
    with pytest.raises(ImmutabilityViolationError):
        with unit_of_work() as session:
            f_row = session.scalar(select(PaperFillModel).limit(1))
            assert f_row is not None
            f_row.fill_price = Decimal("99999.00")
            session.flush()


def test_paper_broker_limit_trade_through_stop_trigger_partial_fills_cancel_reject_and_expire(
    migrated_db_url: str,
) -> None:
    """Verify MARKET/LIMIT/STOP fill models, latency, partial fills, rejection, cancel & expiry."""
    t0 = datetime(2026, 10, 29, 9, 30, tzinfo=IST)
    t1 = t0 + timedelta(minutes=5)
    t2 = t0 + timedelta(minutes=10)
    t3 = t0 + timedelta(minutes=15)

    with unit_of_work() as session:
        seed_foundation_data(session)
        acct = PaperTradingService.get_primary_account_row(session)
        fresh = _healthy_freshness(t0)

        # A. Pre-Trade RISK_REJECTED (exceeding demo capital margin)
        oversized, _ = OrderManagementService.create_and_evaluate_order(
            session,
            account=acct,
            client_order_key="ord-risk-rej",
            instrument_symbol="NIFTY",
            side=Side.BUY,
            order_type=OrderType.MARKET,
            quantity=1500,  # 1500 * 24200 * 0.12 = ₹43.56L > ₹5.00L demo capital
            reference_price=Decimal("24200"),
            freshness=fresh,
        )
        assert oversized.status == OrderStatus.RISK_REJECTED.value
        assert "INSUFFICIENT_PAPER_CAPITAL" in (oversized.rejection_reason or "")

        # B. Simulated Paper Broker REJECTED (SUBMITTED -> REJECTED)
        sim_rej, _ = OrderManagementService.create_and_evaluate_order(
            session,
            account=acct,
            client_order_key="ord-broker-rej",
            instrument_symbol="NIFTY",
            side=Side.BUY,
            order_type=OrderType.MARKET,
            quantity=75,
            reference_price=Decimal("24200"),
            simulate_rejection=True,
            simulated_rejection_reason="Simulated exchange price-band rejection",
            freshness=fresh,
        )
        PaperBrokerService.submit_order_to_paper_broker(session, sim_rej)
        assert sim_rej.status == OrderStatus.REJECTED.value
        assert "price-band" in (sim_rej.rejection_reason or "")

        # C. BUY LIMIT with strict trade-through + 50% Partial Fill across 2 bars
        lim_ord, _ = OrderManagementService.create_and_evaluate_order(
            session,
            account=acct,
            client_order_key="ord-buy-limit-partial",
            instrument_symbol="NIFTY",
            side=Side.BUY,
            order_type=OrderType.LIMIT,
            quantity=100,
            reference_price=Decimal("24200"),
            limit_price=Decimal("24150"),
            created_bar_timestamp=t0,
            latency_bars=1,
            max_fill_ratio_per_bar=Decimal("0.50"),
            freshness=fresh,
        )
        PaperBrokerService.submit_order_to_paper_broker(session, lim_ord)
        assert lim_ord.status == OrderStatus.ACKNOWLEDGED.value

        # Same bar t0: must NOT fill (latency_bars=1 prevents same-bar close fill)
        c0 = _make_candle(ts=t0, open_px="24200", high_px="24210", low_px="24100", close_px="24120", seq=1)
        assert PaperBrokerService.process_candle_event(session, c0) == []
        assert lim_ord.status == OrderStatus.ACKNOWLEDGED.value

        # Bar t1: low touches 24150.00 exactly (NOT strictly < 24150, open=24180 > 24150) -> NO fill!
        c1_touch_only = _make_candle(
            ts=t1, open_px="24180", high_px="24190", low_px="24150", close_px="24170", seq=2
        )
        assert PaperBrokerService.process_candle_event(session, c1_touch_only) == []
        assert lim_ord.status == OrderStatus.ACKNOWLEDGED.value

        # Bar t2: low trades strictly through 24150 (low=24140 < 24150) -> 50% PARTIALLY_FILLED (50/100) @ 24150!
        c2_through = _make_candle(
            ts=t2, open_px="24170", high_px="24175", low_px="24140", close_px="24160", seq=3
        )
        fills_t2 = PaperBrokerService.process_candle_event(session, c2_through)
        assert len(fills_t2) == 1
        assert lim_ord.status == OrderStatus.PARTIALLY_FILLED.value
        assert lim_ord.filled_quantity == 50
        assert lim_ord.leaves_quantity == 50
        assert Decimal(str(fills_t2[0].fill_price)) == Decimal("24150.0000")

        # Bar t3: trades through again -> remaining 50 fills -> FILLED (100/100)!
        c3_through = _make_candle(
            ts=t3, open_px="24160", high_px="24165", low_px="24135", close_px="24155", seq=4
        )
        fills_t3 = PaperBrokerService.process_candle_event(session, c3_through)
        assert len(fills_t3) == 1
        assert lim_ord.status == OrderStatus.FILLED.value
        assert lim_ord.filled_quantity == 100
        assert lim_ord.leaves_quantity == 0

        # Cannot cancel an already FILLED order
        with pytest.raises(InvalidStateTransitionError):
            OrderManagementService.cancel_order(session, lim_ord.id)

        # D. BUY STOP requires trigger condition (high >= stop_price) + Cancel & Expire tests
        stop_ord, _ = OrderManagementService.create_and_evaluate_order(
            session,
            account=acct,
            client_order_key="ord-buy-stop",
            instrument_symbol="BANKNIFTY",
            side=Side.BUY,
            order_type=OrderType.STOP,
            quantity=30,
            reference_price=Decimal("51500"),
            stop_price=Decimal("51600"),
            created_bar_timestamp=t0,
            latency_bars=1,
            slippage_bps=Decimal("0"),
            freshness=fresh,
        )
        PaperBrokerService.submit_order_to_paper_broker(session, stop_ord)

        exp_ord, _ = OrderManagementService.create_and_evaluate_order(
            session,
            account=acct,
            client_order_key="ord-expire-1bar",
            instrument_symbol="BANKNIFTY",
            side=Side.BUY,
            order_type=OrderType.LIMIT,
            quantity=30,
            reference_price=Decimal("51500"),
            limit_price=Decimal("51000"),
            created_bar_timestamp=t0,
            latency_bars=1,
            valid_for_bars=1,
            freshness=fresh,
        )
        PaperBrokerService.submit_order_to_paper_broker(session, exp_ord)

        canc_ord, _ = OrderManagementService.create_and_evaluate_order(
            session,
            account=acct,
            client_order_key="ord-to-cancel",
            instrument_symbol="BANKNIFTY",
            side=Side.SELL,
            order_type=OrderType.LIMIT,
            quantity=30,
            reference_price=Decimal("51500"),
            limit_price=Decimal("52000"),
            created_bar_timestamp=t0,
            latency_bars=1,
            freshness=fresh,
        )
        PaperBrokerService.submit_order_to_paper_broker(session, canc_ord)
        OrderManagementService.cancel_order(session, canc_ord.id, reason="Desk cancel test")
        assert canc_ord.status == OrderStatus.CANCELLED.value

        # Bar t1 on BANKNIFTY: high=51580 (< 51600 stop), low=51450 (> 51000 limit)
        # -> stop_ord stays ACKNOWLEDGED (untriggered), exp_ord expires (EXPIRED after 1 bar TIF)!
        bn_t1 = _make_candle(
            ts=t1,
            open_px="51500",
            high_px="51580",
            low_px="51450",
            close_px="51550",
            seq=10,
            symbol="BANKNIFTY",
        )
        assert PaperBrokerService.process_candle_event(session, bn_t1) == []
        assert stop_ord.status == OrderStatus.ACKNOWLEDGED.value
        assert stop_ord.stop_triggered is False
        assert exp_ord.status == OrderStatus.EXPIRED.value

        # Bar t2 on BANKNIFTY: high=51620 (>= 51600 stop) -> triggers & fills @ 51600!
        bn_t2 = _make_candle(
            ts=t2,
            open_px="51560",
            high_px="51620",
            low_px="51550",
            close_px="51610",
            seq=11,
            symbol="BANKNIFTY",
        )
        bn_fills = PaperBrokerService.process_candle_event(session, bn_t2)
        assert len(bn_fills) == 1
        assert stop_ord.status == OrderStatus.FILLED.value
        assert stop_ord.stop_triggered is True
        assert Decimal(str(bn_fills[0].fill_price)) == Decimal("51600.0000")


def test_position_calculation_realized_and_unrealized_pnl_exact_accounting(
    migrated_db_url: str,
) -> None:
    """Verify LONG & SHORT position derivation, scale-in, partial/full close, realized & unrealized P&L."""
    t0 = datetime(2026, 10, 29, 9, 30, tzinfo=IST)
    t1 = t0 + timedelta(minutes=5)
    t2 = t0 + timedelta(minutes=10)
    t3 = t0 + timedelta(minutes=15)
    t4 = t0 + timedelta(minutes=20)

    with unit_of_work() as session:
        seed_foundation_data(session)
        acct = PaperTradingService.get_primary_account_row(session)
        fresh = _healthy_freshness(t0)

        def _exec_zero_fee_order(key: str, side: Side, qty: int, px: str, ts: datetime) -> PaperPositionModel:
            o, _ = OrderManagementService.create_and_evaluate_order(
                session,
                account=acct,
                client_order_key=key,
                instrument_symbol="NIFTY",
                side=side,
                order_type=OrderType.MARKET,
                quantity=qty,
                reference_price=Decimal(px),
                slippage_bps=Decimal("0"),
                charges_bps=Decimal("0"),
                per_order_fee_inr=Decimal("0"),
                freshness=fresh,
            )
            PaperBrokerService.submit_order_to_paper_broker(session, o)
            PaperBrokerService.record_execution_fill(
                session,
                order=o,
                fill_quantity=qty,
                raw_market_price=Decimal(px),
                fill_price=Decimal(px),
                bar_timestamp=ts,
                fill_idempotency_key=f"fill-{key}",
            )
            pos = session.scalar(
                select(PaperPositionModel)
                .where(PaperPositionModel.instrument_symbol == "NIFTY")
                .order_by(PaperPositionModel.opened_at.desc())
                .limit(1)
            )
            assert pos is not None
            return pos

        # 1. Open LONG 50 @ 24,000 -> Scale-in LONG 50 @ 24,100 => Open 100 @ Avg 24,050.00
        p1 = _exec_zero_fee_order("pnl-1", Side.BUY, 50, "24000", t0)
        assert p1.side == "LONG"
        assert p1.quantity == 50
        assert Decimal(str(p1.average_entry_price)) == Decimal("24000.0000")

        p2 = _exec_zero_fee_order("pnl-2", Side.BUY, 50, "24100", t1)
        assert p2.id == p1.id
        assert p2.quantity == 100
        assert Decimal(str(p2.average_entry_price)) == Decimal("24050.0000")
        # At mark 24,100: unrealized = (24100 - 24050) * 100 = +5,000.00
        assert Decimal(str(p2.unrealized_pnl)) == Decimal("5000.0000")
        assert Decimal(str(p2.realized_pnl)) == Decimal("0.0000")

        # 2. Mark-to-market at 24,200 -> Unrealized = (24200 - 24050) * 100 = +15,000.00
        PositionService.mark_positions_to_market(
            session, symbol="NIFTY", mark_price=Decimal("24200"), bar_timestamp=t2
        )
        assert Decimal(str(p2.unrealized_pnl)) == Decimal("15000.0000")

        # 3. Partial Close SELL 40 @ 24,250:
        #    Realized = (24250 - 24050) * 40 = +8,000.00
        #    Remaining Open = 60 @ 24,050; Unrealized @ 24,250 = (24250 - 24050) * 60 = +12,000.00
        _exec_zero_fee_order("pnl-3", Side.SELL, 40, "24250", t3)
        assert p2.status == PositionStatus.OPEN.value
        assert p2.quantity == 60
        assert Decimal(str(p2.realized_pnl)) == Decimal("8000.0000")
        assert Decimal(str(p2.unrealized_pnl)) == Decimal("12000.0000")

        # 4. Full Close + Reverse: SELL 90 @ 23,950 (closes remaining 60 LONG, opens 30 SHORT @ 23,950)
        #    Close 60 LONG @ 23,950: delta = (23950 - 24050) * 60 = -6,000.00 -> Total p2 Realized = +2,000.00
        short_pos = _exec_zero_fee_order("pnl-4", Side.SELL, 90, "23950", t4)
        assert p2.status == PositionStatus.CLOSED.value
        assert p2.quantity == 0
        assert Decimal(str(p2.realized_pnl)) == Decimal("2000.0000")
        assert Decimal(str(p2.unrealized_pnl)) == Decimal("0.0000")

        assert short_pos.id != p2.id
        assert short_pos.side == "SHORT"
        assert short_pos.quantity == 30
        assert Decimal(str(short_pos.average_entry_price)) == Decimal("23950.0000")

        # Mark SHORT 30 at 23,850 -> Unrealized = (23950 - 23850) * 30 = +3,000.00
        PositionService.mark_positions_to_market(
            session, symbol="NIFTY", mark_price=Decimal("23850"), bar_timestamp=t4 + timedelta(minutes=5)
        )
        assert Decimal(str(short_pos.unrealized_pnl)) == Decimal("3000.0000")

        recon = PositionService.reconcile_positions_from_fills(session, heal_if_needed=False)
        assert recon.reconciled is True
        assert recon.discrepancies_found == 0
        assert recon.total_realized_pnl == Decimal("2000.0000")
        assert recon.total_unrealized_pnl == Decimal("3000.0000")
