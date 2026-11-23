"""Idempotent foundation reference data seeder.

Seeds:
1. Primary demonstration operator user
2. Demonstration Paper Account (₹500,000 clearly marked DEMO_PAPER_ONLY)
3. Configurable NIFTY & BANKNIFTY reference instruments
4. Initial StrategyDefinition (EMA Crossover + RSI Confirmation) + immutable v1
5. Audit event recording reference initialization
"""

from __future__ import annotations

import hashlib
import json
import logging
from decimal import Decimal

from sqlalchemy import select
from sqlalchemy.orm import Session

from core.clock import now_ist
from core.config import AppSettings, get_settings
from core.domain.enums import (
    AccountDesignation,
    ActorType,
    AuditCategory,
    AuditSeverity,
    PaperAccountStatus,
    StrategyVersionStatus,
    UserRole,
)
from core.logging import get_correlation_id
from core.persistence.models import (
    AccountRiskConfigModel,
    AuditEventModel,
    InstrumentModel,
    PaperAccountModel,
    StrategyDefinitionModel,
    StrategyRiskConfigModel,
    StrategyVersionModel,
    UserModel,
)

logger = logging.getLogger(__name__)


def compute_parameter_hash(parameters: dict[str, object]) -> str:
    """Compute canonical SHA-256 fingerprint of a strategy parameter dictionary."""
    canonical = json.dumps(parameters, sort_keys=True, separators=(",", ":"), default=str)
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def seed_foundation_data(session: Session, settings: AppSettings | None = None) -> dict[str, bool]:
    """Idempotently seed initial operator, DEMO paper account, NIFTY/BANKNIFTY, and strategy definition."""
    cfg = settings or get_settings()
    created: dict[str, bool] = {
        "operator_user": False,
        "paper_account": False,
        "nifty_instrument": False,
        "banknifty_instrument": False,
        "ema_rsi_strategy": False,
    }

    # 1. Operator user
    operator = session.scalar(
        select(UserModel).where(UserModel.username == cfg.DEFAULT_OPERATOR_USERNAME)
    )
    if operator is None:
        operator = UserModel(
            username=cfg.DEFAULT_OPERATOR_USERNAME,
            email=cfg.DEFAULT_OPERATOR_EMAIL,
            display_name=cfg.DEFAULT_OPERATOR_DISPLAY_NAME,
            role=UserRole.OPERATOR.value,
            is_active=True,
        )
        session.add(operator)
        session.flush()
        created["operator_user"] = True

    # 2. Demonstration Paper Account (₹500,000 clearly designated DEMO_PAPER_ONLY)
    account = session.scalar(
        select(PaperAccountModel).where(
            PaperAccountModel.account_code == cfg.DEFAULT_PAPER_ACCOUNT_CODE
        )
    )
    if account is None:
        demo_cap = Decimal(str(cfg.DEFAULT_DEMO_CAPITAL_INR)).quantize(Decimal("0.0001"))
        account = PaperAccountModel(
            account_code=cfg.DEFAULT_PAPER_ACCOUNT_CODE,
            account_name=cfg.DEFAULT_PAPER_ACCOUNT_NAME,
            owner_user_id=operator.id,
            is_demo_capital=True,
            account_designation=AccountDesignation.DEMO_PAPER_ONLY.value,
            currency="INR",
            initial_capital=demo_cap,
            available_capital=demo_cap,
            realized_pnl_today=Decimal("0.0000"),
            unrealized_pnl=Decimal("0.0000"),
            paper_trading_halted=False,
            status=PaperAccountStatus.ACTIVE.value,
            version=1,
        )
        session.add(account)
        session.flush()
        created["paper_account"] = True

    # 3. Configurable Reference Instruments (NIFTY, BANKNIFTY) + Authoritative FYERS Historical Symbols
    from core.market_data.models import INSTRUMENT_PROVIDER_REGISTRY

    instruments_seed = [
        (
            "nifty_instrument",
            "NIFTY",
            INSTRUMENT_PROVIDER_REGISTRY["NIFTY"].display_name,
            cfg.NIFTY_DEFAULT_LOT_SIZE,
            cfg.NIFTY_DEFAULT_TICK_SIZE,
            INSTRUMENT_PROVIDER_REGISTRY["NIFTY"].data_provider,
            INSTRUMENT_PROVIDER_REGISTRY["NIFTY"].provider_symbol,
            {
                "index_name": "NIFTY 50",
                "reference_note": "Configurable paper contract spec; FYERS used for historical data only",
            },
        ),
        (
            "banknifty_instrument",
            "BANKNIFTY",
            INSTRUMENT_PROVIDER_REGISTRY["BANKNIFTY"].display_name,
            cfg.BANKNIFTY_DEFAULT_LOT_SIZE,
            cfg.BANKNIFTY_DEFAULT_TICK_SIZE,
            INSTRUMENT_PROVIDER_REGISTRY["BANKNIFTY"].data_provider,
            INSTRUMENT_PROVIDER_REGISTRY["BANKNIFTY"].provider_symbol,
            {
                "index_name": "NIFTY BANK",
                "reference_note": "Configurable paper contract spec; FYERS used for historical data only",
            },
        ),
    ]
    for (
        flag_key,
        symbol,
        display_name,
        lot_size,
        tick_size,
        data_provider,
        provider_symbol,
        meta,
    ) in instruments_seed:
        existing_inst = session.scalar(
            select(InstrumentModel).where(InstrumentModel.symbol == symbol)
        )
        if existing_inst is None:
            session.add(
                InstrumentModel(
                    symbol=symbol,
                    display_name=display_name,
                    exchange=cfg.DEFAULT_EXCHANGE_CODE,
                    segment="INDEX_DERIVATIVES_PAPER",
                    instrument_type="PAPER_INDEX_CONTRACT",
                    data_provider=data_provider,
                    provider_symbol=provider_symbol,
                    currency="INR",
                    lot_size=lot_size,
                    tick_size=Decimal(str(tick_size)).quantize(Decimal("0.0001")),
                    session_open_ist=cfg.DEFAULT_SESSION_OPEN_IST,
                    session_close_ist=cfg.DEFAULT_SESSION_CLOSE_IST,
                    metadata_json=meta,
                    is_active=True,
                )
            )
            created[flag_key] = True
        elif not (existing_inst.provider_symbol or "").strip():
            existing_inst.data_provider = data_provider
            existing_inst.provider_symbol = provider_symbol

    # 4. Initial Strategy Definition: EMA Crossover + RSI Confirmation + Baseline Immutable v1
    from core.strategies.config_schema import (
        EMA_RSI_STRATEGY_CODE,
        default_ema_rsi_parameters,
        default_ema_rsi_schema,
    )

    ema_rsi_code = EMA_RSI_STRATEGY_CODE
    strat_def = session.scalar(
        select(StrategyDefinitionModel).where(StrategyDefinitionModel.code == ema_rsi_code)
    )
    if strat_def is None:
        nifty_params = default_ema_rsi_parameters("NIFTY")
        banknifty_params = default_ema_rsi_parameters("BANKNIFTY")
        param_schema = default_ema_rsi_schema()
        strat_def = StrategyDefinitionModel(
            code=ema_rsi_code,
            name="EMA Crossover + RSI Confirmation",
            category="TREND_MOMENTUM",
            description=(
                "Generates directional candidate signals on closed-bar Fast/Slow EMA crossovers "
                "confirmed by Wilder RSI thresholds inside configured IST session bounds, with "
                "configurable Stop Loss, Profit Target, Trailing Stop, Opposite Signal, Max Holding, "
                "and Mandatory EOD Square-Off exits. Emits Signals only; never directly creates fills."
            ),
            parameter_schema_json=param_schema,
            default_parameters_json=nifty_params,
            supported_symbols_json=["NIFTY", "BANKNIFTY"],
            is_enabled=True,
        )
        session.add(strat_def)
        session.flush()

        for symbol, params_dict in (("NIFTY", nifty_params), ("BANKNIFTY", banknifty_params)):
            session.add(
                StrategyVersionModel(
                    strategy_definition_id=strat_def.id,
                    version_number=1,
                    version_label=f"{symbol}_EMA_RSI_v1",
                    instrument_symbol=symbol,
                    timeframe=str(params_dict["timeframe"]),
                    cloned_from_version_id=None,
                    parameters_json=params_dict,
                    parameter_hash=compute_parameter_hash(params_dict),
                    notes=f"Neutral engineering baseline seeded for {symbol} (non-optimized demo defaults).",
                    created_by="system.seed",
                    status=StrategyVersionStatus.VALIDATED.value,
                )
            )
        created["ema_rsi_strategy"] = True
        session.add(
            AuditEventModel(
                occurred_at=now_ist(),
                correlation_id=get_correlation_id(),
                actor="system.seed",
                actor_type=ActorType.SYSTEM.value,
                category=AuditCategory.STRATEGY.value,
                action="STRATEGY_CREATED",
                event_type="STRATEGY_CREATED",
                severity=AuditSeverity.INFO.value,
                entity_type="strategy_definition",
                entity_id=ema_rsi_code,
                strategy_id=ema_rsi_code,
                source="system.seed",
                summary=f"Registered configurable strategy definition '{ema_rsi_code}' (EMA Crossover + RSI Confirmation).",
                details_json={"code": ema_rsi_code, "supported_symbols": ["NIFTY", "BANKNIFTY"]},
            )
        )

    session.flush()

    # 5. Seed Phase 4 DEMO Account & Strategy Risk Configurations (DEMONSTRATION SETTINGS ONLY)
    acct_risk = session.scalar(
        select(AccountRiskConfigModel).where(AccountRiskConfigModel.account_id == account.id)
    )
    if acct_risk is None:
        acct_risk = AccountRiskConfigModel(
            account_id=account.id,
            is_demo_settings=True,
            max_daily_loss_inr=Decimal(str(cfg.DEMO_MAX_DAILY_LOSS_INR)).quantize(
                Decimal("0.0001")
            ),
            max_open_positions=int(cfg.PAPER_MAX_OPEN_POSITIONS),
            max_gross_exposure_inr=Decimal(str(cfg.DEMO_MAX_GROSS_EXPOSURE_INR)).quantize(
                Decimal("0.0001")
            ),
            max_order_value_inr=Decimal(str(cfg.DEMO_MAX_ORDER_VALUE_INR)).quantize(
                Decimal("0.0001")
            ),
            max_trades_per_day=int(cfg.DEMO_MAX_ACCOUNT_TRADES_PER_DAY),
            max_consecutive_losses=int(cfg.DEMO_CONSECUTIVE_LOSS_PAUSE),
            trading_session_enabled=True,
            session_start_ist=cfg.DEFAULT_SESSION_OPEN_IST,
            session_end_ist=cfg.DEFAULT_SESSION_CLOSE_IST,
            global_kill_switch_active=bool(account.paper_trading_halted),
            kill_switch_reason=account.halt_reason,
            kill_switch_activated_at=account.halted_at,
            kill_switch_activated_by=account.halted_by,
            market_data_stale_threshold_seconds=int(cfg.MARKET_DATA_STALE_THRESHOLD_SECONDS),
            max_signal_age_seconds=int(cfg.SIGNAL_EXPIRY_SECONDS),
            enforce_system_health_gate=True,
            simulated_system_degraded=False,
            enforce_position_reconciliation_gate=True,
            default_max_quantity=300,
            default_max_risk_per_trade_inr=Decimal(str(cfg.DEMO_MAX_RISK_PER_TRADE_INR)).quantize(
                Decimal("0.0001")
            ),
            default_strategy_daily_loss_inr=Decimal(
                str(cfg.DEMO_STRATEGY_DAILY_LOSS_INR)
            ).quantize(Decimal("0.0001")),
            default_max_strategy_drawdown_inr=Decimal(
                str(cfg.DEMO_MAX_STRATEGY_DRAWDOWN_INR)
            ).quantize(Decimal("0.0001")),
            default_max_trades_per_strategy_day=int(cfg.DEMO_MAX_TRADES_PER_STRATEGY_DAY),
            default_cooldown_seconds_after_loss=int(cfg.DEMO_COOLDOWN_SECONDS_AFTER_LOSS),
            default_max_holding_duration_seconds=int(cfg.DEMO_MAX_HOLDING_DURATION_SECONDS),
            default_allowed_instruments_json=["NIFTY", "BANKNIFTY"],
            default_allowed_order_types_json=["MARKET", "LIMIT", "STOP"],
            updated_by="system.seed",
        )
        session.add(acct_risk)
        created["demo_risk_config"] = True

    all_versions = list(session.scalars(select(StrategyVersionModel)).all())
    for sv in all_versions:
        existing_sr = session.scalar(
            select(StrategyRiskConfigModel).where(
                StrategyRiskConfigModel.account_id == account.id,
                StrategyRiskConfigModel.strategy_version_id == sv.id,
            )
        )
        if existing_sr is None:
            session.add(
                StrategyRiskConfigModel(
                    account_id=account.id,
                    strategy_version_id=sv.id,
                    strategy_version_label=sv.version_label,
                    max_quantity=300,
                    max_risk_per_trade_inr=Decimal(
                        str(cfg.DEMO_MAX_RISK_PER_TRADE_INR)
                    ).quantize(Decimal("0.0001")),
                    strategy_daily_loss_inr=Decimal(
                        str(cfg.DEMO_STRATEGY_DAILY_LOSS_INR)
                    ).quantize(Decimal("0.0001")),
                    max_strategy_drawdown_inr=Decimal(
                        str(cfg.DEMO_MAX_STRATEGY_DRAWDOWN_INR)
                    ).quantize(Decimal("0.0001")),
                    max_trades_per_day=int(cfg.DEMO_MAX_TRADES_PER_STRATEGY_DAY),
                    cooldown_seconds_after_loss=int(cfg.DEMO_COOLDOWN_SECONDS_AFTER_LOSS),
                    max_holding_duration_seconds=int(cfg.DEMO_MAX_HOLDING_DURATION_SECONDS),
                    allowed_instruments_json=[sv.instrument_symbol],
                    allowed_order_types_json=["MARKET", "LIMIT", "STOP"],
                    is_enabled=True,
                    updated_by="system.seed",
                )
            )
            created["demo_strategy_risk_config"] = True

    session.flush()

    if any(created.values()):
        session.add(
            AuditEventModel(
                occurred_at=now_ist(),
                correlation_id=get_correlation_id(),
                actor="system.seed",
                actor_type=ActorType.SYSTEM.value,
                category=AuditCategory.SYSTEM.value,
                action="FOUNDATION_REFERENCE_DATA_SEEDED",
                severity=AuditSeverity.INFO.value,
                entity_type="paper_accounts",
                entity_id=account.account_code,
                summary="Initialized demonstration paper account (INR 500,000 DEMO), configurable instruments, and EMA+RSI v1.",
                details_json={
                    "created_flags": created,
                    "demo_capital_inr": str(account.initial_capital),
                    "designation": account.account_designation,
                },
            )
        )
        session.flush()
        logger.info("Foundation reference data seeded", extra={"seeded_entities": created})

    return created
