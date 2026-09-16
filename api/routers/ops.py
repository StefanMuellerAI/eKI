"""Operations endpoints (M10): job overview and delivery dead letters.

All endpoints require an API key with ``is_admin=true`` and return
metadata only -- never script text, findings or reports
(Pflichtenheft §4.1 "Audit & Nachvollziehbarkeit (inhaltsarm)", §6
"Job-Übersicht ohne Inhalte").

Dead letters cannot be *replayed* from here on purpose: the report content
is deleted as soon as a delivery fails definitively (Delete-on-Delivery), so
the only correct recovery is a fresh check triggered by ePro. Operators
acknowledge a dead letter once it has been investigated.
"""

from __future__ import annotations

import logging
import uuid
from datetime import datetime

from fastapi import APIRouter, Depends, HTTPException, Path, Query, status
from sqlalchemy import func, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from api.dependencies import get_db, require_admin_key
from api.rate_limiting import rate_limit_combined
from core import metrics
from core.db_models import ApiKeyModel, DeliveryDeadLetter, JobMetadata
from core.models import (
    DeadLetterAcknowledgeRequest,
    DeadLetterListResponse,
    DeadLetterSummary,
    JobStatus,
    OpsJobListResponse,
    OpsJobSummary,
    OpsSummaryResponse,
)

logger = logging.getLogger(__name__)

router = APIRouter()

_DELIVERY_STATES = ("pending", "delivering", "delivered", "failed", "dead_lettered")


async def _unacknowledged_count(db: AsyncSession) -> int:
    return int(
        (
            await db.execute(
                select(func.count())
                .select_from(DeliveryDeadLetter)
                .where(DeliveryDeadLetter.acknowledged_at.is_(None))
            )
        ).scalar_one()
    )


@router.get(
    "/summary",
    response_model=OpsSummaryResponse,
    summary="Operations summary",
    description="Job counts by status and delivery status plus unacknowledged dead letters.",
    dependencies=[Depends(rate_limit_combined)],
)
async def ops_summary(
    _admin: ApiKeyModel = Depends(require_admin_key),
    db: AsyncSession = Depends(get_db),
) -> OpsSummaryResponse:
    by_status_rows = (
        await db.execute(select(JobMetadata.status, func.count()).group_by(JobMetadata.status))
    ).all()
    by_delivery_rows = (
        await db.execute(
            select(JobMetadata.delivery_status, func.count()).group_by(JobMetadata.delivery_status)
        )
    ).all()
    unacked = await _unacknowledged_count(db)
    metrics.DEAD_LETTERS_UNACKNOWLEDGED.set(unacked)
    return OpsSummaryResponse(
        jobs_by_status={str(getattr(s, "value", s)): int(c) for s, c in by_status_rows},
        jobs_by_delivery_status={str(s): int(c) for s, c in by_delivery_rows},
        dead_letters_unacknowledged=unacked,
    )


@router.get(
    "/jobs",
    response_model=OpsJobListResponse,
    summary="Job overview (metadata only)",
    description="Paged list of jobs across all users, filterable by status and delivery status.",
    dependencies=[Depends(rate_limit_combined)],
)
async def list_jobs(
    _admin: ApiKeyModel = Depends(require_admin_key),
    db: AsyncSession = Depends(get_db),
    job_status: JobStatus | None = Query(None, alias="status", description="Job status filter"),
    delivery_status: str | None = Query(
        None, description="Delivery status filter: " + ", ".join(_DELIVERY_STATES)
    ),
    project_id: str | None = Query(None, max_length=255, description="Project ID filter"),
    limit: int = Query(50, ge=1, le=500, description="Page size"),
    offset: int = Query(0, ge=0, description="Page offset"),
) -> OpsJobListResponse:
    if delivery_status is not None and delivery_status not in _DELIVERY_STATES:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail=f"delivery_status must be one of {', '.join(_DELIVERY_STATES)}",
        )

    filters = []
    if job_status is not None:
        filters.append(JobMetadata.status == job_status)
    if delivery_status is not None:
        filters.append(JobMetadata.delivery_status == delivery_status)
    if project_id:
        filters.append(JobMetadata.project_id == project_id)

    total = int(
        (
            await db.execute(select(func.count()).select_from(JobMetadata).where(*filters))
        ).scalar_one()
    )
    rows = (
        await db.execute(
            select(JobMetadata)
            .where(*filters)
            .order_by(JobMetadata.created_at.desc())
            .limit(limit)
            .offset(offset)
        )
    ).scalars()

    items = [
        OpsJobSummary(
            job_id=j.job_id,
            project_id=j.project_id,
            user_id=j.user_id,
            script_format=j.script_format,
            status=j.status,
            progress_percentage=j.progress_percentage,
            delivery_mode=j.delivery_mode,
            delivery_status=j.delivery_status,
            delivery_attempts=j.delivery_attempts,
            delivery_last_status_code=j.delivery_last_status_code,
            delivery_last_attempt_at=j.delivery_last_attempt_at,
            delivered_at=j.delivered_at,
            report_id=j.report_id,
            error_message=j.error_message,
            created_at=j.created_at,
            updated_at=j.updated_at,
        )
        for j in rows
    ]
    return OpsJobListResponse(items=items, total=total, limit=limit, offset=offset)


def _to_summary(d: DeliveryDeadLetter) -> DeadLetterSummary:
    return DeadLetterSummary(
        id=d.id,
        job_id=d.job_id,
        report_id=d.report_id,
        project_id=d.project_id,
        user_id=d.user_id,
        delivery_mode=d.delivery_mode,
        reason=d.reason,
        attempts=d.attempts,
        last_status_code=d.last_status_code,
        last_error_type=d.last_error_type,
        webhook_sent=d.webhook_sent,
        created_at=d.created_at,
        acknowledged_at=d.acknowledged_at,
        acknowledged_by=d.acknowledged_by,
        note=d.note,
    )


@router.get(
    "/dead-letters",
    response_model=DeadLetterListResponse,
    summary="List delivery dead letters",
    description="Definitively failed deliveries (6h window exhausted, 4xx hard fail, pull TTL expired).",
    dependencies=[Depends(rate_limit_combined)],
)
async def list_dead_letters(
    _admin: ApiKeyModel = Depends(require_admin_key),
    db: AsyncSession = Depends(get_db),
    acknowledged: bool | None = Query(
        None, description="true = only acknowledged, false = only open, omit = all"
    ),
    limit: int = Query(50, ge=1, le=500),
    offset: int = Query(0, ge=0),
) -> DeadLetterListResponse:
    filters = []
    if acknowledged is True:
        filters.append(DeliveryDeadLetter.acknowledged_at.is_not(None))
    elif acknowledged is False:
        filters.append(DeliveryDeadLetter.acknowledged_at.is_(None))

    total = int(
        (
            await db.execute(select(func.count()).select_from(DeliveryDeadLetter).where(*filters))
        ).scalar_one()
    )
    rows = (
        await db.execute(
            select(DeliveryDeadLetter)
            .where(*filters)
            .order_by(DeliveryDeadLetter.created_at.desc())
            .limit(limit)
            .offset(offset)
        )
    ).scalars()
    unacked = await _unacknowledged_count(db)
    metrics.DEAD_LETTERS_UNACKNOWLEDGED.set(unacked)
    return DeadLetterListResponse(
        items=[_to_summary(d) for d in rows],
        total=total,
        unacknowledged=unacked,
        limit=limit,
        offset=offset,
    )


@router.get(
    "/dead-letters/{dead_letter_id}",
    response_model=DeadLetterSummary,
    summary="Get one dead letter",
    dependencies=[Depends(rate_limit_combined)],
)
async def get_dead_letter(
    dead_letter_id: uuid.UUID = Path(...),
    _admin: ApiKeyModel = Depends(require_admin_key),
    db: AsyncSession = Depends(get_db),
) -> DeadLetterSummary:
    row = (
        await db.execute(select(DeliveryDeadLetter).where(DeliveryDeadLetter.id == dead_letter_id))
    ).scalar_one_or_none()
    if row is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Dead letter not found")
    return _to_summary(row)


@router.post(
    "/dead-letters/{dead_letter_id}:acknowledge",
    response_model=DeadLetterSummary,
    summary="Acknowledge a dead letter",
    description=(
        "Marks the dead letter as investigated. Replay is intentionally not offered: the "
        "report content was deleted on failure (Delete-on-Delivery); ePro must re-trigger the check."
    ),
    dependencies=[Depends(rate_limit_combined)],
)
async def acknowledge_dead_letter(
    body: DeadLetterAcknowledgeRequest | None = None,
    dead_letter_id: uuid.UUID = Path(...),
    admin: ApiKeyModel = Depends(require_admin_key),
    db: AsyncSession = Depends(get_db),
) -> DeadLetterSummary:
    row = (
        await db.execute(select(DeliveryDeadLetter).where(DeliveryDeadLetter.id == dead_letter_id))
    ).scalar_one_or_none()
    if row is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Dead letter not found")
    if row.acknowledged_at is not None:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT, detail="Dead letter already acknowledged"
        )

    now = datetime.utcnow()
    await db.execute(
        update(DeliveryDeadLetter)
        .where(DeliveryDeadLetter.id == dead_letter_id)
        .values(
            acknowledged_at=now,
            acknowledged_by=admin.user_id,
            note=(body.note if body else None),
        )
    )
    await db.commit()
    await db.refresh(row)

    unacked = await _unacknowledged_count(db)
    metrics.DEAD_LETTERS_UNACKNOWLEDGED.set(unacked)
    logger.info(
        "Dead letter acknowledged: id=%s job=%s by=%s", dead_letter_id, row.job_id, admin.user_id
    )
    return _to_summary(row)
