"""Paper Trading Orchestration, Signal-to-Order Pipeline, Strategy Instances & Recovery.

STRICT SAFETY INVARIANT:
- THIS SYSTEM IS PAPER_ONLY. Zero real-money broker order-placement calls exist.
- Flow: Market Event -> Strategy -> Signal -> Risk Evaluation -> OMS -> Paper Broker -> Execution -> Position.
- Strategies NEVER create fills; UI NEVER directly edits positions.
"""

from __future__ import annotations

import uuid
from collections import defaultdict
from datetime import datetime, timedelta
from decimal import Decimal, ROUND_FLOOR
from typing import Any

from sqlalchemy import asc, desc, func, select
from sqlalchemy.orm import Session

from core.audit.service import AuditService
from core.broker.paper_broker import PaperBrokerService
from core.clock import ensure_ist, format_ist_iso, now_ist
from core.config import AppSettings, get_settings
from core.domain.enums import (
    STRATEGY_INSTANCE_TRANSITIONS,
    WORKING_ORDER_STATUSES,
    ActorType,
    AuditCategory,
    AuditSeverity,
    ExitTargetUnit,
    MarketFeedSource,
    OrderSource,
    OrderStatus,
    OrderType,
    PositionSizingMode,
    PositionStatus,
    Side,
    SignalDirection,
    SignalStatus,
    StrategyInstanceStatus,
    TradeDirection,
    validate_state_transition,
)
from core.exceptions import ControlCenterError, EntityNotFoundError
from core.logging import get_correlation_id
from core.market_data.events import CandleEvent, MarketEventType
from core.market_data.freshness import MarketDataFreshnessSnapshot, evaluate_market_data_freshness
from core.oms.service import OrderManagementService
from core.persistence.models import (
    AccountRiskConfigModel,
    InstrumentModel,
    PaperAccountModel,
    PaperFillModel,
    PaperOrderModel,
    PaperPositionModel,
    ReplaySessionModel,
    ReplaySignalModel,
    StrategyInstanceModel,
    StrategyVersionModel,
)
from core.positions.service import PositionService
from core.risk.service import RiskControlService
from core.strategies.base import SignalCandidate
from core.strategies.config_schema import EmaRsiStrategyConfig, parse_and_validate_ema_rsi_config
from core.strategies.ema_rsi import EmaRsiCrossoverStrategy

_Q2 = Decimal("0.01")
_Q4 = Decimal("0.0001")


def _compute_entry_stop_and_target(
    *,
    side: Side,
    reference_price: Decimal,
    cfg: EmaRsiStrategyConfig,
) -> tuple[Decimal | None, Decimal | None]:
    def _delta(dist: Decimal) -> Decimal:
        if cfg.stop_target_unit == ExitTargetUnit.PERCENT:
            return (reference_price * dist / Decimal("100")).quantize(_Q4)
        return dist.quantize(_Q4)

    sl_delta = _delta(cfg.stop_loss)
    stop_px = (
        (reference_price - sl_delta).quantize(_Q4)
        if side == Side.BUY
        else (reference_price + sl_delta).quantize(_Q4)
    )
    target_px: Decimal | None = None
    if cfg.profit_target_enabled:
        tp_delta = _delta(cfg.profit_target)
        target_px = (
            (reference_price + tp_delta).quantize(_Q4)
            if side == Side.BUY
            else (reference_price - tp_delta).quantize(_Q4)
        )
    return stop_px, target_px


def _resolve_strategy_order_quantity(
    session: Session,
    *,
    account: PaperAccountModel,
    symbol: str,
    reference_price: Decimal,
    cfg: EmaRsiStrategyConfig,
) -> int:
    inst = session.scalar(
        select(InstrumentModel).where(InstrumentModel.symbol == symbol.strip().upper())
    )
    lot_size = int(inst.lot_size) if inst is not None else 75

    if cfg.position_sizing_mode == PositionSizingMode.FIXED_LOTS:
        lots = max(1, int(cfg.position_size_value))
        return lots * lot_size

    if cfg.position_sizing_mode == PositionSizingMode.FIXED_QUANTITY:
        return max(1, int(cfg.position_size_value))

    # RISK_PER_TRADE_PCT
    avail = Decimal(str(account.available_capital))
    risk_budget = avail * (cfg.position_size_value / Decimal("100"))
    stop_dist = (
        (reference_price * cfg.stop_loss / Decimal("100"))
        if cfg.stop_target_unit == ExitTargetUnit.PERCENT
        else cfg.stop_loss
    )
    if stop_dist <= Decimal("0"):
        return lot_size
    raw_units = (risk_budget / stop_dist).to_integral_value(rounding=ROUND_FLOOR)
    lots = max(1, int(raw_units) // lot_size)
    return lots * lot_size


class PaperTradingService:
    """Central domain orchestrator for Strategy Instances, Signal->OMS->Broker pipeline & Recovery."""

    @staticmethod
    def get_primary_account_row(
        session: Session, settings: AppSettings | None = None
    ) -> PaperAccountModel:
        cfg = settings or get_settings()
        acct = session.scalar(
            select(PaperAccountModel).where(
                PaperAccountModel.account_code == cfg.DEFAULT_PAPER_ACCOUNT_CODE
            )
        )
        if acct is None:
            acct = session.scalar(
                select(PaperAccountModel).order_by(PaperAccountModel.created_at.asc()).limit(1)
            )
        if acct is None:
            raise EntityNotFoundError("Primary demonstration paper account not found.")
        return acct

    @staticmethod
    def _current_freshness_snapshot(
        session: Session, settings: AppSettings | None = None
    ) -> tuple[ReplaySessionModel | None, MarketDataFreshnessSnapshot]:
        cfg = settings or get_settings()
        latest = session.scalar(
            select(ReplaySessionModel).order_by(desc(ReplaySessionModel.created_at)).limit(1)
        )
        if latest is None:
            return None, evaluate_market_data_freshness(
                session_status=None,
                last_candle_received_at=None,
                stale_threshold_seconds=cfg.MARKET_DATA_STALE_THRESHOLD_SECONDS,
            )
        snap = evaluate_market_data_freshness(
            session_status=latest.status,
            last_candle_received_at=latest.last_candle_received_at,
            last_exchange_timestamp=latest.replay_clock_ist,
            stale_threshold_seconds=latest.stale_threshold_seconds,
        )
        return latest, snap

    # -------------------------------------------------------------------------
    # Strategy Instance Management
    # -------------------------------------------------------------------------

    @staticmethod
    def start_strategy_instance(
        session: Session,
        *,
        strategy_version_id: uuid.UUID,
        order_type: str = "MARKET",
        limit_offset_pts: Decimal = Decimal("0"),
        execution_latency_ms: int | None = None,
        latency_bars: int = 1,
        slippage_bps: Decimal | None = None,
        partial_fill_enabled: bool = False,
        max_fill_ratio_per_bar: Decimal = Decimal("1.0"),
        valid_for_bars: int | None = None,
        auto_execute_signals: bool = True,
        actor: str = "desk.operator",
        settings: AppSettings | None = None,
    ) -> dict[str, Any]:
        cfg = settings or get_settings()
        acct = PaperTradingService.get_primary_account_row(session, cfg)
        strat_ver = session.get(StrategyVersionModel, strategy_version_id)
        if strat_ver is None:
            raise EntityNotFoundError(f"StrategyVersion '{strategy_version_id}' not found.")

        strat_cfg = parse_and_validate_ema_rsi_config(strat_ver.parameters_json)
        ord_type_enum = OrderType(order_type.strip().upper())
        ratio = (
            Decimal("0.50")
            if (partial_fill_enabled and max_fill_ratio_per_bar >= Decimal("1.0"))
            else Decimal(str(max_fill_ratio_per_bar))
        )
        if ratio <= Decimal("0") or ratio > Decimal("1"):
            raise ControlCenterError(
                "max_fill_ratio_per_bar must be in (0, 1].", code="INVALID_FILL_RATIO"
            )

        ts_now = now_ist()
        inst_code = (
            f"SINST-{strat_ver.instrument_symbol}-{ts_now.strftime('%H%M%S')}-"
            f"{uuid.uuid4().hex[:4].upper()}"
        )
        row = StrategyInstanceModel(
            instance_code=inst_code,
            account_id=acct.id,
            strategy_version_id=strat_ver.id,
            strategy_version_label=strat_ver.version_label,
            instrument_symbol=strat_ver.instrument_symbol,
            timeframe=strat_ver.timeframe,
            status=StrategyInstanceStatus.RUNNING.value,
            order_type=ord_type_enum.value,
            limit_offset_pts=Decimal(str(limit_offset_pts)),
            execution_latency_ms=(
                int(execution_latency_ms)
                if execution_latency_ms is not None
                else cfg.PAPER_DEFAULT_LATENCY_MS
            ),
            latency_bars=max(1, int(latency_bars)),
            slippage_bps=(
                Decimal(str(slippage_bps))
                if slippage_bps is not None
                else strat_cfg.slippage_bps
            ),
            partial_fill_enabled=bool(partial_fill_enabled or ratio < Decimal("1.0")),
            max_fill_ratio_per_bar=ratio,
            valid_for_bars=valid_for_bars,
            auto_execute_signals=bool(auto_execute_signals),
            signals_emitted=0,
            orders_created=0,
            created_by=actor,
            started_at=ts_now,
            created_at=ts_now,
            updated_at=ts_now,
        )
        session.add(row)
        session.flush()

        AuditService.record(
            session,
            action="STRATEGY_INSTANCE_STARTED",
            summary=(
                f"Started Paper Strategy Instance {inst_code} ({strat_ver.version_label} on "
                f"{strat_ver.instrument_symbol}, order_type={ord_type_enum.value})."
            ),
            actor=actor,
            actor_type=ActorType.OPERATOR,
            category=AuditCategory.STRATEGY,
            severity=AuditSeverity.INFO,
            entity_type="strategy_instance",
            entity_id=str(row.id),
            details={
                "instance_code": inst_code,
                "strategy_version_label": strat_ver.version_label,
                "instrument": strat_ver.instrument_symbol,
                "order_type": ord_type_enum.value,
                "latency_bars": row.latency_bars,
                "max_fill_ratio_per_bar": str(row.max_fill_ratio_per_bar),
            },
        )
        return PaperTradingService.serialize_strategy_instance(row)

    @staticmethod
    def _transition_instance(
        session: Session,
        instance_id: uuid.UUID,
        target: StrategyInstanceStatus,
        *,
        actor: str,
    ) -> dict[str, Any]:
        row = session.get(StrategyInstanceModel, instance_id)
        if row is None:
            raise EntityNotFoundError(f"StrategyInstance '{instance_id}' not found.")
        curr = StrategyInstanceStatus(row.status)
        if curr == target:
            return PaperTradingService.serialize_strategy_instance(row)
        validated = validate_state_transition(
            "StrategyInstance", curr, target, STRATEGY_INSTANCE_TRANSITIONS
        )
        ts_now = now_ist()
        row.status = validated.value
        if validated == StrategyInstanceStatus.PAUSED:
            row.paused_at = ts_now
        elif validated == StrategyInstanceStatus.STOPPED:
            row.stopped_at = ts_now
        row.updated_at = ts_now
        session.flush()

        AuditService.record(
            session,
            action=f"STRATEGY_INSTANCE_{validated.value}",
            summary=f"Strategy Instance {row.instance_code} transitioned {curr.value} -> {validated.value}.",
            actor=actor,
            actor_type=ActorType.OPERATOR,
            category=AuditCategory.STRATEGY,
            severity=AuditSeverity.INFO,
            entity_type="strategy_instance",
            entity_id=str(row.id),
            details={"previous_status": curr.value, "new_status": validated.value},
        )
        return PaperTradingService.serialize_strategy_instance(row)

    @staticmethod
    def pause_strategy_instance(
        session: Session, instance_id: uuid.UUID, *, actor: str = "desk.operator"
    ) -> dict[str, Any]:
        return PaperTradingService._transition_instance(
            session, instance_id, StrategyInstanceStatus.PAUSED, actor=actor
        )

    @staticmethod
    def resume_strategy_instance(
        session: Session, instance_id: uuid.UUID, *, actor: str = "desk.operator"
    ) -> dict[str, Any]:
        return PaperTradingService._transition_instance(
            session, instance_id, StrategyInstanceStatus.RUNNING, actor=actor
        )

    @staticmethod
    def stop_strategy_instance(
        session: Session, instance_id: uuid.UUID, *, actor: str = "desk.operator"
    ) -> dict[str, Any]:
        return PaperTradingService._transition_instance(
            session, instance_id, StrategyInstanceStatus.STOPPED, actor=actor
        )

    # -------------------------------------------------------------------------
    # Signal Persistence & Signal -> Risk -> OMS -> Paper Broker Pipeline
    # -------------------------------------------------------------------------

    @staticmethod
    def persist_signal_idempotent(
        session: Session,
        *,
        candidate: SignalCandidate,
        strategy_version_id: uuid.UUID,
        strategy_version_label: str,
        market_event_id: str,
        replay_session_id: uuid.UUID | None = None,
        strategy_instance_id: uuid.UUID | None = None,
        stale_threshold_seconds: int | None = None,
        deduplication_key: str | None = None,
    ) -> tuple[ReplaySignalModel, bool]:
        """Persist a globally unique Signal record with duplicate prevention."""
        scope = (
            str(strategy_instance_id)
            if strategy_instance_id is not None
            else (str(replay_session_id) if replay_session_id is not None else "standalone")
        )
        dedup = (
            deduplication_key
            or f"{scope}:{strategy_version_id}:{candidate.symbol}:"
            f"{format_ist_iso(candidate.bar_timestamp)}:{candidate.direction.value}"
        )

        existing = session.scalar(
            select(ReplaySignalModel).where(ReplaySignalModel.deduplication_key == dedup)
        )
        if existing is not None:
            return existing, True

        if stale_threshold_seconds is not None:
            expiry_sec = max(1, int(stale_threshold_seconds))
        else:
            rc = session.scalar(select(AccountRiskConfigModel).limit(1))
            expiry_sec = (
                int(rc.max_signal_age_seconds)
                if rc is not None
                else get_settings().SIGNAL_EXPIRY_SECONDS
            )

        recv_ts = now_ist()
        sig_code = (
            f"SIG-{candidate.symbol}-{recv_ts.strftime('%H%M%S')}-"
            f"{uuid.uuid4().hex[:5].upper()}"
        )
        row = ReplaySignalModel(
            signal_code=sig_code,
            deduplication_key=dedup,
            replay_session_id=replay_session_id,
            strategy_instance_id=strategy_instance_id,
            strategy_version_id=strategy_version_id,
            strategy_version_label=strategy_version_label,
            instrument_symbol=candidate.symbol,
            timeframe=candidate.timeframe,
            side=candidate.side.value,
            direction=candidate.direction.value,
            exit_reason=candidate.exit_reason.value if candidate.exit_reason else None,
            bar_timestamp=ensure_ist(candidate.bar_timestamp),
            emitted_at=recv_ts,
            expires_at=recv_ts + timedelta(seconds=expiry_sec),
            reference_price=Decimal(str(candidate.reference_price)).quantize(_Q4),
            status=SignalStatus.GENERATED.value,
            rationale=candidate.rationale,
            indicators_json=candidate.indicators or {},
            market_event_id=market_event_id,
            correlation_id=get_correlation_id(),
        )
        session.add(row)
        session.flush()

        AuditService.record(
            session,
            action="STRATEGY_SIGNAL_GENERATED",
            summary=(
                f"Persisted signal {sig_code} ({candidate.direction.value} on "
                f"{candidate.symbol} @ {candidate.reference_price:.2f})."
            ),
            actor="strategy.engine",
            actor_type=ActorType.SYSTEM,
            category=AuditCategory.STRATEGY,
            severity=AuditSeverity.INFO,
            entity_type="signal",
            entity_id=str(row.id),
            details={
                "signal_code": sig_code,
                "strategy_instance_id": str(strategy_instance_id) if strategy_instance_id else None,
                "strategy_version_label": strategy_version_label,
                "direction": candidate.direction.value,
                "reference_price": str(candidate.reference_price),
            },
        )
        return row, False

    @staticmethod
    def route_signal_to_paper_execution(
        session: Session,
        signal: ReplaySignalModel,
        *,
        strategy_instance: StrategyInstanceModel | None = None,
        freshness: MarketDataFreshnessSnapshot | None = None,
        require_fresh_market_data: bool = True,
        client_order_key: str | None = None,
        actor: str = "strategy.pipeline",
        settings: AppSettings | None = None,
    ) -> tuple[PaperOrderModel, bool]:
        """Route a persisted Signal through Risk Evaluation -> OMS -> Paper Broker.

        Idempotency Guarantee:
        Repeated calls for the same `signal.id` NEVER create a second order!
        """
        cfg = settings or get_settings()
        acct = PaperTradingService.get_primary_account_row(session, cfg)

        # Fast-path signal-to-order idempotency check (also persists a DUPLICATE_SIGNAL_CHECK risk decision!)
        existing_order = session.scalar(
            select(PaperOrderModel).where(PaperOrderModel.signal_id == signal.id)
        )
        if existing_order is not None:
            from core.domain.enums import RiskDecisionOutcome

            RiskControlService.record_risk_decision(
                session,
                outcome=RiskDecisionOutcome.REJECTED,
                primary_rule="DUPLICATE_SIGNAL_CHECK",
                current_value=signal.signal_code,
                limit_value="1 order per unique Signal ID",
                reason=f"Duplicate signal dispatch blocked: Signal '{signal.signal_code}' already created order {existing_order.order_code}.",
                checks=[
                    {
                        "gate": "DUPLICATE_SIGNAL_CHECK",
                        "rule": "DUPLICATE_SIGNAL_CHECK",
                        "scope": "OPERATIONAL",
                        "passed": False,
                        "current_value": signal.signal_code,
                        "limit_value": "UNIQUE_SIGNAL_ID",
                        "detail": f"Existing order {existing_order.order_code}",
                    }
                ],
                account_id=acct.id,
                order_id=existing_order.id,
                client_order_key=client_order_key or existing_order.client_order_key,
                signal_id=signal.id,
                strategy_instance_id=signal.strategy_instance_id,
                strategy_version_id=signal.strategy_version_id,
                strategy_label=signal.strategy_version_label,
                instrument_symbol=signal.instrument_symbol,
                side=signal.side,
                order_type=existing_order.order_type,
                proposed_quantity=int(existing_order.quantity),
                proposed_price=Decimal(str(signal.reference_price)),
            )
            return existing_order, True

        strat_ver = session.get(StrategyVersionModel, signal.strategy_version_id)
        if strat_ver is None:
            raise EntityNotFoundError(
                f"StrategyVersion '{signal.strategy_version_id}' not found."
            )
        strat_cfg = parse_and_validate_ema_rsi_config(strat_ver.parameters_json)

        inst = strategy_instance or (
            session.get(StrategyInstanceModel, signal.strategy_instance_id)
            if signal.strategy_instance_id is not None
            else None
        )

        is_exit_signal = signal.direction in (
            SignalDirection.EXIT_LONG.value,
            SignalDirection.EXIT_SHORT.value,
        )
        ref_px = Decimal(str(signal.reference_price))
        side_enum = Side(signal.side)

        if is_exit_signal:
            open_pos = PositionService._find_open_position(
                session,
                account_id=acct.id,
                strategy_instance_id=inst.id if inst else None,
                instrument_symbol=signal.instrument_symbol,
            )
            qty = (
                int(open_pos.quantity)
                if (open_pos is not None and open_pos.quantity > 0)
                else _resolve_strategy_order_quantity(
                    session,
                    account=acct,
                    symbol=signal.instrument_symbol,
                    reference_price=ref_px,
                    cfg=strat_cfg,
                )
            )
            ord_type = OrderType.MARKET
            lim_px = None
            stop_sl = None
            target_tp = None
        else:
            qty = _resolve_strategy_order_quantity(
                session,
                account=acct,
                symbol=signal.instrument_symbol,
                reference_price=ref_px,
                cfg=strat_cfg,
            )
            ord_type = OrderType(inst.order_type) if inst is not None else OrderType.MARKET
            lim_px = None
            if ord_type == OrderType.LIMIT:
                offset = Decimal(str(inst.limit_offset_pts)) if inst else Decimal("0")
                lim_px = (
                    (ref_px - offset).quantize(_Q4)
                    if side_enum == Side.BUY
                    else (ref_px + offset).quantize(_Q4)
                )
            elif ord_type == OrderType.STOP:
                offset = Decimal(str(inst.limit_offset_pts)) if inst else Decimal("5")
                lim_px = None
            stop_sl, target_tp = _compute_entry_stop_and_target(
                side=side_enum, reference_price=ref_px, cfg=strat_cfg
            )

        stp_px: Decimal | None = None
        if ord_type == OrderType.STOP:
            off = abs(Decimal(str(inst.limit_offset_pts))) if inst else Decimal("5")
            stp_px = (
                (ref_px + off).quantize(_Q4)
                if side_enum == Side.BUY
                else (ref_px - off).quantize(_Q4)
            )

        eff_fresh = freshness
        if eff_fresh is None:
            _, eff_fresh = PaperTradingService._current_freshness_snapshot(session, cfg)

        c_key = client_order_key or f"sig-ord:{signal.id}"
        order, is_dup = OrderManagementService.create_and_evaluate_order(
            session,
            account=acct,
            client_order_key=c_key,
            instrument_symbol=signal.instrument_symbol,
            side=side_enum,
            order_type=ord_type,
            quantity=qty,
            order_source=OrderSource.STRATEGY_SIGNAL,
            timeframe=signal.timeframe,
            reference_price=ref_px,
            limit_price=lim_px,
            stop_price=stp_px,
            reduce_only=is_exit_signal,
            exit_reason=signal.exit_reason,
            strategy_instance_id=inst.id if inst else signal.strategy_instance_id,
            strategy_version_id=signal.strategy_version_id,
            strategy_version_label=signal.strategy_version_label,
            signal=signal,
            replay_session_id=signal.replay_session_id,
            execution_latency_ms=inst.execution_latency_ms if inst else cfg.PAPER_DEFAULT_LATENCY_MS,
            latency_bars=inst.latency_bars if inst else 1,
            created_bar_timestamp=signal.bar_timestamp,
            slippage_bps=inst.slippage_bps if inst else strat_cfg.slippage_bps,
            charges_bps=strat_cfg.estimated_charges_bps,
            per_order_fee_inr=strat_cfg.per_order_fee_inr,
            max_fill_ratio_per_bar=(
                Decimal(str(inst.max_fill_ratio_per_bar)) if inst else Decimal("1.0")
            ),
            valid_for_bars=inst.valid_for_bars if inst else None,
            target_stop_loss_price=stop_sl,
            target_profit_price=target_tp,
            freshness=eff_fresh,
            require_fresh_market_data=require_fresh_market_data,
            actor=actor,
            settings=cfg,
        )

        if not is_dup and inst is not None:
            inst.orders_created = int(inst.orders_created) + 1
            inst.updated_at = now_ist()

        if not is_dup and order.status == OrderStatus.RISK_APPROVED.value:
            PaperBrokerService.submit_order_to_paper_broker(
                session,
                order,
                current_candle=None,
                immediate_match_allowed=False,
                settings=cfg,
            )

        return order, is_dup

    # -------------------------------------------------------------------------
    # Normalized Market Event Hook (Consumed during Market Replay & Live Streams)
    # -------------------------------------------------------------------------

    @staticmethod
    def on_normalized_candle_event(
        session: Session,
        *,
        candle: CandleEvent,
        stream_history: list[CandleEvent],
        replay_session: ReplaySessionModel | None = None,
        freshness: MarketDataFreshnessSnapshot | None = None,
        settings: AppSettings | None = None,
    ) -> dict[str, Any]:
        """Execute full Paper Broker matching + Strategy Instance evaluation on a CandleEvent.

        Sequence:
        1. PaperBroker matches resting orders from prior bars against `candle` OHLC & marks positions.
        2. Running StrategyInstances for `candle.symbol` evaluate `stream_history` with derived open position.
        3. Any emitted Signals persist idempotently and route -> Risk -> OMS -> PaperBroker (for next bar).
        """
        cfg = settings or get_settings()
        fills = PaperBrokerService.process_candle_event(session, candle, settings=cfg)

        running_instances = list(
            session.scalars(
                select(StrategyInstanceModel).where(
                    StrategyInstanceModel.instrument_symbol == candle.symbol.strip().upper(),
                    StrategyInstanceModel.status == StrategyInstanceStatus.RUNNING.value,
                )
            ).all()
        )
        if not running_instances:
            return {"fills_executed": len(fills), "instance_signals": 0, "instance_orders": 0}

        eff_fresh = freshness
        if eff_fresh is None:
            _, eff_fresh = PaperTradingService._current_freshness_snapshot(session, cfg)

        bar_day_str = ensure_ist(candle.exchange_timestamp).date().isoformat()
        working_vals = [s.value for s in WORKING_ORDER_STATUSES]
        emitted_sigs = 0
        created_ords = 0

        for inst in running_instances:
            # Skip signal generation if this StrategyInstance already has an active working order
            has_working = session.scalar(
                select(PaperOrderModel.id).where(
                    PaperOrderModel.strategy_instance_id == inst.id,
                    PaperOrderModel.status.in_(working_vals),
                )
            )
            if has_working is not None:
                continue

            strat_ver = session.get(StrategyVersionModel, inst.strategy_version_id)
            if strat_ver is None:
                continue
            s_cfg = parse_and_validate_ema_rsi_config(strat_ver.parameters_json)
            strategy = EmaRsiCrossoverStrategy(s_cfg)

            open_snap = PositionService.get_open_position_snapshot_for_strategy(
                session,
                account_id=inst.account_id,
                strategy_instance_id=inst.id,
                symbol=inst.instrument_symbol,
            )

            # Count entry signals already emitted today by this instance
            prior_entries = list(
                session.scalars(
                    select(ReplaySignalModel).where(
                        ReplaySignalModel.strategy_instance_id == inst.id,
                        ReplaySignalModel.direction.in_(
                            [SignalDirection.ENTRY_LONG.value, SignalDirection.ENTRY_SHORT.value]
                        ),
                    )
                ).all()
            )
            trades_today = sum(
                1
                for s in prior_entries
                if ensure_ist(s.bar_timestamp).date().isoformat() == bar_day_str
            )

            candidate = strategy.consume_candle_events(
                stream_history,
                open_position=open_snap,
                trades_taken_today=trades_today,
            )
            if candidate is None:
                continue

            sig_row, sig_dup = PaperTradingService.persist_signal_idempotent(
                session,
                candidate=candidate,
                strategy_version_id=inst.strategy_version_id,
                strategy_version_label=inst.strategy_version_label,
                market_event_id=str(candle.event_id),
                replay_session_id=replay_session.id if replay_session else None,
                strategy_instance_id=inst.id,
                stale_threshold_seconds=(
                    replay_session.stale_threshold_seconds
                    if replay_session
                    else cfg.MARKET_DATA_STALE_THRESHOLD_SECONDS
                ),
            )
            if not sig_dup:
                inst.signals_emitted = int(inst.signals_emitted) + 1
                inst.updated_at = now_ist()
                emitted_sigs += 1

            if inst.auto_execute_signals:
                _, ord_dup = PaperTradingService.route_signal_to_paper_execution(
                    session,
                    sig_row,
                    strategy_instance=inst,
                    freshness=eff_fresh,
                    require_fresh_market_data=True,
                    settings=cfg,
                )
                if not ord_dup:
                    created_ords += 1

        session.flush()
        return {
            "fills_executed": len(fills),
            "instance_signals": emitted_sigs,
            "instance_orders": created_ords,
        }

    # -------------------------------------------------------------------------
    # Manual Simulated Paper Order & Explicit Position Exit / Flatten Controls
    # -------------------------------------------------------------------------

    @staticmethod
    def _latest_candle_for_symbol(
        session: Session, symbol: str, fallback_price: Decimal | None = None
    ) -> CandleEvent | None:
        rpl = session.scalar(
            select(ReplaySessionModel)
            .where(ReplaySessionModel.instrument_symbol == symbol.strip().upper())
            .order_by(desc(ReplaySessionModel.updated_at))
            .limit(1)
        )
        if rpl is not None and rpl.last_candle_json:
            cj = rpl.last_candle_json
            ts = datetime.fromisoformat(str(cj["exchange_timestamp"]))
            recv = datetime.fromisoformat(str(cj["received_at"]))
            return CandleEvent(
                event_id=uuid.UUID(str(cj["event_id"])),
                event_type=MarketEventType.CANDLE,
                source=MarketFeedSource.HISTORICAL_REPLAY,
                symbol=str(cj["symbol"]),
                exchange_timestamp=ts,
                received_at=recv,
                sequence_number=int(cj.get("sequence_number", 1)),
                timeframe=str(cj.get("timeframe", "5m")),
                open=Decimal(str(cj["open"])),
                high=Decimal(str(cj["high"])),
                low=Decimal(str(cj["low"])),
                close=Decimal(str(cj["close"])),
                volume=int(cj.get("volume", 0)),
                is_closed=True,
            )
        if fallback_price is not None and fallback_price > Decimal("0"):
            any_rpl = session.scalar(
                select(ReplaySessionModel).order_by(desc(ReplaySessionModel.updated_at)).limit(1)
            )
            sim_ts = (
                ensure_ist(any_rpl.replay_clock_ist)
                if (any_rpl is not None and any_rpl.replay_clock_ist is not None)
                else now_ist()
            )
            return CandleEvent(
                event_id=uuid.uuid4(),
                event_type=MarketEventType.CANDLE,
                source=MarketFeedSource.HISTORICAL_REPLAY,
                symbol=symbol.strip().upper(),
                exchange_timestamp=sim_ts,
                received_at=now_ist(),
                sequence_number=1,
                timeframe="5m",
                open=fallback_price,
                high=fallback_price,
                low=fallback_price,
                close=fallback_price,
                volume=0,
                is_closed=True,
            )
        return None

    @staticmethod
    def submit_manual_paper_order(
        session: Session,
        *,
        client_order_key: str,
        instrument_symbol: str,
        side: str,
        order_type: str,
        quantity: int,
        reference_price: Decimal | None = None,
        limit_price: Decimal | None = None,
        stop_price: Decimal | None = None,
        stop_loss_price: Decimal | None = None,
        target_price: Decimal | None = None,
        execution_latency_ms: int | None = None,
        latency_bars: int = 1,
        slippage_bps: Decimal | None = None,
        max_fill_ratio_per_bar: Decimal = Decimal("1.0"),
        simulate_rejection: bool = False,
        simulated_rejection_reason: str | None = None,
        valid_for_bars: int | None = None,
        expires_at: datetime | None = None,
        require_fresh_market_data: bool = True,
        actor: str = "desk.operator",
        settings: AppSettings | None = None,
    ) -> tuple[dict[str, Any], bool]:
        """Submit an idempotent manual simulated paper order via Risk -> OMS -> Paper Broker."""
        cfg = settings or get_settings()
        acct = PaperTradingService.get_primary_account_row(session, cfg)
        latest_rpl, fresh = PaperTradingService._current_freshness_snapshot(session, cfg)
        clean_sym = instrument_symbol.strip().upper()
        cur_candle = PaperTradingService._latest_candle_for_symbol(
            session, clean_sym, fallback_price=reference_price or limit_price or stop_price
        )
        eff_ref = (
            reference_price
            or (cur_candle.close if cur_candle is not None else None)
            or limit_price
            or stop_price
        )

        order, is_dup = OrderManagementService.create_and_evaluate_order(
            session,
            account=acct,
            client_order_key=client_order_key,
            instrument_symbol=clean_sym,
            side=side,
            order_type=order_type,
            quantity=quantity,
            order_source=OrderSource.OPERATOR_MANUAL,
            timeframe=cur_candle.timeframe if cur_candle else "5m",
            reference_price=eff_ref,
            limit_price=limit_price,
            stop_price=stop_price,
            reduce_only=False,
            replay_session_id=latest_rpl.id if latest_rpl else None,
            execution_latency_ms=execution_latency_ms,
            latency_bars=latency_bars,
            created_bar_timestamp=cur_candle.exchange_timestamp if cur_candle else None,
            slippage_bps=slippage_bps,
            max_fill_ratio_per_bar=max_fill_ratio_per_bar,
            simulate_rejection=simulate_rejection,
            simulated_rejection_reason=simulated_rejection_reason,
            valid_for_bars=valid_for_bars,
            expires_at=expires_at,
            target_stop_loss_price=stop_loss_price,
            target_profit_price=target_price,
            freshness=fresh,
            require_fresh_market_data=require_fresh_market_data,
            actor=actor,
            settings=cfg,
        )

        if not is_dup and order.status == OrderStatus.RISK_APPROVED.value:
            PaperBrokerService.submit_order_to_paper_broker(
                session,
                order,
                current_candle=cur_candle,
                immediate_match_allowed=(int(order.latency_bars) == 0),
                settings=cfg,
            )

        return OrderManagementService.serialize_order(order, include_events=True), is_dup

    @staticmethod
    def exit_open_position(
        session: Session,
        position_id: uuid.UUID,
        *,
        client_order_key: str | None = None,
        actor: str = "desk.operator",
        reason: str = "OPERATOR_EXIT",
        settings: AppSettings | None = None,
    ) -> dict[str, Any]:
        """Close an OPEN position exclusively by routing a simulated closing order -> fill -> position."""
        cfg = settings or get_settings()
        pos = session.get(PaperPositionModel, position_id)
        if pos is None:
            raise EntityNotFoundError(f"PaperPosition '{position_id}' not found.")
        if pos.status != PositionStatus.OPEN.value or int(pos.quantity) <= 0:
            raise ControlCenterError(
                f"Position '{pos.position_code}' is already {pos.status}.",
                code="POSITION_ALREADY_CLOSED",
            )

        acct = session.get(PaperAccountModel, pos.account_id)
        assert acct is not None
        close_side = Side.SELL if pos.side == TradeDirection.LONG.value else Side.BUY
        mark_px = Decimal(str(pos.last_price))
        cur_candle = PaperTradingService._latest_candle_for_symbol(
            session, pos.instrument_symbol, fallback_price=mark_px
        )
        c_key = client_order_key or f"pos-exit:{pos.id}:{pos.closed_quantity}:{pos.quantity}"

        order, is_dup = OrderManagementService.create_and_evaluate_order(
            session,
            account=acct,
            client_order_key=c_key,
            instrument_symbol=pos.instrument_symbol,
            side=close_side,
            order_type=OrderType.MARKET,
            quantity=int(pos.quantity),
            order_source=OrderSource.POSITION_EXIT,
            timeframe=pos.timeframe,
            reference_price=cur_candle.close if cur_candle else mark_px,
            reduce_only=True,
            exit_reason=reason,
            strategy_instance_id=pos.strategy_instance_id,
            strategy_version_id=pos.strategy_version_id,
            strategy_version_label=pos.strategy_version_label,
            latency_bars=0,
            max_fill_ratio_per_bar=Decimal("1.0"),
            require_fresh_market_data=False,
            actor=actor,
            settings=cfg,
        )
        if not is_dup and order.status == OrderStatus.RISK_APPROVED.value:
            PaperBrokerService.submit_order_to_paper_broker(
                session,
                order,
                current_candle=cur_candle,
                immediate_match_allowed=True,
                settings=cfg,
            )

        session.refresh(pos)
        return {
            "order": OrderManagementService.serialize_order(order, include_events=True),
            "position": PositionService.serialize_position(pos),
            "duplicate": is_dup,
        }

    @staticmethod
    def flatten_all_open_positions(
        session: Session,
        *,
        instrument_symbol: str | None = None,
        actor: str = "desk.operator",
        settings: AppSettings | None = None,
    ) -> dict[str, Any]:
        """Explicit operator command closing all open paper positions via simulated executions."""
        cfg = settings or get_settings()
        stmt = select(PaperPositionModel).where(
            PaperPositionModel.status == PositionStatus.OPEN.value
        )
        if instrument_symbol:
            stmt = stmt.where(
                PaperPositionModel.instrument_symbol == instrument_symbol.strip().upper()
            )
        open_list = list(session.scalars(stmt.order_by(asc(PaperPositionModel.opened_at))).all())
        results = [
            PaperTradingService.exit_open_position(
                session,
                p.id,
                actor=actor,
                reason="OPERATOR_FLATTEN_ALL",
                settings=cfg,
            )
            for p in open_list
        ]
        AuditService.record(
            session,
            action="OPERATOR_FLATTEN_ALL_POSITIONS",
            summary=f"Operator executed explicit FLATTEN ALL across {len(results)} open paper position(s).",
            actor=actor,
            actor_type=ActorType.OPERATOR,
            category=AuditCategory.POSITION,
            severity=AuditSeverity.WARNING,
            entity_type="paper_account",
            entity_id="ALL",
            details={"positions_flattened": len(results)},
        )
        return {"positions_flattened": len(results), "results": results}

    # -------------------------------------------------------------------------
    # Container Restart Recovery & Fill-to-Position Reconciliation
    # -------------------------------------------------------------------------

    @staticmethod
    def recover_and_reconcile_on_startup(
        session: Session,
        *,
        reference_time: datetime | None = None,
        settings: AppSettings | None = None,
    ) -> dict[str, Any]:
        """Reload persistent state after container restart, handle incomplete orders & reconcile positions.

        Guarantees:
        1. Reloads incomplete orders (`CREATED`, `RISK_APPROVED`, `SUBMITTED`, `ACKNOWLEDGED`, `PARTIALLY_FILLED`).
        2. Reconciles `order.filled_quantity` and `leaves_quantity` from persisted `PaperFillModel`s.
        3. Transitions expired working orders -> `EXPIRED`.
        4. Transitions unsubmitted mid-flight orders (`CREATED`, `RISK_APPROVED`) -> `FAILED` (never duplicates!).
        5. Transitions stranded `SUBMITTED` orders -> `ACKNOWLEDGED` (preserving resting order without duplication).
        6. Reconciles `PaperPositionModel`s and `PaperAccountModel` strictly from `PaperFillModel`s.
        """
        cfg = settings or get_settings()
        ref_now = ensure_ist(reference_time) if reference_time is not None else now_ist()

        working_vals = [s.value for s in WORKING_ORDER_STATUSES]
        incomplete_orders = list(
            session.scalars(
                select(PaperOrderModel)
                .where(PaperOrderModel.status.in_(working_vals))
                .order_by(asc(PaperOrderModel.created_at))
            ).all()
        )

        expired_cnt = 0
        failed_midflight_cnt = 0
        ack_recovered_cnt = 0
        resting_working_cnt = 0

        for order in incomplete_orders:
            # 1. Reconcile order fill tallies against immutable PaperFillModel rows
            fills = list(
                session.scalars(
                    select(PaperFillModel)
                    .where(PaperFillModel.order_id == order.id)
                    .order_by(asc(PaperFillModel.executed_at))
                ).all()
            )
            actual_filled = sum(int(f.fill_quantity) for f in fills)
            if actual_filled != int(order.filled_quantity):
                order.filled_quantity = actual_filled
                order.leaves_quantity = max(0, int(order.quantity) - actual_filled)
                if actual_filled > 0:
                    w_sum = sum(
                        Decimal(str(f.fill_price)) * Decimal(int(f.fill_quantity)) for f in fills
                    )
                    order.average_fill_price = (w_sum / Decimal(actual_filled)).quantize(_Q4)

            if int(order.leaves_quantity) == 0 and actual_filled >= int(order.quantity):
                if order.status != OrderStatus.FILLED.value:
                    OrderManagementService.transition_order_status(
                        session,
                        order,
                        OrderStatus.FILLED,
                        actor="system.recovery",
                        reason="Startup recovery reconciled complete fill ledger.",
                        occurred_at=ref_now,
                    )
                continue

            # 2. Check expiration
            if order.expires_at is not None and ref_now >= ensure_ist(order.expires_at):
                OrderManagementService.transition_order_status(
                    session,
                    order,
                    OrderStatus.EXPIRED,
                    actor="system.recovery",
                    reason="Order expired while container was offline / restarting.",
                    occurred_at=ref_now,
                )
                expired_cnt += 1
                continue

            # 3. Handle mid-flight pre-submission orders vs broker-submitted orders
            if order.status in (OrderStatus.CREATED.value, OrderStatus.RISK_APPROVED.value):
                OrderManagementService.transition_order_status(
                    session,
                    order,
                    OrderStatus.FAILED,
                    actor="system.recovery",
                    reason=(
                        "Container restart interrupted pre-broker order submission; "
                        "marked FAILED to prevent duplicate resubmission."
                    ),
                    occurred_at=ref_now,
                )
                failed_midflight_cnt += 1
            elif order.status == OrderStatus.SUBMITTED.value:
                OrderManagementService.transition_order_status(
                    session,
                    order,
                    OrderStatus.ACKNOWLEDGED,
                    actor="system.recovery",
                    reason="Container restart recovery acknowledged submitted paper order without duplication.",
                    occurred_at=ref_now,
                )
                ack_recovered_cnt += 1
                resting_working_cnt += 1
            else:
                resting_working_cnt += 1

        from core.reconciliation.service import ReconciliationService

        recon_run_dict = ReconciliationService.run_reconciliation(
            session, apply_repair=True, actor="system.recovery", settings=cfg
        )
        recon_report = PositionService.reconcile_positions_from_fills(
            session, heal_if_needed=False, settings=cfg
        )

        summary_payload = {
            "incomplete_orders_inspected": len(incomplete_orders),
            "expired_on_recovery": expired_cnt,
            "failed_midflight_orders": failed_midflight_cnt,
            "acknowledged_submitted_orders": ack_recovered_cnt,
            "resting_working_orders": resting_working_cnt,
            "reconciliation": recon_report.to_dict(),
            "reconciliation_run": recon_run_dict,
        }

        AuditService.record(
            session,
            action="PAPER_TRADING_RECOVERY_COMPLETED",
            summary=(
                f"Paper Trading startup recovery inspected {len(incomplete_orders)} incomplete order(s) "
                f"and reconciled {recon_report.fills_inspected} execution fill(s)."
            ),
            actor="system.recovery",
            actor_type=ActorType.SYSTEM,
            category=AuditCategory.OMS,
            severity=AuditSeverity.INFO if recon_report.reconciled else AuditSeverity.WARNING,
            entity_type="service",
            entity_id="paper-trading-recovery",
            details=summary_payload,
        )
        return summary_payload

    # -------------------------------------------------------------------------
    # Read / Telemetry Aggregation for Paper Trading, Orders & Positions Screens
    # -------------------------------------------------------------------------

    @staticmethod
    def serialize_strategy_instance(row: StrategyInstanceModel) -> dict[str, Any]:
        return {
            "id": str(row.id),
            "instance_code": row.instance_code,
            "account_id": str(row.account_id),
            "strategy_version_id": str(row.strategy_version_id),
            "strategy_version_label": row.strategy_version_label,
            "instrument_symbol": row.instrument_symbol,
            "timeframe": row.timeframe,
            "status": row.status,
            "order_type": row.order_type,
            "limit_offset_pts": str(Decimal(str(row.limit_offset_pts)).quantize(_Q2)),
            "execution_latency_ms": int(row.execution_latency_ms),
            "latency_bars": int(row.latency_bars),
            "slippage_bps": str(Decimal(str(row.slippage_bps)).quantize(_Q2)),
            "partial_fill_enabled": bool(row.partial_fill_enabled),
            "max_fill_ratio_per_bar": str(Decimal(str(row.max_fill_ratio_per_bar)).quantize(_Q2)),
            "valid_for_bars": row.valid_for_bars,
            "auto_execute_signals": bool(row.auto_execute_signals),
            "signals_emitted": int(row.signals_emitted),
            "orders_created": int(row.orders_created),
            "created_by": row.created_by,
            "started_at": format_ist_iso(row.started_at) if row.started_at else None,
            "paused_at": format_ist_iso(row.paused_at) if row.paused_at else None,
            "stopped_at": format_ist_iso(row.stopped_at) if row.stopped_at else None,
            "created_at": format_ist_iso(row.created_at),
            "updated_at": format_ist_iso(row.updated_at),
        }

    @staticmethod
    def serialize_signal_row(row: ReplaySignalModel) -> dict[str, Any]:
        return {
            "id": str(row.id),
            "signal_code": row.signal_code,
            "correlation_id": row.correlation_id,
            "strategy_instance_id": str(row.strategy_instance_id)
            if row.strategy_instance_id
            else None,
            "replay_session_id": str(row.replay_session_id) if row.replay_session_id else None,
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
            "reference_price": str(Decimal(str(row.reference_price)).quantize(_Q2)),
            "reason": row.rationale,
            "rationale": row.rationale,
            "status": row.status,
            "market_event_id": row.market_event_id,
        }

    @staticmethod
    def get_paper_trading_workspace(
        session: Session, *, settings: AppSettings | None = None
    ) -> dict[str, Any]:
        cfg = settings or get_settings()
        acct = PaperTradingService.get_primary_account_row(session, cfg)
        latest_rpl, fresh = PaperTradingService._current_freshness_snapshot(session, cfg)

        instances = list(
            session.scalars(
                select(StrategyInstanceModel)
                .order_by(desc(StrategyInstanceModel.created_at))
                .limit(25)
            ).all()
        )
        signals = list(
            session.scalars(
                select(ReplaySignalModel)
                .order_by(desc(ReplaySignalModel.emitted_at))
                .limit(25)
            ).all()
        )
        fills = list(
            session.scalars(
                select(PaperFillModel)
                .order_by(desc(PaperFillModel.executed_at))
                .limit(25)
            ).all()
        )
        orders = OrderManagementService.list_orders(session, limit=20)
        open_positions = list(
            session.scalars(
                select(PaperPositionModel)
                .where(PaperPositionModel.status == PositionStatus.OPEN.value)
                .order_by(desc(PaperPositionModel.opened_at))
            ).all()
        )
        versions = list(
            session.scalars(
                select(StrategyVersionModel).order_by(
                    StrategyVersionModel.instrument_symbol.asc(),
                    StrategyVersionModel.version_number.desc(),
                )
            ).all()
        )

        halted = bool(acct.paper_trading_halted)
        risk_gate = (
            "BLOCKED_BY_OPERATOR_HALT"
            if halted
            else (
                "ARMED_AND_HEALTHY"
                if fresh.new_orders_permitted
                else f"REFUSING_NEW_ORDERS_{fresh.status.value}"
            )
        )

        return {
            "simulation_banner": "SIMULATED PAPER TRADING",
            "execution_mode": cfg.EXECUTION_MODE,
            "account": {
                "id": str(acct.id),
                "account_code": acct.account_code,
                "account_designation": acct.account_designation,
                "is_demo_capital": bool(acct.is_demo_capital),
                "initial_capital": str(Decimal(str(acct.initial_capital)).quantize(_Q2)),
                "available_capital": str(Decimal(str(acct.available_capital)).quantize(_Q2)),
                "realized_pnl_today": str(Decimal(str(acct.realized_pnl_today)).quantize(_Q2)),
                "unrealized_pnl": str(Decimal(str(acct.unrealized_pnl)).quantize(_Q2)),
                "paper_trading_halted": halted,
                "halt_reason": acct.halt_reason,
                "status": acct.status,
            },
            "risk_status": {
                "gate_status": risk_gate,
                "paper_trading_halted": halted,
                "market_data_freshness": fresh.status.value,
                "new_orders_permitted": (not halted) and fresh.new_orders_permitted,
                "open_positions_count": len(open_positions),
                "max_open_positions": cfg.PAPER_MAX_OPEN_POSITIONS,
                "margin_rate": str(cfg.PAPER_FUTURES_MARGIN_RATE),
            },
            "market_replay": {
                "has_session": latest_rpl is not None,
                "session_id": str(latest_rpl.id) if latest_rpl else None,
                "session_code": latest_rpl.session_code if latest_rpl else None,
                "instrument_symbol": latest_rpl.instrument_symbol if latest_rpl else None,
                "status": latest_rpl.status if latest_rpl else "STOPPED",
                "replay_speed": latest_rpl.replay_speed if latest_rpl else "1x",
                "candles_processed": int(latest_rpl.candles_processed) if latest_rpl else 0,
                "total_session_candles": int(latest_rpl.total_session_candles)
                if latest_rpl
                else 0,
                "replay_clock_ist": format_ist_iso(latest_rpl.replay_clock_ist)
                if (latest_rpl and latest_rpl.replay_clock_ist)
                else None,
                "freshness": fresh.to_dict(),
            },
            "fill_model": PaperBrokerService.fill_model_documentation(),
            "available_strategy_versions": [
                {
                    "id": str(v.id),
                    "version_label": v.version_label,
                    "instrument_symbol": v.instrument_symbol,
                    "timeframe": v.timeframe,
                }
                for v in versions
            ],
            "strategy_instances": [
                PaperTradingService.serialize_strategy_instance(i) for i in instances
            ],
            "recent_signals": [PaperTradingService.serialize_signal_row(s) for s in signals],
            "recent_fills": [OrderManagementService.serialize_fill(f) for f in fills],
            "recent_orders": orders,
            "open_positions": [PositionService.serialize_position(p) for p in open_positions],
        }
