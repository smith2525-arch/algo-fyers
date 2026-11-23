"""Validated configuration model for EMA Crossover + RSI Confirmation strategy.

Design Note:
Default values represent neutral textbook engineering baselines for demonstration
only. Nothing in this module optimizes parameters or claims strategy profitability.
"""

from __future__ import annotations

from decimal import Decimal
from typing import Any

from pydantic import BaseModel, ConfigDict, Field, ValidationError, model_validator

from core.clock import parse_hms_time
from core.domain.enums import BarTimeframe, ExitTargetUnit, PositionSizingMode
from core.exceptions import StrategyValidationError

EMA_RSI_STRATEGY_CODE = "EMA_RSI_CROSSOVER"
EMA_RSI_SHORT_TAG = "EMA_RSI"


class EmaRsiStrategyConfig(BaseModel):
    """Complete immutable parameter specification for EMA Crossover + RSI Confirmation."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    # --- Strategy Scope ---
    instrument: str = Field(default="NIFTY", min_length=2, max_length=32)
    timeframe: BarTimeframe = Field(default=BarTimeframe.M5)

    # --- Signal Indicator Parameters ---
    fast_ema_period: int = Field(default=9, ge=2, le=200)
    slow_ema_period: int = Field(default=21, ge=3, le=500)
    rsi_period: int = Field(default=14, ge=2, le=100)
    long_rsi_threshold: Decimal = Field(default=Decimal("55.0"), gt=Decimal("0"), lt=Decimal("100"))
    short_rsi_threshold: Decimal = Field(default=Decimal("45.0"), gt=Decimal("0"), lt=Decimal("100"))

    # --- Trading Session & Activity Gates (IST, Asia/Kolkata) ---
    trading_start_time: str = Field(default="09:20:00", pattern=r"^\d{2}:\d{2}(:\d{2})?$")
    last_entry_time: str = Field(default="14:45:00", pattern=r"^\d{2}:\d{2}(:\d{2})?$")
    mandatory_square_off_time: str = Field(default="15:20:00", pattern=r"^\d{2}:\d{2}(:\d{2})?$")
    max_trades_per_day: int = Field(default=5, gt=0, le=100)

    # --- Configurable Exit & Risk Defaults ---
    stop_target_unit: ExitTargetUnit = Field(default=ExitTargetUnit.POINTS)
    stop_loss: Decimal = Field(default=Decimal("30.0"), gt=Decimal("0"))
    profit_target_enabled: bool = Field(default=True)
    profit_target: Decimal = Field(default=Decimal("70.0"), gt=Decimal("0"))
    trailing_stop_enabled: bool = Field(default=False)
    trailing_stop: Decimal = Field(default=Decimal("25.0"), gt=Decimal("0"))
    exit_on_opposite_signal: bool = Field(default=True)
    max_holding_enabled: bool = Field(default=True)
    max_holding_bars: int = Field(default=24, ge=1, le=500)
    eod_square_off_enabled: bool = Field(default=True)

    # --- Position Sizing ---
    position_sizing_mode: PositionSizingMode = Field(default=PositionSizingMode.FIXED_LOTS)
    position_size_value: Decimal = Field(default=Decimal("1"), gt=Decimal("0"))

    # --- Execution & Cost Assumptions ---
    slippage_bps: Decimal = Field(default=Decimal("2.0"), ge=Decimal("0"), le=Decimal("250"))
    estimated_charges_bps: Decimal = Field(default=Decimal("3.5"), ge=Decimal("0"), le=Decimal("250"))
    per_order_fee_inr: Decimal = Field(default=Decimal("20.0"), ge=Decimal("0"), le=Decimal("5000"))

    @model_validator(mode="after")
    def _validate_cross_field_rules(self) -> EmaRsiStrategyConfig:
        if self.fast_ema_period >= self.slow_ema_period:
            raise ValueError(
                f"Fast EMA Period ({self.fast_ema_period}) must be strictly less than "
                f"Slow EMA Period ({self.slow_ema_period})."
            )

        if self.short_rsi_threshold >= self.long_rsi_threshold:
            raise ValueError(
                f"Short RSI Threshold ({self.short_rsi_threshold}) must be strictly less than "
                f"Long RSI Threshold ({self.long_rsi_threshold})."
            )

        t_start = parse_hms_time(self.trading_start_time)
        t_last = parse_hms_time(self.last_entry_time)
        t_sqoff = parse_hms_time(self.mandatory_square_off_time)

        if not (t_start < t_last < t_sqoff):
            raise ValueError(
                "Session times must strictly satisfy Trading Start < Last Entry < Mandatory Square-Off "
                f"(got {self.trading_start_time}, {self.last_entry_time}, {self.mandatory_square_off_time} IST)."
            )

        if self.trailing_stop_enabled and self.trailing_stop <= Decimal("0"):
            raise ValueError("Trailing Stop distance must be > 0 when Trailing Stop is enabled.")

        if self.position_sizing_mode in (
            PositionSizingMode.FIXED_LOTS,
            PositionSizingMode.FIXED_QUANTITY,
        ):
            if self.position_size_value != self.position_size_value.to_integral_value():
                raise ValueError(
                    f"{self.position_sizing_mode.value} requires a whole-number position size >= 1 "
                    f"(got {self.position_size_value})."
                )
        elif self.position_sizing_mode == PositionSizingMode.RISK_PER_TRADE_PCT:
            if self.position_size_value > Decimal("10.0"):
                raise ValueError(
                    f"RISK_PER_TRADE_PCT cannot exceed 10.0% per trade (got {self.position_size_value}%)."
                )

        return self

    def normalized_hms(self, raw: str) -> str:
        t = parse_hms_time(raw)
        return t.strftime("%H:%M:%S")

    def to_canonical_dict(self) -> dict[str, Any]:
        """Return deterministic JSON-safe dictionary used for persistence and SHA-256 hashing."""
        return {
            "instrument": self.instrument.strip().upper(),
            "timeframe": self.timeframe.value,
            "fast_ema_period": self.fast_ema_period,
            "slow_ema_period": self.slow_ema_period,
            "rsi_period": self.rsi_period,
            "long_rsi_threshold": str(self.long_rsi_threshold.quantize(Decimal("0.01"))),
            "short_rsi_threshold": str(self.short_rsi_threshold.quantize(Decimal("0.01"))),
            "trading_start_time": self.normalized_hms(self.trading_start_time),
            "last_entry_time": self.normalized_hms(self.last_entry_time),
            "mandatory_square_off_time": self.normalized_hms(self.mandatory_square_off_time),
            "max_trades_per_day": self.max_trades_per_day,
            "stop_target_unit": self.stop_target_unit.value,
            "stop_loss": str(self.stop_loss.quantize(Decimal("0.01"))),
            "profit_target_enabled": self.profit_target_enabled,
            "profit_target": str(self.profit_target.quantize(Decimal("0.01"))),
            "trailing_stop_enabled": self.trailing_stop_enabled,
            "trailing_stop": str(self.trailing_stop.quantize(Decimal("0.01"))),
            "exit_on_opposite_signal": self.exit_on_opposite_signal,
            "max_holding_enabled": self.max_holding_enabled,
            "max_holding_bars": self.max_holding_bars,
            "eod_square_off_enabled": self.eod_square_off_enabled,
            "position_sizing_mode": self.position_sizing_mode.value,
            "position_size_value": str(self.position_size_value.normalize()),
            "slippage_bps": str(self.slippage_bps.quantize(Decimal("0.01"))),
            "estimated_charges_bps": str(self.estimated_charges_bps.quantize(Decimal("0.01"))),
            "per_order_fee_inr": str(self.per_order_fee_inr.quantize(Decimal("0.01"))),
        }


PARAMETER_METADATA: list[tuple[str, str, str]] = [
    ("instrument", "PARAMETERS", "Instrument"),
    ("timeframe", "PARAMETERS", "Bar Timeframe"),
    ("fast_ema_period", "PARAMETERS", "Fast EMA Period"),
    ("slow_ema_period", "PARAMETERS", "Slow EMA Period"),
    ("rsi_period", "PARAMETERS", "RSI Period"),
    ("long_rsi_threshold", "PARAMETERS", "Long RSI Threshold (>=)"),
    ("short_rsi_threshold", "PARAMETERS", "Short RSI Threshold (<=)"),
    ("trading_start_time", "PARAMETERS", "Trading Start Time (IST)"),
    ("last_entry_time", "PARAMETERS", "Last Entry Time (IST)"),
    ("mandatory_square_off_time", "PARAMETERS", "Mandatory Square-Off (IST)"),
    ("stop_target_unit", "RISK_DEFAULTS", "Stop / Target Unit"),
    ("stop_loss", "RISK_DEFAULTS", "Stop Loss"),
    ("profit_target_enabled", "RISK_DEFAULTS", "Profit Target Enabled"),
    ("profit_target", "RISK_DEFAULTS", "Profit Target"),
    ("trailing_stop_enabled", "RISK_DEFAULTS", "Trailing Stop Enabled"),
    ("trailing_stop", "RISK_DEFAULTS", "Trailing Stop Distance"),
    ("exit_on_opposite_signal", "RISK_DEFAULTS", "Opposite Signal Exit"),
    ("max_holding_enabled", "RISK_DEFAULTS", "Max Holding Period Enabled"),
    ("max_holding_bars", "RISK_DEFAULTS", "Max Holding Bars"),
    ("eod_square_off_enabled", "RISK_DEFAULTS", "Mandatory EOD Square-Off"),
    ("position_sizing_mode", "RISK_DEFAULTS", "Position Sizing Mode"),
    ("position_size_value", "RISK_DEFAULTS", "Position Size Value"),
    ("max_trades_per_day", "RISK_DEFAULTS", "Maximum Trades / Day"),
    ("slippage_bps", "EXECUTION_ASSUMPTIONS", "Slippage Assumption (bps)"),
    ("estimated_charges_bps", "EXECUTION_ASSUMPTIONS", "Estimated Charges (bps)"),
    ("per_order_fee_inr", "EXECUTION_ASSUMPTIONS", "Flat Fee / Order (INR)"),
]


def default_ema_rsi_parameters(instrument: str = "NIFTY") -> dict[str, Any]:
    """Return canonical baseline parameter dictionary for initial seeding or UI reset."""
    sl = Decimal("80.0") if instrument.strip().upper() == "BANKNIFTY" else Decimal("30.0")
    tp = Decimal("160.0") if instrument.strip().upper() == "BANKNIFTY" else Decimal("60.0")
    ts = Decimal("55.0") if instrument.strip().upper() == "BANKNIFTY" else Decimal("25.0")
    cfg = EmaRsiStrategyConfig(
        instrument=instrument.strip().upper(),
        stop_loss=sl,
        profit_target=tp,
        trailing_stop=ts,
    )
    return cfg.to_canonical_dict()


def default_ema_rsi_schema() -> dict[str, Any]:
    """Return structured JSON schema metadata stored on StrategyDefinition."""
    return EmaRsiStrategyConfig.model_json_schema()


def parse_and_validate_ema_rsi_config(
    raw_parameters: dict[str, Any],
    *,
    exchange_open_ist: str | None = None,
    exchange_close_ist: str | None = None,
) -> EmaRsiStrategyConfig:
    """Validate strategy parameters and optional exchange session bounds."""
    try:
        normalized = dict(raw_parameters)
        if "instrument" in normalized and isinstance(normalized["instrument"], str):
            normalized["instrument"] = normalized["instrument"].strip().upper()
        cfg = EmaRsiStrategyConfig(**normalized)
    except ValidationError as exc:
        messages = []
        for err in exc.errors():
            loc = ".".join(str(part) for part in err["loc"])
            msg = err["msg"]
            messages.append(f"{loc}: {msg}" if loc else msg)
        raise StrategyValidationError("; ".join(messages)) from exc

    if exchange_open_ist and exchange_close_ist:
        ex_open = parse_hms_time(exchange_open_ist)
        ex_close = parse_hms_time(exchange_close_ist)
        t_start = parse_hms_time(cfg.trading_start_time)
        t_sqoff = parse_hms_time(cfg.mandatory_square_off_time)
        if t_start < ex_open or t_sqoff > ex_close:
            raise StrategyValidationError(
                f"Configured session ({cfg.trading_start_time}..{cfg.mandatory_square_off_time} IST) "
                f"falls outside {cfg.instrument} reference exchange hours "
                f"({exchange_open_ist}..{exchange_close_ist} IST)."
            )

    return cfg
