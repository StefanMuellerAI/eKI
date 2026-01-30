"""Pytest configuration and fixtures."""

import asyncio
import hashlib
import secrets
from datetime import datetime, timedelta
from typing import AsyncGenerator, Generator
from uuid import uuid4

import pytest
from fastapi.testclient import TestClient
from httpx import AsyncClient
from sqlalchemy.ext.asyncio import AsyncSession, create_async_engine
from sqlalchemy.orm import sessionmaker

from api.dependencies import get_db, get_redis, get_temporal_client
from api.main import app
from core.db_models import ApiKeyModel, Base


@pytest.fixture(scope="session")
def event_loop() -> Generator:
    """Create event loop for async tests."""
    loop = asyncio.get_event_loop_policy().new_event_loop()
    yield loop
    loop.close()


@pytest.fixture(scope="session")
async def test_engine():
    """Create test database engine."""
    engine = create_async_engine(
        "sqlite+aiosqlite:///:memory:",
        echo=False,
    )

    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)

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
def override_get_db(db_session):
    """Override get_db dependency."""

    async def _override():
        yield db_session

    app.dependency_overrides[get_db] = _override
    yield
    app.dependency_overrides.clear()


@pytest.fixture
def override_get_redis():
    """Override get_redis dependency with mock."""

    class MockRedis:
        def __init__(self):
            self._store = {}

        async def ping(self):
            return True

        async def incr(self, key: str) -> int:
            """Increment counter."""
            self._store[key] = self._store.get(key, 0) + 1
            return self._store[key]

        async def expire(self, key: str, seconds: int) -> bool:
            """Set expiration (no-op in mock)."""
            return True

        async def ttl(self, key: str) -> int:
            """Get TTL (return 60 in mock)."""
            return 60

        async def aclose(self):
            pass

    async def _override():
        yield MockRedis()

    app.dependency_overrides[get_redis] = _override
    yield
    app.dependency_overrides.clear()


@pytest.fixture
def override_get_temporal():
    """Override get_temporal_client dependency with mock."""

    class MockTemporalClient:
        async def close(self):
            pass

    async def _override():
        yield MockTemporalClient()

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
    async with AsyncClient(app=app, base_url="http://test") as ac:
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
