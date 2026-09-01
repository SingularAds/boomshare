"""Time helpers.

All timestamps in this application are timezone-aware UTC. SQLite (used in
tests) hands datetimes back naive, so `as_utc` normalises anything we read from
the database before comparing it.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta


def utcnow() -> datetime:
    return datetime.now(UTC)


def as_utc(value: datetime | None) -> datetime | None:
    if value is None:
        return None
    if value.tzinfo is None:
        return value.replace(tzinfo=UTC)
    return value.astimezone(UTC)


def hours_from_now(hours: float) -> datetime:
    return utcnow() + timedelta(hours=hours)
