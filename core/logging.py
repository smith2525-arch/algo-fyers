"""Structured logging with correlation ID propagation and IST timestamps."""

from __future__ import annotations

import contextvars
import json
import logging
import sys
import uuid
from typing import Any

from core.clock import format_ist_iso, now_ist

_correlation_id_ctx: contextvars.ContextVar[str] = contextvars.ContextVar(
    "correlation_id",
    default="",
)

_RESERVED_LOG_ATTRS = frozenset(
    {
        "args",
        "asctime",
        "created",
        "exc_info",
        "exc_text",
        "filename",
        "funcName",
        "levelname",
        "levelno",
        "lineno",
        "module",
        "msecs",
        "message",
        "msg",
        "name",
        "pathname",
        "process",
        "processName",
        "relativeCreated",
        "stack_info",
        "thread",
        "threadName",
        "taskName",
    }
)


def new_correlation_id() -> str:
    """Generate a new UUIDv4 correlation identifier."""
    return str(uuid.uuid4())


def get_correlation_id() -> str:
    """Fetch current execution context's correlation ID, generating one if unset."""
    cid = _correlation_id_ctx.get()
    if not cid:
        cid = new_correlation_id()
        _correlation_id_ctx.set(cid)
    return cid


def set_correlation_id(correlation_id: str | None = None) -> contextvars.Token[str]:
    """Bind a correlation ID into the current execution context."""
    clean = (correlation_id or "").strip() or new_correlation_id()
    return _correlation_id_ctx.set(clean)


def reset_correlation_id(token: contextvars.Token[str]) -> None:
    """Restore previous correlation ID context."""
    _correlation_id_ctx.reset(token)


class ISTJsonFormatter(logging.Formatter):
    """Formats log records as single-line JSON with Asia/Kolkata (+05:30) timestamps."""

    def __init__(self, service_name: str = "algo-control-center") -> None:
        super().__init__()
        self._service_name = service_name

    def format(self, record: logging.LogRecord) -> str:
        payload: dict[str, Any] = {
            "timestamp": format_ist_iso(now_ist()),
            "timezone": "Asia/Kolkata",
            "level": record.levelname,
            "service": self._service_name,
            "logger": record.name,
            "message": record.getMessage(),
            "correlation_id": _correlation_id_ctx.get() or None,
        }
        for key, value in record.__dict__.items():
            if key not in _RESERVED_LOG_ATTRS and not key.startswith("_"):
                payload[key] = value

        if record.exc_info:
            payload["exception"] = self.formatException(record.exc_info)
        return json.dumps(payload, default=str, ensure_ascii=False)


class ISTConsoleFormatter(logging.Formatter):
    """Compact human-readable IST console formatter for local debugging."""

    def format(self, record: logging.LogRecord) -> str:
        ts = format_ist_iso(now_ist())
        cid = _correlation_id_ctx.get() or "-"
        base = f"{ts} | {record.levelname:<8} | cid={cid[:8]} | {record.name} | {record.getMessage()}"
        if record.exc_info:
            return f"{base}\n{self.formatException(record.exc_info)}"
        return base


def configure_logging(
    *,
    level: str = "INFO",
    json_logs: bool = True,
    service_name: str = "algo-control-center",
) -> None:
    """Idempotently configure root structured logging."""
    root = logging.getLogger()
    root.setLevel(getattr(logging, level.upper(), logging.INFO))

    handler = logging.StreamHandler(sys.stdout)
    handler.setFormatter(
        ISTJsonFormatter(service_name=service_name) if json_logs else ISTConsoleFormatter()
    )

    root.handlers.clear()
    root.addHandler(handler)

    # Reduce noisy third-party loggers while keeping warnings/errors visible
    logging.getLogger("uvicorn.access").setLevel(logging.WARNING)
    logging.getLogger("httpx").setLevel(logging.WARNING)
