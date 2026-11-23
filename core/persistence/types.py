"""Custom SQLAlchemy column types enforcing IST (+05:30) everywhere."""

from __future__ import annotations

from datetime import datetime
from typing import Any

from sqlalchemy import DateTime
from sqlalchemy.engine import Dialect
from sqlalchemy.types import TypeDecorator

from core.clock import ensure_ist


class ISTDateTime(TypeDecorator[datetime]):
    """Timezone-aware DateTime column normalizing all reads and writes to IST (+05:30)."""

    impl = DateTime(timezone=True)
    cache_ok = True

    def process_bind_param(self, value: datetime | None, dialect: Dialect) -> datetime | None:
        if value is None:
            return None
        return ensure_ist(value)

    def process_result_value(self, value: Any, dialect: Dialect) -> datetime | None:
        if value is None:
            return None
        if isinstance(value, str):
            value = datetime.fromisoformat(value)
        if isinstance(value, datetime):
            return ensure_ist(value)
        raise TypeError(f"Unexpected datetime column value type: {type(value)!r}")
