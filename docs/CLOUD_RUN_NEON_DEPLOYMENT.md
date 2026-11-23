# Google Cloud Run + Neon PostgreSQL Low-Cost Deployment Runbook

> [!IMPORTANT]
> **Architecture Lock:** Two stateless containers (`atcc-api` FastAPI + `atcc-ui` Streamlit) on **Google Cloud Run (`min-instances=0`)** backed by **Neon Serverless PostgreSQL** via **Google Secret Manager**.
> - **Zero VMs, Zero Cloud SQL, Zero GKE, Zero Paid Load Balancers, Zero Embedded Secrets.**
> - **Execution Safety Lock:** `EXECUTION_MODE=PAPER_ONLY` (`SIMULATED PAPER TRADING | NO REAL ORDERS`).

---

## Cost-Protection Architecture (`≈ ₹0` Idle / Portfolio Scale)

| Component | Low-Cost Setting | Why It Prevents Unexpected Cost |
| :--- | :--- | :--- |
| **Cloud Run Scaling** | `--min-instances=0 --max-instances=2` | Scales to **0 containers** (`₹0/hr`) when idle; caps concurrency bursts at 2 instances. |
| **Cloud Run CPU Billing** | `--cpu-throttling` (Request-based) | CPU is billed **only** during active HTTP request execution, never between requests. |
| **Container Sizing** | `--cpu=1 --memory=512Mi` | Fits comfortably within Cloud Run's monthly free-tier vCPU-seconds and GiB-seconds. |
| **Replay Mode** | `REPLAY_WORKER_ENABLED=false` | Prevents background 1s loops from keeping containers or Neon awake; replay advances on HTTP requests (`advance_due=true`). |
| **SQLAlchemy Pool** | `DB_POOL_MODE=NULLPOOL` + Neon `-pooler` | Holds **0 idle TCP connections** in containers so Neon compute auto-suspends to `0 CU` after 5 minutes of inactivity. |
| **Controlled Migrations** | Cloud Run Job (`atcc-db-migrate`) | Runs once on deploy with `pg_advisory_lock` and exits immediately (`0` standing cost; zero multi-instance startup races). |

---

## 1. Neon PostgreSQL Configuration

1. Log in to [Neon Console](https://console.neon.tech) and create a **Free Plan** project:
   - **Postgres Version:** `16`
   - **Database Name:** `algo_control_center`
   - **Region:** `AWS ap-southeast-1 (Singapore)` *(lowest latency to `asia-south1` Mumbai)* or `AWS us-east-1` *(if deploying Cloud Run in `us-central1`/`us-east1`)*
   - **Compute Auto-Suspend:** Enabled (`5 minutes` default $\rightarrow$ scales compute to `0` when idle).
2. Copy **both** connection strings from the Neon Dashboard (**Connection Details**) and ensure `?sslmode=require` is appended (note: [`build_engine`](../core/persistence/db.py) also enforces `sslmode=require` automatically on `*.neon.tech` hosts):
   - **Pooled Connection String** *(contains `-pooler` in hostname — used by `atcc-api`)*:
     ```text
     postgresql+psycopg://<USER>:<PASSWORD>@ep-<endpoint>-pooler.<region>.aws.neon.tech/algo_control_center?sslmode=require
     ```
   - **Direct Connection String** *(no `-pooler` in hostname — used only by `atcc-db-migrate` for Alembic DDL & `pg_advisory_lock`)*:
     ```text
     postgresql+psycopg://<USER>:<PASSWORD>@ep-<endpoint>.<region>.aws.neon.tech/algo_control_center?sslmode=require
     ```

---

## 2. GCP Project Setup & Required APIs

Set shell variables (Bash / Cloud Shell shown; in PowerShell use `$PROJECT_ID = "..."`):

```bash
export PROJECT_ID="your-gcp-project-id"
export REGION="asia-south1"                  # Or us-central1 for maximum GCP Free Tier quota
export AR_REPO="algo-control-center"
export TAG="v1.0.0"

gcloud auth login
gcloud config set project "${PROJECT_ID}"
gcloud config set run/region "${REGION}"
```

### Enable Only Required Serverless APIs (Zero Paid Infrastructure APIs)
```bash
gcloud services enable \
  run.googleapis.com \
  artifactregistry.googleapis.com \
  cloudbuild.googleapis.com \
  secretmanager.googleapis.com \
  iam.googleapis.com
```

### Create Artifact Registry Repository (with Cost-Capping Cleanup Policy)
```bash
gcloud artifacts repositories create "${AR_REPO}" \
  --repository-format=docker \
  --location="${REGION}" \
  --description="Stateless containers for Algo Trading Control Center"
```

### Create Least-Privilege Service Accounts & Store Neon Secrets in Secret Manager
*(Never pass `DATABASE_URL` via plain-text `--set-env-vars` or commit it to `.env`.)*

```bash
# 1. Dedicated runtime service accounts (separate API vs UI — UI gets ZERO secret access!)
gcloud iam service-accounts create atcc-runtime-sa \
  --display-name="ATCC Backend Cloud Run SA"

gcloud iam service-accounts create atcc-ui-sa \
  --display-name="ATCC Frontend Cloud Run SA"

# 2. Store Neon Pooled & Direct URLs in Secret Manager (prompts securely on stdin)
printf "%s" "postgresql+psycopg://<USER>:<PASSWORD>@ep-<id>-pooler.<region>.aws.neon.tech/algo_control_center?sslmode=require" | \
  gcloud secrets create atcc-neon-database-url --data-file=- --replication-policy="automatic"

printf "%s" "postgresql+psycopg://<USER>:<PASSWORD>@ep-<id>.<region>.aws.neon.tech/algo_control_center?sslmode=require" | \
  gcloud secrets create atcc-neon-direct-url --data-file=- --replication-policy="automatic"

# 3. Grant Secret Accessor ONLY to the Backend/Migration Service Account
gcloud secrets add-iam-policy-binding atcc-neon-database-url \
  --member="serviceAccount:atcc-runtime-sa@${PROJECT_ID}.iam.gserviceaccount.com" \
  --role="roles/secretmanager.secretAccessor"

gcloud secrets add-iam-policy-binding atcc-neon-direct-url \
  --member="serviceAccount:atcc-runtime-sa@${PROJECT_ID}.iam.gserviceaccount.com" \
  --role="roles/secretmanager.secretAccessor"
```

---

## 3. Container Build & Image Push

### Option A (Recommended — Cloud Build, Zero Local Docker Required)
Uses [`deploy/cloudbuild.yaml`](../deploy/cloudbuild.yaml) to build and push both `Dockerfile.api` and `Dockerfile.ui` directly to Artifact Registry:

```bash
gcloud builds submit \
  --config=deploy/cloudbuild.yaml \
  --substitutions=_REGION="${REGION}",_AR_REPO="${AR_REPO}",_TAG="${TAG}" \
  .
```

### Option B (Local Docker Build + Push)
```bash
gcloud auth configure-docker "${REGION}-docker.pkg.dev"

docker build -f Dockerfile.api -t "${REGION}-docker.pkg.dev/${PROJECT_ID}/${AR_REPO}/atcc-api:${TAG}" .
docker build -f Dockerfile.ui  -t "${REGION}-docker.pkg.dev/${PROJECT_ID}/${AR_REPO}/atcc-ui:${TAG}" .

docker push "${REGION}-docker.pkg.dev/${PROJECT_ID}/${AR_REPO}/atcc-api:${TAG}"
docker push "${REGION}-docker.pkg.dev/${PROJECT_ID}/${AR_REPO}/atcc-ui:${TAG}"
```

---

## 4. Controlled Database Migration Execution (Zero Multi-Instance Race)

Application containers run with `AUTO_SEED_REFERENCE_DATA=false` and **never** auto-run DDL on startup. Instead, execute [`deploy/run_migrations.py`](../deploy/run_migrations.py) via a **one-shot Cloud Run Job** against the Neon **Direct** URL (acquires `pg_advisory_lock(84729104)` $\rightarrow$ `alembic upgrade head` $\rightarrow$ seeds `NIFTY`, `BANKNIFTY`, `PAPER-DESK-001`, and `v1` strategies):

```bash
# Create the controlled migration job (costs ₹0 when not executing)
gcloud run jobs create atcc-db-migrate \
  --image="${REGION}-docker.pkg.dev/${PROJECT_ID}/${AR_REPO}/atcc-api:${TAG}" \
  --region="${REGION}" \
  --service-account="atcc-runtime-sa@${PROJECT_ID}.iam.gserviceaccount.com" \
  --max-retries=1 \
  --task-timeout=180s \
  --cpu=1 \
  --memory=512Mi \
  --set-env-vars="APP_ENV=production,EXECUTION_MODE=PAPER_ONLY,TIMEZONE=Asia/Kolkata,DB_POOL_MODE=NULLPOOL,DB_SSLMODE=require" \
  --set-secrets="DATABASE_URL=atcc-neon-direct-url:latest" \
  --command="python" \
  --args="-m,deploy.run_migrations,--seed"

# Execute migration synchronously and wait for completion
gcloud run jobs execute atcc-db-migrate --region="${REGION}" --wait
```

*(Optional: append `--demo-showcase` to `--args="-m,deploy.run_migrations,--seed,--demo-showcase"` if you want the 17-step consulting demo showcase pre-populated immediately.)*

---

## 5. Backend Deployment (`atcc-api`)

```bash
gcloud run deploy atcc-api \
  --image="${REGION}-docker.pkg.dev/${PROJECT_ID}/${AR_REPO}/atcc-api:${TAG}" \
  --region="${REGION}" \
  --platform=managed \
  --service-account="atcc-runtime-sa@${PROJECT_ID}.iam.gserviceaccount.com" \
  --allow-unauthenticated \
  --port=8080 \
  --cpu=1 \
  --memory=512Mi \
  --concurrency=20 \
  --timeout=60s \
  --min-instances=0 \
  --max-instances=2 \
  --cpu-throttling \
  --cpu-boost \
  --set-env-vars="APP_ENV=production,LOG_LEVEL=INFO,LOG_JSON=true,EXECUTION_MODE=PAPER_ONLY,MARKET_MODE=HISTORICAL_AND_REPLAY,TIMEZONE=Asia/Kolkata,DB_POOL_MODE=NULLPOOL,DB_SSLMODE=require,DB_CONNECT_TIMEOUT_SECONDS=15,REPLAY_WORKER_ENABLED=false,AUTO_SEED_REFERENCE_DATA=false" \
  --set-secrets="DATABASE_URL=atcc-neon-database-url:latest"

# Capture the deployed Backend HTTPS URL
export API_URL=$(gcloud run services describe atcc-api --region="${REGION}" --format="value(status.url)")
echo "Backend URL: ${API_URL}"
```

---

## 6. Frontend Deployment (`atcc-ui`) & CORS Binding

```bash
# Deploy stateless Streamlit UI container pointing to ${API_URL}
gcloud run deploy atcc-ui \
  --image="${REGION}-docker.pkg.dev/${PROJECT_ID}/${AR_REPO}/atcc-ui:${TAG}" \
  --region="${REGION}" \
  --platform=managed \
  --service-account="atcc-ui-sa@${PROJECT_ID}.iam.gserviceaccount.com" \
  --allow-unauthenticated \
  --port=8080 \
  --cpu=1 \
  --memory=512Mi \
  --concurrency=15 \
  --timeout=300s \
  --min-instances=0 \
  --max-instances=2 \
  --cpu-throttling \
  --cpu-boost \
  --set-env-vars="API_BASE_URL=${API_URL},API_TIMEOUT_SECONDS=25.0"

# Capture Frontend HTTPS URL and lock Backend CORS_ALLOWED_ORIGINS to it
export UI_URL=$(gcloud run services describe atcc-ui --region="${REGION}" --format="value(status.url)")
echo "Frontend URL: ${UI_URL}"

gcloud run services update atcc-api \
  --region="${REGION}" \
  --update-env-vars="CORS_ALLOWED_ORIGINS=${UI_URL}"
```

---

## 7. Environment Variables Summary

### Backend (`atcc-api`)
| Variable | Cloud Run Value | Source |
| :--- | :--- | :--- |
| `DATABASE_URL` | `atcc-neon-database-url:latest` (Neon `-pooler` `?sslmode=require`) | **Secret Manager** |
| `EXECUTION_MODE` | `PAPER_ONLY` | Env Var |
| `MARKET_MODE` | `HISTORICAL_AND_REPLAY` | Env Var |
| `TIMEZONE` | `Asia/Kolkata` | Env Var |
| `DB_POOL_MODE` | `NULLPOOL` | Env Var |
| `DB_SSLMODE` | `require` | Env Var |
| `DB_CONNECT_TIMEOUT_SECONDS` | `15` | Env Var |
| `REPLAY_WORKER_ENABLED` | `false` | Env Var |
| `AUTO_SEED_REFERENCE_DATA` | `false` | Env Var |
| `FYERS_HISTORICAL_DATA_ENABLED` | `true` *(optional historical-data-only feature flag)* | Env Var |
| `FYERS_CLIENT_ID` | `<FYERS_APP_ID>` *(optional; historical OHLCV read-only)* | Secret Manager (`fyers-client-id:latest`) |
| `FYERS_ACCESS_TOKEN` | `<SHORT_LIVED_TOKEN>` *(optional; historical OHLCV read-only)* | Secret Manager (`fyers-access-token:latest`) |
| `CORS_ALLOWED_ORIGINS` | `https://<atcc-ui-url>.run.app` | Env Var |
| `PORT` | `8080` *(injected automatically by Cloud Run)* | Cloud Run Runtime |

### Frontend (`atcc-ui`)
| Variable | Cloud Run Value | Source |
| :--- | :--- | :--- |
| `API_BASE_URL` | `https://<atcc-api-url>.run.app` | Env Var |
| `API_TIMEOUT_SECONDS` | `25.0` *(absorbs double scale-from-zero cold start)* | Env Var |
| `PORT` | `8080` *(injected automatically by Cloud Run)* | Cloud Run Runtime |

---

## 8. Health & Deployment Verification Commands

```bash
# 1. Stateless Liveness Probe (wakes API container; verifies PAPER_ONLY & IST +05:30)
curl -fsS "${API_URL}/health/live" | python -m json.tool

# 2. Readiness & Neon PostgreSQL Probe (wakes Neon compute; verifies alembic_version=0007_phase5_operational_visibility & Asia/Kolkata TZ)
curl -fsS "${API_URL}/health" | python -m json.tool

# 3. Verify Zero Secret Leakage in Runtime Settings (masked_database_url must show `***`)
curl -fsS "${API_URL}/api/v1/settings" | python -m json.tool

# 4. Verify 17-Step Guided Demo Endpoint
curl -fsS "${API_URL}/api/v1/demo/status" | python -m json.tool

# 5. Verify Streamlit UI Container Health & HTTP 200 Entrypoint
curl -fsS "${UI_URL}/_stcore/health"
curl -I "${UI_URL}"

# 6. Verify Cloud Run Cost Guardrails (confirm minScale=0 and maxScale=2)
gcloud run services describe atcc-api --region="${REGION}" \
  --format="yaml(spec.template.metadata.annotations)"
gcloud run services describe atcc-ui --region="${REGION}" \
  --format="yaml(spec.template.metadata.annotations)"
```

---

## 9. Logs, Redeployment & Rollback

### Inspect Logs
```bash
# Stream/read structured JSON logs for Backend API
gcloud run services logs read atcc-api --region="${REGION}" --limit=50

# Read Streamlit UI logs
gcloud run services logs read atcc-ui --region="${REGION}" --limit=50

# Read Controlled Migration Job logs
gcloud run jobs executions list --job=atcc-db-migrate --region="${REGION}"
```

### Zero-Downtime Redeployment (`NEW_TAG="v1.0.1"`)
```bash
export NEW_TAG="v1.0.1"

# 1. Build & push new images
gcloud builds submit \
  --config=deploy/cloudbuild.yaml \
  --substitutions=_REGION="${REGION}",_AR_REPO="${AR_REPO}",_TAG="${NEW_TAG}" .

# 2. Update & run controlled migration job BEFORE shifting API traffic
gcloud run jobs update atcc-db-migrate \
  --image="${REGION}-docker.pkg.dev/${PROJECT_ID}/${AR_REPO}/atcc-api:${NEW_TAG}" \
  --region="${REGION}"
gcloud run jobs execute atcc-db-migrate --region="${REGION}" --wait

# 3. Roll out new Backend & Frontend revisions
gcloud run deploy atcc-api \
  --image="${REGION}-docker.pkg.dev/${PROJECT_ID}/${AR_REPO}/atcc-api:${NEW_TAG}" \
  --region="${REGION}"

gcloud run deploy atcc-ui \
  --image="${REGION}-docker.pkg.dev/${PROJECT_ID}/${AR_REPO}/atcc-ui:${NEW_TAG}" \
  --region="${REGION}"
```

### Instant Revision Rollback
```bash
# 1. List revisions to identify the last known-good revision name
gcloud run revisions list --service=atcc-api --region="${REGION}"
gcloud run revisions list --service=atcc-ui  --region="${REGION}"

# 2. Shift 100% of traffic back to the prior revision immediately (takes ~3 seconds)
gcloud run services update-traffic atcc-api \
  --region="${REGION}" \
  --to-revisions="atcc-api-00001-xyz=100"

gcloud run services update-traffic atcc-ui \
  --region="${REGION}" \
  --to-revisions="atcc-ui-00001-abc=100"
```

> [!CAUTION]
> **DESTRUCTIVE CLOUD OPERATIONS (DO NOT RUN UNLESS DECOMMISSIONING THE DEMO):**
> The commands below permanently delete deployed Cloud Run services, jobs, container images, or secrets. None of these have been executed.
> ```bash
> # [DESTRUCTIVE] Delete Cloud Run services & migration job
> gcloud run services delete atcc-ui --region="${REGION}"
> gcloud run services delete atcc-api --region="${REGION}"
> gcloud run jobs delete atcc-db-migrate --region="${REGION}"
>
> # [DESTRUCTIVE] Delete Artifact Registry repository & Secret Manager secrets
> gcloud artifacts repositories delete "${AR_REPO}" --location="${REGION}"
> gcloud secrets delete atcc-neon-database-url
> gcloud secrets delete atcc-neon-direct-url
> ```

---

## 10. DEPLOYMENT CHECKLIST

- [ ] **1. Secret Hygiene Verified:** `.env` is untracked/absent; `.dockerignore` excludes `.env*`; `.env.example` contains only fake `PLACEHOLDER_*` values; `pytest tests/integration/test_final_quality_gate_and_demo.py` passes.
- [ ] **2. Neon Project Provisioned:** Neon Free Plan Postgres 16 database created with 5-minute auto-suspend enabled; both `-pooler` and direct connection strings copied with `?sslmode=require`.
- [ ] **3. GCP APIs & Budget Alert Configured:** Only `run`, `artifactregistry`, `cloudbuild`, `secretmanager`, and `iam` APIs enabled; optional ₹100 / \$2 GCP Billing Budget alert configured.
- [ ] **4. Secrets Stored in Secret Manager:** `atcc-neon-database-url` (pooled) and `atcc-neon-direct-url` (direct) stored in Secret Manager; `roles/secretmanager.secretAccessor` granted **only** to `atcc-runtime-sa` (never to `atcc-ui-sa`).
- [ ] **5. Stateless Images Built & Pushed:** `atcc-api:${TAG}` (`Dockerfile.api`) and `atcc-ui:${TAG}` (`Dockerfile.ui`) pushed to Artifact Registry.
- [ ] **6. Controlled Migration Executed:** `atcc-db-migrate` Cloud Run Job executed to completion (`alembic_version = 0007_phase5_operational_visibility` + reference seed).
- [ ] **7. Backend Service Deployed (`atcc-api`):** Deployed with `--min-instances=0 --max-instances=2 --cpu-throttling`, `EXECUTION_MODE=PAPER_ONLY`, `DB_POOL_MODE=NULLPOOL`, `REPLAY_WORKER_ENABLED=false`, `AUTO_SEED_REFERENCE_DATA=false`.
- [ ] **8. Frontend Service Deployed (`atcc-ui`):** Deployed with `--min-instances=0 --max-instances=2 --cpu-throttling` and `API_BASE_URL=${API_URL}`; `atcc-api` `CORS_ALLOWED_ORIGINS` updated to `${UI_URL}`.

---

## 11. POST-DEPLOYMENT VALIDATION CHECKLIST

- [ ] **1. Cold-Start Wake-Up (< 10s):** After idle scale-to-zero, opening `${UI_URL}` wakes `atcc-ui` $\rightarrow$ `atcc-api` $\rightarrow$ Neon compute cleanly without timeout (`API_TIMEOUT_SECONDS=25.0`).
- [ ] **2. Safety Mode & Headers:** `${API_URL}/health/live` returns `execution_mode: "PAPER_ONLY"`, `timezone: "Asia/Kolkata"`, `+05:30` timestamp, and `X-Execution-Mode: PAPER_ONLY` header; UI displays persistent `SIMULATED PAPER TRADING | NO REAL ORDERS` banner across all 12 screens.
- [ ] **3. Neon SSL, Timezone & Schema Head:** `${API_URL}/health` returns `database.status: "HEALTHY"`, `db_timezone: "Asia/Kolkata"`, `alembic_version: "0007_phase5_operational_visibility"`, and all `9` operational components present.
- [ ] **4. Zero Credential Exposure:** `${API_URL}/api/v1/settings` and UI **Settings** screen display `postgresql+psycopg://<user>:***@...` with password masked.
- [ ] **5. End-to-End 3–5 Min Consulting Demo:** Clicking **Guided 3–5 Min Consulting Demo $\rightarrow$ LOAD DETERMINISTIC DEMO SHOWCASE** populates `17/17` steps (`Backtest`, `Market Replay`, `Signal`, `Risk Approval`, `OMS Order`, `Paper Fill`, `Position`, `P&L`, `Risk Rejection`, `Stale-Data Block`, `Recovery`, `Reconciliation`, and `8-Stage Audit Trace`).
- [ ] **6. Scale-to-Zero Persistence Survival:** Wait ~15 minutes for Cloud Run instances and Neon compute to scale to `0`; reopen `${UI_URL}` and verify Global Kill Switch state, positions, P&L, backtests, and append-only audit records persist intact in Neon PostgreSQL.
- [ ] **7. Cost Guardrail Confirmation:** Confirm in GCP Console that `min-instances = 0`, `max-instances = 2`, CPU is `CPU is only allocated during request processing`, and active instance count drops to `0` when browser tabs are closed.
