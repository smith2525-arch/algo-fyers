# Render Free + Neon Post-Deployment Verification Runbook

Run these checks after deploying **`atcc-api`** and **`atcc-ui`** on Render Free to confirm operational readiness, security redaction, CORS restriction, cold-start resilience, and `PAPER_ONLY` enforcement.

---

## Set Target URLs in Your Shell

**PowerShell:**
```powershell
$API_URL = "https://<your-atcc-api>.onrender.com"
$UI_URL  = "https://<your-atcc-ui>.onrender.com"
```

**Bash:**
```bash
export API_URL="https://<your-atcc-api>.onrender.com"
export UI_URL="https://<your-atcc-ui>.onrender.com"
```

---

## 1. Stateless Liveness Check (`/health/live`)

*(Note: If `atcc-api` has been idle for >15 minutes on Render Free, the first request may take ~30–50 seconds while Render spins up the container.)*

```bash
curl -fsS --max-time 60 "${API_URL}/health/live"
```

**Expected Verification:**
- HTTP `200 OK`
- `"status": "ALIVE"`
- `"execution_mode": "PAPER_ONLY"`
- `"timezone": "Asia/Kolkata"`
- `"timestamp_ist"` ends with `+05:30`
- **Zero** database URLs, hostnames, passwords, or secret names appear in the payload.

---

## 2. Database Readiness & 9-Subsystem Health (`/health/ready` & `/api/v1/health`)

```bash
curl -fsS --max-time 60 "${API_URL}/health/ready"
curl -fsS --max-time 60 "${API_URL}/api/v1/health"
```

**Expected Verification:**
- HTTP `200 OK`
- `database.status == "HEALTHY"`
- `database.details.db_timezone == "Asia/Kolkata"`
- `database.details.alembic_version == "0007_phase5_operational_visibility"`
- `database.details.masked_dsn == "postgresql+psycopg://***:***@<redacted-host>/<redacted-db>"` *(host, username, and password redacted in `APP_ENV=production`)*
- `operational_components` lists all `9` subsystems (`API_SERVER`, `DATABASE`, `MARKET_REPLAY`, `MARKET_DATA`, `STRATEGY_ENGINE`, `RISK_ENGINE`, `PAPER_BROKER`, `POSITION_ENGINE`, `LAST_RECONCILIATION`).

---

## 3. Runtime Settings Secret-Redaction Check (`/api/v1/settings`)

```bash
curl -fsS "${API_URL}/api/v1/settings"
```

**Expected Verification:**
- `app_env == "production"`
- `execution_mode == "PAPER_ONLY"`
- `masked_database_url` shows `postgresql+psycopg://***:***@<redacted-host>/<redacted-db>` (zero Neon hostnames or credentials exposed).

---

## 4. CORS Origin Enforcement Check

Verify `atcc-api` permits your `UI_URL` origin and does **not** return wildcard `*` or reflect untrusted origins:

```bash
# Allowed UI origin -> must include Access-Control-Allow-Origin: ${UI_URL}
curl -i -X OPTIONS "${API_URL}/api/v1/dashboard" \
  -H "Origin: ${UI_URL}" \
  -H "Access-Control-Request-Method: GET"

# Untrusted origin -> must NOT return Access-Control-Allow-Origin: https://evil.example.com
curl -i -X OPTIONS "${API_URL}/api/v1/dashboard" \
  -H "Origin: https://evil.example.com" \
  -H "Access-Control-Request-Method: GET"
```

---

## 5. Streamlit Frontend Health Check (`/_stcore/health`)

```bash
curl -fsS --max-time 60 "${UI_URL}/_stcore/health"
```

**Expected Verification:**
- HTTP `200 OK` (`ok`)

---

## 6. Browser UI, Cold-Start & Paper-Safety Verification

1. Open `${UI_URL}` in your browser.
2. Confirm the persistent top banner displays **`SIMULATED PAPER TRADING | PAPER TRADING | SIMULATED EXECUTION | NO REAL ORDERS`** (`MODE: PAPER_ONLY`) across all 12 sidebar screens.
3. Expand **`Guided 3–5 Min Consulting Demo`** in the sidebar and click **`LOAD DETERMINISTIC DEMO SHOWCASE`** — confirm all `17/17` steps complete (`Backtest`, `Market Replay`, `Signal`, `Risk Approval`, `Order`, `Fill`, `Position`, `P&L`, `Risk Rejection`, `Stale Block`, `Recovery`, `Reconciliation`, `8-Stage Audit Trace`).
4. **Sleep / Restart Survival Check:** Close all browser tabs for 20 minutes so both Render Free containers spin down. Reopen `${UI_URL}`:
   - Confirm the professional starting message appears if `atcc-api` is still waking up (with bounded retries and a manual **`RETRY CONNECTION`** button — zero infinite polling).
   - Once awake, confirm all positions, P&L, risk decisions, backtests, Kill Switch state, and append-only audit events recovered intact from Neon PostgreSQL.
