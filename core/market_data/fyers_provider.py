"""FYERS Historical Market Data Provider (STRICTLY HISTORICAL OHLCV READS ONLY).

SECURITY & ARCHITECTURAL INVARIANT:
- This module communicates ONLY with the official FYERS API v3 Historical Data REST
  endpoint (`GET /data/history`) using `httpx`.
- It intentionally avoids installing or importing any broker execution SDK so that
  order placement, order modification, order cancellation, position/fund/orderbook
  retrieval, and live WebSockets are physically absent from the runtime.
- Credentials (`FYERS_CLIENT_ID`, `FYERS_ACCESS_TOKEN`, `FYERS_SECRET_KEY`) are NEVER
  logged, returned in exceptions, persisted in database rows, or sent to the UI.
"""

from __future__ import annotations

import json
import time
from collections.abc import Callable
from datetime import date, datetime, timedelta
from decimal import Decimal
from typing import Any

import httpx

from core.clock import ensure_ist, now_ist
from core.config import AppSettings, get_settings
from core.exceptions import (
    ConfigurationSafetyError,
    FyersAuthenticationRequiredError,
    HistoricalDataError,
    HistoricalProviderUnavailableError,
)
from core.market_data.fyers_totp_auth import (
    FyersTotpAuthenticator,
    clear_fyers_totp_token_cache,
)
from core.market_data.models import (
    DataSourceType,
    HistoricalProviderName,
    get_instrument_provider_spec,
    get_timeframe_spec,
)
from core.market_data.normalization import fyers_tuples_to_candidates
from core.market_data.provider import (
    DateRangeChunk,
    HistoricalDataProvider,
    HistoricalProviderFetchResult,
    sanitize_provider_error_text,
)
from core.market_data.validation import validate_and_normalize_candles

FYERS_AUTH_RENEWAL_UI_MESSAGE = (
    "FYERS historical-data authentication requires renewal. "
    "Existing stored and uploaded datasets remain available."
)

# Official FYERS v3 error codes indicating expired/invalid/unauthorized access tokens
_FYERS_AUTH_ERROR_CODES: frozenset[int] = frozenset(
    {-8, -15, -16, -17, -99, -401, -1601, -1602, 401}
)
_FYERS_PERMISSION_ERROR_CODES: frozenset[int] = frozenset({-403, 403})
_FYERS_RATE_LIMIT_CODES: frozenset[int] = frozenset({-429, 429})
_FYERS_INVALID_SYMBOL_CODES: frozenset[int] = frozenset({-300, -301})
_TRANSIENT_HTTP_STATUSES: frozenset[int] = frozenset({429, 500, 502, 503, 504})


def plan_fyers_date_chunks(
    *,
    from_date: date,
    to_date: date,
    timeframe: str,
    max_total_days: int = 365,
) -> list[DateRangeChunk]:
    """Split `[from_date, to_date]` into chronological FYERS-compliant request windows."""
    if from_date > to_date:
        raise HistoricalDataError(
            f"Invalid date range: from_date ({from_date}) cannot be after to_date ({to_date}).",
            code="INVALID_DATE_RANGE",
        )
    total_days = (to_date - from_date).days + 1
    if total_days > max_total_days:
        raise HistoricalDataError(
            f"Requested interval of {total_days} calendar days exceeds configured maximum "
            f"({max_total_days} calendar days).",
            code="DATE_RANGE_LIMIT_EXCEEDED",
        )

    spec = get_timeframe_spec(timeframe)
    chunk_span = max(1, spec.max_days_per_fyers_chunk)

    chunks: list[DateRangeChunk] = []
    cursor = from_date
    idx = 1
    while cursor <= to_date:
        chunk_end = min(to_date, cursor + timedelta(days=chunk_span - 1))
        chunks.append(DateRangeChunk(chunk_index=idx, from_date=cursor, to_date=chunk_end))
        cursor = chunk_end + timedelta(days=1)
        idx += 1
    return chunks


class FyersHistoricalDataProvider(HistoricalDataProvider):
    """Official FYERS v3 Historical Data REST provider (`GET /data/history` only)."""

    def __init__(
        self,
        settings: AppSettings | None = None,
        *,
        http_transport: httpx.BaseTransport | None = None,
        sleep_fn: Callable[[float], None] = time.sleep,
    ) -> None:
        self._cfg = settings or get_settings()
        self._transport = http_transport
        self._sleep = sleep_fn
        self._validate_history_only_endpoint(self._cfg.FYERS_HISTORICAL_DATA_URL)

    @staticmethod
    def _validate_history_only_endpoint(url: str) -> None:
        lower = (url or "").strip().lower().rstrip("/")
        forbidden = (
            "/orders",
            "/positions",
            "/holdings",
            "/funds",
            "/tradebook",
            "orderbook",
            "ws",
            "socket",
        )
        if any(f in lower for f in forbidden) or not lower.endswith("/history"):
            raise ConfigurationSafetyError(
                "FyersHistoricalDataProvider refuses non-history URLs; execution endpoints are forbidden."
            )

    @property
    def provider_name(self) -> str:
        return HistoricalProviderName.FYERS.value

    @property
    def source_type(self) -> str:
        return DataSourceType.FYERS_HISTORICAL.value

    def _secret_values_for_redaction(self) -> tuple[str, ...]:
        return (
            self._cfg.FYERS_ACCESS_TOKEN,
            self._cfg.FYERS_SECRET_KEY,
            self._cfg.FYERS_CLIENT_ID,
            self._cfg.FYERS_REDIRECT_URI,
            self._cfg.FYERS_USER_ID,
            self._cfg.FYERS_TOTP_SECRET,
            self._cfg.FYERS_PIN,
        )

    @staticmethod
    def _format_client_token_header(raw_cid: str, raw_token: str) -> str:
        tok = raw_token.strip()
        cid = raw_cid.strip()
        if ":" in tok:
            left, _, right = tok.partition(":")
            if not left.strip() or not right.strip():
                raise FyersAuthenticationRequiredError(FYERS_AUTH_RENEWAL_UI_MESSAGE)
            return f"{left.strip()}:{right.strip()}"
        if not cid or cid.startswith("<") or cid.endswith(">"):
            raise FyersAuthenticationRequiredError(FYERS_AUTH_RENEWAL_UI_MESSAGE)
        return f"{cid}:{tok}"

    def _build_authorization_header(self, *, force_totp_refresh: bool = False) -> str:
        if not self._cfg.FYERS_HISTORICAL_DATA_ENABLED:
            raise HistoricalProviderUnavailableError(
                "FYERS historical data integration is disabled by configuration "
                "(FYERS_HISTORICAL_DATA_ENABLED=false). Existing stored and uploaded datasets remain available.",
                code="FYERS_HISTORICAL_DISABLED",
            )

        raw_cid = (self._cfg.FYERS_CLIENT_ID or "").strip()

        # 1. If a static access token is explicitly provided and we are not forcing TOTP renewal, try it first
        if not force_totp_refresh and self._cfg.fyers_static_token_configured:
            return self._format_client_token_header(raw_cid, self._cfg.FYERS_ACCESS_TOKEN)

        # 2. Otherwise (or on 401 expiry recovery), use in-backend headless TOTP (`vagator/v2`)
        if self._cfg.fyers_totp_configured:
            totp_auth = FyersTotpAuthenticator(
                self._cfg,
                http_transport=self._transport,
            )
            minted_token = totp_auth.get_or_mint_access_token(force_refresh=force_totp_refresh)
            return self._format_client_token_header(raw_cid, minted_token)

        missing = self._cfg.fyers_missing_totp_vars
        err = FyersAuthenticationRequiredError(FYERS_AUTH_RENEWAL_UI_MESSAGE)
        err.diagnostic = (  # type: ignore[attr-defined]
            f"Missing backend environment variables on API service: {', '.join(missing)} "
            "(or supply FYERS_CLIENT_ID + FYERS_ACCESS_TOKEN)."
        )
        raise err

    def provider_status(self) -> dict[str, Any]:
        """Return strictly sanitized provider readiness (never exposes tokens or client secrets)."""
        enabled = bool(self._cfg.FYERS_HISTORICAL_DATA_ENABLED)
        configured = bool(self._cfg.fyers_token_configured)
        auth_mode = self._cfg.fyers_auth_mode
        if not enabled:
            status_code = "DISABLED"
            message = (
                "FYERS historical-data ingestion is disabled (FYERS_HISTORICAL_DATA_ENABLED=false). "
                "Existing stored, CSV, and synthetic datasets remain available."
            )
        elif not configured:
            status_code = "FYERS_AUTHENTICATION_REQUIRED"
            message = FYERS_AUTH_RENEWAL_UI_MESSAGE
        else:
            status_code = "CONFIGURED"
            mode_note = (
                "headless TOTP auto-renewal"
                if auth_mode == "HEADLESS_TOTP"
                else "static access token"
            )
            message = (
                f"FYERS historical market-data credentials configured ({mode_note}; verified on request). "
                "Execution remains strictly SIMULATED PAPER TRADING."
            )

        return {
            "provider": self.provider_name,
            "source_type": self.source_type,
            "enabled": enabled,
            "credentials_configured": configured,
            "auth_mode": auth_mode,
            "env_checklist": self._cfg.fyers_env_presence,
            "missing_totp_vars": self._cfg.fyers_missing_totp_vars,
            "status": status_code,
            "message": message,
            "max_date_range_days": self._cfg.FYERS_MAX_DATE_RANGE_DAYS,
            "default_date_range_days": self._cfg.FYERS_DEFAULT_DATE_RANGE_DAYS,
            "execution_capability": "NONE_HISTORICAL_READ_ONLY",
        }

    def _classify_and_raise_fyers_error(
        self,
        *,
        http_status: int,
        payload: dict[str, Any] | None,
        raw_text: str,
    ) -> None:
        fyers_code_raw = payload.get("code") if isinstance(payload, dict) else None
        fyers_code: int | None = None
        if isinstance(fyers_code_raw, int):
            fyers_code = fyers_code_raw
        elif isinstance(fyers_code_raw, str) and fyers_code_raw.lstrip("-").isdigit():
            fyers_code = int(fyers_code_raw)

        raw_msg = (
            str(payload.get("message", ""))
            if isinstance(payload, dict) and payload.get("message")
            else raw_text
        )
        safe_msg = sanitize_provider_error_text(
            raw_msg, extra_secrets=self._secret_values_for_redaction()
        )
        lower_msg = safe_msg.lower()

        # 1. Authentication expired / invalid
        if (
            http_status == 401
            or (fyers_code in _FYERS_AUTH_ERROR_CODES)
            or any(
                kw in lower_msg
                for kw in (
                    "token",
                    "authenticate",
                    "authentication",
                    "unauthorized",
                    "invalid app",
                    "expired",
                    "could not authenticate",
                )
            )
        ):
            raise FyersAuthenticationRequiredError(FYERS_AUTH_RENEWAL_UI_MESSAGE)

        # 2. Permission denied
        if http_status == 403 or (fyers_code in _FYERS_PERMISSION_ERROR_CODES) or "permission" in lower_msg or "forbidden" in lower_msg:
            raise HistoricalDataError(
                "FYERS historical-data permission denied for this application or symbol. "
                "Existing stored datasets remain available.",
                code="FYERS_PERMISSION_DENIED",
            )

        # 3. Rate limit
        if http_status == 429 or (fyers_code in _FYERS_RATE_LIMIT_CODES) or "rate limit" in lower_msg or "too many" in lower_msg:
            raise HistoricalProviderUnavailableError(
                "FYERS historical-data rate limit reached. Wait briefly and retry with a smaller date window.",
                code="FYERS_RATE_LIMITED",
            )

        # 4. Invalid symbol / resolution / date range
        if fyers_code in _FYERS_INVALID_SYMBOL_CODES or "invalid symbol" in lower_msg:
            raise HistoricalDataError(
                "FYERS rejected the historical symbol mapping as invalid.",
                code="FYERS_INVALID_SYMBOL",
            )
        if "resolution" in lower_msg:
            raise HistoricalDataError(
                "FYERS rejected the requested timeframe resolution.",
                code="INVALID_TIMEFRAME",
            )
        if http_status == 400 or "range" in lower_msg or "date" in lower_msg or (fyers_code is not None and fyers_code == -50):
            raise HistoricalDataError(
                f"FYERS rejected historical request parameters: {safe_msg}",
                code="INVALID_DATE_RANGE",
            )

        # 5. Upstream service unavailable
        if http_status >= 500:
            raise HistoricalProviderUnavailableError(
                "FYERS historical data service is temporarily unavailable. Existing stored datasets remain available.",
                code="FYERS_SERVICE_UNAVAILABLE",
            )

        raise HistoricalDataError(
            f"FYERS historical request failed: {safe_msg}",
            code="FYERS_PROVIDER_ERROR",
        )

    def _fetch_single_chunk(
        self,
        client: httpx.Client,
        *,
        auth_header: str,
        provider_symbol: str,
        fyers_resolution: str,
        chunk: DateRangeChunk,
    ) -> list[Any]:
        """Request a single bounded date chunk with bounded retries & exponential backoff on transient failures."""
        params = {
            "symbol": provider_symbol,
            "resolution": fyers_resolution,
            "date_format": "1",
            "range_from": chunk.from_date.isoformat(),
            "range_to": chunk.to_date.isoformat(),
            "cont_flag": "0",
        }
        headers = {
            "Authorization": auth_header,
            "Accept": "application/json",
        }

        max_retries = max(0, int(self._cfg.FYERS_MAX_RETRIES))
        base_backoff = float(self._cfg.FYERS_RETRY_BACKOFF_SECONDS)

        attempt = 0
        while True:
            try:
                resp = client.get(
                    self._cfg.FYERS_HISTORICAL_DATA_URL,
                    params=params,
                    headers=headers,
                )
            except httpx.TimeoutException as exc:
                if attempt < max_retries:
                    self._sleep(base_backoff * (2**attempt))
                    attempt += 1
                    continue
                raise HistoricalProviderUnavailableError(
                    "Network timeout communicating with FYERS historical data endpoint. "
                    "Existing stored datasets remain available.",
                    code="FYERS_NETWORK_TIMEOUT",
                ) from exc
            except httpx.TransportError as exc:
                if attempt < max_retries:
                    self._sleep(base_backoff * (2**attempt))
                    attempt += 1
                    continue
                raise HistoricalProviderUnavailableError(
                    "FYERS is temporarily unreachable due to a network transport error. "
                    "Existing stored datasets remain available.",
                    code="FYERS_SERVICE_UNAVAILABLE",
                ) from exc

            # Never retry 401/403 authentication or permission errors!
            if resp.status_code in {401, 403}:
                parsed_err: dict[str, Any] | None = None
                try:
                    obj = json.loads(resp.text)
                    if isinstance(obj, dict):
                        parsed_err = obj
                except ValueError:
                    parsed_err = None
                self._classify_and_raise_fyers_error(
                    http_status=resp.status_code,
                    payload=parsed_err,
                    raw_text=resp.text[:250],
                )

            # Bounded retry with exponential backoff on 429 / 5xx transient responses
            if resp.status_code in _TRANSIENT_HTTP_STATUSES and attempt < max_retries:
                self._sleep(base_backoff * (2**attempt))
                attempt += 1
                continue

            try:
                # Parse float tokens directly into Decimal so exact decimal digits are preserved!
                payload = json.loads(resp.text, parse_float=Decimal)
            except ValueError as exc:
                if resp.status_code in _TRANSIENT_HTTP_STATUSES:
                    raise HistoricalProviderUnavailableError(
                        "FYERS is temporarily unavailable. Existing stored datasets remain available.",
                        code="FYERS_SERVICE_UNAVAILABLE",
                    ) from exc
                raise HistoricalDataError(
                    "Received malformed non-JSON response from FYERS historical endpoint.",
                    code="FYERS_MALFORMED_RESPONSE",
                ) from exc

            if not isinstance(payload, dict):
                raise HistoricalDataError(
                    "Malformed FYERS historical payload structure (expected JSON object).",
                    code="FYERS_MALFORMED_RESPONSE",
                )

            status_flag = str(payload.get("s", "")).strip().lower()
            if resp.status_code == 200 and status_flag == "no_data":
                return []

            if resp.status_code == 200 and status_flag == "ok":
                candles = payload.get("candles")
                if candles is None and isinstance(payload.get("data"), dict):
                    candles = payload["data"].get("candles")
                if not isinstance(candles, list):
                    raise HistoricalDataError(
                        "Malformed FYERS historical payload: missing 'candles' array.",
                        code="FYERS_MALFORMED_RESPONSE",
                    )
                return candles

            # Check if JSON-level rate-limit error inside HTTP 200 can be retried
            fyers_code = payload.get("code")
            msg_lower = str(payload.get("message", "")).lower()
            is_json_rate_limit = (
                fyers_code in _FYERS_RATE_LIMIT_CODES or "rate limit" in msg_lower
            )
            if is_json_rate_limit and attempt < max_retries:
                self._sleep(base_backoff * (2**attempt))
                attempt += 1
                continue

            self._classify_and_raise_fyers_error(
                http_status=resp.status_code,
                payload=payload,
                raw_text=resp.text[:250],
            )

    def fetch_historical_candles(
        self,
        *,
        instrument: str,
        timeframe: str,
        from_date: date,
        to_date: date,
        provider_symbol: str | None = None,
        session_open_ist: str = "09:15:00",
        session_close_ist: str = "15:30:00",
        allow_partial_current_candle: bool = False,
        reference_now: datetime | None = None,
        on_chunk_completed: Callable[[dict[str, Any]], None] | None = None,
    ) -> HistoricalProviderFetchResult:
        """Download, merge, deduplicate, and validate historical candles across chronological chunks."""
        auth_header = self._build_authorization_header()
        inst_spec = get_instrument_provider_spec(instrument)
        tf_spec = get_timeframe_spec(timeframe)
        eff_provider_symbol = (provider_symbol or "").strip() or inst_spec.provider_symbol

        today_ist = ensure_ist(reference_now or now_ist()).date()
        if from_date > today_ist:
            raise HistoricalDataError(
                f"Requested start date ({from_date}) is in the future relative to IST ({today_ist}).",
                code="INVALID_DATE_RANGE",
            )

        chunks = plan_fyers_date_chunks(
            from_date=from_date,
            to_date=to_date,
            timeframe=tf_spec.code,
            max_total_days=self._cfg.FYERS_MAX_DATE_RANGE_DAYS,
        )

        merged_raw_tuples: list[Any] = []
        chunk_summaries: list[dict[str, Any]] = []
        missing_chunk_intervals: list[dict[str, str]] = []
        chunks_succeeded = 0
        chunks_failed = 0

        did_force_totp_refresh = False
        timeout = httpx.Timeout(float(self._cfg.FYERS_REQUEST_TIMEOUT_SECONDS))
        with httpx.Client(
            timeout=timeout,
            transport=self._transport,
            follow_redirects=False,
            trust_env=False,
        ) as client:
            for ch in chunks:
                try:
                    try:
                        raw_rows = self._fetch_single_chunk(
                            client,
                            auth_header=auth_header,
                            provider_symbol=eff_provider_symbol,
                            fyers_resolution=tf_spec.fyers_resolution,
                            chunk=ch,
                        )
                    except FyersAuthenticationRequiredError:
                        # Self-heal once via headless TOTP if TOTP credentials are configured
                        if self._cfg.fyers_totp_configured and not did_force_totp_refresh:
                            did_force_totp_refresh = True
                            clear_fyers_totp_token_cache()
                            auth_header = self._build_authorization_header(force_totp_refresh=True)
                            raw_rows = self._fetch_single_chunk(
                                client,
                                auth_header=auth_header,
                                provider_symbol=eff_provider_symbol,
                                fyers_resolution=tf_spec.fyers_resolution,
                                chunk=ch,
                            )
                        else:
                            raise

                    chunks_succeeded += 1
                    merged_raw_tuples.extend(raw_rows)
                    summary = {
                        "chunk_index": ch.chunk_index,
                        "chunks_total": len(chunks),
                        "from_date": ch.from_date.isoformat(),
                        "to_date": ch.to_date.isoformat(),
                        "status": "COMPLETED",
                        "candles_returned": len(raw_rows),
                    }
                    chunk_summaries.append(summary)
                    if on_chunk_completed is not None:
                        on_chunk_completed(summary)
                except FyersAuthenticationRequiredError:
                    # Authentication errors abort immediately without masking
                    raise
                except HistoricalDataError as exc:
                    chunks_failed += 1
                    safe_reason = sanitize_provider_error_text(
                        str(exc), extra_secrets=self._secret_values_for_redaction()
                    )
                    missing_chunk_intervals.append(
                        {
                            "from": ch.from_date.isoformat(),
                            "to": ch.to_date.isoformat(),
                            "reason": f"{exc.code}: {safe_reason}",
                        }
                    )
                    chunk_summaries.append(
                        {
                            "chunk_index": ch.chunk_index,
                            "chunks_total": len(chunks),
                            "from_date": ch.from_date.isoformat(),
                            "to_date": ch.to_date.isoformat(),
                            "status": "FAILED",
                            "error_code": exc.code,
                            "candles_returned": 0,
                        }
                    )
                    # If the very first chunk failed and we have 0 candles on a single-chunk request, propagate
                    if len(chunks) == 1:
                        raise

        if chunks_succeeded == 0:
            raise HistoricalProviderUnavailableError(
                "All historical data request chunks failed. Existing stored datasets remain available.",
                code="FYERS_DOWNLOAD_FAILED",
            )

        if len(merged_raw_tuples) == 0:
            raise HistoricalDataError(
                "No historical candles were returned for the selected symbol and period.",
                code="FYERS_NO_DATA_RETURNED",
            )

        retrieved_ts = ensure_ist(reference_now or now_ist())
        candidates = fyers_tuples_to_candidates(merged_raw_tuples)
        candles, quality = validate_and_normalize_candles(
            candidates,
            instrument=inst_spec.internal_symbol,
            timeframe=tf_spec.code,
            source=self.source_type,
            provider_symbol=eff_provider_symbol,
            retrieval_timestamp=retrieved_ts,
            requested_from=from_date,
            requested_to=to_date,
            session_open_ist=session_open_ist,
            session_close_ist=session_close_ist,
            sort_and_deduplicate_exact=True,
            protect_partial_current_candle=True,
            allow_partial_current_candle=allow_partial_current_candle,
            reference_now=retrieved_ts,
            missing_chunk_intervals=missing_chunk_intervals,
        )

        return HistoricalProviderFetchResult(
            provider=self.provider_name,
            source_type=self.source_type,
            instrument=inst_spec.internal_symbol,
            provider_symbol=eff_provider_symbol,
            timeframe=tf_spec.code,
            candles=candles,
            quality=quality,
            retrieved_at=retrieved_ts,
            chunks_total=len(chunks),
            chunks_succeeded=chunks_succeeded,
            chunks_failed=chunks_failed,
            chunk_summaries=chunk_summaries,
        )

