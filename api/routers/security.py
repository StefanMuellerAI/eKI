"""Security check endpoints."""

import uuid
from datetime import datetime

from fastapi import APIRouter, Depends, HTTPException, Path, status
from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession
from temporalio.client import Client as TemporalClient

from api.dependencies import get_actor_headers, get_db, get_temporal_client, verify_api_key
from api.rate_limiting import rate_limit_combined
from core.db_models import ApiKeyModel, JobMetadata, ReportMetadata
from core.models import (
    AsyncSecurityCheckRequest,
    AsyncSecurityCheckResponse,
    JobStatus,
    JobStatusResponse,
    ReportResponse,
    RiskLevel,
    SecurityCheckRequest,
    SecurityReport,
    SyncSecurityCheckResponse,
)

router = APIRouter()


@router.post(
    "/check",
    response_model=SyncSecurityCheckResponse,
    status_code=status.HTTP_200_OK,
    summary="Synchronous security check",
    description="Perform a synchronous security check for scripts ≤1MB or ≤50 scenes.",
    dependencies=[Depends(verify_api_key), Depends(rate_limit_combined)],
)
async def security_check_sync(
    request: SecurityCheckRequest,
    actor_info: dict[str, str | None] = Depends(get_actor_headers),
) -> SyncSecurityCheckResponse:
    """
    Synchronous security check endpoint (stub for M01).

    Accepts a script and returns a security analysis report immediately.
    Suitable for small scripts (≤1MB, ≤50 scenes).

    In M01, this returns a mock report. Real implementation in later milestones.
    """
    # Stub: Create a mock report
    report_id = uuid.uuid4()

    mock_report = SecurityReport(
        report_id=report_id,
        project_id=request.project_id,
        script_format=request.script_format,
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
                "description": "This is a stub response for M01. Real analysis in later milestones.",
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


@router.post(
    "/check:async",
    response_model=AsyncSecurityCheckResponse,
    status_code=status.HTTP_202_ACCEPTED,
    summary="Asynchronous security check",
    description="Start an asynchronous security check for large scripts.",
    dependencies=[Depends(verify_api_key), Depends(rate_limit_combined)],
)
async def security_check_async(
    request: AsyncSecurityCheckRequest,
    temporal_client: TemporalClient = Depends(get_temporal_client),
    actor_info: dict[str, str | None] = Depends(get_actor_headers),
) -> AsyncSecurityCheckResponse:
    """
    Asynchronous security check endpoint (stub for M01).

    Starts a Temporal workflow for processing large scripts.
    Returns a job ID for tracking progress.

    In M01, this creates a workflow but returns stub data. Real workflow execution
    in later milestones.
    """
    job_id = uuid.uuid4()

    # Stub: In real implementation, start Temporal workflow here
    # workflow_handle = await temporal_client.start_workflow(
    #     SecurityCheckWorkflow.run,
    #     args=[request.dict()],
    #     id=str(job_id),
    #     task_queue=settings.temporal_task_queue,
    # )

    return AsyncSecurityCheckResponse(
        job_id=job_id,
        status=JobStatus.PENDING,
        message="Security check job created successfully (M01 stub)",
        status_url=f"/v1/security/jobs/{job_id}",
        estimated_completion_seconds=120,
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
    """
    Get job status endpoint with ownership verification.

    Returns the current status of an asynchronous security check job.
    Only the user who created the job can access it (prevents IDOR attacks).

    In M01, this returns stub data. Real status tracking in later milestones.
    """
    # Check ownership to prevent IDOR
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

    # Stub: Return mock job status (in real implementation, use job data)
    return JobStatusResponse(
        job_id=job_id,
        status=JobStatus.COMPLETED,
        created_at=datetime.utcnow(),
        updated_at=datetime.utcnow(),
        progress_percentage=100,
        report_id=uuid.uuid4(),
        error_message=None,
        metadata={"stub": True},
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
) -> ReportResponse:
    """
    Get report endpoint with ownership verification and one-shot enforcement.

    Retrieves a security report by ID. This is a one-shot operation -
    the URL becomes invalid after the first successful retrieval.
    Only the user who created the report can access it (prevents IDOR attacks).

    In M01, this returns stub data. Real report storage/retrieval in later milestones.
    """
    # Atomically enforce one-shot retrieval to avoid race conditions.
    retrieved_at = datetime.utcnow()
    update_stmt = (
        update(ReportMetadata)
        .where(
            ReportMetadata.report_id == report_id,
            ReportMetadata.user_id == api_key.user_id,
            ReportMetadata.is_retrieved.is_(False),
        )
        .values(
            is_retrieved=True,
            retrieved_at=retrieved_at,
        )
    )
    update_result = await db.execute(update_stmt)

    if not update_result.rowcount:
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

    # Stub: Return mock report (in real implementation, fetch from Redis/storage)
    mock_report = SecurityReport(
        report_id=report_id,
        project_id="stub-project-id",
        script_format="fdx",
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
                "description": "This is a stub report for M01.",
                "recommendation": "No action required.",
                "confidence": 1.0,
                "line_reference": None,
            }
        ],
        processing_time_seconds=0.1,
        metadata={"stub": True},
    )

    return ReportResponse(
        report=mock_report,
        message="Report retrieved successfully (M01 stub). URL invalidated.",
    )
