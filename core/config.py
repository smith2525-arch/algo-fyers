"""Environment-driven application configuration with strict safety validation.

Secrets are NEVER embedded in source code. All runtime settings come from
environment variables (or a local untracked `.env` file during development).
"""

from __future__ import annotations

from decimal import Decimal
from functools import lru_cache

from pydantic import AliasChoices, Field, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

from core.exceptions import ConfigurationSafetyError


class AppSettings(BaseSettings):
    """Centralized settings shared by FastAPI and Core services."""

    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        case_sensitive=True,
        extra="ignore",
    )

    # Identity & Environment
    APP_NAME: str = "Algo Trading Control Center"
    APP_ENV: str = Field(default="development", description="development | staging | production | test")
    APP_VERSION: str = "0.1.0"
    LOG_LEVEL: str = "INFO"
    LOG_JSON: bool = True

    # Locked V1 Operational Modes
    EXECUTION_MODE: str = "PAPER_ONLY"
    EXECUTION_BROKER: str = "INTERNAL_PAPER_BROKER"
    MARKET_MODE: str = "HISTORICAL_AND_REPLAY"
    TIMEZONE: str = "Asia/Kolkata"

    # Optional FYERS Historical Market-Data Ingestion (READ-ONLY HISTORICAL DATA; NEVER EXECUTION)
    FYERS_HISTORICAL_DATA_ENABLED: bool = True
    FYERS_CLIENT_ID: str = Field(
        default="",
        validation_alias=AliasChoices("FYERS_CLIENT_ID", "FYERS_APP_ID"),
        description="FYERS API v3 App ID / Client ID (e.g. XXXXXXXX-100) for historical OHLCV queries only.",
    )
    FYERS_SECRET_KEY: str = Field(
        default="",
        validation_alias=AliasChoices("FYERS_SECRET_KEY", "FYERS_APP_SECRET"),
        description="Optional FYERS App Secret (never exposed via API/UI/logs; used only if generating OAuth tokens).",
    )
    FYERS_REDIRECT_URI: str = Field(
        default="",
        description="Optional FYERS OAuth Redirect URI configured on the FYERS developer app.",
    )
    FYERS_ACCESS_TOKEN: str = Field(
        default="",
        description="Optional pre-generated FYERS v3 access token (short-lived; historical OHLCV read access only).",
    )
    FYERS_USER_ID: str = Field(
        default="",
        validation_alias=AliasChoices("FYERS_USER_ID", "FYERS_ID", "FY_ID"),
        description="Optional FYERS User ID (fy_id) for backend headless TOTP token generation.",
    )
    FYERS_TOTP_SECRET: str = Field(
        default="",
        validation_alias=AliasChoices("FYERS_TOTP_SECRET", "FYERS_TOTP_KEY"),
        description="Optional Base32 TOTP 2FA secret key for backend headless token generation (never logged/exposed).",
    )
    FYERS_PIN: str = Field(
        default="",
        description="Optional FYERS account PIN for backend headless TOTP token generation (never logged/exposed).",
    )
    FYERS_VAGATOR_BASE_URL: str = Field(
        default="https://api-t2.fyers.in/vagator/v2",
        description="FYERS Vagator v2 authentication base URL used only for headless TOTP token handshake.",
    )
    FYERS_AUTH_V3_BASE_URL: str = Field(
        default="https://api-t1.fyers.in/api/v3",
        description="FYERS API v3 OAuth token/validate-authcode base URL used only for token handshake.",
    )
    FYERS_HISTORICAL_DATA_URL: str = Field(
        default="https://api-t1.fyers.in/data/history",
        description="Official FYERS v3 Historical Data REST endpoint (locked to /history path).",
    )
    FYERS_REQUEST_TIMEOUT_SECONDS: float = Field(default=20.0, ge=2.0, le=60.0)
    FYERS_MAX_RETRIES: int = Field(default=3, ge=0, le=5)
    FYERS_RETRY_BACKOFF_SECONDS: float = Field(default=0.5, ge=0.0, le=10.0)
    FYERS_MAX_DATE_RANGE_DAYS: int = Field(default=365, ge=1, le=1825)
    FYERS_DEFAULT_DATE_RANGE_DAYS: int = Field(default=90, ge=1, le=365)

    # Database Connectivity (Required via environment / Secret Manager — no hardcoded credentials)
    DATABASE_URL: str = Field(
        default="",
        description="PostgreSQL connection string (e.g. Neon postgresql+psycopg://...)",
    )
    DB_POOL_MODE: str = Field(
        default="QUEUE",
        description="QUEUE | NULLPOOL (use NULLPOOL on Cloud Run min-instances=0 with Neon -pooler)",
    )
    DB_SSLMODE: str = Field(
        default="prefer",
        description="disable | allow | prefer | require | verify-ca | verify-full (auto-require on *.neon.tech)",
    )
    DB_POOL_SIZE: int = Field(default=2, ge=1, le=50)
    DB_MAX_OVERFLOW: int = Field(default=3, ge=0, le=100)
    DB_POOL_RECYCLE_SECONDS: int = Field(default=180, ge=30)
    DB_CONNECT_TIMEOUT_SECONDS: int = Field(default=15, ge=2, le=60)
    AUTO_SEED_REFERENCE_DATA: bool = True

    # Initial Demonstration Reference Configuration (Seeded idempotently if DB empty)
    DEFAULT_OPERATOR_USERNAME: str = "desk.operator"
    DEFAULT_OPERATOR_EMAIL: str = "operator@demo-trading-desk.local"
    DEFAULT_OPERATOR_DISPLAY_NAME: str = "Primary Desk Operator"

    DEFAULT_PAPER_ACCOUNT_CODE: str = "PAPER-DESK-001"
    DEFAULT_PAPER_ACCOUNT_NAME: str = "Institutional Demo Paper Account"
    DEFAULT_DEMO_CAPITAL_INR: Decimal = Field(default=Decimal("500000.00"))

    # Configurable Instrument Reference Seed Data (Stored as editable DB rows)
    DEFAULT_EXCHANGE_CODE: str = "NSE"
    NIFTY_DEFAULT_LOT_SIZE: int = Field(default=75, ge=1)
    NIFTY_DEFAULT_TICK_SIZE: Decimal = Field(default=Decimal("0.05"), gt=Decimal("0"))
    BANKNIFTY_DEFAULT_LOT_SIZE: int = Field(default=30, ge=1)
    BANKNIFTY_DEFAULT_TICK_SIZE: Decimal = Field(default=Decimal("0.05"), gt=Decimal("0"))
    DEFAULT_SESSION_OPEN_IST: str = "09:15:00"
    DEFAULT_SESSION_CLOSE_IST: str = "15:30:00"

    # Market Replay & Freshness Guardrails (DEMO DEFAULTS — Configurable Paper-Risk Settings)
    MARKET_DATA_STALE_THRESHOLD_SECONDS: int = Field(default=10, ge=1, le=3600)
    SIGNAL_EXPIRY_SECONDS: int = Field(default=15, ge=1, le=3600)
    REPLAY_BASE_CANDLES_PER_SECOND: float = Field(default=1.0, gt=0.05, le=20.0)
    REPLAY_WORKER_ENABLED: bool = True

    # Paper Trading, Pre-Trade Risk & Simulated Paper Broker DEMO Defaults
    DEMO_MAX_DAILY_LOSS_INR: Decimal = Field(default=Decimal("10000.00"), gt=Decimal("0"))
    DEMO_MAX_RISK_PER_TRADE_INR: Decimal = Field(default=Decimal("2500.00"), gt=Decimal("0"))
    DEMO_MAX_TRADES_PER_STRATEGY_DAY: int = Field(default=5, ge=1, le=500)
    DEMO_CONSECUTIVE_LOSS_PAUSE: int = Field(default=3, ge=1, le=100)
    DEMO_MAX_GROSS_EXPOSURE_INR: Decimal = Field(default=Decimal("6000000.00"), gt=Decimal("0"))
    DEMO_MAX_ORDER_VALUE_INR: Decimal = Field(default=Decimal("3000000.00"), gt=Decimal("0"))
    DEMO_MAX_ACCOUNT_TRADES_PER_DAY: int = Field(default=15, ge=1, le=1000)
    DEMO_STRATEGY_DAILY_LOSS_INR: Decimal = Field(default=Decimal("6000.00"), gt=Decimal("0"))
    DEMO_MAX_STRATEGY_DRAWDOWN_INR: Decimal = Field(default=Decimal("8000.00"), gt=Decimal("0"))
    DEMO_COOLDOWN_SECONDS_AFTER_LOSS: int = Field(default=300, ge=0, le=86400)
    DEMO_MAX_HOLDING_DURATION_SECONDS: int = Field(default=14400, ge=60, le=864000)
    PAPER_FUTURES_MARGIN_RATE: Decimal = Field(default=Decimal("0.12"), gt=Decimal("0"), le=Decimal("1"))
    PAPER_DEFAULT_LATENCY_MS: int = Field(default=120, ge=0, le=30000)
    PAPER_DEFAULT_LATENCY_BARS: int = Field(default=1, ge=0, le=20)
    PAPER_DEFAULT_SLIPPAGE_BPS: Decimal = Field(default=Decimal("2.0"), ge=Decimal("0"), le=Decimal("250"))
    PAPER_DEFAULT_CHARGES_BPS: Decimal = Field(default=Decimal("1.5"), ge=Decimal("0"), le=Decimal("250"))
    PAPER_DEFAULT_PER_ORDER_FEE_INR: Decimal = Field(default=Decimal("20.0"), ge=Decimal("0"), le=Decimal("5000"))
    PAPER_MAX_ORDER_QUANTITY: int = Field(default=1800, ge=1, le=50000)
    PAPER_MAX_OPEN_POSITIONS: int = Field(default=4, ge=1, le=100)

    # FastAPI & UI Networking
    API_HOST: str = "0.0.0.0"
    API_PORT: int = 8000
    CORS_ALLOWED_ORIGINS: str = "http://localhost:8501,http://127.0.0.1:8501"
    API_BASE_URL: str = "http://localhost:8000"
    API_TIMEOUT_SECONDS: float = 25.0
    UI_PORT: int = 8501

    @field_validator("DB_POOL_MODE")
    @classmethod
    def _validate_db_pool_mode(cls, value: str) -> str:
        normalized = value.strip().upper()
        if normalized not in {"QUEUE", "NULLPOOL"}:
            raise ConfigurationSafetyError(
                f"DB_POOL_MODE must be 'QUEUE' or 'NULLPOOL', got {value!r}."
            )
        return normalized

    @field_validator("EXECUTION_MODE")
    @classmethod
    def _enforce_paper_only_in_v1(cls, value: str) -> str:
        normalized = value.strip().upper()
        if normalized != "PAPER_ONLY":
            raise ConfigurationSafetyError(
                f"Real-money execution is prohibited in V1. EXECUTION_MODE must be 'PAPER_ONLY', got {value!r}."
            )
        return normalized

    @field_validator("EXECUTION_BROKER")
    @classmethod
    def _enforce_internal_paper_broker_only(cls, value: str) -> str:
        normalized = value.strip().upper()
        if normalized != "INTERNAL_PAPER_BROKER":
            raise ConfigurationSafetyError(
                f"External execution brokers are strictly prohibited. EXECUTION_BROKER must be "
                f"'INTERNAL_PAPER_BROKER' (FYERS is restricted to historical market data only), got {value!r}."
            )
        return normalized

    @field_validator("FYERS_HISTORICAL_DATA_URL")
    @classmethod
    def _enforce_fyers_history_only_endpoint(cls, value: str) -> str:
        clean = value.strip()
        lower = clean.lower()
        forbidden_tokens = (
            "/orders",
            "/positions",
            "/holdings",
            "/funds",
            "/tradebook",
            "orderbook",
            "ws",
            "socket",
        )
        if any(tok in lower for tok in forbidden_tokens) or not lower.rstrip("/").endswith("/history"):
            raise ConfigurationSafetyError(
                "FYERS_HISTORICAL_DATA_URL must point exclusively to a historical '/history' endpoint; "
                "order, position, funds, and WebSocket endpoints are strictly prohibited."
            )
        return clean.rstrip("/")

    @field_validator("TIMEZONE")
    @classmethod
    def _enforce_ist_timezone(cls, value: str) -> str:
        normalized = value.strip()
        if normalized != "Asia/Kolkata":
            raise ConfigurationSafetyError(
                f"System timezone is locked to 'Asia/Kolkata' (IST), got {value!r}."
            )
        return normalized

    @property
    def cors_origins_list(self) -> list[str]:
        return [origin.strip() for origin in self.CORS_ALLOWED_ORIGINS.split(",") if origin.strip()]

    @property
    def masked_database_url(self) -> str:
        """Return database URL with password masked for safe display in Settings/Health."""
        raw = (self.DATABASE_URL or "").strip()
        if not raw:
            return "<NOT_CONFIGURED>"
        if "@" not in raw or "://" not in raw:
            return "<CONFIGURED_NON_STANDARD_DSN>"
        scheme_and_auth, host_part = raw.split("@", 1)
        scheme, _, auth = scheme_and_auth.partition("://")
        user, has_colon, _ = auth.partition(":")
        if has_colon:
            return f"{scheme}://{user}:***@{host_part}"
        return f"{scheme}://{auth}@{host_part}"

    @staticmethod
    def _is_populated_non_placeholder(raw: str | None) -> bool:
        val = (raw or "").strip()
        return bool(val and not val.startswith("<") and not val.endswith(">"))

    @property
    def fyers_totp_configured(self) -> bool:
        """Return True when all 6 headless TOTP fields are populated (never exposes values)."""
        return all(
            self._is_populated_non_placeholder(v)
            for v in (
                self.FYERS_USER_ID,
                self.FYERS_TOTP_SECRET,
                self.FYERS_PIN,
                self.FYERS_CLIENT_ID,
                self.FYERS_SECRET_KEY,
                self.FYERS_REDIRECT_URI,
            )
        )

    @property
    def fyers_static_token_configured(self) -> bool:
        """Return True when a static FYERS_ACCESS_TOKEN (+ FYERS_CLIENT_ID) is populated."""
        tok = (self.FYERS_ACCESS_TOKEN or "").strip()
        cid = (self.FYERS_CLIENT_ID or "").strip()
        if not self._is_populated_non_placeholder(tok):
            return False
        if ":" in tok:
            left, _, right = tok.partition(":")
            return bool(left.strip() and right.strip())
        return self._is_populated_non_placeholder(cid)

    @property
    def fyers_auth_mode(self) -> str:
        """Return sanitized authentication mode indicator (`HEADLESS_TOTP` | `STATIC_ACCESS_TOKEN` | `UNCONFIGURED`)."""
        if self.fyers_totp_configured:
            return "HEADLESS_TOTP"
        if self.fyers_static_token_configured:
            return "STATIC_ACCESS_TOKEN"
        return "UNCONFIGURED"

    @property
    def fyers_env_presence(self) -> dict[str, bool]:
        """Boolean-only checklist of which FYERS env vars are populated on backend (NEVER values)."""
        return {
            "FYERS_CLIENT_ID": self._is_populated_non_placeholder(self.FYERS_CLIENT_ID),
            "FYERS_SECRET_KEY": self._is_populated_non_placeholder(self.FYERS_SECRET_KEY),
            "FYERS_REDIRECT_URI": self._is_populated_non_placeholder(self.FYERS_REDIRECT_URI),
            "FYERS_USER_ID": self._is_populated_non_placeholder(self.FYERS_USER_ID),
            "FYERS_TOTP_SECRET": self._is_populated_non_placeholder(self.FYERS_TOTP_SECRET),
            "FYERS_PIN": self._is_populated_non_placeholder(self.FYERS_PIN),
            "FYERS_ACCESS_TOKEN": self._is_populated_non_placeholder(self.FYERS_ACCESS_TOKEN),
        }

    @property
    def fyers_missing_totp_vars(self) -> list[str]:
        """Return names of any missing required env vars for 6-variable Headless TOTP mode."""
        pres = self.fyers_env_presence
        required = (
            "FYERS_CLIENT_ID",
            "FYERS_SECRET_KEY",
            "FYERS_REDIRECT_URI",
            "FYERS_USER_ID",
            "FYERS_TOTP_SECRET",
            "FYERS_PIN",
        )
        return [k for k in required if not pres.get(k, False)]

    @property
    def fyers_token_configured(self) -> bool:
        """Return True when either headless TOTP or a static access token is configured."""
        return self.fyers_totp_configured or self.fyers_static_token_configured

    @property
    def public_database_display(self) -> str:
        """Return production-safe database indicator hiding host, username, and password in prod."""
        raw = (self.DATABASE_URL or "").strip()
        if not raw:
            return "<NOT_CONFIGURED>"
        if self.APP_ENV.strip().lower() == "production":
            scheme = raw.partition("://")[0] if "://" in raw else "postgresql+psycopg"
            return f"{scheme}://***:***@<redacted-host>/<redacted-db>"
        return self.masked_database_url


@lru_cache(maxsize=1)
def get_settings() -> AppSettings:
    """Return cached application settings instance."""
    return AppSettings()


def clear_settings_cache() -> None:
    """Clear settings cache (used in tests when overriding env vars)."""
    get_settings.cache_clear()

