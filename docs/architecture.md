# Algo Trading Control Center — Architecture Overview

## Layered Stateless Design

1. **Presentation (`frontend/`)**: Stateless Streamlit operations console. Owns zero trading state and imports nothing from `core/` or `backend/`. Communicates exclusively over HTTP with FastAPI via `API_BASE_URL`.
2. **Transport (`backend/`)**: Stateless FastAPI service handling `X-Correlation-ID` propagation, request validation, health probes (`/health/live`, `/health/ready`, `/api/v1/health`), and error mapping.
3. **Domain & Core Services (`core/`)**: Pure Python domain entities, lifecycle state machines, immutable strategy versioning, operational controls (`STOP PAPER TRADING`), append-only audit service, and SQLAlchemy 2.0 Unit-of-Work persistence.
4. **Persistence (`PostgreSQL / Neon`)**: Authoritative ACID state store with `SET TIME ZONE 'Asia/Kolkata'` enforced on every connection and database-level immutability triggers on `audit_events` and `strategy_versions`.

## Operational Safety Invariants

- **Paper Only (`V1`)**: `EXECUTION_MODE` is strictly validated as `PAPER_ONLY`. Real-money execution adapters do not exist in V1.
- **IST Everywhere**: All timestamps across database sessions, domain models, logs, API JSON payloads, and UI blotters operate in `Asia/Kolkata` (`+05:30`).
- **`STOP PAPER TRADING` Boundary**: Engaging `STOP PAPER TRADING` atomically halts generation of **new** simulated orders (`paper_accounts.paper_trading_halted = true`) while leaving any open positions untouched. Flattening open positions is strictly an explicit separate operator command.
