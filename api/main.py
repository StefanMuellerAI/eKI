"""Main FastAPI application instance."""

import logging
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from fastapi import Depends, FastAPI, HTTPException, Request, Response, status
from fastapi.exceptions import RequestValidationError
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from pydantic import ValidationError
from prometheus_client import CONTENT_TYPE_LATEST, generate_latest

from api.config import get_settings
from api.dependencies import verify_api_key
from api.routers import health, security
from core.exceptions import EKIException
from core.db_models import ApiKeyModel
from core.models import ErrorDetail, ErrorResponse

# Configure logging
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
)
logger = logging.getLogger(__name__)


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    """Application lifespan manager for startup and shutdown events."""
    settings = get_settings()
    logger.info(f"Starting eKI API v0.1.0 in {settings.env} environment")

    # Startup: Initialize connections, etc.
    logger.info("Application startup complete")

    yield

    # Shutdown: Clean up resources
    logger.info("Application shutdown complete")


# Create FastAPI app
settings = get_settings()
app = FastAPI(
    title="eKI API",
    description="KI-gestützte Sicherheitsprüfung für Drehbücher - Filmakademie Baden-Württemberg",
    version="0.1.0",
    docs_url="/docs" if settings.debug else None,  # Hide in production
    redoc_url="/redoc" if settings.debug else None,  # Hide in production
    openapi_url="/openapi.json" if settings.debug else None,  # Hide in production
    lifespan=lifespan,
)

# Add CORS middleware
app.add_middleware(
    CORSMiddleware,
    allow_origins=settings.cors_origins,  # Explicit whitelist from config
    allow_credentials=True,
    allow_methods=["GET", "POST", "PUT", "DELETE"],  # Explicit methods only
    allow_headers=[
        "Authorization",
        "Content-Type",
        "X-Request-ID",
        "X-Actor-User-Id",
        "X-Actor-Project-Id",
    ],  # Explicit headers only
    max_age=600,  # Cache preflight requests for 10 minutes
)


# Exception handlers


def _is_infrastructure_error(exc: Exception) -> tuple[bool, str]:
    """Classify infrastructure errors and return a user-facing message."""
    module = type(exc).__module__ or ""
    qualname = type(exc).__qualname__

    if module.startswith("temporalio") or module.startswith("grpc"):
        return True, "Workflow service is temporarily unavailable. Please try again later."

    if module.startswith("redis"):
        return True, "Cache service is temporarily unavailable. Please try again later."

    if module.startswith("sqlalchemy") or module.startswith("asyncpg"):
        return True, "Database service is temporarily unavailable. Please try again later."

    if qualname in ("ConnectionRefusedError", "ConnectionResetError", "TimeoutError"):
        return True, "A backend service is temporarily unavailable. Please try again later."

    return False, ""


@app.exception_handler(EKIException)
async def eki_exception_handler(request: Request, exc: EKIException) -> JSONResponse:
    """Handle custom EKI exceptions with their specific status codes."""
    return JSONResponse(
        status_code=exc.status_code,
        content=ErrorResponse(
            error=exc.__class__.__name__,
            message=exc.message,
            details=[ErrorDetail(message=str(v)) for v in exc.details.values()],
            request_id=request.headers.get("X-Request-ID"),
        ).model_dump(mode="json"),
    )


@app.exception_handler(HTTPException)
async def http_exception_handler(request: Request, exc: HTTPException) -> JSONResponse:
    """Normalize FastAPI HTTPExceptions into the ErrorResponse format."""
    error_name = {
        400: "BadRequest",
        401: "Unauthorized",
        403: "Forbidden",
        404: "NotFound",
        405: "MethodNotAllowed",
        409: "Conflict",
        410: "Gone",
        413: "PayloadTooLarge",
        422: "ValidationError",
        429: "RateLimitExceeded",
    }.get(exc.status_code, "Error")

    response = JSONResponse(
        status_code=exc.status_code,
        content=ErrorResponse(
            error=error_name,
            message=str(exc.detail),
            request_id=request.headers.get("X-Request-ID"),
        ).model_dump(mode="json"),
    )

    if exc.headers:
        response.headers.update(exc.headers)

    return response


@app.exception_handler(RequestValidationError)
async def validation_exception_handler(
    request: Request, exc: RequestValidationError
) -> JSONResponse:
    """Handle Pydantic validation errors."""
    details = [
        ErrorDetail(
            field=".".join(str(loc) for loc in err["loc"]),
            message=err["msg"],
            error_code=err["type"],
        )
        for err in exc.errors()
    ]

    return JSONResponse(
        status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
        content=ErrorResponse(
            error="ValidationError",
            message="Request validation failed",
            details=details,
            request_id=request.headers.get("X-Request-ID"),
        ).model_dump(mode="json"),
    )


@app.exception_handler(ValidationError)
async def pydantic_validation_exception_handler(
    request: Request, exc: ValidationError
) -> JSONResponse:
    """Handle raw Pydantic ValidationError that bypassed FastAPI's wrapper."""
    details = [
        ErrorDetail(
            field=".".join(str(loc) for loc in err["loc"]) if err.get("loc") else None,
            message=err["msg"],
            error_code=err["type"],
        )
        for err in exc.errors()
    ]

    return JSONResponse(
        status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
        content=ErrorResponse(
            error="ValidationError",
            message="Request validation failed",
            details=details,
            request_id=request.headers.get("X-Request-ID"),
        ).model_dump(mode="json"),
    )


@app.exception_handler(Exception)
async def general_exception_handler(request: Request, exc: Exception) -> JSONResponse:
    """Handle unexpected exceptions with infrastructure-aware classification."""
    is_infra, user_message = _is_infrastructure_error(exc)

    if is_infra:
        logger.error(f"Infrastructure error: {type(exc).__name__}: {exc}", exc_info=True)
        return JSONResponse(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            content=ErrorResponse(
                error="ServiceUnavailable",
                message=user_message,
                request_id=request.headers.get("X-Request-ID"),
            ).model_dump(mode="json"),
        )

    logger.error(f"Unexpected error: {exc}", exc_info=True)

    return JSONResponse(
        status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
        content=ErrorResponse(
            error="InternalServerError",
            message="An internal error occurred. Please try again later.",
            request_id=request.headers.get("X-Request-ID"),
        ).model_dump(mode="json"),
    )


# Include routers
app.include_router(health.router, tags=["Health"])
app.include_router(security.router, prefix="/v1/security", tags=["Security"])

if settings.metrics_enabled:

    @app.get("/metrics", include_in_schema=False)
    async def metrics_endpoint(_api_key: ApiKeyModel = Depends(verify_api_key)) -> Response:
        """Prometheus metrics endpoint protected by API key authentication."""
        return Response(content=generate_latest(), media_type=CONTENT_TYPE_LATEST)


@app.get("/", include_in_schema=False)
async def root() -> dict[str, str]:
    """Root endpoint redirect to docs."""
    return {
        "message": "eKI API v0.1.0",
        "docs": "/docs",
        "redoc": "/redoc",
        "openapi": "/openapi.json",
    }
