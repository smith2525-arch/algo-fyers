# FYERS Historical Data — Validation Status & Live Authenticated Smoke-Test Runbook

## 1. Current Validation Classification

| Validation Track | Status | Evidence |
| :--- | :--- | :--- |
| **FYERS Execution Boundary Audit** | **PASS** (`FYERS EXECUTION BOUNDARY: VERIFIED`) | `tests/unit/test_fyers_security_boundary.py` proves zero FYERS order/position/funds/WebSocket endpoints, imports, or UI live-order controls exist; `EXECUTION_MODE=PAPER_ONLY` & `EXECUTION_BROKER=INTERNAL_PAPER_BROKER` enforced. |
| **Unit Suite (Parsing, IST Epoch, 21-Point Validation, Chunking, Backoff, CSV Round-Trip, Decimal Precision, Immutability)** | **PASS** | `tests/unit/test_fyers_historical_unit.py` |
| **Mocked Integration & 6 End-to-End Scenarios (Download -> CSV -> Backtest -> Replay -> Paper Fill -> Expired Token Fallback -> Render Restart Recovery -> UI Labeling)** | **PASS** | `tests/integration/test_fyers_historical_scenarios.py` |
| **Live Authenticated FYERS API Call** | **PENDING LIVE FYERS VALIDATION** | Live `FYERS_CLIENT_ID` and `FYERS_ACCESS_TOKEN` are intentionally not stored in the repository or test environment. Execute the smoke-test procedure below once valid credentials are supplied in `.env` or Render `atcc-api`. |

---

## 2. Exact Procedure for Live Authenticated FYERS Smoke Test

### Prerequisites
1. PostgreSQL / Neon schema migrated to head `0008_fyers_historical_market_data`.
2. Backend configured with valid short-lived FYERS v3 credentials (never committed to Git):
   ```bash
   FYERS_HISTORICAL_DATA_ENABLED=true
   FYERS_CLIENT_ID=<YOUR_REAL_FYERS_APP_ID>
   FYERS_ACCESS_TOKEN=<YOUR_REAL_FYERS_V3_ACCESS_TOKEN>
   ```

### Step-by-Step Smoke Test (1 Completed Trading Day)

1. **Verify Provider Readiness (Zero Secret Leakage):**
   ```bash
   curl -s http://localhost:8000/api/v1/historical-data/providers
   ```
   - Confirm `FYERS` shows `"status": "CONFIGURED"`, `"enabled": true`, and zero token/secret strings appear in the JSON payload.

2. **Execute Safe 1-Day Historical Download (`NIFTY`, `5 minutes`):**
   - Open Streamlit UI -> **Historical Data** (or `POST /api/v1/historical-data/downloads`):
     - **Instrument:** `NIFTY` (verifies provider symbol `NSE:NIFTY50-INDEX`)
     - **Timeframe:** `5 minutes` (verifies FYERS resolution `5`)
     - **From Date / To Date:** Pick one completed non-holiday NSE weekday (e.g., previous completed Tuesday/Wednesday).
   - Click **FETCH HISTORICAL DATA**.

3. **Verify Returned Candles, IST Timestamps, OHLC & Volume:**
   - Confirm `Candle Count` is `75` (normal NSE session `09:15` to `15:25` IST).
   - Confirm `First Candle (IST)` is `YYYY-MM-DD 09:15:00 IST` (`+05:30` — not shifted by 5h 30m!).
   - Confirm `Last Candle (IST)` is `YYYY-MM-DD 15:25:00 IST`.
   - Click **VIEW DATA QUALITY**:
     - Confirm `invalid_ohlc_count = 0`, `conflicting_duplicate_count = 0`.
     - If index volume is `0`, confirm `Quality Status` is `VALID_WITH_WARNINGS` with `ZERO_OR_UNAVAILABLE_INDEX_VOLUME` (values preserved truthfully without fabrication).

4. **Verify Deterministic CSV Export & Round-Trip Upload:**
   - Click **DOWNLOAD CSV** and save `NIFTY_5MIN_FYERS_<DATE>_<DATE>.csv`.
   - Inspect header: confirm columns are strictly `timestamp,open,high,low,close,volume` with `+05:30` timestamps and zero credentials/IDs.
   - Switch Data Source radio to **Upload CSV** and upload the exported file without editing -> confirm `passed_validation = true` and identical candle count.

5. **Verify Backtest Usability:**
   - Select the FYERS dataset and click **USE FOR BACKTEST**.
   - Confirm UI displays `REAL HISTORICAL MARKET DATA • SOURCE: FYERS • EXECUTION: SIMULATED PAPER TRADING` and `SIMULATED BACKTEST RESULTS`.
   - Click **EXECUTE DETERMINISTIC BACKTEST** and confirm the completed run records the immutable FYERS `dataset_id` and `data_version_hash`.

6. **Verify Market Replay Usability & Paper-Only Execution:**
   - Return to **Historical Data**, select the FYERS dataset, and click **USE FOR MARKET REPLAY**.
   - Confirm Market Replay banner displays `REAL HISTORICAL MARKET DATA • SOURCE: FYERS • EXECUTION: SIMULATED PAPER TRADING`.
   - Click **START** / **+1 CANDLE** and verify candles stream into the Strategy Engine, Risk Engine, and Internal Paper OMS without any external broker order API call.
   - Open **Audit Log** and verify `HISTORICAL_DOWNLOAD_COMPLETED` and `REAL_DATA_REPLAY_STARTED` record `data_provider: FYERS`, `data_type: REAL HISTORICAL`, and `execution_type: SIMULATED PAPER` with zero credentials.
