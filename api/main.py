"""Main FastAPI application instance."""

import logging
import time
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from fastapi import Depends, FastAPI, HTTPException, Request, Response, status
from fastapi.exceptions import RequestValidationError
from fastapi.middleware.cors import CORSMiddleware
from fastapi.openapi.utils import get_openapi
from fastapi.responses import JSONResponse
from prometheus_client import CONTENT_TYPE_LATEST, generate_latest
from pydantic import ValidationError

from api.config import get_settings
from api.dependencies import verify_api_key
from api.routers import health, knowledge_base, ops, security
from core.db_models import ApiKeyModel
from core.exceptions import EKIException
from core.logging_config import configure_logging, set_request_id
from core.metrics import (
    HTTP_REQUEST_DURATION,
    HTTP_REQUESTS_IN_FLIGHT,
    HTTP_REQUESTS_TOTAL,
    set_build_info,
)
from core.models import (
    AsyncSecurityCheckRequest,
    ErrorDetail,
    ErrorResponse,
    SecurityCheckRequest,
)
from core.tracing import configure_tracing
from core.version import __version__

# M08: zentrale Logging-Konfiguration. Setzt strukturierte JSON-Logs
# (Default) oder Console-Renderer (LOG_FORMAT=console) und installiert
# den SensitiveContentFilter, der Drehbuch-/Reportinhalte automatisch
# maskiert (Pflichtenheft Abnahmetest 7).
configure_logging(get_settings())
logger = logging.getLogger(__name__)


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    """Application lifespan manager for startup and shutdown events."""
    settings = get_settings()
    logger.info(f"Starting eKI API v{__version__} in {settings.env} environment")

    set_build_info(version=__version__, llm_provider=settings.llm_provider, role="api")
    logger.info("Application startup complete")

    yield

    # Shutdown: Clean up resources
    logger.info("Application shutdown complete")


# Create FastAPI app
settings = get_settings()
app = FastAPI(
    title="eKI API",
    description="KI-gestützte Sicherheitsprüfung für Drehbücher - Filmakademie Baden-Württemberg",
    version=__version__,
    docs_url="/docs" if settings.debug else None,  # Hide in production
    redoc_url="/redoc" if settings.debug else None,  # Hide in production
    openapi_url="/openapi.json" if settings.debug else None,  # Hide in production
    lifespan=lifespan,
)

# M09: OpenTelemetry (opt-in via OTEL_ENABLED). Instrumentiert FastAPI,
# SQLAlchemy und httpx; Temporal-Spans kommen ueber den Client-Interceptor.
configure_tracing(settings, role="api", app=app)

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


# M08: Request-ID-Middleware. Liest X-Request-ID, faellt sonst auf einen
# neu generierten UUID4-Hex zurueck. Bindet den Wert an die Logging-
# ContextVar, sodass alle nachfolgenden Logs den Wert tragen und legt
# ihn in der Response-Header zurueck, damit Aufrufer Trace-Querverweise
# ziehen koennen. Bewusst als Middleware vor allen Routern installiert.
@app.middleware("http")
async def request_id_middleware(request: Request, call_next):
    incoming = request.headers.get("X-Request-ID")
    request_id = set_request_id(incoming)
    try:
        response: Response = await call_next(request)
    except Exception:
        # Selbst bei Exception soll der Header gesetzt sein, falls ein
        # Exception-Handler die Response noch produziert. set_request_id
        # bleibt in der ContextVar erhalten.
        raise
    response.headers.setdefault("X-Request-ID", request_id)
    return response


def _route_template(request: Request) -> str:
    """Return a bounded-cardinality route label for metrics.

    Newer FastAPI versions mount included routers, so ``scope["route"].path``
    is relative to the router prefix. We therefore rebuild the template from
    the concrete request path by replacing matched path parameters with
    ``{name}`` placeholders. Unmatched paths collapse into one label.
    """
    if request.scope.get("route") is None:
        return "unmatched"
    segments = request.url.path.split("/")
    for name, value in (request.scope.get("path_params") or {}).items():
        raw = str(value)
        segments = [f"{{{name}}}" if seg == raw else seg for seg in segments]
    return "/".join(segments) or "/"


# M09: HTTP-Metriken pro Route-Template. Bewusst als Middleware, damit auch
# Fehlerantworten aus Exception-Handlern gezaehlt werden. /metrics selbst
# wird ausgenommen, damit der Scrape die Zahlen nicht verfaelscht.
@app.middleware("http")
async def http_metrics_middleware(request: Request, call_next):
    if request.url.path == "/metrics":
        return await call_next(request)

    HTTP_REQUESTS_IN_FLIGHT.inc()
    started = time.perf_counter()
    status_code = 500
    try:
        response: Response = await call_next(request)
        status_code = response.status_code
        return response
    finally:
        HTTP_REQUESTS_IN_FLIGHT.dec()
        route = _route_template(request)
        HTTP_REQUEST_DURATION.labels(method=request.method, route=route).observe(
            time.perf_counter() - started
        )
        HTTP_REQUESTS_TOTAL.labels(
            method=request.method, route=route, status=str(status_code)
        ).inc()


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
app.include_router(knowledge_base.router, prefix="/v1/kb", tags=["KnowledgeBase"])
app.include_router(ops.router, prefix="/v1/ops", tags=["Operations"])

if settings.metrics_enabled:

    @app.get("/metrics", include_in_schema=False)
    async def metrics_endpoint(_api_key: ApiKeyModel = Depends(verify_api_key)) -> Response:
        """Prometheus metrics endpoint protected by API key authentication."""
        return Response(content=generate_latest(), media_type=CONTENT_TYPE_LATEST)


@app.get("/", include_in_schema=False)
async def root() -> dict[str, str]:
    """Root endpoint redirect to docs."""
    return {
        "message": f"eKI API v{__version__}",
        "docs": "/docs",
        "redoc": "/redoc",
        "openapi": "/openapi.json",
    }


# ---------------------------------------------------------------------------
# Custom OpenAPI schema – injects request body docs for endpoints that
# use raw Request parsing (needed for dual JSON / multipart support).
# ---------------------------------------------------------------------------

_MULTIPART_BASE: dict = {
    "type": "object",
    "required": ["file"],
    "properties": {
        "file": {
            "type": "string",
            "format": "binary",
            "description": "Script file (.fdx or .pdf, max 10 MB)",
        },
        "project_id": {
            "type": "string",
            "description": "eProjekt project ID",
        },
        "script_format": {
            "type": "string",
            "enum": ["fdx", "pdf"],
            "description": "Script format (auto-detected from file extension if omitted)",
        },
        "script_id": {
            "type": "integer",
            "description": "eProjekt document ID of the script/treatment",
        },
        "delivery": {
            "type": "string",
            "enum": ["pull", "push"],
            "default": "pull",
            "description": "Delivery mode: pull (One-Shot GET) or push (POST to ePro)",
        },
        "idempotency_key": {
            "type": "string",
            "maxLength": 255,
            "description": "Optional idempotency key to prevent duplicate jobs",
        },
    },
}

_MULTIPART_ASYNC: dict = {
    **_MULTIPART_BASE,
    "properties": {
        **_MULTIPART_BASE["properties"],
        "priority": {
            "type": "integer",
            "minimum": 1,
            "maximum": 10,
            "default": 5,
            "description": "Job priority (1=highest, 10=lowest)",
        },
    },
}


def _custom_openapi() -> dict:
    if app.openapi_schema:
        return app.openapi_schema

    openapi_schema = get_openapi(
        title=app.title,
        version=app.version,
        description=app.description,
        routes=app.routes,
    )

    schemas = openapi_schema.setdefault("components", {}).setdefault("schemas", {})

    for model_cls in (SecurityCheckRequest, AsyncSecurityCheckRequest):
        model_schema = model_cls.model_json_schema(ref_template="#/components/schemas/{model}")
        for def_name, def_body in model_schema.pop("$defs", {}).items():
            schemas.setdefault(def_name, def_body)
        schemas[model_cls.__name__] = model_schema

    paths = openapi_schema.get("paths", {})

    endpoint_map = [
        ("/v1/security/check", "SecurityCheckRequest", _MULTIPART_BASE),
        ("/v1/security/check:async", "AsyncSecurityCheckRequest", _MULTIPART_ASYNC),
    ]
    for path, json_schema_name, multipart_schema in endpoint_map:
        operation = paths.get(path, {}).get("post")
        if not operation:
            continue
        operation["requestBody"] = {
            "required": True,
            "content": {
                "application/json": {
                    "schema": {"$ref": f"#/components/schemas/{json_schema_name}"},
                },
                "multipart/form-data": {
                    "schema": multipart_schema,
                },
            },
        }

    app.openapi_schema = openapi_schema
    return openapi_schema


app.openapi = _custom_openapi
