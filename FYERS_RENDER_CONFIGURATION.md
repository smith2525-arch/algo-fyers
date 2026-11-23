# FYERS Historical Data — Render Free & Neon PostgreSQL Configuration Guide

> [!IMPORTANT]
> **FYERS is used ONLY as a read-only historical OHLCV market-data source on the FastAPI backend (`atcc-api`).**
> Never add `FYERS_*` environment variables to the Streamlit frontend service (`atcc-ui`). Execution remains locked to `EXECUTION_MODE=PAPER_ONLY`.

---

## 1. Backend Environment Variables (`atcc-api` Only)

Configure the following environment variables in **Render Dashboard -> `atcc-api` -> Environment**:

| Variable | Secret? | Example / Default | Notes |
| :--- | :--- | :--- | :--- |
| `FYERS_HISTORICAL_DATA_ENABLED` | No | `true` | Set `false` to disable FYERS download UI while keeping stored/CSV/synthetic datasets active |
| `FYERS_CLIENT_ID` | **Yes (`sync: false`)** | `AB12345-100` | Your FYERS API v3 App ID |
| `FYERS_ACCESS_TOKEN` | **Yes (`sync: false`)** | `<JWT_ACCESS_TOKEN>` | Short-lived FYERS v3 OAuth token (either raw JWT or `APP_ID:JWT`) |
| `FYERS_MAX_DATE_RANGE_DAYS` | No | `365` | Conservative calendar-day download cap |
| `FYERS_DEFAULT_DATE_RANGE_DAYS` | No | `90` | Default date range shown in Historical Data UI |

*Optional (only if running an offline token-generation helper locally — not required on Render if pasting `FYERS_ACCESS_TOKEN` directly):*
- `FYERS_SECRET_KEY`
- `FYERS_REDIRECT_URI`

Ensure `atcc-ui` has **only** `API_BASE_URL` and `API_TIMEOUT_SECONDS` (never `DATABASE_URL` or `FYERS_*`).

---

## 2. Database Schema Migration (`0008_fyers_historical_market_data`)

Before using Historical Data downloads against Neon PostgreSQL, ensure schema head `0008_fyers_historical_market_data` is applied:

- **Option A (CLI):**
  ```bash
  python -m deploy.run_migrations --seed
  ```
- **Option B (Neon SQL Editor):**
  Execute the idempotent `NEON_INITIAL_SETUP.sql` script and verify `SELECT version_num FROM alembic_version;` returns `0008_fyers_historical_market_data`.

---

## 3. Token Renewal Procedure & Restart Implications

1. FYERS v3 access tokens expire daily (typically ~06:00 IST next day).
2. When a token expires:
   - Existing FYERS datasets already persisted in Neon PostgreSQL remain **100% available** for Backtests, Market Replay, and CSV Export.
   - Attempting a *new* FYERS download returns sanitized status `FYERS_AUTHENTICATION_REQUIRED` without breaking any other screen.
3. To renew:
   - Generate a fresh FYERS v3 `access_token` from your local developer environment using your FYERS v3 app login flow.
   - In **Render Dashboard -> `atcc-api` -> Environment**, update `FYERS_ACCESS_TOKEN` and click **Save Changes**.
   - Render automatically restarts `atcc-api` with the updated environment variable (~30–60 seconds). Any active `RUNNING` replay session safely moves to `PAUSED_RECOVERY` on startup and can be resumed by clicking `RESUME` in Market Replay.

---

## 4. Authentication & Historical Download Smoke Test

1. Check sanitized provider readiness (verifies zero secret leakage):
   ```bash
   curl -s https://<your-atcc-api>.onrender.com/api/v1/historical-data/providers
   ```
   Confirm `"provider": "FYERS"` reports `"status": "CONFIGURED"` and `"execution_capability": "NONE_HISTORICAL_READ_ONLY"`.
2. Open the Streamlit UI -> **Historical Data** screen:
   - Select **FYERS Historical Data**, `NIFTY` (`NSE:NIFTY50-INDEX`), `5 minutes`, and a single completed trading day (`From Date = To Date`).
   - Click **FETCH HISTORICAL DATA**.
   - Confirm `Quality Status` is `VALID` (or `VALID_WITH_WARNINGS` if index volume is `0`), click **DOWNLOAD CSV**, and test **USE FOR BACKTEST** and **USE FOR MARKET REPLAY**.

---

## 5. Secret Rotation & Troubleshooting

- **Secret Rotation:** If a token or App Secret is ever suspected of exposure, revoke/regenerate the app secret in the FYERS API Dashboard (`myapi.fyers.in`), generate a new access token, and update `FYERS_ACCESS_TOKEN` in Render `atcc-api` Environment.
- **`/api/v1/settings` Verification:** `/api/v1/settings` exposes only boolean flags (`fyers_historical_data_enabled` and `fyers_token_configured`) and never returns client secrets, tokens, or authorization headers.
- **Render Cold Start:** On Render Free, the first request after 15 minutes of inactivity may take 20–50 seconds while `atcc-api` wakes up; `ControlCenterApiClient` automatically performs bounded cold-start retries without infinite loops or keep-alive traffic.
