"""Test fixtures.

The whole point of the integration boundaries is visible here: the entire
application runs in-process against SQLite and fakeredis, with fake Meta and
OpenAI clients injected through the same setters production uses. No test
touches the network, and sales-logic changes can be verified without spending a
token or a WhatsApp message.
"""

from __future__ import annotations

import json
import os
from collections.abc import AsyncIterator

# Settings are read once and cached, so the environment has to be right before
# anything imports app.core.config.
#
# The suite configures the application entirely from here. Reading the
# developer's `.env` as well would mean a test passing or failing on whichever
# market, template translation or ladder they happen to have configured
# locally - and passing for a reason CI, which has no `.env`, does not share.
os.environ.setdefault("BOOMSHARE_ENV_FILE", "")
os.environ.setdefault("ENVIRONMENT", "test")
os.environ.setdefault("DATABASE_URL", "sqlite+aiosqlite:///:memory:")
os.environ.setdefault("REDIS_URL", "redis://localhost:6379/15")
os.environ.setdefault("META_APP_SECRET", "test-app-secret")
os.environ.setdefault("META_VERIFY_TOKEN", "test-verify-token")
os.environ.setdefault("META_ACCESS_TOKEN", "test-access-token")
# Two numbers throughout, so nothing can pass by assuming there is one.
# The first is the default sender and matches `scripts/fake_meta.py`.
os.environ.setdefault("WHATSAPP_PHONE_NUMBER_IDS", '["111222333", "444555666"]')
os.environ.setdefault("META_GRAPH_VERSION", "v26.0")
os.environ.setdefault("OPENAI_API_KEY", "sk-test")
os.environ.setdefault("INTERNAL_API_TOKEN", "test-internal-token")
os.environ.setdefault("ADMIN_API_TOKEN", "test-admin-token")
os.environ.setdefault("DASHBOARD_API_TOKEN", "test-dashboard-token")
os.environ.setdefault("DOWNLOAD_BASE_URL", "https://boomshare.test/download")
os.environ.setdefault("LOG_JSON", "false")
os.environ.setdefault("LOG_LEVEL", "WARNING")
# Language policy is asserted on directly, so it is stated here rather than
# inherited from whichever `.env` the developer running the suite happens to
# have. Country defaults are CLDR's until a test says otherwise, and no
# template translation is approved until a test approves one.
os.environ.setdefault("COUNTRY_LANGUAGE_OVERRIDES", "{}")
os.environ.setdefault("WHATSAPP_TEMPLATE_LANGUAGES", "{}")
# The bodies Meta approved, as deployed - `scripts/template_config.py` reads
# them straight off the account. A template message is stored as these words,
# so what a test asserts a customer was shown is checked against the same text
# production sends.
os.environ.setdefault(
    "WHATSAPP_TEMPLATE_BODIES",
    json.dumps(
        {
            "boomshare_lead_intro": {
                "en": "Hi {{1}}, thanks for your interest in Boomshare! "
                "Happy to answer any questions - what made you look into it?",
                "pt_BR": "Olá {{1}}, obrigado pelo seu interesse no Boomshare! "
                "Fico feliz em tirar qualquer dúvida — o que fez você se interessar por ele?",
                "es": "¡Hola {{1}}! Gracias por tu interés en Boomshare. "
                "Con gusto respondo cualquier duda. ¿Qué te llevó a buscar una herramienta como esta?",
            },
            "boomshare_followup": {
                "en": "Hi {{1}}, just checking in about Boomshare. "
                "Still interested? Happy to help whenever suits.",
                "pt_BR": "Olá {{1}}, só passando para saber sobre o Boomshare. "
                "Ainda tem interesse? Fico feliz em ajudar quando for melhor para você.",
                "es": "Hola {{1}}, solo paso para ver qué tal con Boomshare. "
                "¿Sigues interesado? Con gusto te ayudo cuando te convenga.",
            },
        }
    ),
)

import fakeredis.aioredis  # noqa: E402
import pytest  # noqa: E402
import pytest_asyncio  # noqa: E402
from httpx import ASGITransport, AsyncClient  # noqa: E402
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine  # noqa: E402

import app.models  # noqa: F401,E402  - registers the tables on Base.metadata
from app.core import redis as redis_helper  # noqa: E402
from app.core.config import get_settings  # noqa: E402
from app.core.db import Base, configure_sqlite, set_session_factory  # noqa: E402
from app.integrations.meta.client import set_meta_client  # noqa: E402
from app.integrations.openai.client import set_sales_model  # noqa: E402
from app.main import create_app  # noqa: E402
from tests.fakes import BrokenRedis, FakeMetaClient, FakeSalesModel  # noqa: E402

# The event loop is managed by pytest-asyncio (asyncio_mode = "auto",
# asyncio_default_fixture_loop_scope = "function" in pyproject.toml). Do not add
# a custom `event_loop` fixture back: it is deprecated and every async fixture
# here is function-scoped anyway.


@pytest_asyncio.fixture
async def engine(tmp_path):
    """A fresh database per test.

    A file rather than `:memory:` on purpose: a single flow opens several
    sessions (ingest, reply, worker) and they must be able to hold independent
    connections, exactly as they do against PostgreSQL.
    """
    engine = create_async_engine(
        f"sqlite+aiosqlite:///{tmp_path / 'test.db'}",
        connect_args={"check_same_thread": False, "timeout": 30},
    )
    # The same SQLite setup the application applies to its own engine, so the
    # tests exercise the database the way the app actually talks to it.
    configure_sqlite(engine)

    async with engine.begin() as connection:
        await connection.run_sync(Base.metadata.create_all)

    yield engine
    await engine.dispose()


@pytest_asyncio.fixture
async def session_factory(engine):
    factory = async_sessionmaker(bind=engine, expire_on_commit=False, autoflush=False)
    set_session_factory(factory)
    yield factory
    set_session_factory(None)


class InspectionSession:
    """Read the database from a test without holding a transaction open.

    SQLite blocks writers while any reader has a transaction open, so a
    long-lived assertion session would deadlock a test that inspects state and
    then drives more of the flow. Each call gets its own short session; results
    are frozen so the detached rows stay readable afterwards.
    """

    def __init__(self, factory):
        self._factory = factory

    async def execute(self, statement):
        async with self._factory() as session:
            frozen = (await session.execute(statement)).freeze()
        return frozen()

    async def get(self, model, primary_key):
        async with self._factory() as session:
            return await session.get(model, primary_key)

    async def write(self, fn):
        """Run `fn(session)` in a committed transaction, for test arrangement."""
        async with self._factory() as session:
            result = await fn(session)
            await session.commit()
            return result


@pytest_asyncio.fixture
async def db(session_factory) -> AsyncIterator[InspectionSession]:
    """Read/arrange helper for tests."""
    yield InspectionSession(session_factory)


@pytest_asyncio.fixture
async def redis(session_factory):
    client = fakeredis.aioredis.FakeRedis(decode_responses=True)
    redis_helper.set_redis(client)
    yield client
    await client.flushall()
    redis_helper.set_redis(None)


@pytest_asyncio.fixture
async def broken_redis(redis):
    """Swap in a Redis client that fails every call, to exercise degradation."""
    redis_helper.set_redis(BrokenRedis())
    yield
    redis_helper.set_redis(redis)


@pytest.fixture
def meta(redis) -> FakeMetaClient:
    client = FakeMetaClient()
    set_meta_client(client)
    yield client
    set_meta_client(None)


@pytest.fixture
def ai(meta) -> FakeSalesModel:
    model = FakeSalesModel()
    set_sales_model(model)
    yield model
    set_sales_model(None)


@pytest.fixture
def settings():
    return get_settings()


@pytest_asyncio.fixture
async def client(ai) -> AsyncIterator[AsyncClient]:
    """HTTP client bound to the ASGI app, sharing the test datastores."""
    app = create_app()
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as http_client:
        yield http_client
