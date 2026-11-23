"""Order Management System (OMS) enforcing strict state transitions and idempotency."""

from __future__ import annotations

import uuid
from datetime import datetime
from decimal import Decimal
from typing import Any

from sqlalchemy import desc, select
from sqlalchemy.orm import Session

from core.audit.service import AuditService
from core.clock import ensure_ist, format_ist_iso, now_ist
from core.config import AppSettings, get_settings
from core.domain.enums import (
    ORDER_TRANSITIONS,
    SIGNAL_TRANSITIONS,
    TERMINAL_ORDER_STATUSES,
    WORKING_ORDER_STATUSES,
    ActorType,
    AuditCategory,
    AuditSeverity,
    OrderSource,
    OrderStatus,
    OrderType,
    RiskDecisionOutcome,
    Side,
    SignalStatus,
    validate_state_transition,
)
from core.exceptions import ControlCenterError, DuplicateEntityError, EntityNotFoundError
from core.logging import get_correlation_id
from core.market_data.freshness import MarketDataFreshnessSnapshot
from core.persistence.models import (
    OrderStateEventModel,
    PaperAccountModel,
    PaperFillModel,
    PaperOrderModel,
    ReplaySignalModel,
)
from core.risk.evaluator import PreTradeRiskEvaluator
from core.risk.service import RiskControlService

_Q2 = Decimal("0.01")


class OrderManagementService:
    """Stateless OMS service owning order creation, state transitions, and duplicate prevention."""

    @staticmethod
    def transition_order_status(
        session: Session,
        order: PaperOrderModel,
        target_status: OrderStatus,
        *,
        actor: str,
        reason: str,
        details: dict[str, Any] | None = None,
        occurred_at: datetime | None = None,
    ) -> PaperOrderModel:
        """Validate `current -> target` against `ORDER_TRANSITIONS` and persist immutable event."""
        current_status = OrderStatus(order.status)
        validated_target = validate_state_transition(
            "Order", current_status, target_status, ORDER_TRANSITIONS
        )
        ts = ensure_ist(occurred_at) if occurred_at is not None else now_ist()

        order.status = validated_target.value
        if validated_target == OrderStatus.SUBMITTED and order.submitted_at is None:
            order.submitted_at = ts
        elif validated_target == OrderStatus.ACKNOWLEDGED and order.acknowledged_at is None:
            order.acknowledged_at = ts
        elif validated_target in TERMINAL_ORDER_STATUSES:
            order.completed_at = ts
            if validated_target in (
                OrderStatus.CANCELLED,
                OrderStatus.EXPIRED,
                OrderStatus.REJECTED,
                OrderStatus.RISK_REJECTED,
                OrderStatus.FAILED,
            ):
                order.leaves_quantity = 0
                if validated_target == OrderStatus.CANCELLED:
                    order.cancel_reason = reason
                else:
                    order.rejection_reason = reason

        order.updated_at = ts

        evt = OrderStateEventModel(
            order_id=order.id,
            previous_status=current_status.value,
            new_status=validated_target.value,
            occurred_at=ts,
            actor=actor,
            reason=reason,
            details_json=details or {},
            correlation_id=get_correlation_id(),
        )
        session.add(evt)
        session.flush()

        sev = (
            AuditSeverity.WARNING
            if validated_target
            in (
                OrderStatus.RISK_REJECTED,
                OrderStatus.REJECTED,
                OrderStatus.EXPIRED,
                OrderStatus.FAILED,
            )
            else AuditSeverity.INFO
        )
        AuditService.record(
            session,
            action=f"ORDER_STATE_{validated_target.value}",
            summary=(
                f"Paper order {order.order_code} ({order.side} {order.quantity} "
                f"{order.instrument_symbol} {order.order_type}) {current_status.value} -> "
                f"{validated_target.value}: {reason}"
            ),
            actor=actor,
            actor_type=ActorType.OPERATOR if "operator" in actor.lower() else ActorType.SYSTEM,
            category=AuditCategory.OMS,
            severity=sev,
            entity_type="paper_order",
            entity_id=str(order.id),
            details={
                "order_code": order.order_code,
                "client_order_key": order.client_order_key,
                "previous_status": current_status.value,
                "new_status": validated_target.value,
                "reason": reason,
                **(details or {}),
            },
        )
        return order

    @staticmethod
    def create_and_evaluate_order(
        session: Session,
        *,
        account: PaperAccountModel,
        client_order_key: str,
        instrument_symbol: str,
        side: Side | str,
        order_type: OrderType | str,
        quantity: int,
        order_source: OrderSource = OrderSource.OPERATOR_MANUAL,
        timeframe: str = "5m",
        reference_price: Decimal | None = None,
        limit_price: Decimal | None = None,
        stop_price: Decimal | None = None,
        reduce_only: bool = False,
        exit_reason: str | None = None,
        strategy_instance_id: uuid.UUID | None = None,
        strategy_version_id: uuid.UUID | None = None,
        strategy_version_label: str | None = None,
        signal: ReplaySignalModel | None = None,
        replay_session_id: uuid.UUID | None = None,
        execution_latency_ms: int | None = None,
        latency_bars: int | None = None,
        created_bar_timestamp: datetime | None = None,
        slippage_bps: Decimal | None = None,
        charges_bps: Decimal | None = None,
        per_order_fee_inr: Decimal | None = None,
        max_fill_ratio_per_bar: Decimal = Decimal("1.0"),
        simulate_rejection: bool = False,
        simulated_rejection_reason: str | None = None,
        valid_for_bars: int | None = None,
        expires_at: datetime | None = None,
        target_stop_loss_price: Decimal | None = None,
        target_profit_price: Decimal | None = None,
        freshness: MarketDataFreshnessSnapshot | None = None,
        require_fresh_market_data: bool = True,
        actor: str = "desk.operator",
        settings: AppSettings | None = None,
    ) -> tuple[PaperOrderModel, bool]:
        """Idempotently create a `CREATED` paper order and run pre-trade risk evaluation.

        Returns `(order, is_duplicate)`.
        - Duplicate `client_order_key` returns `(existing_order, True)` without creating a second order.
        - Duplicate `signal` (where an order already references `signal.id`) returns `(existing_order, True)`.
        """
        cfg = settings or get_settings()
        clean_key = client_order_key.strip()
        if not clean_key:
            raise ControlCenterError(
                "client_order_key is required for OMS idempotency.", code="MISSING_CLIENT_ORDER_KEY"
            )

        side_enum = Side(side.strip().upper()) if isinstance(side, str) else side
        type_enum = (
            OrderType(order_type.strip().upper()) if isinstance(order_type, str) else order_type
        )
        clean_sym = instrument_symbol.strip().upper()
        strat_lbl = (
            strategy_version_label
            or (signal.strategy_version_label if signal is not None else None)
            or "MANUAL_PAPER_DESK"
        )

        # 1. Operational Risk: Duplicate Order Check by client_order_key
        existing_by_key = session.scalar(
            select(PaperOrderModel).where(PaperOrderModel.client_order_key == clean_key)
        )
        if existing_by_key is not None:
            RiskControlService.record_risk_decision(
                session,
                outcome=RiskDecisionOutcome.REJECTED,
                primary_rule="DUPLICATE_ORDER_CHECK",
                current_value=clean_key,
                limit_value="1 order per client_order_key",
                reason=f"Duplicate order request suppressed: client_order_key '{clean_key}' already exists on {existing_by_key.order_code}.",
                checks=[
                    {
                        "gate": "DUPLICATE_ORDER_CHECK",
                        "rule": "DUPLICATE_ORDER_CHECK",
                        "scope": "OPERATIONAL",
                        "passed": False,
                        "current_value": clean_key,
                        "limit_value": "UNIQUE_KEY",
                        "detail": f"Existing order {existing_by_key.order_code}",
                    }
                ],
                account_id=account.id,
                order_id=existing_by_key.id,
                client_order_key=clean_key,
                signal_id=signal.id if signal is not None else None,
                strategy_instance_id=strategy_instance_id,
                strategy_version_id=strategy_version_id,
                strategy_label=strat_lbl,
                instrument_symbol=clean_sym,
                side=side_enum.value,
                order_type=type_enum.value,
                proposed_quantity=int(quantity),
                proposed_price=reference_price or limit_price or stop_price,
            )
            return existing_by_key, True

        # 2. Operational Risk: Duplicate Signal Check (a signal can NEVER create multiple orders)
        if signal is not None:
            existing_by_sig = session.scalar(
                select(PaperOrderModel).where(PaperOrderModel.signal_id == signal.id)
            )
            if existing_by_sig is not None:
                RiskControlService.record_risk_decision(
                    session,
                    outcome=RiskDecisionOutcome.REJECTED,
                    primary_rule="DUPLICATE_SIGNAL_CHECK",
                    current_value=signal.signal_code,
                    limit_value="1 order per unique Signal ID",
                    reason=f"Duplicate signal processing blocked: Signal '{signal.signal_code}' already created order {existing_by_sig.order_code}.",
                    checks=[
                        {
                            "gate": "DUPLICATE_SIGNAL_CHECK",
                            "rule": "DUPLICATE_SIGNAL_CHECK",
                            "scope": "OPERATIONAL",
                            "passed": False,
                            "current_value": signal.signal_code,
                            "limit_value": "UNIQUE_SIGNAL_ID",
                            "detail": f"Existing order {existing_by_sig.order_code}",
                        }
                    ],
                    account_id=account.id,
                    order_id=existing_by_sig.id,
                    client_order_key=clean_key,
                    signal_id=signal.id,
                    strategy_instance_id=strategy_instance_id,
                    strategy_version_id=strategy_version_id or signal.strategy_version_id,
                    strategy_label=strat_lbl,
                    instrument_symbol=clean_sym,
                    side=side_enum.value,
                    order_type=type_enum.value,
                    proposed_quantity=int(quantity),
                    proposed_price=reference_price or limit_price or stop_price,
                )
                return existing_by_sig, True
            if signal.status != SignalStatus.GENERATED.value:
                raise DuplicateEntityError(
                    f"Signal '{signal.signal_code}' is already in terminal/processed state "
                    f"'{signal.status}' and cannot create another order."
                )

        ts_now = now_ist()

        eff_latency_ms = (
            execution_latency_ms
            if execution_latency_ms is not None
            else cfg.PAPER_DEFAULT_LATENCY_MS
        )
        eff_latency_bars = (
            latency_bars if latency_bars is not None else cfg.PAPER_DEFAULT_LATENCY_BARS
        )
        eff_slippage = (
            Decimal(str(slippage_bps))
            if slippage_bps is not None
            else cfg.PAPER_DEFAULT_SLIPPAGE_BPS
        )
        eff_charges_bps = (
            Decimal(str(charges_bps))
            if charges_bps is not None
            else cfg.PAPER_DEFAULT_CHARGES_BPS
        )
        eff_fee_inr = (
            Decimal(str(per_order_fee_inr))
            if per_order_fee_inr is not None
            else cfg.PAPER_DEFAULT_PER_ORDER_FEE_INR
        )
        eff_fill_ratio = Decimal(str(max_fill_ratio_per_bar))
        if eff_fill_ratio <= Decimal("0") or eff_fill_ratio > Decimal("1"):
            raise ControlCenterError(
                "max_fill_ratio_per_bar must be in (0, 1].", code="INVALID_FILL_RATIO"
            )

        ord_code = f"ORD-{clean_sym}-{ts_now.strftime('%H%M%S')}-{uuid.uuid4().hex[:5].upper()}"
        corr_id = signal.correlation_id if signal is not None else get_correlation_id()

        order = PaperOrderModel(
            order_code=ord_code,
            client_order_key=clean_key,
            correlation_id=corr_id,
            account_id=account.id,
            strategy_instance_id=strategy_instance_id,
            strategy_version_id=strategy_version_id,
            strategy_version_label=strategy_version_label,
            signal_id=signal.id if signal is not None else None,
            replay_session_id=replay_session_id,
            order_source=order_source.value,
            reduce_only=reduce_only,
            exit_reason=exit_reason,
            instrument_symbol=clean_sym,
            timeframe=timeframe,
            side=side_enum.value,
            order_type=type_enum.value,
            quantity=int(quantity),
            filled_quantity=0,
            leaves_quantity=int(quantity),
            requested_reference_price=reference_price,
            limit_price=limit_price,
            stop_price=stop_price,
            stop_triggered=False,
            stop_triggered_at=None,
            average_fill_price=None,
            total_slippage_cost=Decimal("0"),
            total_charges=Decimal("0"),
            execution_latency_ms=int(eff_latency_ms),
            latency_bars=int(eff_latency_bars),
            created_bar_timestamp=ensure_ist(created_bar_timestamp)
            if created_bar_timestamp is not None
            else None,
            bars_waited=0,
            slippage_bps=eff_slippage,
            charges_bps=eff_charges_bps,
            per_order_fee_inr=eff_fee_inr,
            max_fill_ratio_per_bar=eff_fill_ratio,
            simulate_rejection=bool(simulate_rejection),
            simulated_rejection_reason=simulated_rejection_reason,
            valid_for_bars=valid_for_bars,
            expires_at=ensure_ist(expires_at) if expires_at is not None else None,
            target_stop_loss_price=target_stop_loss_price,
            target_profit_price=target_profit_price,
            status=OrderStatus.CREATED.value,
            risk_check_passed=None,
            risk_check_details_json={},
            created_by=actor,
            created_at=ts_now,
            updated_at=ts_now,
        )
        session.add(order)
        session.flush()

        # Record initial CREATED OMS event + canonical ORDER_CREATED audit event
        session.add(
            OrderStateEventModel(
                order_id=order.id,
                previous_status=None,
                new_status=OrderStatus.CREATED.value,
                occurred_at=ts_now,
                actor=actor,
                reason=f"Created {order_source.value} paper order ({side_enum.value} {quantity} {clean_sym} {type_enum.value}).",
                details_json={"client_order_key": clean_key},
                correlation_id=corr_id,
            )
        )
        session.flush()
        AuditService.record(
            session,
            action="ORDER_CREATED",
            event_type="ORDER_CREATED",
            summary=f"Created paper order {order.order_code} ({side_enum.value} {quantity} {clean_sym} {type_enum.value}).",
            actor=actor,
            actor_type=ActorType.OPERATOR if "operator" in actor.lower() else ActorType.SYSTEM,
            category=AuditCategory.OMS,
            severity=AuditSeverity.INFO,
            entity_type="paper_order",
            entity_id=str(order.id),
            strategy_id=str(strategy_version_id) if strategy_version_id else strat_lbl,
            signal_id=str(signal.id) if signal is not None else None,
            order_id=str(order.id),
            source="oms.service",
            details={
                "order_code": order.order_code,
                "client_order_key": clean_key,
                "instrument": clean_sym,
                "side": side_enum.value,
                "order_type": type_enum.value,
                "quantity": int(quantity),
            },
            correlation_id=corr_id,
        )

        # 3. Independent Pre-Trade Risk Evaluation (CREATED -> RISK_APPROVED | RISK_REJECTED)
        risk_decision = PreTradeRiskEvaluator.evaluate_order(
            session,
            account=account,
            instrument_symbol=clean_sym,
            order_type=type_enum,
            quantity=int(quantity),
            reference_price=reference_price,
            limit_price=limit_price,
            stop_price=stop_price,
            stop_loss_price=target_stop_loss_price,
            reduce_only=reduce_only,
            signal=signal,
            strategy_version_id=strategy_version_id,
            strategy_instance_id=strategy_instance_id,
            created_bar_timestamp=created_bar_timestamp,
            freshness=freshness,
            require_fresh_market_data=require_fresh_market_data,
            settings=cfg,
        )
        order.risk_check_passed = risk_decision.approved
        order.risk_check_details_json = risk_decision.to_dict()

        RiskControlService.record_risk_decision(
            session,
            decision=risk_decision,
            account_id=account.id,
            order_id=order.id,
            client_order_key=clean_key,
            signal_id=signal.id if signal is not None else None,
            strategy_instance_id=strategy_instance_id,
            strategy_version_id=strategy_version_id
            or (signal.strategy_version_id if signal is not None else None),
            strategy_label=strat_lbl,
            instrument_symbol=clean_sym,
            side=side_enum.value,
            order_type=type_enum.value,
            proposed_quantity=int(quantity),
            proposed_price=reference_price or limit_price or stop_price,
        )

        if not risk_decision.approved:
            OrderManagementService.transition_order_status(
                session,
                order,
                OrderStatus.RISK_REJECTED,
                actor="risk.engine",
                reason=f"[{risk_decision.code}] {risk_decision.reason}",
                details=risk_decision.to_dict(),
                occurred_at=ts_now,
            )
            if signal is not None and signal.status == SignalStatus.GENERATED.value:
                sig_curr = SignalStatus(signal.status)
                signal.status = validate_state_transition(
                    "Signal", sig_curr, SignalStatus.RISK_REJECTED, SIGNAL_TRANSITIONS
                ).value
            session.flush()
            return order, False

        OrderManagementService.transition_order_status(
            session,
            order,
            OrderStatus.RISK_APPROVED,
            actor="risk.engine",
            reason=risk_decision.reason,
            details=risk_decision.to_dict(),
            occurred_at=ts_now,
        )
        if signal is not None:
            sig_curr = SignalStatus(signal.status)
            approved_sig = validate_state_transition(
                "Signal", sig_curr, SignalStatus.RISK_APPROVED, SIGNAL_TRANSITIONS
            )
            signal.status = validate_state_transition(
                "Signal", approved_sig, SignalStatus.ORDER_CREATED, SIGNAL_TRANSITIONS
            ).value
        session.flush()
        return order, False

    @staticmethod
    def cancel_order(
        session: Session,
        order_id: uuid.UUID,
        *,
        actor: str = "desk.operator",
        reason: str = "Cancelled by operator",
    ) -> PaperOrderModel:
        order = session.get(PaperOrderModel, order_id)
        if order is None:
            raise EntityNotFoundError(f"PaperOrder '{order_id}' not found.")
        return OrderManagementService.transition_order_status(
            session,
            order,
            OrderStatus.CANCELLED,
            actor=actor,
            reason=reason,
            details={"filled_before_cancel": int(order.filled_quantity)},
        )

    @staticmethod
    def cancel_all_working_orders(
        session: Session,
        *,
        instrument_symbol: str | None = None,
        actor: str = "desk.operator",
        reason: str = "Operator cancelled all working paper orders",
    ) -> list[PaperOrderModel]:
        working_vals = [s.value for s in WORKING_ORDER_STATUSES]
        stmt = select(PaperOrderModel).where(PaperOrderModel.status.in_(working_vals))
        if instrument_symbol:
            stmt = stmt.where(PaperOrderModel.instrument_symbol == instrument_symbol.strip().upper())
        rows = list(session.scalars(stmt.order_by(PaperOrderModel.created_at.asc())).all())
        for r in rows:
            OrderManagementService.transition_order_status(
                session, r, OrderStatus.CANCELLED, actor=actor, reason=reason
            )
        return rows

    @staticmethod
    def list_orders(
        session: Session,
        *,
        status_filter: str | None = None,
        instrument_symbol: str | None = None,
        limit: int = 100,
    ) -> list[dict[str, Any]]:
        stmt = select(PaperOrderModel).order_by(desc(PaperOrderModel.created_at))
        if status_filter and status_filter.upper() != "ALL":
            if status_filter.upper() == "WORKING":
                stmt = stmt.where(
                    PaperOrderModel.status.in_([s.value for s in WORKING_ORDER_STATUSES])
                )
            else:
                stmt = stmt.where(PaperOrderModel.status == status_filter.strip().upper())
        if instrument_symbol and instrument_symbol.upper() != "ALL":
            stmt = stmt.where(
                PaperOrderModel.instrument_symbol == instrument_symbol.strip().upper()
            )
        rows = list(session.scalars(stmt.limit(max(1, min(limit, 500)))).all())
        return [OrderManagementService.serialize_order(r, include_events=False) for r in rows]

    @staticmethod
    def get_order_detail(session: Session, order_id: uuid.UUID) -> dict[str, Any]:
        row = session.get(PaperOrderModel, order_id)
        if row is None:
            raise EntityNotFoundError(f"PaperOrder '{order_id}' not found.")
        return OrderManagementService.serialize_order(row, include_events=True)

    @staticmethod
    def serialize_fill(fill: PaperFillModel) -> dict[str, Any]:
        return {
            "id": str(fill.id),
            "fill_code": fill.fill_code,
            "fill_idempotency_key": fill.fill_idempotency_key,
            "order_id": str(fill.order_id),
            "account_id": str(fill.account_id),
            "strategy_instance_id": str(fill.strategy_instance_id)
            if fill.strategy_instance_id
            else None,
            "strategy_version_label": fill.strategy_version_label,
            "instrument_symbol": fill.instrument_symbol,
            "side": fill.side,
            "fill_quantity": int(fill.fill_quantity),
            "raw_market_price": str(Decimal(str(fill.raw_market_price)).quantize(_Q2)),
            "fill_price": str(Decimal(str(fill.fill_price)).quantize(_Q2)),
            "slippage_cost": str(Decimal(str(fill.slippage_cost)).quantize(_Q2)),
            "charges": str(Decimal(str(fill.charges)).quantize(_Q2)),
            "simulated_latency_ms": int(fill.simulated_latency_ms),
            "bar_timestamp": format_ist_iso(fill.bar_timestamp),
            "executed_at": format_ist_iso(fill.executed_at),
            "liquidity_note": fill.liquidity_note,
            "correlation_id": fill.correlation_id,
        }

    @staticmethod
    def serialize_order(
        order: PaperOrderModel, *, include_events: bool = False
    ) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "id": str(order.id),
            "order_code": order.order_code,
            "client_order_key": order.client_order_key,
            "correlation_id": order.correlation_id,
            "account_id": str(order.account_id),
            "strategy_instance_id": str(order.strategy_instance_id)
            if order.strategy_instance_id
            else None,
            "strategy_version_id": str(order.strategy_version_id)
            if order.strategy_version_id
            else None,
            "strategy_version_label": order.strategy_version_label or "MANUAL_PAPER_DESK",
            "signal_id": str(order.signal_id) if order.signal_id else None,
            "replay_session_id": str(order.replay_session_id) if order.replay_session_id else None,
            "order_source": order.order_source,
            "reduce_only": bool(order.reduce_only),
            "exit_reason": order.exit_reason,
            "instrument_symbol": order.instrument_symbol,
            "timeframe": order.timeframe,
            "side": order.side,
            "order_type": order.order_type,
            "quantity": int(order.quantity),
            "filled_quantity": int(order.filled_quantity),
            "leaves_quantity": int(order.leaves_quantity),
            "requested_reference_price": (
                str(Decimal(str(order.requested_reference_price)).quantize(_Q2))
                if order.requested_reference_price is not None
                else None
            ),
            "limit_price": (
                str(Decimal(str(order.limit_price)).quantize(_Q2))
                if order.limit_price is not None
                else None
            ),
            "stop_price": (
                str(Decimal(str(order.stop_price)).quantize(_Q2))
                if order.stop_price is not None
                else None
            ),
            "stop_triggered": bool(order.stop_triggered),
            "average_fill_price": (
                str(Decimal(str(order.average_fill_price)).quantize(_Q2))
                if order.average_fill_price is not None
                else None
            ),
            "total_slippage_cost": str(Decimal(str(order.total_slippage_cost)).quantize(_Q2)),
            "total_charges": str(Decimal(str(order.total_charges)).quantize(_Q2)),
            "execution_latency_ms": int(order.execution_latency_ms),
            "latency_bars": int(order.latency_bars),
            "bars_waited": int(order.bars_waited),
            "slippage_bps": str(Decimal(str(order.slippage_bps)).quantize(_Q2)),
            "max_fill_ratio_per_bar": str(Decimal(str(order.max_fill_ratio_per_bar)).quantize(_Q2)),
            "valid_for_bars": order.valid_for_bars,
            "expires_at": format_ist_iso(order.expires_at) if order.expires_at else None,
            "status": order.status,
            "is_working": order.status in {s.value for s in WORKING_ORDER_STATUSES},
            "risk_check_passed": order.risk_check_passed,
            "risk_check_details": order.risk_check_details_json or {},
            "rejection_reason": order.rejection_reason,
            "cancel_reason": order.cancel_reason,
            "submitted_at": format_ist_iso(order.submitted_at) if order.submitted_at else None,
            "acknowledged_at": format_ist_iso(order.acknowledged_at)
            if order.acknowledged_at
            else None,
            "completed_at": format_ist_iso(order.completed_at) if order.completed_at else None,
            "created_by": order.created_by,
            "created_at": format_ist_iso(order.created_at),
            "updated_at": format_ist_iso(order.updated_at),
        }
        if include_events:
            payload["state_events"] = [
                {
                    "id": str(ev.id),
                    "previous_status": ev.previous_status,
                    "new_status": ev.new_status,
                    "occurred_at": format_ist_iso(ev.occurred_at),
                    "actor": ev.actor,
                    "reason": ev.reason,
                    "details": ev.details_json or {},
                }
                for ev in order.state_events
            ]
            payload["fills"] = [
                OrderManagementService.serialize_fill(f) for f in order.fills
            ]
        return payload
