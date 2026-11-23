"""Provider-independent HistoricalDataProvider contract and credential sanitization utilities."""

from __future__ import annotations

import re
from abc import ABC, abstractmethod
from collections.abc import Sequence
from dataclasses import dataclass, field
from datetime import date, datetime
from typing import Any

from core.market_data.models import (
    DataQualityStatus,
    DatasetQualityResult,
    NormalizedCandle,
)

# Regex patterns scrubbing JWTs, Bearer headers, AppID:Token pairs, and URL query tokens
_BEARER_RE = re.compile(r"(?i)bearer\s+[A-Za-z0-9\-._~+/]+=*")
_JWT_RE = re.compile(r"eyJ[A-Za-z0-9\-_]+\.[A-Za-z0-9\-_]+\.[A-Za-z0-9\-_]*")
_APP_TOKEN_RE = re.compile(r"[A-Za-z0-9\-]{6,32}:[A-Za-z0-9\-._~+/]{16,}")
_QUERY_SECRET_RE = re.compile(
    r"(?i)(access_token|auth_code|secret_key|client_secret|token|authorization)=([^&\s\"']+)"
)


def sanitize_provider_error_text(
    raw_message: str | None,
    *,
    extra_secrets: Sequence[str] = (),
) -> str:
    """Strip tokens, authorization headers, secret keys, and credential query params from text."""
    if not raw_message:
        return "Historical market-data provider error."
    cleaned = str(raw_message)
    for sec in extra_secrets:
        s = (sec or "").strip()
        if len(s) >= 4:
            cleaned = cleaned.replace(s, "[REDACTED]")
    cleaned = _BEARER_RE.sub("Bearer [REDACTED]", cleaned)
    cleaned = _JWT_RE.sub("[REDACTED_TOKEN]", cleaned)
    cleaned = _APP_TOKEN_RE.sub("[REDACTED_APP_TOKEN]", cleaned)
    cleaned = _QUERY_SECRET_RE.sub(r"\1=[REDACTED]", cleaned)
    return cleaned[:400]


@dataclass(frozen=True)
class HistoricalProviderFetchResult:
    """Normalized result returned by a HistoricalDataProvider."""

    provider: str
    source_type: str
    instrument: str
    provider_symbol: str | None
    timeframe: str
    candles: list[NormalizedCandle]
    quality: DatasetQualityResult
    retrieved_at: datetime
    chunks_total: int = 1
    chunks_succeeded: int = 1
    chunks_failed: int = 0
    chunk_summaries: list[dict[str, Any]] = field(default_factory=list)


class HistoricalDataProvider(ABC):
    """Source-agnostic interface implemented by FYERS, CSV, and Synthetic providers."""

    @property
    @abstractmethod
    def provider_name(self) -> str:
        """Canonical provider name (`FYERS` | `CSV` | `SYNTHETIC`)."""

    @property
    @abstractmethod
    def source_type(self) -> str:
        """Canonical dataset source_type (`FYERS_HISTORICAL` | `IMPORTED_CSV` | `BUNDLED_CSV`)."""

    @abstractmethod
    def provider_status(self) -> dict[str, Any]:
        """Return sanitized availability & configuration summary (ZERO secrets)."""


def build_failed_quality_result(
    *,
    status: DataQualityStatus,
    message: str,
    check_code: str,
) -> DatasetQualityResult:
    """Construct a structured failure DatasetQualityResult when download or auth fails."""
    return DatasetQualityResult(
        quality_status=status,
        passed_validation=False,
        has_warnings=False,
        is_complete=False,
        total_rows_inspected=0,
        valid_candle_count=0,
        null_value_count=0,
        timezone_issue_count=0,
        out_of_order_count=0,
        duplicate_timestamp_count=0,
        exact_duplicate_removed_count=0,
        conflicting_duplicate_count=0,
        invalid_ohlc_count=0,
        missing_candle_count=0,
        unexpected_session_gap_count=0,
        zero_volume_candle_count=0,
        partial_current_candle_excluded=False,
        missing_intervals=[],
        issues=[
            {
                "severity": "ERROR",
                "check_code": check_code,
                "message": sanitize_provider_error_text(message),
                "row_index": None,
                "timestamp": None,
            }
        ],
    )


@dataclass(frozen=True)
class DateRangeChunk:
    """Inclusive calendar-date chunk window for bounded historical provider requests."""

    chunk_index: int
    from_date: date
    to_date: date

    @property
    def day_span(self) -> int:
        return (self.to_date - self.from_date).days + 1
