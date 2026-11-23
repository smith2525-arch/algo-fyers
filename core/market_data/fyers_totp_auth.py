"""In-Backend Headless FYERS v3 TOTP Authenticator (`vagator/v2` + `/api/v3`).

SECURITY & ARCHITECTURAL INVARIANTS:
- Used EXCLUSIVELY to mint short-lived `access_token`s for `FyersHistoricalDataProvider`
  (`GET /data/history` historical OHLCV queries only).
- Zero external TOTP or broker SDK dependencies: implements RFC 6238 TOTP (`HMAC-SHA1`,
  30s step, 6 digits) using Python's standard library (`hmac`, `hashlib`, `base64`, `struct`).
- Caches minted `access_token` in process memory (thread-safe) until JWT `exp` / next
  05:55 IST daily reset so multi-chunk historical downloads do NOT spam `vagator/v2`.
- Strictly scrubs `FYERS_TOTP_SECRET`, `FYERS_PIN`, `FYERS_SECRET_KEY`, `FYERS_USER_ID`,
  Base64-encoded identifiers, `request_key`, `auth_code`, and tokens from all error strings.
"""

from __future__ import annotations

import base64
import binascii
from dataclasses import dataclass
from datetime import datetime, time as dt_time, timedelta, timezone
import hashlib
import hmac
import json
import struct
import threading
import time
from typing import Any
from urllib.parse import parse_qs, urlparse

import httpx

from core.clock import IST, ensure_ist, now_ist
from core.config import AppSettings
from core.exceptions import (
    ConfigurationSafetyError,
    FyersAuthenticationRequiredError,
    HistoricalProviderUnavailableError,
)
from core.market_data.provider import sanitize_provider_error_text

FYERS_TOTP_AUTH_FAILED_MESSAGE = (
    "FYERS historical-data authentication requires renewal. "
    "Existing stored and uploaded datasets remain available."
)

_BROWSER_LIKE_HEADERS: dict[str, str] = {
    "Accept": "application/json, text/plain, */*",
    "Content-Type": "application/json",
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
    ),
}


def _raise_totp_step_auth_error(step_diagnostic: str) -> None:
    err = FyersAuthenticationRequiredError(FYERS_TOTP_AUTH_FAILED_MESSAGE)
    err.diagnostic = step_diagnostic  # type: ignore[attr-defined]
    raise err


def _b64_encode_ascii(value: str) -> str:
    return base64.b64encode(value.encode("ascii")).decode("ascii")


def generate_rfc6238_totp(
    totp_secret: str,
    *,
    unix_time: float | int | None = None,
    step_seconds: int = 30,
    digits: int = 6,
) -> str:
    """Generate a 6-digit RFC 6238 TOTP code from a Base32 secret using Python stdlib only."""
    cleaned = "".join(ch for ch in (totp_secret or "").strip() if not ch.isspace() and ch != "-").upper()
    if not cleaned or cleaned.startswith("<") or cleaned.endswith(">"):
        raise FyersAuthenticationRequiredError(FYERS_TOTP_AUTH_FAILED_MESSAGE)

    # Restore standard Base32 '=' padding if omitted
    rem = len(cleaned) % 8
    if rem:
        cleaned += "=" * (8 - rem)

    try:
        key_bytes = base64.b32decode(cleaned, casefold=True)
    except (binascii.Error, ValueError) as exc:
        err = FyersAuthenticationRequiredError(FYERS_TOTP_AUTH_FAILED_MESSAGE)
        err.diagnostic = "FYERS_TOTP_SECRET is not a valid Base32-encoded 2FA secret key."  # type: ignore[attr-defined]
        raise err from exc

    now_sec = int(time.time() if unix_time is None else unix_time)
    if now_sec < 0:
        raise FyersAuthenticationRequiredError(FYERS_TOTP_AUTH_FAILED_MESSAGE)

    counter = now_sec // step_seconds
    counter_bytes = struct.pack(">Q", counter)
    digest = hmac.new(key_bytes, counter_bytes, hashlib.sha1).digest()
    offset = digest[-1] & 0x0F
    binary_code = struct.unpack(">I", digest[offset : offset + 4])[0] & 0x7FFFFFFF
    otp_int = binary_code % (10**digits)
    return str(otp_int).zfill(digits)


def split_fyers_client_id(client_id: str) -> tuple[str, str]:
    """Split `APPID-100` into `('APPID', '100')` for step-4 `/api/v3/token` payload."""
    clean = (client_id or "").strip().strip('"').strip("'").strip()
    if "-" in clean:
        prefix, _, suffix = clean.rpartition("-")
        if prefix and suffix.isdigit():
            return prefix, suffix
    return clean, "100"


def normalize_fyers_full_client_id(client_id: str) -> str:
    """Ensure full FYERS v3 App ID includes appType suffix (e.g. `APPID` -> `APPID-100`)."""
    prefix, app_type = split_fyers_client_id(client_id)
    if not prefix:
        return ""
    return f"{prefix}-{app_type}"


def _extract_jwt_expiry_ist(jwt_token: str) -> datetime | None:
    """Best-effort decode of standard JWT `exp` claim (unverified metadata only) for cache TTL."""
    parts = (jwt_token or "").strip().split(".")
    if len(parts) != 3:
        return None
    payload_b64 = parts[1]
    rem = len(payload_b64) % 4
    if rem:
        payload_b64 += "=" * (4 - rem)
    try:
        raw = base64.urlsafe_b64decode(payload_b64.encode("ascii"))
        obj = json.loads(raw.decode("utf-8"))
        exp_val = obj.get("exp")
        if isinstance(exp_val, (int, float)) and exp_val > 0:
            return datetime.fromtimestamp(float(exp_val), tz=timezone.utc).astimezone(IST)
    except Exception:  # noqa: BLE001
        return None
    return None


def _default_next_daily_reset_ist(now_dt: datetime) -> datetime:
    """FYERS tokens reset daily around 06:00 IST; expire cache conservatively at 05:55 IST."""
    cur_ist = ensure_ist(now_dt)
    cutoff_today = datetime.combine(cur_ist.date(), dt_time(5, 55, 0), tzinfo=IST)
    if cur_ist < cutoff_today:
        return cutoff_today
    return cutoff_today + timedelta(days=1)


@dataclass(frozen=True)
class _CachedFyersToken:
    cache_fingerprint: str
    access_token: str
    expires_at_ist: datetime


_TOKEN_CACHE_LOCK = threading.Lock()
_CACHED_TOKEN: _CachedFyersToken | None = None


def clear_fyers_totp_token_cache() -> None:
    """Clear in-memory cached TOTP access token (used on 401 invalidation and in unit tests)."""
    global _CACHED_TOKEN
    with _TOKEN_CACHE_LOCK:
        _CACHED_TOKEN = None


def _validate_auth_only_base_url(url: str, label: str) -> str:
    clean = (url or "").strip().rstrip("/")
    lower = clean.lower()
    forbidden = (
        "/orders",
        "/positions",
        "/holdings",
        "/funds",
        "/tradebook",
        "orderbook",
        "ws://",
        "wss://",
    )
    if not clean.startswith(("https://", "http://127.0.0.1", "http://localhost")) or any(
        tok in lower for tok in forbidden
    ):
        raise ConfigurationSafetyError(
            f"{label} must use HTTPS and never point to execution or WebSocket endpoints."
        )
    return clean


class FyersTotpAuthenticator:
    """Executes the 5-step headless FYERS `vagator/v2` + `/api/v3` TOTP token handshake."""

    def __init__(
        self,
        settings: AppSettings,
        *,
        http_transport: httpx.BaseTransport | None = None,
        unix_time_fn: Any = None,
    ) -> None:
        self._cfg = settings
        self._transport = http_transport
        self._unix_time_fn = unix_time_fn
        self._vagator_base = _validate_auth_only_base_url(
            settings.FYERS_VAGATOR_BASE_URL, "FYERS_VAGATOR_BASE_URL"
        )
        self._auth_v3_base = _validate_auth_only_base_url(
            settings.FYERS_AUTH_V3_BASE_URL, "FYERS_AUTH_V3_BASE_URL"
        )

    def _redaction_secrets(self) -> tuple[str, ...]:
        uid = (self._cfg.FYERS_USER_ID or "").strip()
        pin = (self._cfg.FYERS_PIN or "").strip()
        secrets_list = [
            uid,
            pin,
            (self._cfg.FYERS_TOTP_SECRET or "").strip(),
            (self._cfg.FYERS_SECRET_KEY or "").strip(),
            (self._cfg.FYERS_CLIENT_ID or "").strip(),
            (self._cfg.FYERS_ACCESS_TOKEN or "").strip(),
        ]
        if uid:
            try:
                secrets_list.append(_b64_encode_ascii(uid))
            except UnicodeEncodeError:
                pass
        if pin:
            try:
                secrets_list.append(_b64_encode_ascii(pin))
            except UnicodeEncodeError:
                pass
        return tuple(s for s in secrets_list if s)

    def _cache_fingerprint(self) -> str:
        raw = "|".join(
            [
                (self._cfg.FYERS_CLIENT_ID or "").strip(),
                (self._cfg.FYERS_USER_ID or "").strip(),
                (self._cfg.FYERS_REDIRECT_URI or "").strip(),
            ]
        )
        return hashlib.sha256(raw.encode("utf-8")).hexdigest()[:24]

    def get_or_mint_access_token(self, *, force_refresh: bool = False) -> str:
        """Return valid cached `access_token` or execute the 5-step headless TOTP handshake."""
        global _CACHED_TOKEN

        if not self._cfg.fyers_totp_configured:
            raise FyersAuthenticationRequiredError(FYERS_TOTP_AUTH_FAILED_MESSAGE)

        fp = self._cache_fingerprint()
        now_dt = now_ist()

        if not force_refresh:
            with _TOKEN_CACHE_LOCK:
                if (
                    _CACHED_TOKEN is not None
                    and _CACHED_TOKEN.cache_fingerprint == fp
                    and (_CACHED_TOKEN.expires_at_ist - timedelta(seconds=60)) > now_dt
                ):
                    return _CACHED_TOKEN.access_token

        minted = self._execute_five_step_totp_handshake()
        jwt_exp = _extract_jwt_expiry_ist(minted)
        fallback_exp = _default_next_daily_reset_ist(now_dt)
        eff_exp = min(jwt_exp, fallback_exp) if jwt_exp is not None else fallback_exp

        with _TOKEN_CACHE_LOCK:
            _CACHED_TOKEN = _CachedFyersToken(
                cache_fingerprint=fp,
                access_token=minted,
                expires_at_ist=eff_exp,
            )
        return minted

    def _post_json_step(
        self,
        client: httpx.Client,
        url: str,
        *,
        payload: dict[str, Any],
        headers: dict[str, str] | None = None,
        allowed_statuses: frozenset[int] = frozenset({200}),
        step_label: str,
    ) -> dict[str, Any]:
        merged_headers = dict(_BROWSER_LIKE_HEADERS)
        if headers:
            merged_headers.update(headers)
        try:
            resp = client.post(url, json=payload, headers=merged_headers)
        except (httpx.TimeoutException, httpx.TransportError) as exc:
            raise HistoricalProviderUnavailableError(
                f"FYERS authentication gateway unreachable during {step_label}. "
                "Existing stored datasets remain available.",
                code="FYERS_SERVICE_UNAVAILABLE",
            ) from exc

        if resp.status_code in {500, 502, 503, 504}:
            raise HistoricalProviderUnavailableError(
                f"FYERS authentication gateway returned HTTP {resp.status_code} during {step_label}. "
                "Existing stored datasets remain available.",
                code="FYERS_SERVICE_UNAVAILABLE",
            )

        if resp.status_code == 429:
            raise HistoricalProviderUnavailableError(
                "FYERS authentication rate limit reached. Avoid repeated rapid login attempts; "
                "existing stored datasets remain available.",
                code="FYERS_RATE_LIMITED",
            )

        try:
            body = resp.json()
        except ValueError as exc:
            _raise_totp_step_auth_error(
                f"Headless TOTP failed at {step_label} (HTTP {resp.status_code}: non-JSON gateway response; "
                "check if Cloudflare blocked datacenter IP or verify endpoint)."
            )
            raise AssertionError from exc

        if not isinstance(body, dict) or resp.status_code not in allowed_statuses:
            safe_msg = sanitize_provider_error_text(
                str(body.get("message") if isinstance(body, dict) else body),
                extra_secrets=self._redaction_secrets(),
            )
            _raise_totp_step_auth_error(
                f"Headless TOTP failed at {step_label} (HTTP {resp.status_code}): {safe_msg}"
            )

        return body

    def _execute_five_step_totp_handshake(self) -> str:
        fy_id = self._cfg.FYERS_USER_ID.strip()
        totp_secret = self._cfg.FYERS_TOTP_SECRET.strip()
        pin = self._cfg.FYERS_PIN.strip()
        client_id = self._cfg.FYERS_CLIENT_ID.strip()
        secret_key = self._cfg.FYERS_SECRET_KEY.strip()
        redirect_uri = self._cfg.FYERS_REDIRECT_URI.strip()

        try:
            b64_fy_id = _b64_encode_ascii(fy_id)
            b64_pin = _b64_encode_ascii(pin)
        except UnicodeEncodeError as exc:
            raise FyersAuthenticationRequiredError(FYERS_TOTP_AUTH_FAILED_MESSAGE) from exc

        unix_ts = self._unix_time_fn() if callable(self._unix_time_fn) else None
        otp_code = generate_rfc6238_totp(totp_secret, unix_time=unix_ts)
        app_prefix, app_type = split_fyers_client_id(client_id)
        full_client_id = normalize_fyers_full_client_id(client_id)

        timeout = httpx.Timeout(float(self._cfg.FYERS_REQUEST_TIMEOUT_SECONDS))
        with httpx.Client(
            timeout=timeout,
            transport=self._transport,
            follow_redirects=False,
            trust_env=False,
        ) as client:
            # Step 1: send_login_otp_v2
            step1 = self._post_json_step(
                client,
                f"{self._vagator_base}/send_login_otp_v2",
                payload={"fy_id": b64_fy_id, "app_id": "2"},
                step_label="step 1 (send_login_otp_v2)",
            )
            req_key_1 = str(step1.get("request_key") or "").strip()
            if str(step1.get("s", "")).lower() != "ok" or not req_key_1:
                _raise_totp_step_auth_error(
                    "Headless TOTP failed at step 1 (send_login_otp_v2): verify FYERS_USER_ID (fy_id)."
                )

            # Step 2: verify_otp (TOTP)
            step2 = self._post_json_step(
                client,
                f"{self._vagator_base}/verify_otp",
                payload={"request_key": req_key_1, "otp": otp_code},
                step_label="step 2 (verify_otp)",
            )
            req_key_2 = str(step2.get("request_key") or "").strip()
            if str(step2.get("s", "")).lower() != "ok" or not req_key_2:
                _raise_totp_step_auth_error(
                    "Headless TOTP failed at step 2 (verify_otp): TOTP rejected — verify FYERS_TOTP_SECRET Base32 key."
                )

            # Step 3: verify_pin_v2
            step3 = self._post_json_step(
                client,
                f"{self._vagator_base}/verify_pin_v2",
                payload={
                    "request_key": req_key_2,
                    "identity_type": "pin",
                    "identifier": b64_pin,
                },
                step_label="step 3 (verify_pin_v2)",
            )
            data3 = step3.get("data")
            intermediate_token = (
                str(data3.get("access_token") or "").strip() if isinstance(data3, dict) else ""
            )
            if str(step3.get("s", "")).lower() != "ok" or not intermediate_token:
                _raise_totp_step_auth_error(
                    "Headless TOTP failed at step 3 (verify_pin_v2): PIN rejected — verify FYERS_PIN."
                )

            # Step 4: /api/v3/token -> extract OAuth auth_code from redirect Url (HTTP 200 or 308)
            step4 = self._post_json_step(
                client,
                f"{self._auth_v3_base}/token",
                payload={
                    "fyers_id": fy_id,
                    "app_id": app_prefix,
                    "redirect_uri": redirect_uri,
                    "appType": app_type,
                    "code_challenge": "",
                    "state": "atcc_hist_state",
                    "scope": "",
                    "nonce": "",
                    "response_type": "code",
                    "create_cookie": True,
                },
                headers={"Authorization": f"Bearer {intermediate_token}"},
                allowed_statuses=frozenset({200, 308}),
                step_label="step 4 (v3_token)",
            )
            redirect_target = str(step4.get("Url") or step4.get("url") or "").strip()
            if not redirect_target:
                _raise_totp_step_auth_error(
                    "Headless TOTP failed at step 4 (v3_token): no redirect Url returned — "
                    "verify FYERS_CLIENT_ID and FYERS_REDIRECT_URI match your FYERS developer app."
                )

            parsed_q = parse_qs(urlparse(redirect_target).query)
            auth_codes = parsed_q.get("auth_code") or []
            auth_code = str(auth_codes[0]).strip() if auth_codes else ""
            if not auth_code:
                _raise_totp_step_auth_error(
                    "Headless TOTP failed at step 4 (v3_token): redirect Url missing auth_code."
                )

            # Step 5: /api/v3/validate-authcode -> final daily access_token
            # Always try canonical `APPID-100` first so single-use `auth_code` is not burned!
            cand_hash = hashlib.sha256(
                f"{full_client_id}:{secret_key}".encode("utf-8")
            ).hexdigest()
            try:
                step5 = self._post_json_step(
                    client,
                    f"{self._auth_v3_base}/validate-authcode",
                    payload={
                        "grant_type": "authorization_code",
                        "appIdHash": cand_hash,
                        "code": auth_code,
                    },
                    step_label="step 5 (validate_authcode)",
                )
            except FyersAuthenticationRequiredError:
                _raise_totp_step_auth_error(
                    "Headless TOTP failed at step 5 (validate_authcode): appIdHash rejected — "
                    "verify FYERS_CLIENT_ID and FYERS_SECRET_KEY."
                )

            tok = str(step5.get("access_token") or "").strip()
            if str(step5.get("s", "")).lower() != "ok" or not tok:
                _raise_totp_step_auth_error(
                    "Headless TOTP failed at step 5 (validate_authcode): no access_token returned."
                )

            return f"{full_client_id}:{tok}" if ":" not in tok else tok

