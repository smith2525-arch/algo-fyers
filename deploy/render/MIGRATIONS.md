# Controlled Database Migrations for Render Free + Neon PostgreSQL

> [!IMPORTANT]
> **Why Migrations Are Not Run Automatically Inside `atcc-api` Startup:**
> Running `alembic upgrade head` automatically inside every web container startup risks concurrent DDL locks during rolling deploys and slows down Render Free cold wake-ups (increasing health-check timeout risk). Render Free also does not provide free background workers or free one-shot pre-deploy jobs (`preDeployCommand` requires paid instances).
>
> Therefore, migrations are executed **deterministically from your operator workstation (or CI runner)** against Neon's **Direct** PostgreSQL connection before or during deployment.

---

## 1. Required Neon Connection String (Direct vs Pooled)

Neon provides two connection strings in the **Neon Console $\rightarrow$ Connection Details**:

1. **Direct Connection String (Use ONLY for Migrations):**
   - Hostname does **NOT** contain `-pooler`:
     ```text
     postgresql+psycopg://<NEON_USER>:<NEON_PASSWORD>@ep-<endpoint>.<region>.aws.neon.tech/<NEON_DB>?sslmode=require
     ```
   - Supports session-level PostgreSQL advisory locks (`pg_advisory_lock`) and multi-statement DDL transactions cleanly.
2. **Pooled Connection String (Configure on Render `atcc-api` Runtime):**
   - Hostname contains `-pooler`:
     ```text
     postgresql+psycopg://<NEON_USER>:<NEON_PASSWORD>@ep-<endpoint>-pooler.<region>.aws.neon.tech/<NEON_DB>?sslmode=require
     ```
   - Used by `atcc-api` with `DB_POOL_MODE=NULLPOOL` for fast serverless reconnects after Render sleep.

*(Note: Always ensure the URL scheme starts with `postgresql+psycopg://` or `postgresql://` and includes `?sslmode=require`.)*

---

## 2. Migration Command (Operator Workstation $\rightarrow$ Neon Direct)

Activate your local virtual environment and set `DATABASE_URL` **in your current terminal session only** (never commit it to Git):

### Option A — Controlled Advisory-Locked Runner + Reference Seed (Recommended)
Acquires PostgreSQL advisory lock `84729104`, runs `alembic upgrade head`, and idempotently seeds `NIFTY`, `BANKNIFTY`, `PAPER-DESK-001` (`₹5,00,000` DEMO capital), and `v1` strategies:

**PowerShell (Windows):**
```powershell
.\.venv\Scripts\Activate.ps1
$env:DATABASE_URL = "postgresql+psycopg://<NEON_USER>:<NEON_PASSWORD>@ep-<endpoint>.<region>.aws.neon.tech/<NEON_DB>?sslmode=require"
$env:DB_SSLMODE = "require"

python -m deploy.run_migrations --seed
```
*(Optional: add `--demo-showcase` to pre-populate all 17 guided consulting demo steps immediately: `python -m deploy.run_migrations --seed --demo-showcase`.)*

**Bash (macOS / Linux):**
```bash
source .venv/bin/activate
export DATABASE_URL="postgresql+psycopg://<NEON_USER>:<NEON_PASSWORD>@ep-<endpoint>.<region>.aws.neon.tech/<NEON_DB>?sslmode=require"
export DB_SSLMODE="require"

python -m deploy.run_migrations --seed
```

### Option B — Raw Alembic CLI (`alembic upgrade head`)
If running schema DDL only (without seeding reference rows):
```powershell
.\.venv\Scripts\alembic.exe upgrade head
```

Clear the variable from your shell when finished:
```powershell
Remove-Item Env:DATABASE_URL
```

---

## 3. How Migration Success Is Verified

1. **CLI Output Confirmation:**
   `python -m deploy.run_migrations --seed` prints:
   ```text
   MIGRATION_OK alembic_version=0007_phase5_operational_visibility
   ```
2. **Confirm Current Alembic Revision Locally:**
   ```powershell
   $env:DATABASE_URL = "<YOUR_NEON_DIRECT_URL>"
   .\.venv\Scripts\alembic.exe current
   .\.venv\Scripts\alembic.exe heads
   ```
   Both `current` and `heads` must report:
   `0007_phase5_operational_visibility (head)`
3. **Confirm via Deployed Render Readiness Endpoint:**
   Once `atcc-api` is deployed on Render:
   ```bash
   curl -fsS "https://<your-atcc-api>.onrender.com/health/ready"
   ```
   Verify the JSON response contains:
   - `"status": "HEALTHY"` under `"database"`
   - `"alembic_version": "0007_phase5_operational_visibility"`
   - `"db_timezone": "Asia/Kolkata"`

---

## 4. Rollback Considerations

1. **Prefer Neon Instant Point-in-Time Branch Restore for Production Incidents:**
   - Append-only compliance tables (`audit_events`, `strategy_versions`, `backtest_runs`, `backtest_trades`, `order_state_events`, `paper_fills`, `position_events`, `risk_decisions`, `reconciliation_runs`) enforce PostgreSQL immutability triggers (`trg_*_immutable`).
   - Before applying any future schema migration (`0008+`), create a free Neon branch snapshot in the Neon Console (**Branches $\rightarrow$ Create Branch**) so you have an instant zero-data-loss restore point.
2. **Controlled Alembic Step-Back (Development / Empty Demo DB Only):**
   > [!CAUTION]
   > Downgrading Alembic revisions drops tables introduced in newer phases (for example, downgrading `0007` drops `reconciliation_runs`). Execute only when intentionally rolling back schema on a disposable demo database:
   ```powershell
   .\.venv\Scripts\alembic.exe downgrade -1
   ```
3. **Application Rollback Compatibility:**
   When rolling back `atcc-api` on Render (**Events / Deploys $\rightarrow$ Rollback to this deploy**), verify `alembic current` matches the schema expected by that image tag (`0007_phase5_operational_visibility` for V1).
