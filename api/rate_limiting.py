"""Rate limiting middleware using Redis."""

import hashlib
from fastapi import Depends, HTTPException, Request, status
import redis.asyncio as aioredis

from api.config import Settings, get_settings
from api.dependencies import get_redis


async def rate_limit_by_ip(
    request: Request,
    redis: aioredis.Redis = Depends(get_redis),
    settings: Settings = Depends(get_settings),
) -> None:
    """
    Rate limit by IP address.

    Args:
        request: FastAPI request object
        redis: Redis client
        settings: Application settings
    """
    if not settings.rate_limit_enabled:
        return

    # Get client IP (check X-Forwarded-For for proxies)
    client_ip = request.headers.get("X-Forwarded-For", "").split(",")[0].strip()
    if not client_ip:
        client_ip = request.client.host if request.client else "unknown"

    # Create rate limit key
    key = f"rate_limit:ip:{client_ip}"
    limit = 60  # requests per minute
    window = 60  # seconds

    # Increment counter
    current = await redis.incr(key)

    # Set expiration on first request
    if current == 1:
        await redis.expire(key, window)

    # Check limit
    if current > limit:
        # Get TTL for Retry-After header
        ttl = await redis.ttl(key)

        raise HTTPException(
            status_code=status.HTTP_429_TOO_MANY_REQUESTS,
            detail=f"Rate limit exceeded. Maximum {limit} requests per {window} seconds.",
            headers={"Retry-After": str(ttl if ttl > 0 else window)},
        )


async def rate_limit_by_api_key(
    request: Request,
    redis: aioredis.Redis = Depends(get_redis),
    settings: Settings = Depends(get_settings),
) -> None:
    """
    Rate limit by API key (more generous than IP).

    Args:
        request: FastAPI request object
        redis: Redis client
        settings: Application settings
    """
    if not settings.rate_limit_enabled:
        return

    # Extract API key from Authorization header
    auth_header = request.headers.get("Authorization", "")

    if not auth_header.startswith("Bearer "):
        # No API key, skip (IP rate limit will catch it)
        return

    api_key = auth_header[7:]

    # Hash API key for privacy
    key_hash = hashlib.sha256(api_key.encode()).hexdigest()[:16]

    # Create rate limit key
    key = f"rate_limit:api_key:{key_hash}"
    limit = 1000  # requests per hour
    window = 3600  # seconds (1 hour)

    # Increment counter
    current = await redis.incr(key)

    # Set expiration on first request
    if current == 1:
        await redis.expire(key, window)

    # Check limit
    if current > limit:
        ttl = await redis.ttl(key)

        raise HTTPException(
            status_code=status.HTTP_429_TOO_MANY_REQUESTS,
            detail=f"Rate limit exceeded. Maximum {limit} requests per {window // 60} minutes.",
            headers={"Retry-After": str(ttl if ttl > 0 else window)},
        )


async def rate_limit_combined(
    request: Request,
    redis: aioredis.Redis = Depends(get_redis),
    settings: Settings = Depends(get_settings),
) -> None:
    """
    Combined rate limiting: strict IP limit + generous API key limit.

    IP-based: 60 req/min (prevents abuse without auth)
    API key: 1000 req/hour (generous for authenticated users)
    """
    # Always check IP rate limit
    await rate_limit_by_ip(request, redis, settings)

    # Check API key rate limit if authenticated
    if "Authorization" in request.headers:
        await rate_limit_by_api_key(request, redis, settings)
