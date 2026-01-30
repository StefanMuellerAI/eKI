"""FastAPI dependency injection functions."""

import hashlib
from datetime import datetime

import redis.asyncio as aioredis
from fastapi import Depends, Header, HTTPException, status
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from temporalio.client import Client as TemporalClient

from api.config import Settings, get_settings
from core.db_models import ApiKeyModel
from db.session import get_db_session


async def get_settings_dependency() -> Settings:
    """Get application settings."""
    return get_settings()


async def get_db() -> AsyncSession:
    """Get database session."""
    async for session in get_db_session():
        yield session


async def get_redis() -> aioredis.Redis:
    """Get Redis connection."""
    settings = get_settings()
    redis_client = aioredis.from_url(
        str(settings.redis_url),
        encoding="utf-8",
        decode_responses=True,
        max_connections=settings.redis_max_connections,
    )
    try:
        yield redis_client
    finally:
        await redis_client.aclose()


async def get_temporal_client() -> TemporalClient:
    """Get Temporal client."""
    settings = get_settings()
    client = await TemporalClient.connect(settings.temporal_host)
    try:
        yield client
    finally:
        await client.close()


async def verify_api_key(
    authorization: str | None = Header(None, description="Bearer API key"),
    db: AsyncSession = Depends(get_db),
) -> ApiKeyModel:
    """
    Verify API key against database.

    Returns ApiKeyModel with user_id, organization_id, and usage tracking.

    Security features:
    - API keys stored as SHA-256 hashes (never plaintext)
    - Expiration checking
    - Active status validation
    - Usage tracking for monitoring
    """
    if not authorization:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Missing authorization header",
            headers={"WWW-Authenticate": "Bearer"},
        )

    if not authorization.startswith("Bearer "):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid authorization header format",
            headers={"WWW-Authenticate": "Bearer"},
        )

    token = authorization[7:]  # Remove "Bearer " prefix

    if not token:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid token",
            headers={"WWW-Authenticate": "Bearer"},
        )

    # Hash the token (API keys are stored hashed)
    token_hash = hashlib.sha256(token.encode()).hexdigest()

    # Query database for valid API key
    stmt = select(ApiKeyModel).where(
        ApiKeyModel.key_hash == token_hash,
        ApiKeyModel.is_active == True,  # noqa: E712
        ApiKeyModel.expires_at > datetime.utcnow(),
    )

    result = await db.execute(stmt)
    api_key = result.scalar_one_or_none()

    if not api_key:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid or expired API key",
            headers={"WWW-Authenticate": "Bearer"},
        )

    # Update last_used_at and usage_count for monitoring
    api_key.last_used_at = datetime.utcnow()
    api_key.usage_count += 1
    await db.commit()

    return api_key


def get_actor_headers(
    x_actor_user_id: str | None = Header(None, description="Actor user ID"),
    x_actor_project_id: str | None = Header(None, description="Actor project ID"),
) -> dict[str, str | None]:
    """
    Extract actor information from headers for audit logging.

    Returns dict with user_id and project_id for audit trail.
    """
    return {
        "user_id": x_actor_user_id,
        "project_id": x_actor_project_id,
    }
