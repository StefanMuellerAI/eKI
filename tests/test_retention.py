"""Metadata retention (1.0.1, Decision 5): settings, cleanup, workflow, schedules.

Runs in both DB modes: the ``retention_db`` fixture is a private SQLite file
or, with ``TEST_POSTGRES_URL``, a private PostgreSQL database (created once
per module, truncated per test). The shared test database is not used because
the cleanup deletes *every* old row and the counts it returns are asserted.

Every job status and every table has an explicit keep/delete assertion.
"""

from __future__ import annotations

import dataclasses
import logging
import uuid
from collections.abc import Iterator
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock, MagicMock
from uuid import UUID, uuid4

import pytest
from prometheus_client import REGISTRY
from pydantic import ValidationError
from sqlalchemy import Select, create_engine, event, insert, select, text, update
from sqlalchemy.engine import make_url
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from sqlalchemy.pool import NullPool
from temporalio import activity, workflow
from temporalio.client import (
    Schedule,
    ScheduleActionStartWorkflow,
    ScheduleAlreadyRunningError,
    ScheduleCalendarSpec,
    ScheduleIntervalSpec,
    ScheduleOverlapPolicy,
    ScheduleSpec,
    ScheduleState,
)
from temporalio.service import RPCError, RPCStatusCode
from temporalio.testing import WorkflowEnvironment
from temporalio.worker import Worker

from api.config import Settings
from core.db_models import (
    ApiKeyModel,
    AuditLog,
    Base,
    DeliveryDeadLetter,
    JobMetadata,
    ReportMetadata,
)
from core.models import JobStatus, ScriptFormat
from tests.conftest import TEST_POSTGRES_URL
from workflows import maintenance
from workflows.maintenance import (
    KB_CLEANUP_SCHEDULE_ID,
    METADATA_CLEANUP_COUNTS,
    METADATA_CLEANUP_SCHEDULE_ID,
    METADATA_TABLES,
    ensure_kb_cleanup_schedule,
    ensure_metadata_cleanup_schedule,
    metadata_cleanup_expired_activity,
    purge_expired_metadata,
)

with workflow.unsafe.imports_passed_through():
    from workflows.maintenance import MetadataCleanupWorkflow

NOW = datetime(2026, 10, 16, 12, 0, tzinfo=UTC)
CUTOFF = NOW - timedelta(days=90)
OLD = CUTOFF - timedelta(days=1)
RECENT = CUTOFF + timedelta(days=1)
# Longest possible job with the default settings: workflow execution timeout
# (10 h) + buffer/pull TTL (6 h).
MAX_JOB_LIFETIME = timedelta(hours=16)

TERMINAL = {JobStatus.COMPLETED, JobStatus.FAILED, JobStatus.CANCELLED}
ACTIVE = {JobStatus.PENDING, JobStatus.RUNNING, JobStatus.DELIVERING}

_MODELS = {
    "job_metadata": JobMetadata,
    "report_metadata": ReportMetadata,
    "delivery_dead_letters": DeliveryDeadLetter,
    "api_keys": ApiKeyModel,
    "audit_logs": AuditLog,
}
_TABLES = [model.__table__ for model in _MODELS.values()]


def _deleted_metric(table: str) -> float:
    return float(
        REGISTRY.get_sample_value("eki_metadata_cleanup_deleted_total", {"table": table}) or 0.0
    )


# ===========================================================================
# Isolated database (SQLite file or private PostgreSQL database)
# ===========================================================================


@pytest.fixture(scope="module")
def _pg_retention_url() -> Iterator[str | None]:
    """A private PostgreSQL database with the metadata tables (PG mode only)."""
    if not TEST_POSTGRES_URL:
        yield None
        return
    base = make_url(TEST_POSTGRES_URL)
    name = f"{base.database}_ret_{uuid4().hex[:10]}"
    sync = base.set(drivername="postgresql+psycopg2")
    admin = create_engine(
        sync.set(database="postgres"), isolation_level="AUTOCOMMIT", poolclass=NullPool
    )
    with admin.connect() as conn:
        conn.execute(text(f'CREATE DATABASE "{name}"'))
    engine = create_engine(sync.set(database=name), poolclass=NullPool)
    try:
        with engine.begin() as conn:
            Base.metadata.create_all(conn, tables=_TABLES)
        yield base.set(database=name).render_as_string(hide_password=False)
    finally:
        engine.dispose()
        with admin.connect() as conn:
            conn.execute(text(f'DROP DATABASE IF EXISTS "{name}" WITH (FORCE)'))
        admin.dispose()


@pytest.fixture
async def retention_db(tmp_path, _pg_retention_url):
    if _pg_retention_url:
        url = _pg_retention_url
        engine = create_async_engine(url, poolclass=NullPool)
        async with engine.begin() as conn:
            await conn.execute(text("TRUNCATE " + ", ".join(METADATA_TABLES)))
    else:
        url = f"sqlite+aiosqlite:///{tmp_path / 'retention.db'}"
        engine = create_async_engine(url)
        async with engine.begin() as conn:
            await conn.run_sync(
                lambda sync_conn: Base.metadata.create_all(sync_conn, tables=_TABLES)
            )
    yield SimpleNamespace(
        url=url,
        engine=engine,
        session_factory=async_sessionmaker(engine, expire_on_commit=False),
    )
    await engine.dispose()


async def _insert(db: SimpleNamespace, model: Any, **values: Any) -> None:
    async with db.engine.begin() as conn:
        await conn.execute(insert(model.__table__).values(**values))


async def add_job(
    db: SimpleNamespace,
    status: JobStatus = JobStatus.COMPLETED,
    *,
    created: datetime = OLD,
    updated: datetime | None = None,
) -> UUID:
    job_id = uuid4()
    await _insert(
        db,
        JobMetadata,
        job_id=job_id,
        project_id="proj-secret",
        script_format=ScriptFormat.FDX,
        status=status,
        user_id="user-secret",
        created_at=created,
        updated_at=updated or created,
    )
    return job_id


async def add_report(
    db: SimpleNamespace,
    *,
    job_id: UUID | None = None,
    created: datetime = OLD,
    retrieved: datetime | None = None,
) -> UUID:
    report_id = uuid4()
    await _insert(
        db,
        ReportMetadata,
        report_id=report_id,
        job_id=job_id or uuid4(),
        project_id="proj-secret",
        user_id="user-secret",
        script_format=ScriptFormat.PDF,
        processing_time_seconds=1.0,
        created_at=created,
        retrieved_at=retrieved,
        is_retrieved=retrieved is not None,
    )
    return report_id


async def add_dead_letter(
    db: SimpleNamespace, *, created: datetime = OLD, acknowledged: datetime | None = None
) -> UUID:
    dead_letter_id = uuid4()
    await _insert(
        db,
        DeliveryDeadLetter,
        id=dead_letter_id,
        job_id=uuid4(),
        project_id="proj-secret",
        user_id="user-secret",
        reason="delivery_failed",
        created_at=created,
        acknowledged_at=acknowledged,
    )
    return dead_letter_id


async def add_key(
    db: SimpleNamespace,
    *,
    active: bool,
    expires: datetime,
    created: datetime = OLD - timedelta(days=400),
    last_used: datetime | None = None,
) -> UUID:
    key_id = uuid4()
    await _insert(
        db,
        ApiKeyModel,
        id=key_id,
        user_id="user-secret",
        key_hash=uuid4().hex + uuid4().hex,
        name="key",
        is_active=active,
        created_at=created,
        expires_at=expires,
        last_used_at=last_used,
    )
    return key_id


async def add_audit(db: SimpleNamespace, *, at: datetime = OLD) -> UUID:
    audit_id = uuid4()
    await _insert(
        db, AuditLog, id=audit_id, endpoint="/v1/x", method="GET", status_code=200, timestamp=at
    )
    return audit_id


async def remaining(db: SimpleNamespace, table: str) -> set[UUID]:
    model = _MODELS[table]
    primary_key = model.__table__.primary_key.columns.values()[0]
    async with db.session_factory() as session:
        return set((await session.execute(select(primary_key))).scalars().all())


async def purge(db: SimpleNamespace, **kwargs: Any) -> dict[str, Any]:
    kwargs.setdefault("retention_days", 90)
    kwargs.setdefault("max_job_lifetime", MAX_JOB_LIFETIME)
    kwargs.setdefault("now", NOW)
    return await purge_expired_metadata(db.session_factory, **kwargs)


# ===========================================================================
# Settings
# ===========================================================================


class TestSettings:
    def test_defaults(self):
        settings = Settings(_env_file=None)
        assert settings.metadata_retention_days == 90
        assert settings.metadata_cleanup_enabled is True
        assert settings.metadata_cleanup_cron == "30 3 * * *"
        assert settings.kb_cleanup_cron == "0 3 * * *"

    def test_environment_overrides(self, monkeypatch):
        monkeypatch.setenv("METADATA_RETENTION_DAYS", "30")
        monkeypatch.setenv("METADATA_CLEANUP_ENABLED", "false")
        monkeypatch.setenv("METADATA_CLEANUP_CRON", "  15  2 * *   MON-FRI ")
        settings = Settings(_env_file=None)
        assert settings.metadata_retention_days == 30
        assert settings.metadata_cleanup_enabled is False
        assert settings.metadata_cleanup_cron == "15 2 * * MON-FRI"  # whitespace-normalised

    @pytest.mark.parametrize("days", [0, -1])
    def test_retention_days_must_be_positive(self, days):
        with pytest.raises(ValidationError, match="metadata_retention_days"):
            Settings(_env_file=None, metadata_retention_days=days)

    @pytest.mark.parametrize(
        "cron",
        [
            "30 3 * * *",
            "*/15 * * * *",
            "0 0-23/2 1,15 * *",
            "0 3 1/2 * *",
            "59 23 31 12 6",
            "0 6 * jan-mar mon-fri",
            "0 6 * JAN,DEC SUN",
        ],
    )
    @pytest.mark.parametrize("field", ["kb_cleanup_cron", "metadata_cleanup_cron"])
    def test_valid_cron(self, field, cron):
        assert getattr(Settings(_env_file=None, **{field: cron}), field) == cron

    @pytest.mark.parametrize(
        ("cron", "message"),
        [
            ("", "must have 5 fields"),
            ("30 3 * *", "must have 5 fields"),
            ("0 30 3 * * *", "must have 5 fields"),
            ("@daily", "must have 5 fields"),
            ("60 3 * * *", "minute field"),
            ("0 24 * * *", "hour field"),
            ("0 3 0 * *", "day of month field"),
            ("0 3 32 * *", "day of month field"),
            ("0 3 * 13 *", "month field"),
            ("0 3 * * 7", "day of week field"),
            ("0 3 * * MON#2", "day of week field"),
            ("0 3 * FOO *", "month field"),
            ("*/0 3 * * *", "invalid step"),
            ("5/x 3 * * *", "invalid step"),
            ("5/ 3 * * *", "invalid step"),
            ("10-5 3 * * *", "invalid range"),
            ("1-2-3 3 * * *", "invalid range"),
            ("1,,2 3 * * *", "minute field"),
            ("? 3 * * *", "minute field"),
        ],
    )
    @pytest.mark.parametrize("field", ["kb_cleanup_cron", "metadata_cleanup_cron"])
    def test_invalid_cron_is_rejected_at_startup(self, field, cron, message):
        with pytest.raises(ValidationError, match=message) as exc_info:
            Settings(_env_file=None, **{field: cron})
        assert field.upper() in str(exc_info.value)

    def test_cron_must_be_a_string(self):
        with pytest.raises(ValidationError, match="METADATA_CLEANUP_CRON must be a string"):
            Settings(_env_file=None, metadata_cleanup_cron=5)


# ===========================================================================
# purge_expired_metadata -- per table keep/delete rules
# ===========================================================================


class TestJobMetadata:
    @pytest.mark.parametrize("status", list(JobStatus))
    async def test_old_job_is_deleted_in_every_status(self, retention_db, status):
        """Terminal jobs count as ``job_metadata``; jobs left pending/running/delivering
        (abandoned: no workflow lives 90 days) as ``job_metadata_abandoned``."""
        job = await add_job(retention_db, status)

        result = await purge(retention_db)

        assert job not in await remaining(retention_db, "job_metadata")
        assert result["abandoned_cleanup"] is True
        if status in TERMINAL:
            assert result["deleted"]["job_metadata"] == 1
            assert result["deleted"]["job_metadata_abandoned"] == 0
        else:
            assert status in ACTIVE
            assert result["deleted"]["job_metadata"] == 0
            assert result["deleted"]["job_metadata_abandoned"] == 1

    @pytest.mark.parametrize("status", list(JobStatus))
    async def test_recent_job_is_kept_in_every_status(self, retention_db, status):
        job = await add_job(retention_db, status, created=RECENT)
        result = await purge(retention_db)
        assert await remaining(retention_db, "job_metadata") == {job}
        assert result["deleted"]["job_metadata"] == 0
        assert result["deleted"]["job_metadata_abandoned"] == 0

    @pytest.mark.parametrize("status", list(JobStatus))
    async def test_old_job_updated_after_the_cutoff_is_kept(self, retention_db, status):
        """Terminal: finished only recently. Active: still making progress."""
        job = await add_job(retention_db, status, created=OLD, updated=RECENT)
        await purge(retention_db)
        assert await remaining(retention_db, "job_metadata") == {job}

    async def test_cutoff_is_exclusive(self, retention_db):
        at_cutoff = await add_job(retention_db, created=CUTOFF)
        just_before = await add_job(retention_db, created=CUTOFF - timedelta(seconds=1))
        abandoned_at_cutoff = await add_job(retention_db, JobStatus.RUNNING, created=CUTOFF)

        result = await purge(retention_db)

        assert await remaining(retention_db, "job_metadata") == {at_cutoff, abandoned_at_cutoff}
        assert just_before not in await remaining(retention_db, "job_metadata")
        assert result["cutoff"] == CUTOFF.isoformat()


class TestAbandonedJobGuard:
    """A misconfiguration (cutoff younger than the longest possible job) must never
    delete a job that might still be live, nor the report of such a job."""

    @pytest.mark.parametrize("status", sorted(ACTIVE))
    async def test_cutoff_inside_the_job_lifetime_keeps_active_jobs(
        self, retention_db, status, caplog
    ):
        # retention 1 day, but a workflow may live 2 days
        created = NOW - timedelta(hours=30)
        active = await add_job(retention_db, status, created=created)
        report = await add_report(retention_db, job_id=active, created=created)
        terminal = await add_job(retention_db, JobStatus.COMPLETED, created=created)

        with caplog.at_level(logging.INFO, logger="workflows.maintenance"):
            result = await purge(retention_db, retention_days=1, max_job_lifetime=timedelta(days=2))

        assert result["abandoned_cleanup"] is False
        assert result["deleted"]["job_metadata_abandoned"] == 0
        assert await remaining(retention_db, "job_metadata") == {active}
        assert terminal not in await remaining(retention_db, "job_metadata")
        assert await remaining(retention_db, "report_metadata") == {report}
        errors = [r for r in caplog.records if r.levelno == logging.ERROR]
        assert len(errors) == 1
        assert "abandoned jobs NOT deleted" in errors[0].getMessage()
        assert "METADATA_RETENTION_DAYS" in errors[0].getMessage()

    async def test_cutoff_equal_to_the_job_lifetime_is_allowed(self, retention_db):
        abandoned = await add_job(
            retention_db, JobStatus.DELIVERING, created=NOW - timedelta(days=1, seconds=1)
        )

        result = await purge(retention_db, retention_days=1, max_job_lifetime=timedelta(days=1))

        assert result["abandoned_cleanup"] is True
        assert abandoned not in await remaining(retention_db, "job_metadata")
        assert result["deleted"]["job_metadata_abandoned"] == 1


class TestReportMetadata:
    async def test_old_report_is_deleted_and_recent_is_kept(self, retention_db):
        old = await add_report(retention_db)
        recent = await add_report(retention_db, created=RECENT)

        result = await purge(retention_db)

        assert await remaining(retention_db, "report_metadata") == {recent}
        assert old not in await remaining(retention_db, "report_metadata")
        assert result["deleted"]["report_metadata"] == 1

    async def test_old_report_retrieved_after_the_cutoff_is_kept(self, retention_db):
        retrieved_recently = await add_report(retention_db, retrieved=RECENT)
        retrieved_long_ago = await add_report(retention_db, retrieved=OLD)

        await purge(retention_db)

        assert await remaining(retention_db, "report_metadata") == {retrieved_recently}
        assert retrieved_long_ago not in await remaining(retention_db, "report_metadata")

    @pytest.mark.parametrize("status", list(JobStatus))
    async def test_report_of_an_old_job_is_deleted_in_every_status(self, retention_db, status):
        """Also the report of an abandoned (pending/running/delivering) job."""
        job = await add_job(retention_db, status)
        await add_report(retention_db, job_id=job)

        result = await purge(retention_db)

        assert await remaining(retention_db, "report_metadata") == set()
        assert result["deleted"]["report_metadata"] == 1

    @pytest.mark.parametrize("status", list(JobStatus))
    async def test_report_of_a_live_job_is_kept(self, retention_db, status):
        """Old report, job updated after the cutoff: kept only while the job is active."""
        job = await add_job(retention_db, status, created=OLD, updated=RECENT)
        report = await add_report(retention_db, job_id=job)

        await purge(retention_db)

        reports = await remaining(retention_db, "report_metadata")
        if status in ACTIVE:
            assert reports == {report}
        else:
            assert status in TERMINAL
            assert reports == set()

    async def test_orphaned_old_report_is_deleted(self, retention_db):
        await add_report(retention_db, job_id=uuid4())  # job row already gone
        await purge(retention_db)
        assert await remaining(retention_db, "report_metadata") == set()


class TestDeadLetters:
    async def test_rules(self, retention_db):
        old_unacknowledged = await add_dead_letter(retention_db)
        old_acknowledged = await add_dead_letter(retention_db, acknowledged=OLD)
        acknowledged_recently = await add_dead_letter(retention_db, acknowledged=RECENT)
        recent = await add_dead_letter(retention_db, created=RECENT)

        result = await purge(retention_db)

        kept = await remaining(retention_db, "delivery_dead_letters")
        assert kept == {acknowledged_recently, recent}
        assert old_unacknowledged not in kept
        assert old_acknowledged not in kept
        assert result["deleted"]["delivery_dead_letters"] == 2


class TestApiKeys:
    async def test_rules(self, retention_db):
        far_future = NOW + timedelta(days=365)
        keys = {
            # never deleted: active and not expired, however old and unused
            "active_unexpired_never_used": (
                await add_key(retention_db, active=True, expires=far_future),
                True,
            ),
            "active_unexpired_used_long_ago": (
                await add_key(retention_db, active=True, expires=far_future, last_used=OLD),
                True,
            ),
            "active_expires_tomorrow": (
                await add_key(retention_db, active=True, expires=NOW + timedelta(days=1)),
                True,
            ),
            # expired: deleted once the expiry is older than the cutoff
            "active_expired_recently": (
                await add_key(retention_db, active=True, expires=NOW - timedelta(days=1)),
                True,
            ),
            "active_expired_before_cutoff": (
                await add_key(retention_db, active=True, expires=OLD, last_used=OLD),
                False,
            ),
            "expired_before_cutoff_created_recently": (
                await add_key(retention_db, active=True, expires=OLD, created=RECENT),
                True,
            ),
            # deactivated: deleted once creation and last use are older than the cutoff
            "inactive_unused": (
                await add_key(retention_db, active=False, expires=far_future),
                False,
            ),
            "inactive_used_long_ago": (
                await add_key(retention_db, active=False, expires=far_future, last_used=OLD),
                False,
            ),
            "inactive_used_recently": (
                await add_key(retention_db, active=False, expires=far_future, last_used=RECENT),
                True,
            ),
            "inactive_created_recently": (
                await add_key(retention_db, active=False, expires=far_future, created=RECENT),
                True,
            ),
            "inactive_and_expired": (
                await add_key(retention_db, active=False, expires=OLD),
                False,
            ),
        }

        result = await purge(retention_db)

        kept = await remaining(retention_db, "api_keys")
        for label, (key_id, expect_kept) in keys.items():
            assert (key_id in kept) is expect_kept, label
        assert result["deleted"]["api_keys"] == sum(1 for _, keep in keys.values() if not keep)

    async def test_key_reactivated_between_select_and_delete_survives(self, retention_db):
        """The DELETE re-checks the conditions, the SELECT alone is not trusted."""
        key = await add_key(retention_db, active=False, expires=NOW + timedelta(days=30))
        keys_table = ApiKeyModel.__table__
        real_factory = retention_db.session_factory

        class ReactivatingSession:
            def __init__(self) -> None:
                self._session = real_factory()

            async def __aenter__(self) -> ReactivatingSession:
                await self._session.__aenter__()
                return self

            async def __aexit__(self, *exc: Any) -> Any:
                return await self._session.__aexit__(*exc)

            async def commit(self) -> None:
                await self._session.commit()

            async def execute(self, statement: Any, *args: Any, **kwargs: Any) -> Any:
                result = await self._session.execute(statement, *args, **kwargs)
                if isinstance(statement, Select) and keys_table in statement.get_final_froms():
                    # an admin re-activates the key right after the SELECT
                    await self._session.execute(
                        update(keys_table).where(keys_table.c.id == key).values(is_active=True)
                    )
                return result

        result = await purge_expired_metadata(
            ReactivatingSession, retention_days=90, max_job_lifetime=MAX_JOB_LIFETIME, now=NOW
        )

        assert result["deleted"]["api_keys"] == 0
        assert await remaining(retention_db, "api_keys") == {key}


class TestAuditLogs:
    async def test_rules(self, retention_db):
        old = await add_audit(retention_db)
        recent = await add_audit(retention_db, at=RECENT)

        result = await purge(retention_db)

        assert await remaining(retention_db, "audit_logs") == {recent}
        assert old not in await remaining(retention_db, "audit_logs")
        assert result["deleted"]["audit_logs"] == 1


# ===========================================================================
# purge_expired_metadata -- batching, result, metrics, logging, arguments
# ===========================================================================


class TestPurgeMechanics:
    async def test_deletes_in_bounded_batches(self, retention_db):
        jobs = [await add_job(retention_db) for _ in range(7)]
        keep = await add_job(retention_db, created=RECENT)
        statements: list[str] = []

        def _record(_conn, _cursor, statement, *_args):
            statements.append(" ".join(statement.split()))

        event.listen(retention_db.engine.sync_engine, "before_cursor_execute", _record)
        try:
            result = await purge(retention_db, batch_size=3)
        finally:
            event.remove(retention_db.engine.sync_engine, "before_cursor_execute", _record)

        job_deletes = [s for s in statements if s.startswith("DELETE FROM job_metadata")]
        assert len(job_deletes) == 3  # 3 + 3 + 1
        assert result["deleted"]["job_metadata"] == len(jobs)
        assert await remaining(retention_db, "job_metadata") == {keep}

    async def test_exact_multiple_of_the_batch_size(self, retention_db):
        for _ in range(4):
            await add_job(retention_db)
        result = await purge(retention_db, batch_size=2)
        assert result["deleted"]["job_metadata"] == 4
        assert await remaining(retention_db, "job_metadata") == set()

    async def test_empty_database(self, retention_db):
        result = await purge(retention_db)
        assert result == {
            "retention_days": 90,
            "cutoff": CUTOFF.isoformat(),
            "deleted": dict.fromkeys(METADATA_CLEANUP_COUNTS, 0),
            "abandoned_cleanup": True,
        }

    async def test_result_metrics_and_logs(self, retention_db, caplog):
        job = await add_job(retention_db)
        await add_report(retention_db, job_id=job)
        await add_report(retention_db)
        await add_dead_letter(retention_db)
        await add_key(retention_db, active=False, expires=OLD)
        await add_audit(retention_db)
        await add_audit(retention_db)
        await add_audit(retention_db)
        abandoned = await add_job(retention_db, JobStatus.RUNNING)
        await add_report(retention_db, job_id=abandoned)
        before = {table: _deleted_metric(table) for table in METADATA_CLEANUP_COUNTS}

        with caplog.at_level(logging.INFO, logger="workflows.maintenance"):
            result = await purge(retention_db)

        expected = {
            "report_metadata": 3,
            "delivery_dead_letters": 1,
            "job_metadata": 1,
            "job_metadata_abandoned": 1,
            "api_keys": 1,
            "audit_logs": 3,
        }
        assert result["deleted"] == expected
        assert list(result["deleted"]) == list(METADATA_CLEANUP_COUNTS)  # deletion order
        for table, count in expected.items():
            assert _deleted_metric(table) == before[table] + count

        messages = [record.getMessage() for record in caplog.records]
        summary = next(m for m in messages if m.startswith("Metadata retention cleanup:"))
        assert "retention_days=90" in summary
        assert "report_metadata=3 delivery_dead_letters=1 job_metadata=1" in summary
        assert "job_metadata_abandoned=1 api_keys=1 audit_logs=3" in summary
        warnings = [
            r
            for r in caplog.records
            if r.levelno == logging.WARNING and r.name == "workflows.maintenance"
        ]
        assert len(warnings) == 1
        assert "deleted 1 abandoned job(s)" in warnings[0].getMessage()
        # counts only: no ids, user ids or project ids
        for message in messages:
            assert "secret" not in message
            assert str(job) not in message
            assert str(abandoned) not in message

    async def test_retention_days_moves_the_cutoff(self, retention_db):
        ten_days = await add_job(retention_db, created=NOW - timedelta(days=10))
        result = await purge(retention_db, retention_days=7)
        assert result["cutoff"] == (NOW - timedelta(days=7)).isoformat()
        assert ten_days not in await remaining(retention_db, "job_metadata")

    async def test_default_now_is_the_current_utc_time(self, retention_db):
        old = await add_job(retention_db, created=datetime.now(UTC) - timedelta(days=91))
        fresh = await add_job(retention_db, created=datetime.now(UTC) - timedelta(days=89))

        result = await purge_expired_metadata(
            retention_db.session_factory, retention_days=90, max_job_lifetime=MAX_JOB_LIFETIME
        )

        assert await remaining(retention_db, "job_metadata") == {fresh}
        assert old not in await remaining(retention_db, "job_metadata")
        cutoff = datetime.fromisoformat(result["cutoff"])
        assert cutoff.tzinfo is not None
        assert abs(datetime.now(UTC) - timedelta(days=90) - cutoff) < timedelta(minutes=1)

    @pytest.mark.parametrize(
        ("kwargs", "message"),
        [
            ({"retention_days": 0}, "retention_days must be >= 1"),
            ({"batch_size": 0}, "batch_size must be >= 1"),
            ({"now": datetime(2026, 10, 16, 12, 0)}, "timezone-aware"),
        ],
    )
    async def test_invalid_arguments(self, kwargs, message):
        factory = MagicMock(side_effect=AssertionError("no database access expected"))
        arguments = {"retention_days": 90, "max_job_lifetime": MAX_JOB_LIFETIME, "now": NOW}
        arguments.update(kwargs)
        with pytest.raises(ValueError, match=message):
            await purge_expired_metadata(factory, **arguments)
        factory.assert_not_called()


# ===========================================================================
# Activity (real engine from the settings URL)
# ===========================================================================


class TestActivity:
    async def test_activity_reads_settings_and_returns_counts(self, retention_db, monkeypatch):
        now = datetime.now(UTC)
        old = await add_job(retention_db, created=now - timedelta(days=31))
        fresh = await add_job(retention_db, created=now - timedelta(days=29))
        abandoned = await add_job(retention_db, JobStatus.RUNNING, created=now - timedelta(days=31))
        old_audit = await add_audit(retention_db, at=now - timedelta(days=40))
        settings = SimpleNamespace(
            database_url=retention_db.url,
            metadata_retention_days=30,
            temporal_workflow_execution_timeout=36000,
            buffer_ttl_seconds=21600,
        )
        monkeypatch.setattr("api.config.get_settings", lambda: settings)

        result = await metadata_cleanup_expired_activity({})

        assert result["retention_days"] == 30
        assert result["abandoned_cleanup"] is True
        assert result["deleted"]["job_metadata"] == 1
        assert result["deleted"]["job_metadata_abandoned"] == 1
        assert result["deleted"]["audit_logs"] == 1
        assert await remaining(retention_db, "job_metadata") == {fresh}
        assert old not in await remaining(retention_db, "job_metadata")
        assert abandoned not in await remaining(retention_db, "job_metadata")
        assert old_audit not in await remaining(retention_db, "audit_logs")

    @pytest.mark.parametrize(
        ("execution_timeout", "buffer_ttl", "abandoned_cleanup"),
        [
            (36000, 21600, True),  # defaults: 16 h < 1 day
            (86400, 0, True),  # exactly 1 day
            (86400, 1, False),  # 1 day + 1 s > retention
            (172800, 21600, False),  # 2 days + 6 h
        ],
    )
    async def test_activity_guard_uses_execution_timeout_plus_buffer_ttl(
        self, retention_db, monkeypatch, execution_timeout, buffer_ttl, abandoned_cleanup
    ):
        settings = SimpleNamespace(
            database_url=retention_db.url,
            metadata_retention_days=1,
            temporal_workflow_execution_timeout=execution_timeout,
            buffer_ttl_seconds=buffer_ttl,
        )
        monkeypatch.setattr("api.config.get_settings", lambda: settings)
        purge_spy = AsyncMock(return_value={"ok": True})
        monkeypatch.setattr(maintenance, "purge_expired_metadata", purge_spy)

        assert await metadata_cleanup_expired_activity({}) == {"ok": True}

        kwargs = purge_spy.call_args.kwargs
        assert kwargs["retention_days"] == 1
        assert kwargs["max_job_lifetime"] == timedelta(seconds=execution_timeout + buffer_ttl)
        # the same comparison purge_expired_metadata makes
        assert (timedelta(days=1) >= kwargs["max_job_lifetime"]) is abandoned_cleanup

    async def test_activity_disposes_the_engine_on_error(self, monkeypatch):
        engine = MagicMock()
        engine.dispose = AsyncMock()
        monkeypatch.setattr(
            "api.config.get_settings",
            lambda: SimpleNamespace(
                database_url="sqlite+aiosqlite://",
                metadata_retention_days=90,
                temporal_workflow_execution_timeout=36000,
                buffer_ttl_seconds=21600,
            ),
        )
        monkeypatch.setattr("sqlalchemy.ext.asyncio.create_async_engine", lambda _url: engine)
        monkeypatch.setattr(
            maintenance, "purge_expired_metadata", AsyncMock(side_effect=RuntimeError("db down"))
        )

        with pytest.raises(RuntimeError, match="db down"):
            await metadata_cleanup_expired_activity()

        engine.dispose.assert_awaited_once()


# ===========================================================================
# Workflow (Temporal time-skipping test server)
# ===========================================================================


@pytest.mark.temporal
async def test_metadata_cleanup_workflow_retries_the_activity_and_returns_its_result():
    calls: list[tuple[int, Any]] = []
    summary = {"deleted": dict.fromkeys(METADATA_CLEANUP_COUNTS, 1), "abandoned_cleanup": True}

    @activity.defn(name="metadata_cleanup_expired")
    async def metadata_cleanup(payload: dict) -> dict:
        calls.append((activity.info().attempt, payload))
        if activity.info().attempt == 1:
            raise RuntimeError("database restarting")
        return summary

    async with await WorkflowEnvironment.start_time_skipping() as env:
        queue = f"retention-{uuid.uuid4().hex}"
        async with Worker(
            env.client,
            task_queue=queue,
            workflows=[MetadataCleanupWorkflow],
            activities=[metadata_cleanup],
        ):
            result = await env.client.execute_workflow(
                MetadataCleanupWorkflow.run, id=f"ret-{uuid.uuid4().hex}", task_queue=queue
            )

    assert result == summary
    assert calls == [(1, {}), (2, {})]


# ===========================================================================
# Schedule bootstrap (fake schedule client modelled on Temporal 1.2x)
# ===========================================================================


class FakeScheduleClient:
    """In-memory stand-in for the schedule API of ``temporalio.client.Client``.

    Mirrors what a real Temporal server (CLI 1.9.1 / server 1.32, checked
    manually) returns on describe: cron strings are compiled into calendar
    specs, ``cron_expressions`` comes back empty and a ``# comment`` survives
    as ``ScheduleCalendarSpec.comment``. ``keep_cron_strings=True`` models a
    server that returns the raw strings instead.
    """

    def __init__(self, *, keep_cron_strings: bool = False) -> None:
        self.keep_cron_strings = keep_cron_strings
        self.schedules: dict[str, Schedule] = {}
        self.created: list[str] = []
        self.updated: list[str] = []
        self.update_error: Exception | None = None
        self.create_error: Exception | None = None

    def _store(self, schedule_id: str, schedule: Schedule) -> None:
        spec = schedule.spec
        if not self.keep_cron_strings and spec.cron_expressions:
            calendars = [
                ScheduleCalendarSpec(comment=expr.partition("#")[2].strip() or None)
                for expr in spec.cron_expressions
            ]
            spec = dataclasses.replace(spec, calendars=calendars, cron_expressions=[])
        self.schedules[schedule_id] = dataclasses.replace(schedule, spec=spec)

    async def create_schedule(self, schedule_id: str, schedule: Schedule) -> None:
        if self.create_error is not None:
            raise self.create_error
        if schedule_id in self.schedules:
            raise ScheduleAlreadyRunningError()
        self.created.append(schedule_id)
        self._store(schedule_id, schedule)

    def get_schedule_handle(self, schedule_id: str) -> SimpleNamespace:
        async def _update(updater: Any) -> None:
            if self.update_error is not None:
                raise self.update_error
            if schedule_id not in self.schedules:
                raise RPCError("workflow not found", RPCStatusCode.NOT_FOUND, b"")
            description = SimpleNamespace(schedule=self.schedules[schedule_id])
            change = updater(SimpleNamespace(description=description))
            if change is not None:
                self.updated.append(schedule_id)
                self._store(schedule_id, change.schedule)

        return SimpleNamespace(update=_update)


_MANAGED = [
    pytest.param(
        ensure_kb_cleanup_schedule,
        "kb_cleanup",
        KB_CLEANUP_SCHEDULE_ID,
        "KBCleanupWorkflow",
        "0 3 * * *",
        id="kb",
    ),
    pytest.param(
        ensure_metadata_cleanup_schedule,
        "metadata_cleanup",
        METADATA_CLEANUP_SCHEDULE_ID,
        "MetadataCleanupWorkflow",
        "30 3 * * *",
        id="metadata",
    ),
]


def _settings(prefix: str, *, cron: str | None = None, enabled: bool = True, queue: str = "q"):
    values: dict[str, Any] = {"temporal_task_queue": queue, f"{prefix}_enabled": enabled}
    if cron is not None:
        values[f"{prefix}_cron"] = cron
    return SimpleNamespace(**values)


@pytest.mark.parametrize(
    ("ensure", "prefix", "schedule_id", "workflow_name", "default_cron"), _MANAGED
)
class TestScheduleBootstrap:
    async def test_creates_missing_schedule(
        self, ensure, prefix, schedule_id, workflow_name, default_cron
    ):
        client = FakeScheduleClient(keep_cron_strings=True)

        assert await ensure(client, _settings(prefix, cron="15 4 * * *", queue="eki-q")) is True

        assert client.created == [schedule_id]
        schedule = client.schedules[schedule_id]
        assert schedule.spec.cron_expressions == ["15 4 * * * # eki-cron=15 4 * * *"]
        assert schedule.state.paused is False
        assert schedule.policy.overlap is ScheduleOverlapPolicy.SKIP
        assert isinstance(schedule.action, ScheduleActionStartWorkflow)
        assert schedule.action.workflow == workflow_name
        assert schedule.action.task_queue == "eki-q"
        assert schedule.action.execution_timeout is not None

    async def test_defaults_when_settings_lack_the_fields(
        self, ensure, prefix, schedule_id, workflow_name, default_cron
    ):
        client = FakeScheduleClient(keep_cron_strings=True)
        assert await ensure(client, SimpleNamespace(temporal_task_queue="q")) is True
        expected = f"{default_cron} # eki-cron={default_cron}"
        assert client.schedules[schedule_id].spec.cron_expressions == [expected]

    async def test_missing_and_disabled_is_not_created(
        self, ensure, prefix, schedule_id, workflow_name, default_cron
    ):
        client = FakeScheduleClient()
        assert await ensure(client, _settings(prefix, enabled=False)) is False
        assert client.schedules == {}
        assert client.created == []

    @pytest.mark.parametrize("keep_cron_strings", [False, True])
    async def test_unchanged_settings_do_not_update(
        self, ensure, prefix, schedule_id, workflow_name, default_cron, keep_cron_strings
    ):
        client = FakeScheduleClient(keep_cron_strings=keep_cron_strings)
        settings = _settings(prefix, cron="15 4 * * *")
        await ensure(client, settings)

        assert await ensure(client, settings) is True
        assert await ensure(client, settings) is True

        assert client.created == [schedule_id]
        assert client.updated == []

    @pytest.mark.parametrize("keep_cron_strings", [False, True])
    async def test_changed_cron_updates_the_schedule(
        self, ensure, prefix, schedule_id, workflow_name, default_cron, keep_cron_strings
    ):
        client = FakeScheduleClient(keep_cron_strings=keep_cron_strings)
        await ensure(client, _settings(prefix, cron="15 4 * * *"))

        assert await ensure(client, _settings(prefix, cron="45 1 * * MON-FRI")) is True

        assert client.updated == [schedule_id]
        spec = client.schedules[schedule_id].spec
        if keep_cron_strings:
            assert spec.cron_expressions == ["45 1 * * MON-FRI # eki-cron=45 1 * * MON-FRI"]
        else:
            assert [c.comment for c in spec.calendars] == ["eki-cron=45 1 * * MON-FRI"]
        # and it is stable afterwards
        await ensure(client, _settings(prefix, cron="45 1 * * MON-FRI"))
        assert client.updated == [schedule_id]

    async def test_disabling_pauses_and_enabling_resumes(
        self, ensure, prefix, schedule_id, workflow_name, default_cron
    ):
        client = FakeScheduleClient()
        await ensure(client, _settings(prefix))

        assert await ensure(client, _settings(prefix, enabled=False)) is False
        state = client.schedules[schedule_id].state
        assert state.paused is True
        assert state.note == f"{prefix.upper()}_ENABLED=false"
        assert await ensure(client, _settings(prefix, enabled=False)) is False
        assert client.updated == [schedule_id]  # second call: already paused

        assert await ensure(client, _settings(prefix)) is True
        assert client.schedules[schedule_id].state.paused is False
        assert client.updated == [schedule_id, schedule_id]

    async def test_changed_task_queue_updates_the_schedule(
        self, ensure, prefix, schedule_id, workflow_name, default_cron
    ):
        client = FakeScheduleClient()
        await ensure(client, _settings(prefix, queue="old-q"))
        await ensure(client, _settings(prefix, queue="new-q"))
        assert client.updated == [schedule_id]
        assert client.schedules[schedule_id].action.task_queue == "new-q"

    @pytest.mark.parametrize(
        "legacy_spec",
        [
            # created by the pre-1.0.1 bootstrap: no marker comment
            ScheduleSpec(calendars=[ScheduleCalendarSpec()]),
            # edited by hand: an extra interval next to the cron
            ScheduleSpec(
                calendars=[ScheduleCalendarSpec(comment="eki-cron=0 3 * * *")],
                intervals=[ScheduleIntervalSpec(every=timedelta(hours=1))],
            ),
            # raw-string server: string without marker
            ScheduleSpec(cron_expressions=["0 3 * * *"]),
            # raw-string server: marker string plus a calendar
            ScheduleSpec(
                cron_expressions=["0 3 * * * # eki-cron=0 3 * * *"],
                calendars=[ScheduleCalendarSpec()],
            ),
        ],
    )
    async def test_drifted_spec_is_replaced(
        self, ensure, prefix, schedule_id, workflow_name, default_cron, legacy_spec
    ):
        client = FakeScheduleClient(keep_cron_strings=True)
        client.schedules[schedule_id] = Schedule(
            action=ScheduleActionStartWorkflow(workflow_name, id="x", task_queue="q"),
            spec=legacy_spec,
        )

        assert await ensure(client, _settings(prefix, cron="0 3 * * *")) is True

        assert client.updated == [schedule_id]
        assert client.schedules[schedule_id].spec == ScheduleSpec(
            cron_expressions=["0 3 * * * # eki-cron=0 3 * * *"]
        )

    async def test_foreign_workflow_type_is_replaced(
        self, ensure, prefix, schedule_id, workflow_name, default_cron
    ):
        client = FakeScheduleClient(keep_cron_strings=True)
        client.schedules[schedule_id] = Schedule(
            action=ScheduleActionStartWorkflow("SomethingElse", id="x", task_queue="q"),
            spec=ScheduleSpec(cron_expressions=[f"{default_cron} # eki-cron={default_cron}"]),
            state=ScheduleState(paused=False),
        )
        await ensure(client, _settings(prefix))
        assert client.updated == [schedule_id]
        assert client.schedules[schedule_id].action.workflow == workflow_name

    async def test_concurrent_create_is_tolerated(
        self, ensure, prefix, schedule_id, workflow_name, default_cron
    ):
        client = FakeScheduleClient()
        client.create_error = ScheduleAlreadyRunningError()
        assert await ensure(client, _settings(prefix)) is True

    async def test_other_rpc_errors_propagate(
        self, ensure, prefix, schedule_id, workflow_name, default_cron
    ):
        client = FakeScheduleClient()
        client.update_error = RPCError("unavailable", RPCStatusCode.UNAVAILABLE, b"")
        with pytest.raises(RPCError, match="unavailable"):
            await ensure(client, _settings(prefix))
        assert client.created == []


# ===========================================================================
# worker/main.py wiring
# ===========================================================================


@pytest.fixture
def worker_module(monkeypatch):
    import worker.main as module

    client = object()
    worker_instance = MagicMock()
    worker_instance.run = AsyncMock()
    mocks = SimpleNamespace(
        module=module,
        client=client,
        worker_cls=MagicMock(return_value=worker_instance),
        worker=worker_instance,
        kb=AsyncMock(return_value=True),
        metadata=AsyncMock(return_value=True),
        settings=SimpleNamespace(
            metrics_enabled=False,
            llm_provider="local_mistral",
            temporal_host="temporal.test:7233",
            temporal_namespace="default",
            temporal_task_queue="q",
            worker_max_concurrent_workflow_tasks=1,
            worker_max_concurrent_activities=1,
            max_concurrent_documents=1,
            ollama_max_concurrent_requests=1,
            otel_enabled=False,
            api_secret_key="worker-test-secret-key-with-32-chars!!",
        ),
    )
    monkeypatch.setattr(module, "get_settings", lambda: mocks.settings)
    monkeypatch.setattr(module, "Client", SimpleNamespace(connect=AsyncMock(return_value=client)))
    monkeypatch.setattr(module, "Worker", mocks.worker_cls)
    monkeypatch.setattr(module, "ensure_kb_cleanup_schedule", mocks.kb)
    monkeypatch.setattr(module, "ensure_metadata_cleanup_schedule", mocks.metadata)
    monkeypatch.setattr(module, "configure_tracing", MagicMock(return_value=False))
    # payload codec options (W-CODEC) are not under test here
    monkeypatch.setattr(module, "temporal_client_options", lambda *_a, **_kw: {}, raising=False)
    monkeypatch.setattr(module, "set_build_info", MagicMock())
    return mocks


@pytest.mark.asyncio
class TestWorkerWiring:
    async def test_registers_workflow_activity_and_both_schedules(self, worker_module):
        await worker_module.module.main()

        kwargs = worker_module.worker_cls.call_args.kwargs
        assert MetadataCleanupWorkflow in kwargs["workflows"]
        assert metadata_cleanup_expired_activity in kwargs["activities"]
        worker_module.kb.assert_awaited_once_with(worker_module.client, worker_module.settings)
        worker_module.metadata.assert_awaited_once_with(
            worker_module.client, worker_module.settings
        )
        worker_module.worker.run.assert_awaited_once()

    @pytest.mark.parametrize("failing", ["kb", "metadata"])
    async def test_one_failing_bootstrap_does_not_block_the_other(
        self, worker_module, failing, caplog
    ):
        getattr(worker_module, failing).side_effect = RuntimeError("schedule api down")

        with caplog.at_level(logging.WARNING, logger="worker.main"):
            await worker_module.module.main()

        worker_module.kb.assert_awaited_once()
        worker_module.metadata.assert_awaited_once()
        worker_module.worker.run.assert_awaited_once()
        assert any(
            "schedule bootstrap failed (non-fatal): schedule api down" in r.getMessage()
            for r in caplog.records
        )
