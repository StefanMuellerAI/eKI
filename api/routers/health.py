"""Health check endpoints."""

import logging
from datetime import datetime

import redis.asyncio as aioredis
from fastapi import APIRouter, Depends, Response, status
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession
from temporalio.client import Client as TemporalClient

from api.config import get_settings
from api.dependencies import get_db, get_redis, verify_api_key
from core.db_models import ApiKeyModel
from core.models import HealthResponse, ReadinessResponse

logger = logging.getLogger(__name__)

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
        version="0.5.0",
    )


@router.get(
    "/ready",
    response_model=ReadinessResponse,
    status_code=status.HTTP_200_OK,
    summary="Readiness probe",
    description="Checks if the service is ready to handle requests by verifying all dependencies.",
)
async def readiness_check(
    http_response: Response,
    db: AsyncSession = Depends(get_db),
    redis: aioredis.Redis = Depends(get_redis),
    _api_key: ApiKeyModel = Depends(verify_api_key),
) -> ReadinessResponse:
    """
    Readiness probe endpoint.

    Checks connectivity to all required services:
    - PostgreSQL database
    - Redis cache
    - Temporal workflow engine

    Returns 200 if all services are available, 503 otherwise.
    Temporal is checked inside the handler body so that a connection
    failure does not crash the entire endpoint.
    """
    services_status: dict[str, bool] = {}

    # Check database
    try:
        await db.execute(text("SELECT 1"))
        services_status["database"] = True
    except Exception:
        services_status["database"] = False

    # Check Redis
    try:
        await redis.ping()
        services_status["redis"] = True
    except Exception:
        services_status["redis"] = False

    # Check Temporal – connect and perform a real RPC health check
    try:
        settings = get_settings()
        client = await TemporalClient.connect(settings.temporal_host)
        await client.service_client.check_health()
        services_status["temporal"] = True
    except Exception:
        logger.warning("Temporal health check failed", exc_info=True)
        services_status["temporal"] = False

    # Determine overall status
    all_ready = all(services_status.values())
    overall_status = "ready" if all_ready else "not_ready"

    readiness_response = ReadinessResponse(
        status=overall_status,
        timestamp=datetime.utcnow(),
        services=services_status,
    )

    if not all_ready:
        http_response.status_code = status.HTTP_503_SERVICE_UNAVAILABLE

    return readiness_response
