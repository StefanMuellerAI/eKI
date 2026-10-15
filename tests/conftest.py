"""Pytest configuration and fixtures."""

import hashlib
import os
import secrets
from collections.abc import AsyncGenerator
from datetime import datetime, timedelta
from uuid import uuid4

import pytest
from fastapi.testclient import TestClient
from httpx import ASGITransport, AsyncClient
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession, create_async_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import NullPool

from api.dependencies import get_db, get_redis, get_temporal_client
from api.main import app
from core.db_models import ApiKeyModel, Base

# Same as the Docker images: single-threaded tesseract, otherwise OCR tests hit
# the per-page timeout on a loaded machine (OpenMP thread contention).
os.environ.setdefault("OMP_THREAD_LIMIT", "1")

# Minimal PDF that passes the ``%PDF`` magic-byte validator and is parseable
# by pdfplumber. Used by API tests that need a syntactically valid PDF body.
MINIMAL_PDF_BYTES = (
    b"%PDF-1.4\n"
    b"1 0 obj<</Type/Catalog/Pages 2 0 R>>endobj\n"
    b"2 0 obj<</Type/Pages/Kids[3 0 R]/Count 1>>endobj\n"
    b"3 0 obj<</Type/Page/Parent 2 0 R/MediaBox[0 0 612 792]>>endobj\n"
    b"trailer<</Root 1 0 R>>\n%%EOF\n"
)


class MockRedis:
    """In-memory stand-in for ``redis.asyncio.Redis``.

    Implements the subset used by rate limiting (``incr``/``expire``/``ttl``)
    and by ``SecureBuffer`` (``setex``/``get``/``delete``/``exists``).
    """

    def __init__(self) -> None:
        self._store: dict[str, object] = {}

    async def ping(self) -> bool:
        return True

    async def incr(self, key: str) -> int:
        current = self._store.get(key, 0)
        value = (current if isinstance(current, int) else 0) + 1
        self._store[key] = value
        return value

    async def expire(self, key: str, seconds: int) -> bool:
        return True

    async def ttl(self, key: str) -> int:
        return 60

    async def setex(self, key: str, seconds: int, value: bytes | str) -> bool:
        self._store[key] = value
        return True

    async def set(self, key: str, value: bytes | str, ex: int | None = None) -> bool:
        self._store[key] = value
        return True

    async def get(self, key: str) -> object | None:
        return self._store.get(key)

    async def delete(self, *keys: str) -> int:
        removed = 0
        for key in keys:
            if key in self._store:
                del self._store[key]
                removed += 1
        return removed

    async def exists(self, *keys: str) -> int:
        return sum(1 for key in keys if key in self._store)

    async def keys(self, pattern: str = "*") -> list[str]:
        prefix = pattern.rstrip("*")
        return [k for k in self._store if k.startswith(prefix)]

    async def aclose(self) -> None:
        pass


# Opt-in: point the whole suite at a real PostgreSQL (+pgvector) database.
# The database is wiped (drop_all/create_all) at session start, so never point
# this at anything but a throwaway test database.  CI sets it; locally e.g.
#   TEST_POSTGRES_URL=postgresql+asyncpg://eki_user:eki_password@localhost:5432/eki_test
TEST_POSTGRES_URL = os.environ.get("TEST_POSTGRES_URL", "")

requires_postgres = pytest.mark.skipif(
    not TEST_POSTGRES_URL, reason="TEST_POSTGRES_URL not set (PostgreSQL integration test)"
)


@pytest.fixture(scope="session")
async def test_engine():
    """Create test database engine.

    Default: SQLite in-memory.  SQLite cannot render Postgres-specific column
    types (JSONB, pgvector ``Vector(1024)``), so the M06 ``kb_*`` tables are
    excluded there.  SQLite also ignores ``VARCHAR(n)`` lengths, which once
    hid a production bug -- hence the optional PostgreSQL mode
    (``TEST_POSTGRES_URL``), which runs the same tests against the real
    dialect with every table.
    """
    if TEST_POSTGRES_URL:
        # NullPool: pytest-asyncio gives each test its own event loop and
        # asyncpg connections must not be shared across loops.
        engine = create_async_engine(TEST_POSTGRES_URL, echo=False, poolclass=NullPool)
        async with engine.begin() as conn:
            await conn.execute(text("CREATE EXTENSION IF NOT EXISTS vector"))
            await conn.run_sync(Base.metadata.drop_all)
            await conn.run_sync(Base.metadata.create_all)
        yield engine
        await engine.dispose()
        return

    engine = create_async_engine(
        "sqlite+aiosqlite:///:memory:",
        echo=False,
    )

    sqlite_tables = [
        t for t in Base.metadata.sorted_tables if t.name not in {"kb_documents", "kb_embeddings"}
    ]

    async with engine.begin() as conn:
        await conn.run_sync(
            lambda sync_conn: Base.metadata.create_all(sync_conn, tables=sqlite_tables)
        )

    yield engine

    await engine.dispose()


@pytest.fixture
async def db_session(test_engine) -> AsyncGenerator[AsyncSession, None]:
    """Create test database session."""
    async_session = sessionmaker(
        test_engine,
        class_=AsyncSession,
        expire_on_commit=False,
    )

    async with async_session() as session:
        yield session


@pytest.fixture
def override_get_db(db_session, test_engine):
    """Override get_db dependency.

    SQLite: every request shares the test's session (the in-memory database
    lives on that connection).  PostgreSQL: one fresh session per request,
    because the sync ``TestClient`` runs each request on its own event loop and
    an asyncpg connection cannot cross loops.
    """
    if TEST_POSTGRES_URL:
        factory = sessionmaker(test_engine, class_=AsyncSession, expire_on_commit=False)

        async def _override():
            async with factory() as session:
                yield session

    else:

        async def _override():
            yield db_session

    app.dependency_overrides[get_db] = _override
    yield
    app.dependency_overrides.clear()


@pytest.fixture
def mock_redis() -> MockRedis:
    """Shared MockRedis instance for a single test."""
    return MockRedis()


@pytest.fixture
def override_get_redis(mock_redis: MockRedis):
    """Override get_redis dependency with the shared in-memory mock."""

    async def _override():
        yield mock_redis

    app.dependency_overrides[get_redis] = _override
    yield
    app.dependency_overrides.clear()


class MockTemporalClient:
    """Temporal client stand-in that records ``start_workflow`` calls."""

    def __init__(self) -> None:
        self.started: list[dict[str, object]] = []

    async def start_workflow(self, *args: object, **kwargs: object) -> object:
        self.started.append({"args": args, "kwargs": kwargs})
        return object()

    async def close(self) -> None:
        pass


@pytest.fixture
def mock_temporal() -> MockTemporalClient:
    return MockTemporalClient()


@pytest.fixture
def override_get_temporal(mock_temporal: MockTemporalClient):
    """Override get_temporal_client dependency with mock."""

    async def _override():
        yield mock_temporal

    app.dependency_overrides[get_temporal_client] = _override
    yield
    app.dependency_overrides.clear()


@pytest.fixture
def client(override_get_db, override_get_redis, override_get_temporal) -> TestClient:
    """Create test client."""
    return TestClient(app)


@pytest.fixture
async def async_client(
    override_get_db, override_get_redis, override_get_temporal
) -> AsyncGenerator[AsyncClient, None]:
    """Create async test client."""
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as ac:
        yield ac


@pytest.fixture
async def test_api_key(db_session) -> tuple[str, ApiKeyModel]:
    """Create a test API key in the database."""
    # Generate API key
    api_key = f"eki_{secrets.token_hex(32)}"
    key_hash = hashlib.sha256(api_key.encode()).hexdigest()

    # Create API key model
    api_key_model = ApiKeyModel(
        id=uuid4(),
        user_id="test-user-123",
        organization_id="test-org",
        key_hash=key_hash,
        name="Test API Key",
        description="API key for testing",
        is_active=True,
        created_at=datetime.utcnow(),
        expires_at=datetime.utcnow() + timedelta(days=365),
        usage_count=0,
    )

    db_session.add(api_key_model)
    await db_session.commit()
    await db_session.refresh(api_key_model)

    return api_key, api_key_model


@pytest.fixture
async def test_api_key_user2(db_session) -> tuple[str, ApiKeyModel]:
    """Create a second test API key for a different user."""
    # Generate API key
    api_key = f"eki_{secrets.token_hex(32)}"
    key_hash = hashlib.sha256(api_key.encode()).hexdigest()

    # Create API key model
    api_key_model = ApiKeyModel(
        id=uuid4(),
        user_id="test-user-456",
        organization_id="test-org",
        key_hash=key_hash,
        name="Test API Key User 2",
        description="API key for testing user 2",
        is_active=True,
        created_at=datetime.utcnow(),
        expires_at=datetime.utcnow() + timedelta(days=365),
        usage_count=0,
    )

    db_session.add(api_key_model)
    await db_session.commit()
    await db_session.refresh(api_key_model)

    return api_key, api_key_model


@pytest.fixture
async def auth_headers(test_api_key) -> dict[str, str]:
    """Create authentication headers for testing."""
    api_key, _ = test_api_key
    return {
        "Authorization": f"Bearer {api_key}",
        "X-Actor-User-Id": "test-user-123",
        "X-Actor-Project-Id": "test-project-456",
    }


@pytest.fixture
async def auth_headers_user2(test_api_key_user2) -> dict[str, str]:
    """Create authentication headers for second test user."""
    api_key, _ = test_api_key_user2
    return {
        "Authorization": f"Bearer {api_key}",
        "X-Actor-User-Id": "test-user-456",
        "X-Actor-Project-Id": "test-project-789",
    }


@pytest.fixture
async def test_admin_key(db_session) -> tuple[str, ApiKeyModel]:
    """M10: API key with ``is_admin=True`` for the /v1/ops/* endpoints."""
    api_key = f"eki_{secrets.token_hex(32)}"
    key_hash = hashlib.sha256(api_key.encode()).hexdigest()

    api_key_model = ApiKeyModel(
        id=uuid4(),
        user_id="ops-admin",
        organization_id="test-org",
        key_hash=key_hash,
        name="Ops Admin Key",
        description="admin key for testing",
        is_active=True,
        is_admin=True,
        created_at=datetime.utcnow(),
        expires_at=datetime.utcnow() + timedelta(days=365),
        usage_count=0,
    )
    db_session.add(api_key_model)
    await db_session.commit()
    await db_session.refresh(api_key_model)
    return api_key, api_key_model


@pytest.fixture
async def admin_headers(test_admin_key) -> dict[str, str]:
    api_key, _ = test_admin_key
    return {"Authorization": f"Bearer {api_key}"}
