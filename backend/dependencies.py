"""FastAPI dependency injection providers for settings, correlation IDs, and DB sessions."""

from __future__ import annotations

from collections.abc import Iterator

from fastapi import Depends
from sqlalchemy.orm import Session

from core.config import AppSettings, get_settings
from core.logging import get_correlation_id
from core.persistence.uow import unit_of_work


def provide_settings() -> AppSettings:
    """Inject cached application settings."""
    return get_settings()


def provide_correlation_id() -> str:
    """Inject active request correlation ID."""
    return get_correlation_id()


def provide_db_session(
    settings: AppSettings = Depends(provide_settings),
) -> Iterator[Session]:
    """Provide a transactional SQLAlchemy session scoped to the HTTP request."""
    with unit_of_work(settings=settings) as session:
        yield session
