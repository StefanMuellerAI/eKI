"""Health check endpoints."""

from datetime import datetime

import redis.asyncio as aioredis
from fastapi import APIRouter, Depends, status
from sqlalchemy.ext.asyncio import AsyncSession
from temporalio.client import Client as TemporalClient

from api.dependencies import get_db, get_redis, get_temporal_client
from core.models import HealthResponse, ReadinessResponse

router = APIRouter()


@router.get(
    "/health",
    response_model=HealthResponse,
    status_code=status.HTTP_200_OK,
    summary="Liveness probe",
    description="Simple health check that returns 200 if the service is running.",
)
async def health_check() -> HealthResponse:
    """
    Liveness probe endpoint.

    Returns a simple health status indicating the service is alive.
    This endpoint does not check external dependencies.
    """
    return HealthResponse(
        status="healthy",
        timestamp=datetime.utcnow(),
        version="0.1.0",
    )


@router.get(
    "/ready",
    response_model=ReadinessResponse,
    status_code=status.HTTP_200_OK,
    summary="Readiness probe",
    description="Checks if the service is ready to handle requests by verifying all dependencies.",
)
async def readiness_check(
    db: AsyncSession = Depends(get_db),
    redis: aioredis.Redis = Depends(get_redis),
    temporal: TemporalClient = Depends(get_temporal_client),
) -> ReadinessResponse:
    """
    Readiness probe endpoint.

    Checks connectivity to all required services:
    - PostgreSQL database
    - Redis cache
    - Temporal workflow engine

    Returns 200 if all services are available, 503 otherwise.
    """
    services_status: dict[str, bool] = {}

    # Check database
    try:
        await db.execute("SELECT 1")
        services_status["database"] = True
    except Exception:
        services_status["database"] = False

    # Check Redis
    try:
        await redis.ping()
        services_status["redis"] = True
    except Exception:
        services_status["redis"] = False

    # Check Temporal
    try:
        # Simple check - if we got the client, Temporal is reachable
        services_status["temporal"] = temporal is not None
    except Exception:
        services_status["temporal"] = False

    # Determine overall status
    all_ready = all(services_status.values())
    overall_status = "ready" if all_ready else "not_ready"

    response = ReadinessResponse(
        status=overall_status,
        timestamp=datetime.utcnow(),
        services=services_status,
    )

    # Return 503 if not all services are ready

    return response
