# Render Free + Neon PostgreSQL Deployment Guide

> [!IMPORTANT]
> **Execution Mode & Billing Scope:**
> - This deployment hosts **Algo Trading Control Center** on **Render Free Web Services** (`atcc-api` + `atcc-ui`) backed by **Neon Free Serverless PostgreSQL** — requiring **zero Google Cloud billing configuration** and **zero paid VMs, workers, or Render PostgreSQL add-ons**.
> - The application is strictly locked to **`EXECUTION_MODE=PAPER_ONLY`** (`PAPER TRADING | SIMULATED EXECUTION | NO REAL ORDERS`). No real-money broker integration exists.
> - Existing **Google Cloud Run + Neon** deployment support ([`docs/CLOUD_RUN_NEON_DEPLOYMENT.md`](./docs/CLOUD_RUN_NEON_DEPLOYMENT.md) and `deploy/cloudrun-*.yaml`) remains 100% intact.

---

## Architecture Overview

```text
Git Repository (GitHub / GitLab)
      │
      ├─────────────────────────────┐
      │                             │
      ▼                             ▼
atcc-ui (Render Free Web)     atcc-api (Render Free Web)
Dockerfile.ui (Stateless)     Dockerfile.api (Stateless)
      │                             │
      │                             ▼ (SSL + NullPool via -pooler)
      │                       Neon Serverless PostgreSQL
      │
      └──── HTTPS (API_BASE_URL) ──► /api/v1/* & /health/*
```

---

## 1. Prerequisites

1. **Local Python 3.11+ Environment** (used once from your workstation to run controlled Alembic migrations against Neon before traffic hits `atcc-api`).
2. **GitHub or GitLab Account** (to host the repository connected to Render).
3. **Neon Account (Free Plan):** [https://neon.tech](https://neon.tech) (no credit card required).
4. **Render Account (Free Individual Tier):** [https://render.com](https://render.com) (Free Docker Web Services do not require a paid plan).

---

## 2. Neon Preparation

1. Sign in to the [Neon Console](https://console.neon.tech) and click **New Project**:
   - **Postgres Version:** `16`
   - **Database Name:** `algo_control_center`
   - **Region:** Choose **AWS `ap-southeast-1` (Singapore)** (paired with Render **Singapore** region for lowest latency to India) or **AWS `us-east-1`** (paired with Render **Virginia / Ohio**).
   - **Compute Auto-Suspend:** Keep enabled (`5 minutes` idle suspend).
2. Open **Connection Details** in Neon and copy **both** connection strings (keep them in a secure password manager — never paste into repo files):
   - **Pooled Connection String** *(contains `-pooler` — used by Render `atcc-api` runtime)*:
     ```text
     postgresql+psycopg://<NEON_USER>:<NEON_PASSWORD>@ep-<id>-pooler.<region>.aws.neon.tech/algo_control_center?sslmode=require
     ```
   - **Direct Connection String** *(no `-pooler` — used only for Step 9 controlled Alembic migrations)*:
     ```text
     postgresql+psycopg://<NEON_USER>:<NEON_PASSWORD>@ep-<id>.<region>.aws.neon.tech/algo_control_center?sslmode=require
     ```
   *(Note: If Neon displays `postgresql://...`, either keep `postgresql://...` or change the prefix to `postgresql+psycopg://...` — [`core/persistence/db.py`](./core/persistence/db.py) normalizes both automatically and enforces `sslmode=require` on `*.neon.tech` hosts.)*

---

## 3. Git Repository Preparation

Before pushing to GitHub/GitLab, verify that `.gitignore` protects local `.env` and credential files while keeping `.env.example`, `render.yaml`, `Dockerfile.api`, and `Dockerfile.ui`:

1. Confirm **no `.env` file or secrets** are staged:
   - Check [`.gitignore`](./.gitignore) includes `.env`, `.env.*`, `!.env.example`, `*.pem`, `*.key`, `*credentials*.json`, and `*.sqlite3`.
   - Check [`.env.example`](./.env.example) contains only fake placeholders (`DATABASE_URL="<YOUR_NEON_DATABASE_URL>"`).
2. Run the automated secret-hygiene & contract check locally:
   ```powershell
   .\.venv\Scripts\pytest.exe tests/integration/test_final_quality_gate_and_demo.py -v
   ```
3. Push the repository to a **private** (or portfolio public) GitHub/GitLab repository containing:
   - `Dockerfile.api` & `Dockerfile.ui`
   - `render.yaml`
   - `core/`, `backend/`, `frontend/`, `migrations/`, `demo_data/`, `deploy/`

---

## 4. Create Render Account

1. Go to [https://dashboard.render.com/register](https://dashboard.render.com/register) and sign up (signing in with GitHub is fastest).
2. Verify your email address in Render. No Render PostgreSQL database or paid worker needs to be created.

---

## 5. Connect Git Repository (Blueprint or Manual Web Services)

You can create the two Render Free Web Services either **automatically via Blueprint (`render.yaml`)** or **manually via the Render Dashboard**:

- **Blueprint Path (Fastest):** In Render Dashboard, click **New + $\rightarrow$ Blueprint**, select your Git repository, verify Render detects [`render.yaml`](./render.yaml) (`atcc-api` and `atcc-ui`, both on `plan: free`), and supply the three `sync: false` environment prompts as detailed in Steps 7–11 below.
- **Manual Dashboard Path:** Follow Steps 6–11 step-by-step for full explicit control.

---

## 6. Create `atcc-api` (Backend Web Service)

1. In Render Dashboard, click **New + $\rightarrow$ Web Service**.
2. Select **Build and deploy from a Git repository** and connect your repository.
3. Configure service settings:
   - **Name:** `atcc-api`
   - **Language / Runtime:** `Docker`
   - **Branch:** `main`
   - **Region:** Match your Neon region (e.g., **Singapore (`Southeast Asia`)** if Neon is in `ap-southeast-1`, or **Virginia (`US East`)** if Neon is in `us-east-1`).
   - **Root Directory:** *(leave blank — repo root)*
   - **Dockerfile Path:** `./Dockerfile.api`
   - **Instance Type:** **Free** (`$0/month`)
4. Expand **Advanced**:
   - **Health Check Path:** `/health/live`

*(Note: `Dockerfile.api` automatically binds Uvicorn to `--host 0.0.0.0 --port ${PORT:-8080}` using Render's injected `$PORT` environment variable and handles `SIGTERM` gracefully via `exec`.)*

---

## 7. Configure Backend Environment Variables (`atcc-api`)

In the **Environment** section of `atcc-api`, set these non-secret configuration variables (already pre-populated if you used `render.yaml`):

| Key | Value | Purpose |
| :--- | :--- | :--- |
| `APP_ENV` | `production` | Enables production host/user redaction in `/health` & `/api/v1/settings` |
| `EXECUTION_MODE` | `PAPER_ONLY` | **Mandatory safety lock** (startup fails if changed) |
| `MARKET_MODE` | `HISTORICAL_AND_REPLAY` | Normalized historical & 24/7 replay market stream |
| `TIMEZONE` | `Asia/Kolkata` | Enforces IST (`+05:30`) across Python & PostgreSQL sessions |
| `LOG_LEVEL` | `INFO` | Structured operational logging level |
| `LOG_JSON` | `true` | JSON structured logs with `correlation_id` |
| `DB_POOL_MODE` | `NULLPOOL` | Holds `0` idle connections across Render sleep & Neon `-pooler` suspend |
| `DB_SSLMODE` | `require` | Enforces TLS encryption to Neon PostgreSQL |
| `DB_CONNECT_TIMEOUT_SECONDS` | `15` | Absorbs Neon compute cold wake-up cleanly |
| `REPLAY_WORKER_ENABLED` | `false` | Disables background loop on scale-to-zero hosting (uses HTTP `advance_due=true`) |
| `AUTO_SEED_REFERENCE_DATA` | `false` | Prevents startup seed races; controlled seeding runs in Step 9 |
| `CORS_ALLOWED_ORIGINS` | `https://placeholder.onrender.com` | Temporary until `atcc-ui` URL is created in Step 10, then updated in Step 12 |

---

## 8. Configure `DATABASE_URL` Securely on `atcc-api`

1. Still inside **`atcc-api` $\rightarrow$ Environment**, add **`DATABASE_URL`**:
   - **Key:** `DATABASE_URL`
   - **Value:** Paste your **Neon Pooled (`-pooler`)** connection string with `?sslmode=require`:
     `postgresql+psycopg://<NEON_USER>:<NEON_PASSWORD>@ep-<id>-pooler.<region>.aws.neon.tech/algo_control_center?sslmode=require`
2. Click **Create Web Service** (or **Save Changes**).
   - Never place `DATABASE_URL` in `render.yaml`, `Dockerfile.api`, or `atcc-ui`.

---

## 9. Run & Verify Controlled Database Migrations (Against Neon Direct URL)

Render Free does not run a permanent background worker or paid pre-deploy job, and `atcc-api` intentionally does **not** auto-run Alembic DDL on container boot. Run the controlled advisory-locked migration from your local terminal against your **Neon Direct** connection string (see full details in [`deploy/render/MIGRATIONS.md`](./deploy/render/MIGRATIONS.md)):

```powershell
.\.venv\Scripts\Activate.ps1
$env:DATABASE_URL = "postgresql+psycopg://<NEON_USER>:<NEON_PASSWORD>@ep-<id>.<region>.aws.neon.tech/algo_control_center?sslmode=require"
$env:DB_SSLMODE = "require"

# Applies migrations 0001..0007 under pg_advisory_lock + seeds NIFTY, BANKNIFTY, DEMO account & v1 strategies
python -m deploy.run_migrations --seed

# Verify current revision matches head (0007_phase5_operational_visibility)
.\.venv\Scripts\alembic.exe current
Remove-Item Env:DATABASE_URL
```

*(Optional: append `--demo-showcase` to `python -m deploy.run_migrations --seed --demo-showcase` to pre-populate all 17 guided demo steps in Neon immediately, or click **LOAD DETERMINISTIC DEMO SHOWCASE** in the UI sidebar later.)*

Once `atcc-api` finishes building on Render, copy its public HTTPS URL from the top of the Render service page (for example, `https://atcc-api-xxxx.onrender.com`).

---

## 10. Create `atcc-ui` (Streamlit Frontend Web Service)

1. In Render Dashboard, click **New + $\rightarrow$ Web Service** and select the same Git repository.
2. Configure service settings:
   - **Name:** `atcc-ui`
   - **Language / Runtime:** `Docker`
   - **Branch:** `main`
   - **Region:** Same Render region as `atcc-api`
   - **Dockerfile Path:** `./Dockerfile.ui`
   - **Instance Type:** **Free** (`$0/month`)
3. Expand **Advanced**:
   - **Health Check Path:** `/_stcore/health`

---

## 11. Configure Frontend Environment Variables (`atcc-ui`)

In **`atcc-ui` $\rightarrow$ Environment**, configure **only** these two variables (**never** add `DATABASE_URL` to `atcc-ui`):

| Key | Value | Purpose |
| :--- | :--- | :--- |
| `API_BASE_URL` | `https://<your-atcc-api>.onrender.com` | Exact Render HTTPS URL of `atcc-api` (no trailing slash) |
| `API_TIMEOUT_SECONDS` | `25` | Allows clean cold-start wake-up of `atcc-api` + Neon |

Click **Create Web Service** and copy your deployed `atcc-ui` URL once live (for example, `https://atcc-ui-yyyy.onrender.com`).

---

## 12. Lock Backend `CORS_ALLOWED_ORIGINS` to `atcc-ui`

1. Return to **`atcc-api` $\rightarrow$ Environment** in Render Dashboard.
2. Update `CORS_ALLOWED_ORIGINS` to the exact `atcc-ui` HTTPS origin (no trailing slash, no `*` wildcard):
   ```text
   CORS_ALLOWED_ORIGINS=https://<your-atcc-ui>.onrender.com
   ```
3. Click **Save Changes** (Render automatically redeploys `atcc-api` with the locked origin).

---

## 13. Post-Deployment Verification & Cold-Start Behavior

Execute the verification commands in [`deploy/render/verification.md`](./deploy/render/verification.md):

```bash
export API_URL="https://<your-atcc-api>.onrender.com"
export UI_URL="https://<your-atcc-ui>.onrender.com"

# 1. Liveness (must show PAPER_ONLY, Asia/Kolkata, zero DB details)
curl -fsS --max-time 60 "${API_URL}/health/live"

# 2. Readiness & Schema Head (must show HEALTHY, 0007_phase5_operational_visibility, redacted host)
curl -fsS --max-time 60 "${API_URL}/health/ready"

# 3. Streamlit UI Health
curl -fsS --max-time 60 "${UI_URL}/_stcore/health"
```

### Expected Render Free Sleep / Wake Behavior
- Render Free Web Services spin down after **15 minutes** of zero inbound HTTP traffic; Neon Free compute suspends after **5 minutes** of zero queries.
- **No artificial keep-alive traffic** is generated.
- When opening `${UI_URL}` after inactivity, `atcc-ui` displays:
  > *"Trading Control Center services are starting. This demo uses scale-to-zero hosting and may take a short time after inactivity."*
  and performs a **bounded 3-attempt wake-up check** (`2s`, `4s` backoff). If `atcc-api` takes ~35–50s to cold-boot on Render Free, click **`RETRY CONNECTION`** once and all 12 screens load with full PostgreSQL state intact.

---

## 14. Logs, Redeployment & Rollback on Render

- **View Live Logs:** Open **Render Dashboard $\rightarrow$ `atcc-api` (or `atcc-ui`) $\rightarrow$ Logs** to inspect structured JSON logs with `correlation_id`s (zero credentials printed).
- **Redeploy:** Push a commit to `main` (auto-deploys if `autoDeploy: true`) or click **Manual Deploy $\rightarrow$ Deploy latest commit**. If new Alembic migrations were added, run `python -m deploy.run_migrations` against Neon Direct **before** deploying.
- **Instant Rollback:** Open **`atcc-api` (or `atcc-ui`) $\rightarrow$ Events**, locate the previous green deploy, and click **Rollback to this deploy**.

---

## 15. Render Deployment & Post-Deployment Checklists

### Deployment Checklist
- [ ] `.gitignore` present; `.env` not tracked in Git; `.env.example` and `render.yaml` contain zero credentials.
- [ ] Neon Postgres 16 database created; Pooled (`-pooler`) and Direct URLs saved securely with `?sslmode=require`.
- [ ] Controlled migrations executed via Neon Direct URL (`alembic current` == `0008_fyers_historical_market_data`).
- [ ] `atcc-api` deployed on Render Free (`./Dockerfile.api`, health check `/health/live`, `EXECUTION_MODE=PAPER_ONLY`, `DB_POOL_MODE=NULLPOOL`, `DATABASE_URL` set in Render Environment; optional `FYERS_CLIENT_ID` & `FYERS_ACCESS_TOKEN` set on `atcc-api` only — see [`FYERS_RENDER_CONFIGURATION.md`](./FYERS_RENDER_CONFIGURATION.md)).
- [ ] `atcc-ui` deployed on Render Free (`./Dockerfile.ui`, health check `/_stcore/health`, `API_BASE_URL` pointing to `atcc-api`, **no** `DATABASE_URL` and **no** `FYERS_*` secrets).
- [ ] `atcc-api` `CORS_ALLOWED_ORIGINS` locked to `https://<your-atcc-ui>.onrender.com`.

### Post-Deployment Validation Checklist
- [ ] `/health/live` returns `200` with `execution_mode: "PAPER_ONLY"` and zero database metadata.
- [ ] `/health/ready` and `/api/v1/settings` redact database host, username, and password (`postgresql+psycopg://***:***@<redacted-host>/<redacted-db>`).
- [ ] Persistent banner `SIMULATED PAPER TRADING | PAPER TRADING | SIMULATED EXECUTION | NO REAL ORDERS` appears across all 12 screens.
- [ ] Sidebar **Guided 3–5 Min Consulting Demo $\rightarrow$ LOAD DETERMINISTIC DEMO SHOWCASE** completes `17/17` steps end-to-end.
- [ ] After a >15-minute idle sleep, reopening `${UI_URL}` wakes cleanly and preserves Kill Switch state, positions, P&L, backtests, and audit records from Neon PostgreSQL.
