"""Transactional Unit of Work managing SQLAlchemy session boundaries."""

from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager

from sqlalchemy.exc import OperationalError, SQLAlchemyError
from sqlalchemy.orm import Session, sessionmaker

from core.config import AppSettings
from core.exceptions import ControlCenterError, DatabaseUnavailableError
from core.persistence.db import get_session_factory


@contextmanager
def unit_of_work(
    *,
    session_factory: sessionmaker[Session] | None = None,
    settings: AppSettings | None = None,
) -> Iterator[Session]:
    """Provide an atomic database transaction scope.

    Commits automatically when the block exits cleanly; rolls back on any error.
    """
    factory = session_factory or get_session_factory(settings)
    session = factory()
    try:
        yield session
        session.commit()
    except ControlCenterError:
        session.rollback()
        raise
    except OperationalError as exc:
        session.rollback()
        raise DatabaseUnavailableError(f"Database operational error: {exc}") from exc
    except SQLAlchemyError as exc:
        session.rollback()
        raise ControlCenterError(f"Database transaction failed: {exc}") from exc
    except Exception:
        session.rollback()
        raise
    finally:
        session.close()
