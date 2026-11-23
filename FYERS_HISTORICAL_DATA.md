# FYERS Historical Market Data, CSV Export, and Real-Data Market Replay

> [!IMPORTANT]
> **FYERS is used only to retrieve historical market data. All executions displayed by the application are simulated paper executions. The platform does not place real orders.**

---

## 1. Scope

This subsystem adds **real historical Indian index OHLCV ingestion** (`NIFTY` and `BANKNIFTY`) via the official FYERS API v3 Historical Data REST endpoint (`GET https://api-t1.fyers.in/data/history`), normalizes candles into a provider-independent `Decimal` + `Asia/Kolkata` (`IST`) schema, validates 21 data-quality invariants, persists datasets in Neon PostgreSQL, supports deterministic CSV export/re-import, and enables both **Deterministic Backtesting** and **Real-Data Market Replay** while keeping all order management, broker fills, positions, and P&L **100% simulated paper trading**.

## 2. Explicit Non-Scope (No Execution and No Live Trading)

The following capabilities are **strictly excluded and prohibited**:

- FYERS order placement, modification, or cancellation
- FYERS live trading or broker execution
- FYERS position, holdings, fund/margin, order-book, or trade-book retrieval
- FYERS live market WebSocket or market-depth WebSocket feeds
- Real-money order routing or live broker reconciliation

The end-to-end trading pipeline remains locked to:

```text
Historical Market Data (FYERS Historical | User CSV | Synthetic Demo)
    ↓
Strategy Engine (EMA Crossover + RSI Confirmation)
    ↓
Signal
    ↓
Independent Risk Engine
    ↓
Internal Paper OMS
    ↓
Internal Paper Broker (Simulated Execution)
    ↓
Paper Position & Simulated P&L
    ↓
Immutable Audit Log
```

## 3. Architecture & SDK Trade-Off

```text
core/market_data/
    models.py              # NormalizedCandle, TimeframeSpec, InstrumentProviderSpec, DataQualityStatus
    provider.py            # HistoricalDataProvider ABC & credential redaction utilities
    fyers_provider.py      # Official FYERS v3 /data/history HTTPS client + chunking + backoff
    csv_provider.py        # Uploaded CSV parser & strict validator
    synthetic_provider.py  # Bundled deterministic demo CSV fallback
    normalization.py       # Epoch -> IST conversion, Decimal parsing, CSV renderer, SHA-256 fingerprint
    validation.py          # 21-point OHLCV & completeness validator
    dataset_service.py     # Dataset registration, deduplication, quality inspection & CSV export
```

### Why Official HTTPS (`httpx`) Instead of `fyers-apiv3` Broker SDK
- **Zero Execution Surface Area:** The third-party `fyers-apiv3` package bundles live order-placement (`place_order`, `modify_order`, `cancel_order`), account/funds/positions APIs, and WebSocket clients, and writes local SDK log files on disk. Using the project's existing pinned `httpx` client against the official documented `GET https://api-t1.fyers.in/data/history` endpoint guarantees that **no order-placement client method exists anywhere in the container image**, keeps Docker images small and stateless on Render Free / Cloud Run, and parses JSON floats directly into `Decimal` (`json.loads(..., parse_float=Decimal)`).

## 4. Authentication

FYERS API v3 Historical Data authenticates via the standard HTTP `Authorization` header:

```http
Authorization: <FYERS_CLIENT_ID>:<FYERS_ACCESS_TOKEN>
```

Configuration variables (backend only — never passed to Streamlit UI):

| Variable | Required | Purpose |
| :--- | :--- | :--- |
| `FYERS_HISTORICAL_DATA_ENABLED` | Optional (`true`) | Feature flag enabling/disabling FYERS download controls |
| `FYERS_CLIENT_ID` | Required for FYERS download | FYERS v3 App ID / Client ID (e.g., `XXXXXXXX-100`) |
| `FYERS_ACCESS_TOKEN` | Required for FYERS download | Current short-lived FYERS v3 OAuth access token |
| `FYERS_SECRET_KEY` | Optional | Optional App Secret (used only if generating tokens externally; never exposed) |
| `FYERS_REDIRECT_URI` | Optional | Optional OAuth Redirect URI configured in FYERS developer portal |
| `FYERS_MAX_DATE_RANGE_DAYS` | Optional (`365`) | Maximum calendar-day span selectable per download |
| `FYERS_DEFAULT_DATE_RANGE_DAYS` | Optional (`90`) | Default calendar-day window in UI |

## 5. Supported Instruments

Configured in `core/market_data/models.py` (`INSTRUMENT_PROVIDER_REGISTRY`) and persisted in PostgreSQL `instruments`:

| Internal Code | Display Name | Data Provider | Provider Symbol | Exchange | Type |
| :--- | :--- | :--- | :--- | :--- | :--- |
| `NIFTY` | NIFTY 50 Index Paper Contract | `FYERS` | `NSE:NIFTY50-INDEX` | `NSE` | `PAPER_INDEX_CONTRACT` |
| `BANKNIFTY` | NIFTY Bank Index Paper Contract | `FYERS` | `NSE:NIFTYBANK-INDEX` | `NSE` | `PAPER_INDEX_CONTRACT` |

## 6. Supported Timeframes

Centralized in `core/market_data/models.py` (`TIMEFRAME_REGISTRY`):

| UI Display Name | Internal Code | FYERS `resolution` | CSV Filename Tag | Max Chunk Window |
| :--- | :--- | :--- | :--- | :--- |
| `1 minute` | `1m` | `1` | `1MIN` | 60 days |
| `3 minutes` | `3m` | `3` | `3MIN` | 90 days |
| `5 minutes` | `5m` | `5` | `5MIN` | 90 days |
| `10 minutes` | `10m` | `10` | `10MIN` | 90 days |
| `15 minutes` | `15m` | `15` | `15MIN` | 90 days |
| `30 minutes` | `30m` | `30` | `30MIN` | 90 days |
| `60 minutes` | `60m` | `60` | `60MIN` | 90 days |
| `1 day` | `1d` | `1D` | `1DAY` | 365 days |

## 7. Date-Range & Chunking Behavior

- Official FYERS v3 limits single intraday history queries to `<= 100` calendar days and daily (`1D`) queries to `<= 366` calendar days.
- `plan_fyers_date_chunks` splits `[from_date, to_date]` into chronological non-overlapping windows (`60` days for `1m`, `90` days for `3m..60m`, `365` days for `1d`).
- Merged chunks are sorted ascending by normalized IST timestamp; exact boundary duplicates are safely deduplicated while conflicting duplicates fail validation.
- If some chunks succeed and others fail, the dataset is persisted with `is_complete = false` and `quality_status = PARTIAL` — never labeled complete.

## 8. Normalized Candle Schema & Timezone Policy

- **Canonical Persisted & Displayed Timezone:** `Asia/Kolkata` (`IST`, `UTC+05:30`), stored in PostgreSQL `TIMESTAMPTZ` columns via `ISTDateTime`.
- **Deterministic Epoch Conversion:** `epoch_seconds_to_ist` converts Unix epoch seconds via `datetime.fromtimestamp(..., tz=timezone.utc).astimezone(ZoneInfo("Asia/Kolkata"))` with zero reliance on container/OS local timezone.
  - Intraday bars open at `09:15:00+05:30`.
  - Daily (`1D`) bars stamped by FYERS at `00:00:00 UTC` map deterministically to `05:30:00+05:30` on the same IST trading date.
- **Fields:** `timestamp`, `open` (`Decimal`), `high` (`Decimal`), `low` (`Decimal`), `close` (`Decimal`), `volume` (`int`), plus internal metadata (`instrument`, `timeframe`, `source`, `provider_symbol`, `retrieval_timestamp`, `dataset_id`).

## 9. Data-Quality Rules (21-Point Validation)

Every dataset is validated across all 21 checks: required fields, timestamp parsing, uniqueness, chronological order, numeric `Decimal` OHLC, `O/H/L/C > 0`, `volume >= 0`, `high >= max(open, close, low)`, `low <= min(open, close)`, intraday missing-candle analysis, exact vs. conflicting duplicates, IST timezone consistency, requested vs. returned date coverage, and partial-current-candle exclusion.
- **Index Zero-Volume Handling:** Cash index feeds (`NSE:NIFTY50-INDEX`, `NSE:NIFTYBANK-INDEX`) may report `volume = 0`. Zero values are preserved truthfully without fabrication and flagged with `ZERO_OR_UNAVAILABLE_INDEX_VOLUME` (`VALID_WITH_WARNINGS`); EMA/RSI calculations are unaffected.
- **Special Sessions:** Off-hours or weekend special sessions (e.g., Muhurat trading) are preserved and flagged as warnings rather than deleted.
- **Statuses:** `VALID`, `VALID_WITH_WARNINGS`, `PARTIAL`, `INVALID`, `DOWNLOAD_FAILED`, `AUTHENTICATION_REQUIRED`.

## 10. Database Persistence

- `instruments`: extended with `data_provider` and `provider_symbol`.
- `historical_datasets`: stores dataset identity, `provider`, `provider_symbol`, `source_type` (`FYERS_HISTORICAL` | `IMPORTED_CSV` | `BUNDLED_CSV`), `quality_status`, `is_complete`, `requested_from`, `requested_to`, `start_timestamp`, `end_timestamp`, `retrieved_at`, `data_version_hash`, `data_quality_report_json`, and `metadata_json`.
- `historical_candles`: stores `(dataset_id, bar_timestamp)` unique normalized candles with `NUMERIC(18,4)` prices and `BIGINT` volume.
- `historical_download_runs`: stores chunk counts, status, and sanitized diagnostics for every download attempt.
- **Neon Storage Estimate:** 1 trading year of 5m candles is ~`18,500` rows (~1.5 MB per instrument), comfortably within Neon Free (`512 MB`) limits.

## 11. CSV Export

- Endpoint: `GET /api/v1/historical-data/datasets/{dataset_id}/csv`
- Columns: `timestamp,open,high,low,close,volume` (ascending chronological ISO-8601 IST `+05:30`, zero DB IDs, zero credentials).
- Deterministic filename: e.g. `NIFTY_5MIN_FYERS_2026-01-01_2026-03-31.csv`.
- Round-trip compatible with `/api/v1/historical-data/uploads` and `/api/v1/backtests/datasets/import-csv` without manual edits.

## 12. Backtest Integration

- Supports stored `FYERS_HISTORICAL` datasets, `IMPORTED_CSV` datasets, and `BUNDLED_CSV` synthetic datasets.
- Verifies instrument & timeframe alignment, blocks `INVALID`/`DOWNLOAD_FAILED` datasets, and requires explicit operator acknowledgment (`allow_partial_dataset=true`) for `PARTIAL` datasets.
- Displays unmistakable `REAL HISTORICAL MARKET DATA | SOURCE: FYERS | EXECUTION: SIMULATED PAPER TRADING` alongside `SIMULATED BACKTEST RESULTS`.

## 13. Market Replay Integration

- Allows filtering by data source (`Real Historical (FYERS)`, `User-Uploaded CSV`, `Synthetic Demo Data`), selecting any stored dataset, inspecting quality status, choosing session & speed (`1x`–`50x`), and streaming normalized `CandleEvent` objects into the unchanged Strategy -> Risk -> Paper OMS -> Paper Broker pipeline.

## 14. Dataset Reproducibility & Immutability

- Core dataset identity fields (`dataset_code`, `data_version_hash`, `row_count`, `start_timestamp`, `end_timestamp`) are guarded by ORM immutability listeners.
- Identical candle downloads deduplicate by SHA-256 `data_version_hash`; changed upstream candles mint a new immutable `dataset_code` so completed backtests remain reproducible.
- `DELETE /api/v1/historical-data/datasets/{id}` is blocked (`409 DATASET_IN_USE`) whenever any `BacktestRun` or `ReplaySession` references the dataset.

## 15. Token Renewal

- FYERS access tokens expire daily. When missing or expired, the backend returns HTTP `401` with `error = "FYERS_AUTHENTICATION_REQUIRED"` and UI message:
  `"FYERS historical-data authentication requires renewal. Existing stored and uploaded datasets remain available."`
- Stored PostgreSQL datasets, CSV uploads, synthetic demo data, Backtests, and Market Replay continue operating without interruption when FYERS is unauthenticated.

## 16. Rate-Limit Handling

- Transient HTTP `429` and `500/502/503/504` / timeout errors use bounded retries (`FYERS_MAX_RETRIES=3`) with exponential backoff (`0.5s, 1.0s, 2.0s`).
- Authentication (`401`/`-16`) and permission (`403`) errors **never** retry.

## 17. Error Handling & Credential Sanitization

- `sanitize_provider_error_text` scrubs Bearer tokens, JWTs (`eyJ...`), `app_id:token` strings, query secrets, and configured secret values before persisting any error message, log, or audit event.
- Raw Python tracebacks are never shown in Streamlit.

## 18. Render Configuration

See [`FYERS_RENDER_CONFIGURATION.md`](./FYERS_RENDER_CONFIGURATION.md). All `FYERS_*` secrets are configured exclusively on the `atcc-api` backend service in Render (`sync: false` in `render.yaml`); `atcc-ui` receives zero FYERS credentials.

## 19. Cloud Run Configuration

On Google Cloud Run (`deploy/cloudrun-api.yaml` / `docs/CLOUD_RUN_NEON_DEPLOYMENT.md`), store `FYERS_CLIENT_ID` and `FYERS_ACCESS_TOKEN` in **Google Secret Manager** and mount them as environment variables on `algo-control-center-api` only (`--set-secrets="FYERS_CLIENT_ID=fyers-client-id:latest,FYERS_ACCESS_TOKEN=fyers-access-token:latest"`). Never pass them to `algo-control-center-ui`.

## 20. Security Boundary

Verified continuously by `tests/unit/test_fyers_security_boundary.py`:
- `EXECUTION_MODE` is locked to `PAPER_ONLY`.
- `EXECUTION_BROKER` is locked to `INTERNAL_PAPER_BROKER` (configuring `FYERS` raises `ConfigurationSafetyError`).
- `FYERS_HISTORICAL_DATA_URL` rejects any URL not ending in `/history` or containing `/orders`, `/positions`, `/holdings`, `/funds`, `/tradebook`, `orderbook`, or `ws`.
- Paper OMS routes exclusively to `core.broker.paper_broker.PaperBroker`.
- No UI screen contains any `"Place Live Order"` control or `"LIVE DATA"` label.

## 21. Testing

- Unit tests: `tests/unit/test_fyers_historical_unit.py` and `tests/unit/test_fyers_security_boundary.py`
- Integration & 6 end-to-end scenarios (mocked FYERS transport, zero live credentials required): `tests/integration/test_fyers_historical_scenarios.py`
- Live authenticated smoke test runbook: [`FYERS_HISTORICAL_DATA_VALIDATION.md`](./FYERS_HISTORICAL_DATA_VALIDATION.md)

## 22. Known Limitations

- FYERS OAuth v3 access tokens expire daily and must be refreshed in environment configuration when performing *new* FYERS downloads (already-ingested PostgreSQL datasets remain permanently usable without a token).
- Cash index symbols (`NSE:NIFTY50-INDEX`, `NSE:NIFTYBANK-INDEX`) typically report `0` traded volume in exchange index feeds; `0` is preserved truthfully with a data-quality warning.
- Render Free instances spin down after ~15 minutes of inactivity; interrupted `RUNNING` replay sessions safely transition to `PAUSED_RECOVERY` on wake-up and require explicit operator `RESUME`.

## 23. Troubleshooting

| Symptom | Cause | Resolution |
| :--- | :--- | :--- |
| `FYERS_AUTHENTICATION_REQUIRED` | `FYERS_ACCESS_TOKEN` unset or expired | Generate a fresh FYERS v3 access token, update `FYERS_ACCESS_TOKEN` on `atcc-api`, and re-run download |
| `FYERS_NO_DATA_RETURNED` | Selected range contains only weekends/NSE holidays | Select a completed weekday trading date range |
| `PARTIAL_DATASET_ACKNOWLEDGMENT_REQUIRED` | Dataset has `quality_status = PARTIAL` | Inspect Data Quality report and check the explicit acknowledgment box on Backtests |
| `409 DATASET_IN_USE` on delete | Dataset is referenced by a persisted Backtest or ReplaySession | Protected for reproducibility; unreferenced datasets can be deleted |
