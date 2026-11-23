"""FastAPI application factory, startup recovery/seeding lifespan, and error handlers."""

from __future__ import annotations

import asyncio
import logging
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from fastapi import FastAPI, Request, status
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse

from backend.api.v1 import (
    audit,
    backtests,
    controls,
    dashboard,
    demo,
    health,
    historical_data,
    instruments,
    orders,
    paper_trading,
    pnl,
    positions,
    reconciliation,
    replay,
    risk,
    settings as settings_router,
    strategies,
)
from backend.middleware import CorrelationAndSafetyMiddleware
from core.audit.service import AuditService
from core.config import get_settings
from core.domain.enums import (
    ActorType,
    AuditCategory,
    AuditSeverity,
    SystemComponent,
    SystemComponentStatus,
)
from core.exceptions import (
    BrokerRejectedError,
    ControlCenterError,
    DatabaseUnavailableError,
    DatasetInUseError,
    DirectPositionMutationForbiddenError,
    DuplicateEntityError,
    EntityNotFoundError,
    FyersAuthenticationRequiredError,
    HistoricalProviderUnavailableError,
    ImmutabilityViolationError,
    InvalidStateTransitionError,
    PaperTradingHaltedError,
    RiskRejectedError,
    StaleMarketDataError,
)
from core.health.service import HealthService
from core.logging import configure_logging, get_correlation_id, set_correlation_id
from core.market_data.service import MarketReplayService, run_replay_background_worker
from core.paper_trading.service import PaperTradingService
from core.persistence.db import probe_database, reset_engine
from core.persistence.seed import seed_foundation_data
from core.persistence.uow import unit_of_work

logger = logging.getLogger("backend.main")


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    cfg = get_settings()
    configure_logging(
        level=cfg.LOG_LEVEL,
        json_logs=cfg.LOG_JSON,
        service_name="algo-control-center-api",
    )
    token = set_correlation_id()
    logger.info(
        "Starting Algo Trading Control Center API",
        extra={
            "env": cfg.APP_ENV,
            "version": cfg.APP_VERSION,
            "execution_mode": cfg.EXECUTION_MODE,
            "timezone": cfg.TIMEZONE,
            "database": cfg.masked_database_url,
        },
    )

    probe = probe_database(cfg)
    if probe["reachable"]:
        try:
            with unit_of_work(settings=cfg) as session:
                if cfg.AUTO_SEED_REFERENCE_DATA:
                    seed_foundation_data(session, cfg)
                recovered_replays = MarketReplayService.recover_interrupted_sessions_on_startup(
                    session
                )
                paper_recovery = PaperTradingService.recover_and_reconcile_on_startup(
                    session, settings=cfg
                )
                AuditService.record(
                    session,
                    action="API_SERVICE_STARTED",
                    summary="FastAPI backend started and verified PostgreSQL connectivity.",
                    actor="system.lifespan",
                    actor_type=ActorType.SYSTEM,
                    category=AuditCategory.SYSTEM,
                    severity=AuditSeverity.INFO,
                    entity_type="service",
                    entity_id="fastapi-backend",
                    details={
                        "version": cfg.APP_VERSION,
                        "alembic_version": probe.get("alembic_version"),
                        "db_timezone": probe.get("db_timezone"),
                        "recovered_replay_sessions": recovered_replays,
                        "paper_recovery": paper_recovery,
                    },
                )
                HealthService.record_health_event(
                    session,
                    component=SystemComponent.DATABASE,
                    status=SystemComponentStatus.HEALTHY,
                    latency_ms=probe["latency_ms"],
                    message="Startup PostgreSQL verification succeeded.",
                    details={
                        "alembic_version": probe.get("alembic_version"),
                        "db_timezone": probe.get("db_timezone"),
                    },
                )
        except Exception as exc:  # noqa: BLE001
            logger.warning(
                "Database reachable but foundation tables not yet initialized (run `alembic upgrade head`)",
                extra={"error": str(exc)},
            )
    else:
        logger.warning(
            "Starting API in degraded database state; readiness probe will report 503 until DB connects.",
            extra={"reason": probe.get("error")},
        )

    from core.logging import reset_correlation_id

    reset_correlation_id(token)

    stop_worker_event = asyncio.Event()
    worker_task = asyncio.create_task(run_replay_background_worker(stop_worker_event))
    try:
        yield
    finally:
        stop_worker_event.set()
        worker_task.cancel()
        try:
            await worker_task
        except asyncio.CancelledError:
            pass

    logger.info("Shutting down Algo Trading Control Center API")
    reset_engine()


def create_app() -> FastAPI:
    cfg = get_settings()
    application = FastAPI(
        title=cfg.APP_NAME,
        version=cfg.APP_VERSION,
        description=(
            "Stateless operational control & trading automation API. "
            "PAPER TRADING ONLY — NO REAL ORDERS."
        ),
        lifespan=lifespan,
    )

    application.add_middleware(CorrelationAndSafetyMiddleware)
    application.add_middleware(
        CORSMiddleware,
        allow_origins=cfg.cors_origins_list,
        allow_credentials=True,
        allow_methods=["*"],
        allow_headers=["*"],
    )

    @application.exception_handler(EntityNotFoundError)
    async def _handle_not_found(request: Request, exc: EntityNotFoundError) -> JSONResponse:
        return JSONResponse(
            status_code=status.HTTP_404_NOT_FOUND,
            content={
                "error": exc.code,
                "message": str(exc),
                "correlation_id": get_correlation_id(),
            },
        )

    @application.exception_handler(DatabaseUnavailableError)
    async def _handle_db_unavailable(
        request: Request, exc: DatabaseUnavailableError
    ) -> JSONResponse:
        return JSONResponse(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            content={
                "error": exc.code,
                "message": str(exc),
                "correlation_id": get_correlation_id(),
            },
        )

    async def _handle_conflict(request: Request, exc: Exception) -> JSONResponse:
        code = getattr(exc, "code", "CONFLICT")
        return JSONResponse(
            status_code=status.HTTP_409_CONFLICT,
            content={
                "error": code,
                "message": str(exc),
                "correlation_id": get_correlation_id(),
            },
        )

    @application.exception_handler(FyersAuthenticationRequiredError)
    async def _handle_fyers_auth_required(
        request: Request, exc: FyersAuthenticationRequiredError
    ) -> JSONResponse:
        return JSONResponse(
            status_code=status.HTTP_401_UNAUTHORIZED,
            content={
                "error": exc.code,
                "message": str(exc),
                "correlation_id": get_correlation_id(),
            },
        )

    @application.exception_handler(HistoricalProviderUnavailableError)
    async def _handle_provider_unavailable(
        request: Request, exc: HistoricalProviderUnavailableError
    ) -> JSONResponse:
        return JSONResponse(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            content={
                "error": exc.code,
                "message": str(exc),
                "correlation_id": get_correlation_id(),
            },
        )

    for conflict_exc in (
        InvalidStateTransitionError,
        ImmutabilityViolationError,
        DatasetInUseError,
        PaperTradingHaltedError,
        StaleMarketDataError,
        DuplicateEntityError,
        RiskRejectedError,
        BrokerRejectedError,
        DirectPositionMutationForbiddenError,
    ):
        application.add_exception_handler(conflict_exc, _handle_conflict)

    @application.exception_handler(ControlCenterError)
    async def _handle_domain_error(request: Request, exc: ControlCenterError) -> JSONResponse:
        return JSONResponse(
            status_code=status.HTTP_400_BAD_REQUEST,
            content={
                "error": exc.code,
                "message": str(exc),
                "correlation_id": get_correlation_id(),
            },
        )

    application.include_router(health.router)
    application.include_router(dashboard.router)
    application.include_router(controls.router)
    application.include_router(instruments.router)
    application.include_router(strategies.router)
    application.include_router(historical_data.router)
    application.include_router(backtests.router)
    application.include_router(replay.router)
    application.include_router(paper_trading.router)
    application.include_router(orders.router)
    application.include_router(positions.router)
    application.include_router(risk.router)
    application.include_router(pnl.router)
    application.include_router(reconciliation.router)
    application.include_router(audit.router)
    application.include_router(demo.router)
    application.include_router(settings_router.router)

    return application


app = create_app()
