# Render Free + Neon PostgreSQL Deployment Assets (`deploy/render/`)

This directory contains operational documentation and runbooks for hosting **Algo Trading Control Center** (`EXECUTION_MODE=PAPER_ONLY`) on **Render Free Web Services + Neon Serverless PostgreSQL** with zero cloud billing setup required.

## Coexistence with Google Cloud Run

This repository supports **two** first-class stateless container deployment targets using the exact same [`Dockerfile.api`](../../Dockerfile.api) and [`Dockerfile.ui`](../../Dockerfile.ui):

1. **Google Cloud Run + Neon PostgreSQL** *(preserved intact)*:
   - Guide: [`docs/CLOUD_RUN_NEON_DEPLOYMENT.md`](../../docs/CLOUD_RUN_NEON_DEPLOYMENT.md)
   - Manifests: [`deploy/cloudbuild.yaml`](../cloudbuild.yaml), [`deploy/cloudrun-api.yaml`](../cloudrun-api.yaml), [`deploy/cloudrun-ui.yaml`](../cloudrun-ui.yaml)
2. **Render Free + Neon PostgreSQL** *(zero billing card required for Free Web Services)*:
   - Step-by-Step Guide: [`RENDER_NEON_DEPLOYMENT.md`](../../RENDER_NEON_DEPLOYMENT.md)
   - Blueprint Infrastructure-as-Code: [`render.yaml`](../../render.yaml)
   - Controlled Migration Runbook: [`deploy/render/MIGRATIONS.md`](./MIGRATIONS.md)
   - Post-Deployment Verification Suite: [`deploy/render/verification.md`](./verification.md)

## Deployed Render Services

| Service Name | Runtime | Dockerfile | Health Check Path | Database Access |
| :--- | :--- | :--- | :--- | :--- |
| **`atcc-api`** | Docker (`plan: free`) | `Dockerfile.api` | `/health/live` | Neon **Pooled** PostgreSQL (`DATABASE_URL`, `DB_POOL_MODE=NULLPOOL`, `DB_SSLMODE=require`) |
| **`atcc-ui`** | Docker (`plan: free`) | `Dockerfile.ui` | `/_stcore/health` | **None** — Stateless HTTP client communicating strictly via `API_BASE_URL` |

## Scale-to-Zero & Ephemeral Container Design

Render Free Web Services spin down after 15 minutes of inactivity and discard local container memory/filesystem on restart:
- **Persistent State in Neon PostgreSQL:** Strategy versions, paper account balances, Global Kill Switch state, OMS orders, execution fills, execution-derived positions, P&L ledger inputs, risk configurations/decisions, reconciliation runs, imported CSV datasets, and append-only audit logs are stored exclusively in Neon PostgreSQL.
- **No Keep-Alive Hacks:** Neither `atcc-ui` nor `atcc-api` emits artificial keep-alive pings. When waking from sleep, `atcc-ui` displays a professional startup notice with a bounded finite retry (`max_attempts=3`) and a manual **RETRY CONNECTION** button.
