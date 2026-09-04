"""Database engine, session factory and shared column types."""

from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from typing import Any

from sqlalchemy import JSON, MetaData, event
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)
from sqlalchemy.orm import DeclarativeBase

from app.core.config import Settings, get_settings

# JSONB on PostgreSQL (indexable, our production target), plain JSON on SQLite
# so the same models can run against the in-memory database used by tests.
JsonB = JSON().with_variant(JSONB(), "postgresql")

NAMING_CONVENTION = {
    "ix": "ix_%(table_name)s_%(column_0_N_name)s",
    "uq": "uq_%(table_name)s_%(column_0_N_name)s",
    "ck": "ck_%(table_name)s_%(constraint_name)s",
    "fk": "fk_%(table_name)s_%(column_0_name)s_%(referred_table_name)s",
    "pk": "pk_%(table_name)s",
}


class Base(DeclarativeBase):
    metadata = MetaData(naming_convention=NAMING_CONVENTION)


_engine: AsyncEngine | None = None
_session_factory: async_sessionmaker[AsyncSession] | None = None


def _engine_kwargs(settings: Settings) -> dict[str, Any]:
    kwargs: dict[str, Any] = {"echo": settings.db_echo, "pool_pre_ping": True}
    if settings.database_url.startswith("postgresql"):
        kwargs["pool_size"] = settings.db_pool_size
        kwargs["max_overflow"] = settings.db_max_overflow
    if settings.database_url.startswith("sqlite"):
        # Wait for a busy database rather than failing instantly. See
        # `configure_sqlite` for why this only helps once transactions are
        # IMMEDIATE.
        kwargs["connect_args"] = {"check_same_thread": False, "timeout": 30}
    return kwargs


def configure_sqlite(engine: AsyncEngine) -> None:
    """Make SQLite behave enough like PostgreSQL to run this application.

    SQLite is not the production database. It is what the test suite and
    `scripts/e2e_smoke.py` run against, so neither needs a server - which is
    only worth anything if the application behaves the same way on it.

    Two defaults have to change:

    * **No implicit transactions.** pysqlite (and so aiosqlite) opens its own,
      which breaks SAVEPOINT - and `services.base.insert_or_get` relies on
      savepoints for its race-safe insert.
    * **`BEGIN IMMEDIATE` rather than the default deferred begin.** A deferred
      transaction takes a read lock and upgrades to a write lock when it first
      writes, and SQLite refuses that upgrade with SQLITE_BUSY *without
      consulting the busy timeout*, because waiting there could deadlock. Every
      transaction in this application reads before it writes, so two workers
      running at once hit it immediately and the timeout above never gets a
      say. IMMEDIATE takes the write lock up front, which is a case the busy
      handler does wait on, so concurrent workers serialise instead of failing.
      PostgreSQL needs none of this: MVCC and row-level locks give the same
      outcome at a much finer grain.
    """

    @event.listens_for(engine.sync_engine, "connect")
    def _disable_implicit_begin(dbapi_connection: Any, _record: Any) -> None:
        dbapi_connection.isolation_level = None

    @event.listens_for(engine.sync_engine, "begin")
    def _begin_immediate(connection: Any) -> None:
        connection.exec_driver_sql("BEGIN IMMEDIATE")


def get_engine() -> AsyncEngine:
    global _engine
    if _engine is None:
        settings = get_settings()
        _engine = create_async_engine(settings.database_url, **_engine_kwargs(settings))
        if settings.database_url.startswith("sqlite"):
            configure_sqlite(_engine)
    return _engine


def get_session_factory() -> async_sessionmaker[AsyncSession]:
    global _session_factory
    if _session_factory is None:
        _session_factory = async_sessionmaker(
            bind=get_engine(), expire_on_commit=False, autoflush=False
        )
    return _session_factory


def set_session_factory(factory: async_sessionmaker[AsyncSession] | None) -> None:
    """Point the application at a different engine (used by the test suite)."""
    global _session_factory
    _session_factory = factory


@asynccontextmanager
async def session_scope() -> AsyncIterator[AsyncSession]:
    """Transactional session for background work: commit on success, rollback on error."""
    async with get_session_factory()() as session:
        try:
            yield session
            await session.commit()
        except Exception:
            await session.rollback()
            raise


async def get_db() -> AsyncIterator[AsyncSession]:
    """FastAPI dependency."""
    async with session_scope() as session:
        yield session


async def dispose_engine() -> None:
    global _engine, _session_factory
    if _engine is not None:
        await _engine.dispose()
    _engine = None
    _session_factory = None
