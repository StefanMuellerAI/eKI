"""Security check endpoints.

``POST /v1/security/check:async`` supports both JSON (base64-encoded
script_content) and multipart/form-data file uploads.  Sensitive data is stored
in the encrypted SecureBuffer (Redis) and only opaque reference keys are passed
to Temporal workflows.  (The synchronous M01 stub ``POST /v1/security/check``
was removed in 1.0.1; the path answers 404.)
"""

import base64
import json
import logging
import uuid
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any

from fastapi import (
    APIRouter,
    Depends,
    HTTPException,
    Path,
    Request,
    Response,
    status,
)
from pydantic import ValidationError
from sqlalchemy import select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession
from temporalio.client import Client as TemporalClient
from temporalio.common import Priority

from api.config import get_settings
from api.dependencies import (
    RedisClient,
    get_actor_headers,
    get_db,
    get_redis,
    get_temporal_client,
    verify_api_key,
)
from api.rate_limiting import rate_limit_combined
from core import metrics
from core.db_models import ApiKeyModel, JobMetadata, ReportMetadata
from core.exceptions import NotFoundException, ServiceUnavailableException
from core.models import (
    AsyncSecurityCheckRequest,
    AsyncSecurityCheckResponse,
    JobStatus,
    JobStatusResponse,
    ReportResponse,
    ScriptFormat,
    SecurityReport,
    UploadAsyncSecurityCheckRequest,
)
from services.secure_buffer import SecureBuffer
from workflows.security_check import SecurityCheckWorkflow

logger = logging.getLogger(__name__)

router = APIRouter()

# Fallback-Konstante: gilt nur, wenn Settings nicht ladbar sind. Im
# Produktivpfad zieht _max_upload_size() aus settings.max_upload_size_bytes.
_MAX_UPLOAD_SIZE = 10 * 1024 * 1024  # 10 MB


def _max_upload_size() -> int:
    """Resolve effective max upload size from settings, falling back to 10 MB."""
    try:
        return int(get_settings().max_upload_size_bytes)
    except Exception:
        return _MAX_UPLOAD_SIZE


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _get_buffer(redis_client: RedisClient) -> SecureBuffer:
    settings = get_settings()
    return SecureBuffer(
        redis_client,
        secret_key=settings.api_secret_key,
        default_ttl=settings.buffer_ttl_seconds,
    )


@dataclass
class ResolvedRequest:
    """Parsed request data from JSON or multipart."""

    b64_content: str
    script_format: ScriptFormat
    project_id: str
    metadata: dict[str, Any]
    priority: int = 5
    delivery: str = "pull"
    idempotency_key: str | None = None
    script_id: int | None = None
    callback_url: str | None = None


def _validation_error_to_http(exc: ValidationError) -> HTTPException:
    details = "; ".join(
        f"{'.'.join(str(loc) for loc in e['loc'])}: {e['msg']}" if e.get("loc") else e["msg"]
        for e in exc.errors()
    )
    return HTTPException(
        status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
        detail=f"Request validation failed: {details}",
    )


def _validate_priority(raw: Any) -> int:
    """Validate the multipart ``priority`` form field (1..10) outside of Pydantic.

    Multipart sends the priority as a string; this funnels it through the same
    bounds as ``AsyncSecurityCheckRequest.priority`` with a stable message.
    """
    try:
        priority = int(raw)
    except (TypeError, ValueError) as exc:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail="Request validation failed: priority: must be an integer between 1 and 10",
        ) from exc
    if not 1 <= priority <= 10:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail="Request validation failed: priority: must be between 1 and 10",
        )
    return priority


async def _resolve_request(request: Request) -> ResolvedRequest:
    """Inspect Content-Type and parse the request into a ResolvedRequest."""
    ct = (request.headers.get("content-type") or "").lower()
    if "multipart/form-data" in ct:
        return await _resolve_multipart(request)
    return await _resolve_json(request)


def _to_resolved(req: AsyncSecurityCheckRequest) -> ResolvedRequest:
    return ResolvedRequest(
        b64_content=req.script_content,
        script_format=req.script_format,
        project_id=req.project_id,
        metadata=req.metadata,
        priority=req.priority,
        delivery=req.delivery,
        idempotency_key=req.idempotency_key,
        script_id=req.script_id,
        callback_url=str(req.callback_url) if req.callback_url is not None else None,
    )


async def _resolve_json(request: Request) -> ResolvedRequest:
    raw = await request.body()
    try:
        body = json.loads(raw)
    except (ValueError, RecursionError) as exc:
        # ValueError covers JSONDecodeError (syntax, empty body, form data) and
        # UnicodeDecodeError; RecursionError a pathologically nested document.
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Request body must be valid JSON (or multipart/form-data with a 'file' field)",
        ) from exc
    if not isinstance(body, dict):
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail=(
                f"Request validation failed: body: JSON object expected, got {type(body).__name__}"
            ),
        )
    try:
        req = AsyncSecurityCheckRequest(**body)
    except ValidationError as exc:
        raise _validation_error_to_http(exc) from exc
    return _to_resolved(req)


def _form_text(form: Any, name: str) -> str | None:
    """Return a text form field, ``None`` if absent; a file part is a client error."""
    value = form.get(name)
    if value is None or isinstance(value, str):
        return value
    raise HTTPException(
        status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
        detail=f"Request validation failed: {name}: must be a text field, not a file",
    )


async def _resolve_multipart(request: Request) -> ResolvedRequest:
    form = await request.form()
    upload = form.get("file")
    if upload is None or not hasattr(upload, "read"):
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Multipart request must include a 'file' field",
        )

    raw = await upload.read()
    max_size = _max_upload_size()
    if len(raw) > max_size:
        raise HTTPException(
            status_code=status.HTTP_413_REQUEST_ENTITY_TOO_LARGE,
            detail=f"File exceeds maximum size of {max_size} bytes",
        )
    if not raw:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Uploaded file is empty",
        )

    filename = (getattr(upload, "filename", "") or "").lower()
    if filename.endswith(".fdx"):
        fmt = ScriptFormat.FDX
    elif filename.endswith(".pdf"):
        fmt = ScriptFormat.PDF
    else:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Unsupported file type. Allowed: .fdx, .pdf",
        )

    if fmt == ScriptFormat.PDF and not raw[:5].startswith(b"%PDF"):
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Uploaded file does not appear to be a valid PDF",
        )

    # Every form field goes through the same Pydantic rules as the JSON body
    # (project_id pattern, delivery enum + callback_url policy, idempotency_key,
    # script_id range) and the content sniffing runs against the *final*
    # script_format, so an explicit ``script_format=pdf`` cannot bypass the
    # %PDF check.
    fields: dict[str, Any] = {
        "script_content": base64.b64encode(raw).decode("ascii"),
        "script_format": _form_text(form, "script_format") or fmt.value,
        # Optional for multipart (documented contract); same placeholder the
        # router has always stored for a missing project.
        "project_id": _form_text(form, "project_id") or "unknown",
    }
    for name in ("script_id", "delivery", "idempotency_key", "callback_url"):
        value = _form_text(form, name)
        if value not in (None, ""):
            fields[name] = value
    raw_priority = _form_text(form, "priority")
    fields["priority"] = _validate_priority(5 if raw_priority is None else raw_priority)

    try:
        req = UploadAsyncSecurityCheckRequest(**fields)
    except ValidationError as exc:
        raise _validation_error_to_http(exc) from exc
    return _to_resolved(req)


async def _find_idempotent_job(
    db: AsyncSession, user_id: str, idempotency_key: str
) -> JobMetadata | None:
    stmt = select(JobMetadata).where(
        JobMetadata.user_id == user_id,
        JobMetadata.idempotency_key == idempotency_key,
    )
    return (await db.execute(stmt)).scalar_one_or_none()


def _existing_job_response(job: JobMetadata) -> AsyncSecurityCheckResponse:
    return AsyncSecurityCheckResponse(
        job_id=job.job_id,
        status=job.status,
        message="Existing job returned (idempotency key matched)",
        status_url=f"/v1/security/jobs/{job.job_id}",
        estimated_completion_seconds=120,
    )


def temporal_priority_key(priority: int, levels: int) -> int:
    """Map the API priority 1 (highest) .. 10 (lowest) onto Temporal keys 1..``levels``.

    Monotonic, 1 -> 1 and 10 -> ``levels``; with the shipped server config
    (``matching.priorityLevels: 10``) the mapping is the identity, so the API
    default 5 equals the server default key ``(levels + 1) // 2`` = 5.
    """
    levels = max(1, int(levels))
    priority = min(10, max(1, int(priority)))
    return ((priority - 1) * levels) // 10 + 1


# ---------------------------------------------------------------------------
# Async endpoint
# ---------------------------------------------------------------------------


@router.post(
    "/check:async",
    response_model=AsyncSecurityCheckResponse,
    status_code=status.HTTP_202_ACCEPTED,
    summary="Asynchronous security check",
    description=(
        "Start an asynchronous security check for large scripts. "
        "Accepts JSON (base64 script_content) or multipart/form-data file upload."
    ),
    dependencies=[Depends(rate_limit_combined)],
)
async def security_check_async(
    request: Request,
    temporal_client: TemporalClient = Depends(get_temporal_client),
    redis_client: RedisClient = Depends(get_redis),
    db: AsyncSession = Depends(get_db),
    actor_info: dict[str, str | None] = Depends(get_actor_headers),
) -> AsyncSecurityCheckResponse:
    """Asynchronous security check endpoint.

    Stores script content in an encrypted Redis buffer and starts a
    Temporal workflow that receives only the buffer reference key.
    Supports idempotency and configurable delivery mode (push/pull).
    """
    settings = get_settings()
    buffer = _get_buffer(redis_client)

    resolved = await _resolve_request(request)
    user_id = actor_info.get("user_id") or ""

    # Idempotency (M10): scoped to the authenticated user. Fast path first;
    # the unique constraint (user_id, idempotency_key) closes the race between
    # two concurrent POSTs -- see the IntegrityError handling below.
    if resolved.idempotency_key:
        existing_job = await _find_idempotent_job(db, user_id, resolved.idempotency_key)
        if existing_job:
            return _existing_job_response(existing_job)

    job_id = uuid.uuid4()
    report_id = uuid.uuid4()
    delivery_mode = resolved.delivery or settings.delivery_mode

    # Create JobMetadata in DB
    job_meta = JobMetadata(
        job_id=job_id,
        project_id=resolved.project_id or "unknown",
        script_format=resolved.script_format,
        status=JobStatus.PENDING,
        user_id=user_id,
        priority=resolved.priority,
        idempotency_key=resolved.idempotency_key,
        delivery_mode=delivery_mode,
        script_id=resolved.script_id,
    )
    db.add(job_meta)
    try:
        await db.commit()
    except IntegrityError:
        # Lost the race against a concurrent request with the same key:
        # roll back and return the winner's job (Pflichtenheft Abnahmetest 5).
        await db.rollback()
        if resolved.idempotency_key:
            existing_job = await _find_idempotent_job(db, user_id, resolved.idempotency_key)
            if existing_job:
                return _existing_job_response(existing_job)
        raise

    # Store script content encrypted in Redis -- NOT in Temporal
    try:
        ref_key = await buffer.store({"script_content": resolved.b64_content})
    except Exception as exc:
        logger.error("Buffering script for job %s failed: %s", job_id, type(exc).__name__)
        await _fail_unstarted_job(db, job_id, "Secure buffer temporarily unavailable.")
        raise ServiceUnavailableException(
            "Security check could not be started. The secure buffer is temporarily unavailable.",
            details={"job_id": str(job_id)},
        ) from exc

    # Temporal receives only metadata + ref_key.  Die M07-Konfiguration wird
    # beim Workflow-Start in job_data eingefroren, damit der Workflow rein
    # deterministisch (ohne env-Zugriff im Replay) entscheiden kann, ob er
    # den Parallel-Pfad nutzt und welches Activity-Timeout gilt.
    job_data = {
        "ref_key": ref_key,
        "script_format": resolved.script_format.value,
        "project_id": resolved.project_id or "unknown",
        "job_id": str(job_id),
        "report_id": str(report_id),
        "user_id": actor_info.get("user_id"),
        "priority": resolved.priority,
        "delivery_mode": delivery_mode,
        "metadata": resolved.metadata,
        "script_id": resolved.script_id,
        # Decision 10b: per-job push target (None = EPRO_BASE_URL default). Not
        # stored in PostgreSQL; the worker re-validates it before every push.
        "callback_url": resolved.callback_url,
        "llm_parallel_enabled": settings.llm_parallel_enabled,
        "pdf_structure_concurrency": settings.pdf_structure_concurrency,
        "risk_analysis_concurrency": settings.risk_analysis_concurrency,
        "llm_activity_timeout_seconds": settings.llm_activity_timeout_seconds,
        # M10: Pull-TTL-Watch laeuft deterministisch mit dem beim Start gueltigen TTL.
        "buffer_ttl_seconds": settings.buffer_ttl_seconds,
        # Dokument-Limit: > 0 = der Job stellt sich vor der Verarbeitung an
        # (die Grenze selbst liest der Worker beim Start); maximale Wartezeit.
        "max_concurrent_documents": settings.max_concurrent_documents,
        "document_queue_timeout_seconds": settings.document_queue_timeout_seconds,
    }

    try:
        await temporal_client.start_workflow(
            SecurityCheckWorkflow.run,
            job_data,
            id=str(job_id),
            task_queue=settings.temporal_task_queue,
            execution_timeout=timedelta(seconds=settings.temporal_workflow_execution_timeout),
            # Decision 10b: task-queue priority; activities inherit it from the
            # workflow (server side), the workflow code does not change.
            priority=Priority(
                priority_key=temporal_priority_key(
                    resolved.priority, settings.temporal_priority_levels
                )
            ),
        )
    except Exception as exc:
        logger.error(f"Failed to start workflow for job {job_id}: {exc}", exc_info=True)
        # M10: no orphaned script content in Redis when the workflow never starts.
        try:
            await buffer.delete(ref_key)
            metrics.BUFFER_DELETES_TOTAL.labels(source="cleanup").inc()
        except Exception:
            logger.warning("Buffer cleanup after failed workflow start failed (TTL will reap)")
        await _fail_unstarted_job(db, job_id, "Workflow engine temporarily unavailable.")
        raise ServiceUnavailableException(
            "Security check could not be started. The workflow engine is temporarily unavailable.",
            details={"job_id": str(job_id)},
        ) from exc

    return AsyncSecurityCheckResponse(
        job_id=job_id,
        status=JobStatus.PENDING,
        message=f"Security check job started (delivery={delivery_mode})",
        status_url=f"/v1/security/jobs/{job_id}",
        estimated_completion_seconds=120,
    )


async def _fail_unstarted_job(db: AsyncSession, job_id: uuid.UUID, message: str) -> None:
    """Mark a job that never started as FAILED and release its idempotency key.

    Without releasing the key, a client retrying after the 503 with the same
    Idempotency-Key got this dead job back forever instead of a new one.  The
    row itself stays for operators.
    """
    try:
        await db.execute(
            update(JobMetadata)
            .where(JobMetadata.job_id == job_id)
            .values(status=JobStatus.FAILED, error_message=message, idempotency_key=None)
        )
        await db.commit()
    except Exception as exc:
        logger.error("Could not mark unstarted job %s as failed: %s", job_id, type(exc).__name__)


# ---------------------------------------------------------------------------
# Job status & report endpoints
# ---------------------------------------------------------------------------

_REPORT_UNAVAILABLE = (
    "Report is no longer available (expired or already delivered). URL is no longer valid."
)


@router.get(
    "/jobs/{job_id}",
    response_model=JobStatusResponse,
    status_code=status.HTTP_200_OK,
    summary="Get job status",
    description="Query the status of an asynchronous security check job.",
    dependencies=[Depends(rate_limit_combined)],
)
async def get_job_status(
    job_id: uuid.UUID = Path(..., description="Job ID to query"),
    api_key: ApiKeyModel = Depends(verify_api_key),
    db: AsyncSession = Depends(get_db),
) -> JobStatusResponse:
    """Get real job status from database with ownership verification."""
    stmt = select(JobMetadata).where(
        JobMetadata.job_id == job_id,
        JobMetadata.user_id == api_key.user_id,
    )
    result = await db.execute(stmt)
    job = result.scalar_one_or_none()

    if not job:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Job not found or access denied",
        )

    return JobStatusResponse(
        job_id=job.job_id,
        status=job.status,
        created_at=job.created_at,
        updated_at=job.updated_at,
        progress_percentage=job.progress_percentage,
        report_id=job.report_id,
        error_message=job.error_message,
        metadata={
            "delivery_mode": job.delivery_mode,
            "delivery_status": job.delivery_status,
            "delivery_attempts": job.delivery_attempts,
            "delivery_last_status_code": job.delivery_last_status_code,
            "delivered_at": job.delivered_at.isoformat() if job.delivered_at else None,
        },
    )


@router.get(
    "/reports/{report_id}",
    response_model=ReportResponse,
    status_code=status.HTTP_200_OK,
    summary="Retrieve report (one-shot)",
    description="Retrieve a security report. URL is invalidated after retrieval (pull mode).",
    dependencies=[Depends(rate_limit_combined)],
)
async def get_report(
    http_response: Response,
    report_id: uuid.UUID = Path(..., description="Report ID to retrieve"),
    api_key: ApiKeyModel = Depends(verify_api_key),
    db: AsyncSession = Depends(get_db),
    redis_client: RedisClient = Depends(get_redis),
) -> ReportResponse:
    """One-Shot report retrieval: fetch from Redis, return JSON + PDF, then delete.

    * 200 + ``X-One-Shot: true``: report delivered; the URL is invalidated.
    * 410: already retrieved, or the encrypted report is no longer in the
      buffer (TTL expired / already pushed / unreadable).  In the latter case
      nothing is marked as delivered and no placeholder report is returned.
    * 404: unknown report or owned by another API key.
    """
    # Atomically enforce one-shot retrieval
    retrieved_at = datetime.now(UTC)
    update_stmt = (
        update(ReportMetadata)
        .where(
            ReportMetadata.report_id == report_id,
            ReportMetadata.user_id == api_key.user_id,
            ReportMetadata.is_retrieved.is_(False),
        )
        .values(is_retrieved=True, retrieved_at=retrieved_at)
        .returning(ReportMetadata.report_ref_key)
    )
    update_result = await db.execute(update_stmt)
    row = update_result.first()

    if not row:
        # Check why it failed
        state_stmt = select(ReportMetadata.is_retrieved).where(
            ReportMetadata.report_id == report_id,
            ReportMetadata.user_id == api_key.user_id,
        )
        state_result = await db.execute(state_stmt)
        is_retrieved = state_result.scalar_one_or_none()

        if is_retrieved is None:
            metrics.REPORT_RETRIEVALS_TOTAL.labels(outcome="not_found").inc()
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail="Report not found or access denied",
            )
        metrics.REPORT_RETRIEVALS_TOTAL.labels(outcome="gone").inc()
        raise HTTPException(
            status_code=status.HTTP_410_GONE,
            detail="Report already retrieved. URL is no longer valid.",
        )

    report_ref_key = row[0]

    # Read the buffer *before* committing the claim: if the report is gone
    # (TTL expired, already pushed, unreadable) nothing was delivered, so the
    # claim is rolled back -- no fabricated report, no ``delivered`` bookkeeping,
    # and the pull-TTL watch still sees the report as not retrieved.
    buffer = _get_buffer(redis_client)
    try:
        if not report_ref_key:
            raise NotFoundException("Report has no buffer reference")
        report_package = await buffer.retrieve(report_ref_key)
    except NotFoundException:
        await db.rollback()
        metrics.REPORT_RETRIEVALS_TOTAL.labels(outcome="expired").inc()
        raise HTTPException(
            status_code=status.HTTP_410_GONE,
            detail=_REPORT_UNAVAILABLE,
        ) from None
    except Exception:
        # Redis outage etc.: keep the report retrievable, surface 503 via the
        # global handler instead of masking it.
        await db.rollback()
        raise

    # Build the response before committing the claim: a stored report that does
    # not fit the response model is a server fault and must stay retrievable.
    report_data = report_package.get("report", report_package)
    try:
        response = ReportResponse(
            report=SecurityReport(
                report_id=report_data.get("report_id", str(report_id)),
                project_id=report_data.get("project_id", ""),
                script_format=report_data.get("script_format", "fdx"),
                created_at=report_data.get("created_at", retrieved_at),
                risk_summary=report_data.get("risk_summary", {}),
                total_findings=report_data.get("total_findings", 0),
                findings=report_data.get("findings", []),
                processing_time_seconds=report_data.get("processing_time_seconds", 0),
                metadata=report_data.get("metadata", {}),
            ),
            pdf_base64=report_package.get("pdf_base64"),
            message="Report retrieved successfully. URL is now invalidated.",
        )
    except ValidationError as exc:
        await db.rollback()
        # Not a client error: re-raise as a plain exception -> 500 envelope.
        raise RuntimeError(f"Stored report {report_id} does not match ReportResponse") from exc

    # M10: delivery bookkeeping for pull mode -- the one-shot GET *is* the delivery.
    await db.execute(
        update(JobMetadata)
        .where(JobMetadata.report_id == report_id, JobMetadata.user_id == api_key.user_id)
        .values(delivery_status="delivered", delivered_at=retrieved_at)
    )
    await db.commit()

    # Pflichtenheft Anhang 1: X-One-Shot signals that this URL is now invalid.
    http_response.headers["X-One-Shot"] = "true"

    # Delete from Redis after successful retrieval (One-Shot); the TTL is the
    # safety net if this fails.
    try:
        await buffer.delete(report_ref_key)
        metrics.BUFFER_DELETES_TOTAL.labels(source="pull").inc()
    except Exception:
        logger.warning("Buffer delete after report retrieval failed (TTL will reap)")
    metrics.REPORT_RETRIEVALS_TOTAL.labels(outcome="success").inc()

    return response
