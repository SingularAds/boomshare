"""Small persistence helpers shared by the services."""

from __future__ import annotations

from typing import Any, TypeVar

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.db import Base

T = TypeVar("T", bound=Base)


async def insert_or_get(
    session: AsyncSession,
    model: type[T],
    defaults: dict[str, Any] | None = None,
    **unique_filters: Any,
) -> tuple[T, bool]:
    """Fetch a row by its unique key, inserting it if it is missing.

    Race-safe: two workers processing a retried webhook at the same time will
    both end up with the same row, because the loser of the insert race catches
    the unique-constraint violation inside a savepoint and re-reads.

    Returns `(row, created)`.
    """
    stmt = select(model).filter_by(**unique_filters)
    existing = (await session.execute(stmt)).scalar_one_or_none()
    if existing is not None:
        return existing, False

    instance = model(**unique_filters, **(defaults or {}))
    try:
        async with session.begin_nested():
            session.add(instance)
            await session.flush()
    except IntegrityError:
        existing = (await session.execute(stmt)).scalar_one()
        return existing, False
    return instance, True


def apply_if_missing(instance: Any, values: dict[str, Any]) -> list[str]:
    """Fill in attributes that are currently empty. Never overwrites known data.

    Used when a later signal (a lead form, a WhatsApp profile) tells us
    something we did not have before, without clobbering what we already know.
    """
    changed: list[str] = []
    for key, value in values.items():
        if value in (None, "", {}, []):
            continue
        if getattr(instance, key, None) in (None, "", {}, []):
            setattr(instance, key, value)
            changed.append(key)
    return changed


def merge_json(current: dict | None, updates: dict | None) -> dict:
    """Merge into a JSON column, returning a new dict.

    A new object is required: SQLAlchemy will not notice an in-place mutation of
    a JSON column, and the update would silently not be persisted.
    """
    merged = dict(current or {})
    merged.update(updates or {})
    return merged
