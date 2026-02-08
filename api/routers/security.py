"""Security check endpoints.

Supports both JSON (base64-encoded script_content) and multipart/form-data
file uploads.  Sensitive data is stored in the encrypted SecureBuffer (Redis)
and only opaque reference keys are passed to Temporal workflows.
"""

import base64
import logging
import uuid
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any

import redis.asyncio as aioredis
from fastapi import (
    APIRouter,
    Depends,
    File,
    Form,
    HTTPException,
    Path,
    Request,
    UploadFile,
    status,
)
from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession
from temporalio.client import Client as TemporalClient

from api.config import get_settings
from api.dependencies import (
    get_actor_headers,
    get_db,
    get_redis,
    get_temporal_client,
    verify_api_key,
)
from api.rate_limiting import rate_limit_combined
from core.db_models import ApiKeyModel, JobMetadata, ReportMetadata
from core.models import (
    AsyncSecurityCheckRequest,
    AsyncSecurityCheckResponse,
    JobStatus,
    JobStatusResponse,
    ReportResponse,
    RiskLevel,
    ScriptFormat,
    SecurityCheckRequest,
    SecurityReport,
    SyncSecurityCheckResponse,
)
from services.secure_buffer import SecureBuffer
from workflows.security_check import SecurityCheckWorkflow

logger = logging.getLogger(__name__)

router = APIRouter()

_MAX_UPLOAD_SIZE = 10 * 1024 * 1024  # 10 MB


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _get_buffer(redis_client: aioredis.Redis) -> SecureBuffer:
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


async def _resolve_request(request: Request) -> ResolvedRequest:
    """Inspect Content-Type and parse the request into a ResolvedRequest."""
    ct = (request.headers.get("content-type") or "").lower()
    if "multipart/form-data" in ct:
        return await _resolve_multipart(request)
    return await _resolve_json(request)


async def _resolve_json(request: Request) -> ResolvedRequest:
    body = await request.json()
    req = SecurityCheckRequest(**body)
    return ResolvedRequest(
        b64_content=req.script_content,
        script_format=req.script_format,
        project_id=req.project_id,
        metadata=req.metadata,
        priority=body.get("priority", 5),
        delivery=req.delivery,
        idempotency_key=req.idempotency_key,
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
    if len(raw) > _MAX_UPLOAD_SIZE:
        raise HTTPException(
            status_code=status.HTTP_413_REQUEST_ENTITY_TOO_LARGE,
            detail=f"File exceeds maximum size of {_MAX_UPLOAD_SIZE} bytes",
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

    b64 = base64.b64encode(raw).decode("ascii")
    project_id = str(form.get("project_id", ""))
    sf = str(form.get("script_format", ""))
    if sf:
        fmt = ScriptFormat(sf)

    return ResolvedRequest(
        b64_content=b64,
        script_format=fmt,
        project_id=project_id,
        metadata={},
        priority=int(form.get("priority", 5)),
        delivery=str(form.get("delivery", "pull")),
        idempotency_key=str(form.get("idempotency_key", "")) or None,
    )


# ---------------------------------------------------------------------------
# Sync endpoint
# ---------------------------------------------------------------------------


@router.post(
    "/check",
    response_model=SyncSecurityCheckResponse,
    status_code=status.HTTP_200_OK,
    summary="Synchronous security check",
    description=(
        "Perform a synchronous security check for scripts ≤1 MB or ≤50 scenes. "
        "Accepts JSON (base64 script_content) or multipart/form-data file upload."
    ),
    dependencies=[Depends(rate_limit_combined)],
)
async def security_check_sync(
    request: Request,
    actor_info: dict[str, str | None] = Depends(get_actor_headers),
) -> SyncSecurityCheckResponse:
    """Synchronous security check endpoint.

    Accepts a script and returns a security analysis report immediately.
    """
    resolved = await _resolve_request(request)
    fmt = resolved.script_format
    proj_id = resolved.project_id

    report_id = uuid.uuid4()

    mock_report = SecurityReport(
        report_id=report_id,
        project_id=proj_id or "unknown",
        script_format=fmt,
        created_at=datetime.utcnow(),
        risk_summary={
            RiskLevel.CRITICAL: 0,
            RiskLevel.HIGH: 0,
            RiskLevel.MEDIUM: 0,
            RiskLevel.LOW: 0,
            RiskLevel.INFO: 1,
        },
        total_findings=1,
        findings=[
            {
                "id": str(uuid.uuid4()),
                "scene_number": None,
                "risk_level": RiskLevel.INFO,
                "category": "stub",
                "description": "Stub response. Real analysis in later milestones.",
                "recommendation": "No action required for stub data.",
                "confidence": 1.0,
                "line_reference": None,
            }
        ],
        processing_time_seconds=0.1,
        metadata={
            "user_id": actor_info.get("user_id"),
            "project_id": actor_info.get("project_id"),
            "stub": True,
        },
    )

    return SyncSecurityCheckResponse(
        report=mock_report,
        message="Security check completed successfully (M01 stub)",
    )


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
    redis_client: aioredis.Redis = Depends(get_redis),
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

    # Idempotency check: if key exists, return existing job
    if resolved.idempotency_key:
        stmt = select(JobMetadata).where(
            JobMetadata.idempotency_key == resolved.idempotency_key
        )
        existing = await db.execute(stmt)
        existing_job = existing.scalar_one_or_none()
        if existing_job:
            return AsyncSecurityCheckResponse(
                job_id=existing_job.job_id,
                status=existing_job.status,
                message="Existing job returned (idempotency key matched)",
                status_url=f"/v1/security/jobs/{existing_job.job_id}",
                estimated_completion_seconds=120,
            )

    job_id = uuid.uuid4()
    report_id = uuid.uuid4()
    delivery_mode = resolved.delivery or settings.delivery_mode

    # Create JobMetadata in DB
    job_meta = JobMetadata(
        job_id=job_id,
        project_id=resolved.project_id or "unknown",
        script_format=resolved.script_format,
        status=JobStatus.PENDING,
        user_id=actor_info.get("user_id") or "",
        priority=resolved.priority,
        idempotency_key=resolved.idempotency_key,
        delivery_mode=delivery_mode,
    )
    db.add(job_meta)
    await db.commit()

    # Store script content encrypted in Redis -- NOT in Temporal
    ref_key = await buffer.store({"script_content": resolved.b64_content})

    # Temporal receives only metadata + ref_key
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
    }

    await temporal_client.start_workflow(
        SecurityCheckWorkflow.run,
        job_data,
        id=str(job_id),
        task_queue=settings.temporal_task_queue,
        execution_timeout=timedelta(seconds=settings.temporal_workflow_execution_timeout),
    )

    return AsyncSecurityCheckResponse(
        job_id=job_id,
        status=JobStatus.PENDING,
        message=f"Security check job started (delivery={delivery_mode})",
        status_url=f"/v1/security/jobs/{job_id}",
        estimated_completion_seconds=120,
    )


# ---------------------------------------------------------------------------
# Job status & report endpoints (unchanged from M01)
# ---------------------------------------------------------------------------


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
        metadata={"delivery_mode": job.delivery_mode},
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
    report_id: uuid.UUID = Path(..., description="Report ID to retrieve"),
    api_key: ApiKeyModel = Depends(verify_api_key),
    db: AsyncSession = Depends(get_db),
    redis_client: aioredis.Redis = Depends(get_redis),
) -> ReportResponse:
    """One-Shot report retrieval: fetch from Redis, return JSON + PDF, then delete.

    After the first successful 2xx response, the report URL is invalidated.
    """
    # Atomically enforce one-shot retrieval
    retrieved_at = datetime.utcnow()
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
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail="Report not found or access denied",
            )
        raise HTTPException(
            status_code=status.HTTP_410_GONE,
            detail="Report already retrieved. URL is no longer valid.",
        )

    await db.commit()
    report_ref_key = row[0]

    # Fetch report from Redis SecureBuffer
    buffer = _get_buffer(redis_client)
    pdf_base64 = None

    try:
        report_package = await buffer.retrieve(report_ref_key)
        report_data = report_package.get("report", report_package)
        pdf_base64 = report_package.get("pdf_base64")

        # Delete from Redis after successful retrieval (One-Shot)
        await buffer.delete(report_ref_key)
    except Exception:
        # Report expired from Redis (TTL), return what we can from metadata
        report_data = {
            "report_id": str(report_id),
            "project_id": "expired",
            "script_format": "fdx",
            "created_at": retrieved_at.isoformat(),
            "risk_summary": {"critical": 0, "high": 0, "medium": 0, "low": 0, "info": 0},
            "total_findings": 0,
            "findings": [],
            "processing_time_seconds": 0,
            "metadata": {"error": "Report expired from buffer"},
        }

    # Build SecurityReport from the data
    report_obj = SecurityReport(
        report_id=report_data.get("report_id", str(report_id)),
        project_id=report_data.get("project_id", ""),
        script_format=report_data.get("script_format", "fdx"),
        created_at=report_data.get("created_at", retrieved_at),
        risk_summary=report_data.get("risk_summary", {}),
        total_findings=report_data.get("total_findings", 0),
        findings=report_data.get("findings", []),
        processing_time_seconds=report_data.get("processing_time_seconds", 0),
        metadata=report_data.get("metadata", {}),
    )

    return ReportResponse(
        report=report_obj,
        pdf_base64=pdf_base64,
        message="Report retrieved successfully. URL is now invalidated.",
    )
