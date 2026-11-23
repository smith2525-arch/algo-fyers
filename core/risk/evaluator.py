"""Independent, Fail-Closed Pre-Trade Risk Evaluator (`core/risk/evaluator.py`).

Architectural Guarantee:
- Strategy logic ONLY proposes signals; it NEVER decides risk approval.
- `PreTradeRiskEvaluator` inspects Operational, Strategy-Level, and Account-Level
  rules independently and FAILS CLOSED if any critical dependency, freshness check,
  reconciliation check, or kill switch is tripped.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass, field
from datetime import datetime
from decimal import Decimal
from typing import Any

from sqlalchemy import asc, desc, func, select, text
from sqlalchemy.orm import Session

from core.clock import IST, ensure_ist, now_ist, parse_hms_time
from core.config import AppSettings, get_settings
from core.domain.enums import (
    MarketDataFreshnessStatus,
    OrderStatus,
    OrderType,
    PaperAccountStatus,
    PositionStatus,
    RiskDecisionOutcome,
    RiskRuleScope,
    SignalStatus,
)
from core.market_data.freshness import MarketDataFreshnessSnapshot
from core.persistence.models import (
    AccountRiskConfigModel,
    InstrumentModel,
    PaperAccountModel,
    PaperOrderModel,
    PaperPositionModel,
    ReplaySignalModel,
    StrategyRiskConfigModel,
    StrategyVersionModel,
)
from core.positions.service import PositionService

_Q2 = Decimal("0.01")
_Q4 = Decimal("0.0001")


def _fmt_inr_str(val: Decimal) -> str:
    return f"₹{val.quantize(_Q2):,.2f}"


@dataclass(frozen=True)
class RiskDecision:
    """Structured result of an independent pre-trade risk evaluation."""

    approved: bool
    decision: RiskDecisionOutcome
    code: str
    primary_rule: str
    current_value: str
    limit_value: str
    reason: str
    estimated_notional_inr: Decimal
    estimated_margin_inr: Decimal
    available_capital_inr: Decimal
    checks: list[dict[str, Any]] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {
            "approved": self.approved,
            "decision": self.decision.value,
            "code": self.code,
            "primary_rule": self.primary_rule,
            "current_value": self.current_value,
            "limit_value": self.limit_value,
            "reason": self.reason,
            "estimated_notional_inr": str(self.estimated_notional_inr.quantize(_Q2)),
            "estimated_margin_inr": str(self.estimated_margin_inr.quantize(_Q2)),
            "available_capital_inr": str(self.available_capital_inr.quantize(_Q2)),
            "checks": self.checks,
        }


class PreTradeRiskEvaluator:
    """Independent, fail-closed pre-trade risk engine gating all simulated paper execution."""

    @staticmethod
    def _compute_strategy_drawdown_and_daily_loss(
        positions: list[PaperPositionModel],
        *,
        trading_day_iso: str,
    ) -> tuple[Decimal, Decimal, datetime | None]:
        """Return `(strategy_daily_loss_inr, max_drawdown_inr, last_losing_close_at)`."""
        daily_net_pnl = Decimal("0.0000")
        equity = Decimal("0.0000")
        peak = Decimal("0.0000")
        max_dd = Decimal("0.0000")
        last_loss_at: datetime | None = None

        ordered = sorted(positions, key=lambda p: ensure_ist(p.opened_at))
        for p in ordered:
            p_real = Decimal(str(p.realized_pnl))
            p_unreal = (
                Decimal(str(p.unrealized_pnl))
                if p.status == PositionStatus.OPEN.value
                else Decimal("0.0000")
            )
            equity += p_real + p_unreal
            if equity > peak:
                peak = equity
            dd = peak - equity
            if dd > max_dd:
                max_dd = dd

            ref_dt = ensure_ist(p.closed_at or p.last_mark_at or p.opened_at)
            if ref_dt.date().isoformat() == trading_day_iso:
                daily_net_pnl += p_real + p_unreal

            if (
                p.status == PositionStatus.CLOSED.value
                and p_real < Decimal("0")
                and p.closed_at is not None
            ):
                closed_ist = ensure_ist(p.closed_at)
                if last_loss_at is None or closed_ist > last_loss_at:
                    last_loss_at = closed_ist

        daily_loss = abs(daily_net_pnl) if daily_net_pnl < Decimal("0") else Decimal("0.0000")
        return daily_loss.quantize(_Q4), max_dd.quantize(_Q4), last_loss_at

    @staticmethod
    def _count_consecutive_account_losses(
        session: Session, account_id: uuid.UUID
    ) -> int:
        closed_rows = list(
            session.scalars(
                select(PaperPositionModel)
                .where(
                    PaperPositionModel.account_id == account_id,
                    PaperPositionModel.status == PositionStatus.CLOSED.value,
                )
                .order_by(desc(PaperPositionModel.closed_at), desc(PaperPositionModel.updated_at))
                .limit(50)
            ).all()
        )
        streak = 0
        for r in closed_rows:
            if Decimal(str(r.realized_pnl)) < Decimal("0"):
                streak += 1
            else:
                break
        return streak

    @staticmethod
    def evaluate_order(
        session: Session,
        *,
        account: PaperAccountModel,
        instrument_symbol: str,
        order_type: OrderType,
        quantity: int,
        reference_price: Decimal | None,
        limit_price: Decimal | None = None,
        stop_price: Decimal | None = None,
        stop_loss_price: Decimal | None = None,
        reduce_only: bool = False,
        signal: ReplaySignalModel | None = None,
        strategy_version_id: uuid.UUID | None = None,
        strategy_instance_id: uuid.UUID | None = None,
        created_bar_timestamp: datetime | None = None,
        freshness: MarketDataFreshnessSnapshot | None = None,
        require_fresh_market_data: bool = True,
        evaluation_time: datetime | None = None,
        force_unknown_risk_state: bool = False,
        settings: AppSettings | None = None,
    ) -> RiskDecision:
        """Evaluate Operational, Strategy-Level, and Account-Level risk rules (Fail-Closed)."""
        cfg = settings or get_settings()
        eval_now = ensure_ist(evaluation_time) if evaluation_time is not None else now_ist()
        checks: list[dict[str, Any]] = []
        avail_cap = Decimal(str(account.available_capital))

        val_price = reference_price or limit_price or stop_price or Decimal("0")
        if order_type == OrderType.LIMIT and limit_price is not None and limit_price > Decimal("0"):
            val_price = limit_price
        elif order_type == OrderType.STOP and stop_price is not None and stop_price > Decimal("0"):
            val_price = stop_price

        notional = (val_price * Decimal(max(0, quantity))).quantize(_Q4)
        est_margin = (
            Decimal("0.0000")
            if reduce_only
            else (notional * cfg.PAPER_FUTURES_MARGIN_RATE).quantize(_Q4)
        )

        def _add_check(
            rule: str,
            scope: RiskRuleScope,
            passed: bool,
            current_val: str,
            limit_val: str,
            detail: str,
        ) -> None:
            checks.append(
                {
                    "gate": rule,
                    "rule": rule,
                    "scope": scope.value,
                    "passed": passed,
                    "current_value": current_val,
                    "limit_value": limit_val,
                    "detail": detail,
                }
            )

        def _reject(
            rule: str,
            current_val: str,
            limit_val: str,
            reason: str,
            *,
            code: str | None = None,
        ) -> RiskDecision:
            return RiskDecision(
                approved=False,
                decision=RiskDecisionOutcome.REJECTED,
                code=code or rule,
                primary_rule=rule,
                current_value=current_val,
                limit_value=limit_val,
                reason=reason,
                estimated_notional_inr=notional,
                estimated_margin_inr=est_margin,
                available_capital_inr=avail_cap,
                checks=checks,
            )

        # =====================================================================
        # STAGE 1: OPERATIONAL & FAIL-CLOSED SAFETY GATES
        # =====================================================================
        try:
            session.execute(text("SELECT 1"))
            db_ok = True
        except Exception as exc:  # noqa: BLE001
            _add_check(
                "DATABASE_AVAILABILITY_GATE",
                RiskRuleScope.OPERATIONAL,
                False,
                "UNREACHABLE",
                "CONNECTED",
                f"Database health query failed: {exc}",
            )
            return _reject(
                "DATABASE_AVAILABILITY_GATE",
                "UNREACHABLE",
                "CONNECTED",
                "Fail-closed block: PostgreSQL state store unavailable during risk check.",
            )
        _add_check(
            "DATABASE_AVAILABILITY_GATE",
            RiskRuleScope.OPERATIONAL,
            db_ok,
            "CONNECTED",
            "CONNECTED",
            "Risk state store reachable.",
        )

        # Load persisted AccountRiskConfigModel — FAIL CLOSED if missing or forced unknown
        risk_cfg = (
            None
            if force_unknown_risk_state
            else session.scalar(
                select(AccountRiskConfigModel).where(
                    AccountRiskConfigModel.account_id == account.id
                )
            )
        )
        if risk_cfg is None:
            _add_check(
                "UNKNOWN_CRITICAL_RISK_STATE",
                RiskRuleScope.OPERATIONAL,
                False,
                "MISSING_RISK_CONFIG",
                "VALID_RISK_CONFIG",
                "Persisted AccountRiskConfig is missing or unreadable.",
            )
            return _reject(
                "UNKNOWN_CRITICAL_RISK_STATE",
                "MISSING_RISK_CONFIG",
                "VALID_RISK_CONFIG",
                "Fail-closed block: critical persisted risk configuration is unavailable.",
            )

        # PAPER_ONLY mode enforcement
        paper_only_ok = cfg.EXECUTION_MODE == "PAPER_ONLY" and bool(account.is_demo_capital)
        _add_check(
            "PAPER_ONLY_MODE",
            RiskRuleScope.OPERATIONAL,
            paper_only_ok,
            cfg.EXECUTION_MODE,
            "PAPER_ONLY",
            f"mode={cfg.EXECUTION_MODE}, demo_capital={account.is_demo_capital}",
        )
        if not paper_only_ok:
            return _reject(
                "PAPER_ONLY_MODE",
                cfg.EXECUTION_MODE,
                "PAPER_ONLY",
                "System is locked to PAPER_ONLY demonstration capital.",
                code="EXECUTION_MODE_VIOLATION",
            )

        # Global Kill Switch / STOP PAPER TRADING Gate (Blocks NEW entry orders; allows explicit reduce_only flatten)
        kill_active = (
            bool(risk_cfg.global_kill_switch_active)
            or bool(account.paper_trading_halted)
            or account.status == PaperAccountStatus.HALTED.value
        )
        if kill_active and not reduce_only:
            ks_reason = (
                risk_cfg.kill_switch_reason
                or account.halt_reason
                or "Global Kill Switch active"
            )
            _add_check(
                "GLOBAL_KILL_SWITCH",
                RiskRuleScope.ACCOUNT,
                False,
                "ACTIVE",
                "INACTIVE",
                ks_reason,
            )
            return _reject(
                "GLOBAL_KILL_SWITCH",
                "ACTIVE",
                "INACTIVE",
                f"Global Kill Switch is active ({ks_reason}). New paper orders are blocked; "
                "open positions remain untouched unless explicitly flattened.",
                code="PAPER_TRADING_HALTED",
            )
        _add_check(
            "GLOBAL_KILL_SWITCH",
            RiskRuleScope.ACCOUNT,
            True,
            "REDUCE_ONLY_EXIT" if kill_active else "INACTIVE",
            "INACTIVE",
            "Reduce-only position exit permitted" if kill_active else "Kill switch inactive",
        )

        # Signal Validity & Maximum Signal Age (Operational Risk — Fail Closed on invalid/expired signal)
        clean_sym = instrument_symbol.strip().upper()
        if signal is not None:
            sig_sym_ok = signal.instrument_symbol.strip().upper() == clean_sym
            sig_px_ok = Decimal(str(signal.reference_price)) > Decimal("0")
            if not (sig_sym_ok and sig_px_ok):
                _add_check(
                    "INVALID_SIGNAL",
                    RiskRuleScope.OPERATIONAL,
                    False,
                    f"{signal.instrument_symbol}@{signal.reference_price}",
                    clean_sym,
                    "Signal symbol mismatch or non-positive reference price.",
                )
                return _reject(
                    "INVALID_SIGNAL",
                    f"{signal.instrument_symbol}@{signal.reference_price}",
                    clean_sym,
                    "Fail-closed block: invalid strategy signal payload.",
                )

            sig_emitted = ensure_ist(signal.emitted_at)
            sig_expires = ensure_ist(signal.expires_at)
            sig_age_sec = max(0.0, (eval_now - sig_emitted).total_seconds())
            max_sig_age = int(risk_cfg.max_signal_age_seconds)
            is_sig_expired = (
                signal.status == SignalStatus.EXPIRED.value
                or eval_now >= sig_expires
                or sig_age_sec > float(max_sig_age)
            )
            _add_check(
                "MAXIMUM_SIGNAL_AGE",
                RiskRuleScope.OPERATIONAL,
                not is_sig_expired,
                f"{sig_age_sec:.1f}s",
                f"{max_sig_age}s",
                f"Signal {signal.signal_code} age {sig_age_sec:.1f}s vs {max_sig_age}s limit.",
            )
            if is_sig_expired:
                if signal.status == SignalStatus.GENERATED.value:
                    signal.status = SignalStatus.EXPIRED.value
                return _reject(
                    "MAXIMUM_SIGNAL_AGE",
                    f"{sig_age_sec:.1f}s",
                    f"{max_sig_age}s",
                    f"Signal {signal.signal_code} expired (age {sig_age_sec:.1f}s exceeds {max_sig_age}s limit).",
                    code="SIGNAL_EXPIRED",
                )

        # Market Data Freshness Limit (Operational Risk — Fail Closed when STALE or STOPPED)
        if require_fresh_market_data and not reduce_only:
            stale_limit = int(risk_cfg.market_data_stale_threshold_seconds)
            age_dec = (
                freshness.market_data_age_seconds
                if (freshness is not None and freshness.market_data_age_seconds is not None)
                else None
            )
            fresh_ok = (
                freshness is not None
                and freshness.status == MarketDataFreshnessStatus.HEALTHY
                and freshness.new_orders_permitted
                and (age_dec is None or age_dec <= Decimal(stale_limit))
            )
            cur_fresh_str = (
                f"{freshness.status.value} ({age_dec:.1f}s)"
                if (freshness is not None and age_dec is not None)
                else (freshness.status.value if freshness is not None else "UNKNOWN")
            )
            lim_fresh_str = f"HEALTHY (<={stale_limit}s)"
            fresh_detail = (
                freshness.reason if freshness is not None else "No market-data freshness snapshot"
            )
            _add_check(
                "MARKET_DATA_FRESHNESS_LIMIT",
                RiskRuleScope.OPERATIONAL,
                fresh_ok,
                cur_fresh_str,
                lim_fresh_str,
                fresh_detail,
            )
            if not fresh_ok:
                return _reject(
                    "MARKET_DATA_FRESHNESS_LIMIT",
                    cur_fresh_str,
                    lim_fresh_str,
                    f"Fail-closed block: market data is stale/inactive ({cur_fresh_str}; max {stale_limit}s). {fresh_detail}",
                    code="STALE_MARKET_DATA",
                )
        else:
            _add_check(
                "MARKET_DATA_FRESHNESS_LIMIT",
                RiskRuleScope.OPERATIONAL,
                True,
                "BYPASSED_REDUCE_ONLY",
                f"<={risk_cfg.market_data_stale_threshold_seconds}s",
                "Bypassed for explicit reduce-only exit or offline test fixture.",
            )

        # System Health Gate & Position Reconciliation Gate (Opening Orders Only — Fail Closed)
        if not reduce_only:
            if risk_cfg.enforce_system_health_gate and risk_cfg.simulated_system_degraded:
                _add_check(
                    "SYSTEM_HEALTH_GATE",
                    RiskRuleScope.OPERATIONAL,
                    False,
                    "DEGRADED",
                    "HEALTHY",
                    "Critical subsystem marked DEGRADED.",
                )
                return _reject(
                    "SYSTEM_HEALTH_GATE",
                    "DEGRADED",
                    "HEALTHY",
                    "Fail-closed block: System Health Gate detected DEGRADED critical subsystem.",
                )
            _add_check(
                "SYSTEM_HEALTH_GATE",
                RiskRuleScope.OPERATIONAL,
                True,
                "HEALTHY",
                "HEALTHY",
                "Core subsystems healthy.",
            )

            if risk_cfg.enforce_position_reconciliation_gate:
                recon = PositionService.reconcile_positions_from_fills(
                    session, account_id=account.id, heal_if_needed=False, settings=cfg
                )
                recon_ok = recon.reconciled and recon.discrepancies_found == 0
                _add_check(
                    "POSITION_RECONCILIATION_GATE",
                    RiskRuleScope.OPERATIONAL,
                    recon_ok,
                    "RECONCILED"
                    if recon_ok
                    else f"UNRECONCILED ({recon.discrepancies_found} drift)",
                    "RECONCILED",
                    "; ".join(recon.details),
                )
                if not recon_ok:
                    return _reject(
                        "POSITION_RECONCILIATION_GATE",
                        f"UNRECONCILED ({recon.discrepancies_found} drift)",
                        "RECONCILED",
                        f"Fail-closed block: position book has {recon.discrepancies_found} unreconciled discrepancy(ies) vs execution fills.",
                    )

        # Instrument & Basic Order Parameter Sanity
        inst = session.scalar(
            select(InstrumentModel).where(InstrumentModel.symbol == clean_sym)
        )
        inst_ok = inst is not None and bool(inst.is_active)
        _add_check(
            "INSTRUMENT_ACTIVE",
            RiskRuleScope.OPERATIONAL,
            inst_ok,
            clean_sym if inst_ok else f"{clean_sym} (INACTIVE/UNKNOWN)",
            "ACTIVE_INSTRUMENT",
            f"{clean_sym} active={bool(inst and inst.is_active)}",
        )
        if not inst_ok:
            return _reject(
                "INSTRUMENT_ACTIVE",
                clean_sym,
                "ACTIVE_INSTRUMENT",
                f"Instrument '{clean_sym}' is unknown or inactive for simulated trading.",
                code="INSTRUMENT_INACTIVE_OR_UNKNOWN",
            )

        if quantity <= 0 or quantity > cfg.PAPER_MAX_ORDER_QUANTITY:
            _add_check(
                "HARD_QUANTITY_SANITY",
                RiskRuleScope.OPERATIONAL,
                False,
                str(quantity),
                str(cfg.PAPER_MAX_ORDER_QUANTITY),
                "Order quantity outside system bounds.",
            )
            return _reject(
                "HARD_QUANTITY_SANITY",
                str(quantity),
                str(cfg.PAPER_MAX_ORDER_QUANTITY),
                f"Order quantity {quantity} violates system bounds (1 .. {cfg.PAPER_MAX_ORDER_QUANTITY}).",
                code="MAX_ORDER_QUANTITY_EXCEEDED",
            )

        if order_type == OrderType.LIMIT and (limit_price is None or limit_price <= Decimal("0")):
            return _reject(
                "ORDER_PRICE_PARAMETERS",
                str(limit_price),
                "> 0",
                "LIMIT order requires limit_price > 0.",
                code="INVALID_LIMIT_PRICE",
            )
        if order_type == OrderType.STOP and (stop_price is None or stop_price <= Decimal("0")):
            return _reject(
                "ORDER_PRICE_PARAMETERS",
                str(stop_price),
                "> 0",
                "STOP order requires stop_price > 0.",
                code="INVALID_STOP_PRICE",
            )
        if val_price <= Decimal("0"):
            return _reject(
                "MISSING_REFERENCE_PRICE",
                str(val_price),
                "> 0",
                "Positive reference/market price is required for pre-trade risk valuation.",
            )

        # If this is an explicit reduce-only exit order, approve immediately after operational checks!
        if reduce_only:
            return RiskDecision(
                approved=True,
                decision=RiskDecisionOutcome.APPROVED,
                code="RISK_APPROVED",
                primary_rule="REDUCE_ONLY_EXIT_APPROVED",
                current_value=f"{quantity} {clean_sym}",
                limit_value="REDUCE_ONLY",
                reason="Explicit reduce-only position exit approved.",
                estimated_notional_inr=notional,
                estimated_margin_inr=est_margin,
                available_capital_inr=avail_cap,
                checks=checks,
            )

        # Determine effective market clock timestamp for session / daily / cooldown / holding rules
        eff_market_ts = (
            ensure_ist(signal.bar_timestamp)
            if signal is not None
            else (
                ensure_ist(created_bar_timestamp)
                if created_bar_timestamp is not None
                else (
                    ensure_ist(freshness.last_exchange_timestamp)
                    if (freshness is not None and freshness.last_exchange_timestamp is not None)
                    else None
                )
            )
        )
        trading_day_dt = (eff_market_ts or eval_now).astimezone(IST)
        trading_day_iso = trading_day_dt.date().isoformat()

        # =====================================================================
        # STAGE 2: STRATEGY-LEVEL INDEPENDENT RISK RULES
        # =====================================================================
        eff_strat_ver_id = strategy_version_id or (
            signal.strategy_version_id if signal is not None else None
        )
        strat_risk_row: StrategyRiskConfigModel | None = None
        if eff_strat_ver_id is not None:
            strat_risk_row = session.scalar(
                select(StrategyRiskConfigModel).where(
                    StrategyRiskConfigModel.account_id == account.id,
                    StrategyRiskConfigModel.strategy_version_id == eff_strat_ver_id,
                )
            )

        if strat_risk_row is not None and not bool(strat_risk_row.is_enabled):
            _add_check(
                "STRATEGY_RISK_ENABLED",
                RiskRuleScope.STRATEGY,
                False,
                "DISABLED",
                "ENABLED",
                f"Strategy {strat_risk_row.strategy_version_label} disabled in Risk Center.",
            )
            return _reject(
                "STRATEGY_RISK_ENABLED",
                "DISABLED",
                "ENABLED",
                f"Strategy '{strat_risk_row.strategy_version_label}' is disabled by Risk Center.",
            )

        s_allowed_insts = [
            s.upper()
            for s in (
                strat_risk_row.allowed_instruments_json
                if strat_risk_row is not None
                else risk_cfg.default_allowed_instruments_json
            )
        ]
        s_allowed_types = [
            t.upper()
            for t in (
                strat_risk_row.allowed_order_types_json
                if strat_risk_row is not None
                else risk_cfg.default_allowed_order_types_json
            )
        ]
        s_max_qty = int(
            strat_risk_row.max_quantity
            if strat_risk_row is not None
            else risk_cfg.default_max_quantity
        )
        s_max_risk_trade = Decimal(
            str(
                strat_risk_row.max_risk_per_trade_inr
                if strat_risk_row is not None
                else risk_cfg.default_max_risk_per_trade_inr
            )
        )
        s_daily_loss_lim = Decimal(
            str(
                strat_risk_row.strategy_daily_loss_inr
                if strat_risk_row is not None
                else risk_cfg.default_strategy_daily_loss_inr
            )
        )
        s_max_dd_lim = Decimal(
            str(
                strat_risk_row.max_strategy_drawdown_inr
                if strat_risk_row is not None
                else risk_cfg.default_max_strategy_drawdown_inr
            )
        )
        s_max_trades_day = int(
            strat_risk_row.max_trades_per_day
            if strat_risk_row is not None
            else risk_cfg.default_max_trades_per_strategy_day
        )
        s_cooldown_sec = int(
            strat_risk_row.cooldown_seconds_after_loss
            if strat_risk_row is not None
            else risk_cfg.default_cooldown_seconds_after_loss
        )
        s_max_hold_sec = int(
            strat_risk_row.max_holding_duration_seconds
            if strat_risk_row is not None
            else risk_cfg.default_max_holding_duration_seconds
        )

        # 2.1 Allowed Instruments
        inst_allowed_ok = clean_sym in s_allowed_insts
        _add_check(
            "ALLOWED_INSTRUMENTS",
            RiskRuleScope.STRATEGY,
            inst_allowed_ok,
            clean_sym,
            ",".join(s_allowed_insts),
            f"Instrument {clean_sym} vs allowed {s_allowed_insts}.",
        )
        if not inst_allowed_ok:
            return _reject(
                "ALLOWED_INSTRUMENTS",
                clean_sym,
                ",".join(s_allowed_insts),
                f"Instrument '{clean_sym}' is not in strategy allowed instruments {s_allowed_insts}.",
            )

        # 2.2 Allowed Order Types
        type_allowed_ok = order_type.value in s_allowed_types
        _add_check(
            "ALLOWED_ORDER_TYPES",
            RiskRuleScope.STRATEGY,
            type_allowed_ok,
            order_type.value,
            ",".join(s_allowed_types),
            f"Order type {order_type.value} vs allowed {s_allowed_types}.",
        )
        if not type_allowed_ok:
            return _reject(
                "ALLOWED_ORDER_TYPES",
                order_type.value,
                ",".join(s_allowed_types),
                f"Order type '{order_type.value}' is not permitted (allowed: {s_allowed_types}).",
            )

        # 2.3 Available Demo Margin check BEFORE soft quantity/value caps so extreme orders hit INSUFFICIENT_PAPER_CAPITAL cleanly
        margin_ok = est_margin <= avail_cap
        _add_check(
            "AVAILABLE_DEMO_MARGIN",
            RiskRuleScope.ACCOUNT,
            margin_ok,
            _fmt_inr_str(est_margin),
            _fmt_inr_str(avail_cap),
            f"Required margin {_fmt_inr_str(est_margin)} vs available {_fmt_inr_str(avail_cap)}.",
        )
        if not margin_ok:
            return _reject(
                "AVAILABLE_DEMO_MARGIN",
                _fmt_inr_str(est_margin),
                _fmt_inr_str(avail_cap),
                f"Estimated simulated margin {_fmt_inr_str(est_margin)} exceeds available paper capital {_fmt_inr_str(avail_cap)}.",
                code="INSUFFICIENT_PAPER_CAPITAL",
            )

        # 2.4 Strategy Maximum Quantity
        s_qty_ok = quantity <= s_max_qty
        _add_check(
            "STRATEGY_MAX_QUANTITY",
            RiskRuleScope.STRATEGY,
            s_qty_ok,
            str(quantity),
            str(s_max_qty),
            f"Proposed qty {quantity} vs strategy max {s_max_qty}.",
        )
        if not s_qty_ok:
            return _reject(
                "STRATEGY_MAX_QUANTITY",
                str(quantity),
                str(s_max_qty),
                f"Proposed order quantity {quantity} exceeds strategy maximum quantity {s_max_qty}.",
            )

        # 2.5 Strategy Maximum Risk Per Trade (when stop_loss_price is known/configured)
        if stop_loss_price is not None and stop_loss_price > Decimal("0"):
            trade_risk_inr = (abs(val_price - Decimal(str(stop_loss_price))) * Decimal(quantity)).quantize(_Q4)
            risk_trade_ok = trade_risk_inr <= s_max_risk_trade
            _add_check(
                "MAX_RISK_PER_TRADE",
                RiskRuleScope.STRATEGY,
                risk_trade_ok,
                _fmt_inr_str(trade_risk_inr),
                _fmt_inr_str(s_max_risk_trade),
                f"Stop risk {_fmt_inr_str(trade_risk_inr)} vs limit {_fmt_inr_str(s_max_risk_trade)}.",
            )
            if not risk_trade_ok:
                return _reject(
                    "MAX_RISK_PER_TRADE",
                    _fmt_inr_str(trade_risk_inr),
                    _fmt_inr_str(s_max_risk_trade),
                    f"Proposed per-trade stop risk {_fmt_inr_str(trade_risk_inr)} exceeds limit {_fmt_inr_str(s_max_risk_trade)}.",
                )
        else:
            _add_check(
                "MAX_RISK_PER_TRADE",
                RiskRuleScope.STRATEGY,
                True,
                "NO_STOP_ATTACHED",
                _fmt_inr_str(s_max_risk_trade),
                "No per-order stop_loss_price attached; covered by account daily loss & margin caps.",
            )

        # 2.6 Strategy Daily Loss, Max Strategy Drawdown, Cooldown After Loss, Max Holding Duration & Strategy Max Trades/Day
        if eff_strat_ver_id is not None:
            strat_positions = list(
                session.scalars(
                    select(PaperPositionModel).where(
                        PaperPositionModel.account_id == account.id,
                        PaperPositionModel.strategy_version_id == eff_strat_ver_id,
                    )
                ).all()
            )
            s_daily_loss, s_max_dd, last_loss_ts = (
                PreTradeRiskEvaluator._compute_strategy_drawdown_and_daily_loss(
                    strat_positions, trading_day_iso=trading_day_iso
                )
            )

            s_dl_ok = s_daily_loss < s_daily_loss_lim
            _add_check(
                "STRATEGY_DAILY_LOSS",
                RiskRuleScope.STRATEGY,
                s_dl_ok,
                _fmt_inr_str(s_daily_loss),
                _fmt_inr_str(s_daily_loss_lim),
                f"Strategy daily loss {_fmt_inr_str(s_daily_loss)} vs limit {_fmt_inr_str(s_daily_loss_lim)}.",
            )
            if not s_dl_ok:
                return _reject(
                    "STRATEGY_DAILY_LOSS",
                    _fmt_inr_str(s_daily_loss),
                    _fmt_inr_str(s_daily_loss_lim),
                    f"Strategy daily loss {_fmt_inr_str(s_daily_loss)} reached/exceeded limit {_fmt_inr_str(s_daily_loss_lim)}.",
                )

            s_dd_ok = s_max_dd < s_max_dd_lim
            _add_check(
                "MAX_STRATEGY_DRAWDOWN",
                RiskRuleScope.STRATEGY,
                s_dd_ok,
                _fmt_inr_str(s_max_dd),
                _fmt_inr_str(s_max_dd_lim),
                f"Strategy drawdown {_fmt_inr_str(s_max_dd)} vs limit {_fmt_inr_str(s_max_dd_lim)}.",
            )
            if not s_dd_ok:
                return _reject(
                    "MAX_STRATEGY_DRAWDOWN",
                    _fmt_inr_str(s_max_dd),
                    _fmt_inr_str(s_max_dd_lim),
                    f"Strategy drawdown {_fmt_inr_str(s_max_dd)} reached/exceeded limit {_fmt_inr_str(s_max_dd_lim)}.",
                )

            if s_cooldown_sec > 0 and last_loss_ts is not None:
                elapsed_since_loss = max(0.0, (trading_day_dt - last_loss_ts).total_seconds())
                cd_ok = elapsed_since_loss >= float(s_cooldown_sec)
                _add_check(
                    "COOLDOWN_AFTER_LOSS",
                    RiskRuleScope.STRATEGY,
                    cd_ok,
                    f"{int(elapsed_since_loss)}s elapsed",
                    f"{s_cooldown_sec}s required",
                    f"Elapsed since last strategy loss: {int(elapsed_since_loss)}s.",
                )
                if not cd_ok:
                    return _reject(
                        "COOLDOWN_AFTER_LOSS",
                        f"{int(elapsed_since_loss)}s elapsed",
                        f"{s_cooldown_sec}s required",
                        f"Strategy loss cooldown active ({int(elapsed_since_loss)}s < {s_cooldown_sec}s required after losing trade).",
                    )

            for op_pos in strat_positions:
                if op_pos.status == PositionStatus.OPEN.value:
                    held_sec = max(
                        0, int((trading_day_dt - ensure_ist(op_pos.opened_at)).total_seconds())
                    )
                    if held_sec > s_max_hold_sec:
                        _add_check(
                            "MAX_HOLDING_DURATION",
                            RiskRuleScope.STRATEGY,
                            False,
                            f"{held_sec}s",
                            f"{s_max_hold_sec}s",
                            f"Open position {op_pos.position_code} exceeded max holding duration.",
                        )
                        return _reject(
                            "MAX_HOLDING_DURATION",
                            f"{held_sec}s",
                            f"{s_max_hold_sec}s",
                            f"Strategy holds overdue open position {op_pos.position_code} ({held_sec}s > {s_max_hold_sec}s).",
                        )

            strat_orders = list(
                session.scalars(
                    select(PaperOrderModel).where(
                        PaperOrderModel.account_id == account.id,
                        PaperOrderModel.strategy_version_id == eff_strat_ver_id,
                        PaperOrderModel.reduce_only.is_(False),
                        PaperOrderModel.status.notin_(
                            [
                                OrderStatus.RISK_REJECTED.value,
                                OrderStatus.REJECTED.value,
                                OrderStatus.FAILED.value,
                            ]
                        ),
                    )
                ).all()
            )
            strat_trades_today = sum(
                1
                for o in strat_orders
                if ensure_ist(o.created_bar_timestamp or o.created_at).date().isoformat()
                == trading_day_iso
                and o.status != OrderStatus.CREATED.value
            )
            s_tc_ok = strat_trades_today < s_max_trades_day
            _add_check(
                "STRATEGY_MAX_TRADES_PER_DAY",
                RiskRuleScope.STRATEGY,
                s_tc_ok,
                f"{strat_trades_today} trades",
                f"{s_max_trades_day} max",
                f"Strategy trades on {trading_day_iso}: {strat_trades_today}/{s_max_trades_day}.",
            )
            if not s_tc_ok:
                return _reject(
                    "STRATEGY_MAX_TRADES_PER_DAY",
                    f"{strat_trades_today} trades",
                    f"{s_max_trades_day} max",
                    f"Strategy trade count ({strat_trades_today}) reached daily limit ({s_max_trades_day}) on {trading_day_iso}.",
                )

        # =====================================================================
        # STAGE 3: ACCOUNT-LEVEL INDEPENDENT RISK RULES
        # =====================================================================

        # 3.1 Trading Session Gate (IST)
        if risk_cfg.trading_session_enabled and eff_market_ts is not None:
            t_open = parse_hms_time(risk_cfg.session_start_ist)
            t_close = parse_hms_time(risk_cfg.session_end_ist)
            mkt_t = eff_market_ts.timetz().replace(tzinfo=None)
            sess_ok = (eff_market_ts.weekday() < 5) and (t_open <= mkt_t <= t_close)
            cur_t_str = eff_market_ts.strftime("%H:%M:%S IST")
            lim_t_str = f"{risk_cfg.session_start_ist}..{risk_cfg.session_end_ist} IST"
            _add_check(
                "TRADING_SESSION",
                RiskRuleScope.ACCOUNT,
                sess_ok,
                cur_t_str,
                lim_t_str,
                f"Market time {cur_t_str} vs session {lim_t_str}.",
            )
            if not sess_ok:
                return _reject(
                    "TRADING_SESSION",
                    cur_t_str,
                    lim_t_str,
                    f"Order timestamp {cur_t_str} is outside allowed IST trading session ({lim_t_str}).",
                )

        # 3.2 Account Maximum Daily Loss (Realized + Adverse Open Unrealized Loss)
        realized_today = Decimal(str(account.realized_pnl_today))
        unrealized_now = Decimal(str(account.unrealized_pnl))
        combined_pnl = realized_today + min(Decimal("0"), unrealized_now)
        acct_daily_loss = (
            abs(combined_pnl).quantize(_Q4)
            if combined_pnl < Decimal("0")
            else Decimal("0.0000")
        )
        max_dl_inr = Decimal(str(risk_cfg.max_daily_loss_inr))
        dl_ok = acct_daily_loss < max_dl_inr
        _add_check(
            "MAX_DAILY_LOSS",
            RiskRuleScope.ACCOUNT,
            dl_ok,
            _fmt_inr_str(acct_daily_loss),
            _fmt_inr_str(max_dl_inr),
            f"Account daily loss {_fmt_inr_str(acct_daily_loss)} vs limit {_fmt_inr_str(max_dl_inr)}.",
        )
        if not dl_ok:
            return _reject(
                "MAX_DAILY_LOSS",
                _fmt_inr_str(acct_daily_loss),
                _fmt_inr_str(max_dl_inr),
                f"Account daily loss {_fmt_inr_str(acct_daily_loss)} reached/exceeded Maximum Daily Loss {_fmt_inr_str(max_dl_inr)}.",
            )

        # 3.3 Account Maximum Consecutive Losses Pause
        consec_losses = PreTradeRiskEvaluator._count_consecutive_account_losses(
            session, account.id
        )
        max_consec = int(risk_cfg.max_consecutive_losses)
        consec_ok = consec_losses < max_consec
        _add_check(
            "MAX_CONSECUTIVE_LOSSES",
            RiskRuleScope.ACCOUNT,
            consec_ok,
            f"{consec_losses} losses",
            f"{max_consec} max",
            f"Trailing consecutive losing positions: {consec_losses}/{max_consec}.",
        )
        if not consec_ok:
            return _reject(
                "MAX_CONSECUTIVE_LOSSES",
                f"{consec_losses} losses",
                f"{max_consec} max",
                f"Consecutive loss circuit breaker tripped ({consec_losses} consecutive losses >= {max_consec}).",
            )

        # 3.4 Account Maximum Open Positions
        open_positions = list(
            session.scalars(
                select(PaperPositionModel).where(
                    PaperPositionModel.account_id == account.id,
                    PaperPositionModel.status == PositionStatus.OPEN.value,
                )
            ).all()
        )
        open_pos_cnt = len(open_positions)
        max_open_pos = int(risk_cfg.max_open_positions)
        pos_cap_ok = open_pos_cnt < max_open_pos
        _add_check(
            "MAX_OPEN_POSITIONS",
            RiskRuleScope.ACCOUNT,
            pos_cap_ok,
            f"{open_pos_cnt} open",
            f"{max_open_pos} max",
            f"Open positions {open_pos_cnt}/{max_open_pos}.",
        )
        if not pos_cap_ok:
            return _reject(
                "MAX_OPEN_POSITIONS",
                f"{open_pos_cnt} open",
                f"{max_open_pos} max",
                f"Account already holds {open_pos_cnt} open positions (Maximum Open Positions = {max_open_pos}).",
                code="MAX_OPEN_POSITIONS_REACHED",
            )

        # 3.5 Account Maximum Trades Per Day
        acct_orders = list(
            session.scalars(
                select(PaperOrderModel).where(
                    PaperOrderModel.account_id == account.id,
                    PaperOrderModel.reduce_only.is_(False),
                    PaperOrderModel.status.notin_(
                        [
                            OrderStatus.CREATED.value,
                            OrderStatus.RISK_REJECTED.value,
                            OrderStatus.REJECTED.value,
                            OrderStatus.FAILED.value,
                        ]
                    ),
                )
            ).all()
        )
        acct_trades_today = sum(
            1
            for o in acct_orders
            if ensure_ist(o.created_bar_timestamp or o.created_at).date().isoformat()
            == trading_day_iso
        )
        max_acct_trades = int(risk_cfg.max_trades_per_day)
        acct_tc_ok = acct_trades_today < max_acct_trades
        _add_check(
            "ACCOUNT_MAX_TRADES_PER_DAY",
            RiskRuleScope.ACCOUNT,
            acct_tc_ok,
            f"{acct_trades_today} trades",
            f"{max_acct_trades} max",
            f"Account trades on {trading_day_iso}: {acct_trades_today}/{max_acct_trades}.",
        )
        if not acct_tc_ok:
            return _reject(
                "ACCOUNT_MAX_TRADES_PER_DAY",
                f"{acct_trades_today} trades",
                f"{max_acct_trades} max",
                f"Account trade count ({acct_trades_today}) reached Maximum Trades Per Day ({max_acct_trades}) on {trading_day_iso}.",
            )

        # 3.6 Account Maximum Order Value
        max_ord_val = Decimal(str(risk_cfg.max_order_value_inr))
        ord_val_ok = notional <= max_ord_val
        _add_check(
            "MAX_ORDER_VALUE",
            RiskRuleScope.ACCOUNT,
            ord_val_ok,
            _fmt_inr_str(notional),
            _fmt_inr_str(max_ord_val),
            f"Proposed notional {_fmt_inr_str(notional)} vs limit {_fmt_inr_str(max_ord_val)}.",
        )
        if not ord_val_ok:
            return _reject(
                "MAX_ORDER_VALUE",
                _fmt_inr_str(notional),
                _fmt_inr_str(max_ord_val),
                f"Proposed order value {_fmt_inr_str(notional)} exceeds Maximum Order Value {_fmt_inr_str(max_ord_val)}.",
            )

        # 3.7 Account Maximum Gross Exposure
        cur_gross_exp = sum(
            (
                Decimal(str(p.last_price)) * Decimal(int(p.quantity))
                for p in open_positions
            ),
            Decimal("0.0000"),
        )
        projected_gross = (cur_gross_exp + notional).quantize(_Q4)
        max_gross_exp = Decimal(str(risk_cfg.max_gross_exposure_inr))
        gross_ok = projected_gross <= max_gross_exp
        _add_check(
            "MAX_GROSS_EXPOSURE",
            RiskRuleScope.ACCOUNT,
            gross_ok,
            _fmt_inr_str(projected_gross),
            _fmt_inr_str(max_gross_exp),
            f"Projected gross exposure {_fmt_inr_str(projected_gross)} vs limit {_fmt_inr_str(max_gross_exp)}.",
        )
        if not gross_ok:
            return _reject(
                "MAX_GROSS_EXPOSURE",
                _fmt_inr_str(projected_gross),
                _fmt_inr_str(max_gross_exp),
                f"Projected gross exposure {_fmt_inr_str(projected_gross)} exceeds Maximum Gross Exposure {_fmt_inr_str(max_gross_exp)}.",
            )

        # All Operational, Strategy-Level, and Account-Level Rules Passed!
        return RiskDecision(
            approved=True,
            decision=RiskDecisionOutcome.APPROVED,
            code="RISK_APPROVED",
            primary_rule="MAX_DAILY_LOSS",
            current_value=_fmt_inr_str(acct_daily_loss),
            limit_value=_fmt_inr_str(max_dl_inr),
            reason=(
                f"All {len(checks)} independent operational, strategy, and account pre-trade "
                f"risk gates passed (daily loss {_fmt_inr_str(acct_daily_loss)} / {_fmt_inr_str(max_dl_inr)})."
            ),
            estimated_notional_inr=notional,
            estimated_margin_inr=est_margin,
            available_capital_inr=avail_cap,
            checks=checks,
        )
