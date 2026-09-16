"""M10 -- delivery bookkeeping helpers and dead-letter activities against the test DB.

Uses the SQLite in-memory engine from conftest by pointing
``workflows.activities._session_factory`` at it.
"""

from __future__ import annotations

from datetime import datetime
from unittest.mock import patch
from uuid import uuid4

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from core import metrics
from core.db_models import DeliveryDeadLetter, JobMetadata, ReportMetadata
from workflows import activities
from workflows.activities import (
    _mark_delivering,
    _record_attempt,
    check_report_retrieved_activity,
    record_dead_letter_activity,
)


class _NoDisposeEngine:
    """Wrapper so the activities' ``engine.dispose()`` does not tear down the shared test engine."""

    def __init__(self, engine):
        self._engine = engine

    async def dispose(self) -> None:
        return None


@pytest.fixture
def db_factory(test_engine):
    factory = async_sessionmaker(test_engine, class_=AsyncSession, expire_on_commit=False)
    with patch.object(
        activities, "_session_factory", return_value=(_NoDisposeEngine(test_engine), factory)
    ):
        yield factory


async def _insert_job(session: AsyncSession, **kw) -> JobMetadata:
    job = JobMetadata(
        job_id=uuid4(),
        project_id="p-db",
        script_format="fdx",
        status="running",
        user_id="u-db",
        priority=5,
        created_at=datetime.utcnow(),
        updated_at=datetime.utcnow(),
        delivery_mode=kw.pop("delivery_mode", "push"),
        delivery_status=kw.pop("delivery_status", "pending"),
        delivery_attempts=kw.pop("delivery_attempts", 0),
        **kw,
    )
    session.add(job)
    await session.commit()
    return job


@pytest.mark.asyncio
class TestMarkDeliveringAndRecordAttempt:
    async def test_mark_delivering_creates_report_once_and_sets_status(self, db_factory):
        async with db_factory() as session:
            job = await _insert_job(session)
        report_id = str(uuid4())
        kwargs = {
            "report_id": report_id,
            "job_id": str(job.job_id),
            "project_id": "p-db",
            "user_id": "u-db",
            "script_format": "fdx",
            "total_findings": 3,
            "report_ref_key": "ref",
            "delivery_mode": "push",
        }
        await _mark_delivering(**kwargs)
        await _mark_delivering(**kwargs)  # retry: must not raise / duplicate

        async with db_factory() as session:
            reports = (await session.execute(select(ReportMetadata))).scalars().all()
            mine = [r for r in reports if str(r.report_id) == report_id]
            assert len(mine) == 1
            assert mine[0].total_findings == 3
            assert mine[0].processing_time_seconds >= 0
            fresh = (
                await session.execute(select(JobMetadata).where(JobMetadata.job_id == job.job_id))
            ).scalar_one()
            assert fresh.status == "delivering"
            assert fresh.delivery_status == "delivering"
            assert str(fresh.report_id) == report_id

    async def test_record_attempt_push_success_completes_job(self, db_factory):
        async with db_factory() as session:
            job = await _insert_job(session)
        await _record_attempt(
            job_id=str(job.job_id),
            attempt=2,
            status_code=503,
            delivered=False,
            delivery_mode="push",
        )
        async with db_factory() as session:
            fresh = (
                await session.execute(select(JobMetadata).where(JobMetadata.job_id == job.job_id))
            ).scalar_one()
            assert fresh.delivery_attempts == 2
            assert fresh.delivery_last_status_code == 503
            assert fresh.delivery_last_attempt_at is not None
            assert fresh.status == "running"  # not completed on failure

        await _record_attempt(
            job_id=str(job.job_id), attempt=3, status_code=201, delivered=True, delivery_mode="push"
        )
        async with db_factory() as session:
            fresh = (
                await session.execute(select(JobMetadata).where(JobMetadata.job_id == job.job_id))
            ).scalar_one()
            assert fresh.status == "completed"
            assert fresh.delivery_status == "delivered"
            assert fresh.delivery_attempts == 3
            assert fresh.progress_percentage == 100
            assert fresh.delivered_at is not None

    async def test_record_attempt_pull_ready_keeps_pending_delivery(self, db_factory):
        async with db_factory() as session:
            job = await _insert_job(session, delivery_mode="pull")
        await _record_attempt(
            job_id=str(job.job_id),
            attempt=1,
            status_code=None,
            delivered=True,
            delivery_mode="pull",
        )
        async with db_factory() as session:
            fresh = (
                await session.execute(select(JobMetadata).where(JobMetadata.job_id == job.job_id))
            ).scalar_one()
            assert fresh.status == "completed"
            assert fresh.delivery_status == "pending"  # delivered only via one-shot GET

    async def test_record_attempt_without_job_is_noop(self, db_factory):
        await _record_attempt(
            job_id="", attempt=1, status_code=200, delivered=True, delivery_mode="push"
        )


@pytest.mark.asyncio
class TestDeadLetterAndRetrievalCheck:
    async def test_record_dead_letter_persists_and_updates_gauge(self, db_factory):
        async with db_factory() as session:
            job = await _insert_job(session, delivery_attempts=4, delivery_last_status_code=503)
        result = await record_dead_letter_activity(
            {
                "job_id": str(job.job_id),
                "report_id": str(uuid4()),
                "reason": "retry_window_exhausted",
                "attempts": None,
                "delivery_mode": "push",
                "webhook_sent": True,
            }
        )
        assert result["recorded"] is True
        async with db_factory() as session:
            dl = (
                await session.execute(
                    select(DeliveryDeadLetter).where(DeliveryDeadLetter.job_id == job.job_id)
                )
            ).scalar_one()
            assert dl.reason == "retry_window_exhausted"
            assert dl.attempts == 4  # taken from job when payload has none
            assert dl.last_status_code == 503
            assert dl.webhook_sent is True
            assert dl.project_id == "p-db" and dl.user_id == "u-db"
            fresh = (
                await session.execute(select(JobMetadata).where(JobMetadata.job_id == job.job_id))
            ).scalar_one()
            assert fresh.delivery_status == "dead_lettered"
        assert metrics.DEAD_LETTERS_UNACKNOWLEDGED._value.get() >= 1

    async def test_record_dead_letter_for_unknown_job_uses_payload(self, db_factory):
        job_id = uuid4()
        result = await record_dead_letter_activity(
            {
                "job_id": str(job_id),
                "reason": "pull_ttl_expired",
                "attempts": 0,
                "delivery_mode": "pull",
                "project_id": "from-payload",
                "user_id": "payload-user",
            }
        )
        assert result["recorded"] is True
        async with db_factory() as session:
            dl = (
                await session.execute(
                    select(DeliveryDeadLetter).where(DeliveryDeadLetter.job_id == job_id)
                )
            ).scalar_one()
            assert dl.project_id == "from-payload"
            assert dl.delivery_mode == "pull"
            assert dl.report_id is None

    async def test_check_report_retrieved_states(self, db_factory):
        report_id = uuid4()
        async with db_factory() as session:
            session.add(
                ReportMetadata(
                    report_id=report_id,
                    job_id=uuid4(),
                    project_id="p",
                    user_id="u",
                    script_format="fdx",
                    created_at=datetime.utcnow(),
                    is_retrieved=False,
                    total_findings=0,
                    processing_time_seconds=1.0,
                )
            )
            await session.commit()
        assert await check_report_retrieved_activity({"report_id": str(report_id)}) == {
            "found": True,
            "retrieved": False,
        }
        async with db_factory() as session:
            row = (
                await session.execute(
                    select(ReportMetadata).where(ReportMetadata.report_id == report_id)
                )
            ).scalar_one()
            row.is_retrieved = True
            await session.commit()
        assert (await check_report_retrieved_activity({"report_id": str(report_id)}))[
            "retrieved"
        ] is True
        assert (await check_report_retrieved_activity({"report_id": str(uuid4())}))[
            "found"
        ] is False
