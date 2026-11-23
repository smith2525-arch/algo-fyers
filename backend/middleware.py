"""FastAPI middleware for Correlation ID propagation, IST request logging, and safety headers."""

from __future__ import annotations

import logging
import time
from collections.abc import Awaitable, Callable

from fastapi import Request, Response
from starlette.middleware.base import BaseHTTPMiddleware

from core.logging import reset_correlation_id, set_correlation_id

logger = logging.getLogger("backend.http")


class CorrelationAndSafetyMiddleware(BaseHTTPMiddleware):
    """Binds X-Correlation-ID per request and attaches institutional safety headers."""

    async def dispatch(
        self, request: Request, call_next: Callable[[Request], Awaitable[Response]]
    ) -> Response:
        incoming_cid = request.headers.get("X-Correlation-ID")
        token = set_correlation_id(incoming_cid)
        cid = request.headers.get("X-Correlation-ID") or ""
        from core.logging import get_correlation_id

        cid = get_correlation_id()
        start = time.perf_counter()
        try:
            response = await call_next(request)
        finally:
            duration_ms = round((time.perf_counter() - start) * 1000.0, 2)
            logger.info(
                "HTTP request processed",
                extra={
                    "method": request.method,
                    "path": request.url.path,
                    "duration_ms": duration_ms,
                },
            )
            reset_correlation_id(token)

        response.headers["X-Correlation-ID"] = cid
        response.headers["X-Execution-Mode"] = "PAPER_ONLY"
        response.headers["X-Timezone"] = "Asia/Kolkata"
        return response
