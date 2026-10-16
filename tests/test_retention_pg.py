"""PostgreSQL: retention index migration ``c4e6a8b0d2f1`` and the cleanup on the migrated schema.

Needs ``TEST_POSTGRES_URL`` (CI sets it); each test gets its own throw-away
database from the ``migration_db`` fixture of ``tests/test_pg_integration.py``.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any
from uuid import UUID, uuid4

import pytest
from alembic import command
from alembic.migration import MigrationContext
from sqlalchemy import insert, select, text
from sqlalchemy.engine import make_url
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from sqlalchemy.pool import NullPool

from core.db_models import ApiKeyModel, AuditLog, DeliveryDeadLetter, JobMetadata, ReportMetadata
from core.models import JobStatus, ScriptFormat
from tests import test_pg_integration
from tests.conftest import TEST_POSTGRES_URL, requires_postgres
from workflows.maintenance import purge_expired_metadata

pytestmark = requires_postgres

# Throw-away database per test, Alembic wired to it (see tests/test_pg_integration.py).
migration_db = test_pg_integration.migration_db

_REVISION = "c4e6a8b0d2f1"
_PREVIOUS = "b8d0f2a4c6e3"
_INDEXES = {
    "ix_job_metadata_created_at": ("job_metadata", "created_at"),
    "ix_report_metadata_created_at": ("report_metadata", "created_at"),
    "ix_delivery_dead_letters_created_at": ("delivery_dead_letters", "created_at"),
    "ix_audit_logs_timestamp": ("audit_logs", '"timestamp"'),
}
NOW = datetime(2026, 10, 16, 12, 0, tzinfo=UTC)
OLD = NOW - timedelta(days=91)
RECENT = NOW - timedelta(days=89)


def _index_defs(engine: Any) -> dict[str, str]:
    with engine.connect() as conn:
        rows = conn.execute(
            text("SELECT indexname, indexdef FROM pg_indexes WHERE indexname = ANY(:names)"),
            {"names": list(_INDEXES)},
        ).all()
    return dict(rows)


def _revision(engine: Any) -> str | None:
    with engine.connect() as conn:
        return MigrationContext.configure(conn).get_current_revision()


class TestRetentionIndexMigration:
    def test_upgrade_creates_and_downgrade_drops_the_indexes(self, migration_db):
        command.upgrade(migration_db.cfg, _PREVIOUS)
        assert _index_defs(migration_db.engine) == {}

        command.upgrade(migration_db.cfg, "head")
        assert _revision(migration_db.engine) == _REVISION
        definitions = _index_defs(migration_db.engine)
        assert set(definitions) == set(_INDEXES)
        for name, (table, column) in _INDEXES.items():
            assert f"ON public.{table} USING btree ({column})" in definitions[name]
            assert "UNIQUE" not in definitions[name]
        assert (
            test_pg_integration._schema_diff(migration_db.engine) == []
        )  # model and migration agree

        command.downgrade(migration_db.cfg, _PREVIOUS)
        assert _revision(migration_db.engine) == _PREVIOUS
        assert _index_defs(migration_db.engine) == {}

        command.upgrade(migration_db.cfg, "head")
        assert set(_index_defs(migration_db.engine)) == set(_INDEXES)

    @pytest.mark.parametrize(("index", "table", "column"), [(n, *tc) for n, tc in _INDEXES.items()])
    def test_cleanup_predicate_can_use_the_index(self, migration_db, index, table, column):
        command.upgrade(migration_db.cfg, "head")
        with migration_db.engine.begin() as conn:
            conn.execute(text("SET LOCAL enable_seqscan = off"))
            plan = "\n".join(
                conn.execute(
                    text(
                        f"EXPLAIN SELECT 1 FROM {table} "  # nosec B608 - constants
                        f"WHERE {column} < now() - interval '90 days' LIMIT 1000"
                    )
                ).scalars()
            )
        assert index in plan


async def test_purge_on_the_migrated_schema(migration_db):
    """The cleanup against the Alembic schema (VARCHAR status, server defaults), not create_all."""
    command.upgrade(migration_db.cfg, "head")
    ids: dict[str, UUID] = {}

    def job(
        label: str, status: JobStatus, created: datetime, updated: datetime | None = None
    ) -> dict[str, Any]:
        ids[label] = uuid4()
        return {
            "job_id": ids[label],
            "project_id": "p",
            "script_format": ScriptFormat.FDX,
            "status": status,
            "user_id": "u",
            "created_at": created,
            "updated_at": updated or created,
        }

    with migration_db.engine.begin() as conn:
        conn.execute(
            insert(JobMetadata.__table__),
            [
                job("old_completed", JobStatus.COMPLETED, OLD),
                job("old_failed", JobStatus.FAILED, OLD),
                job("old_cancelled", JobStatus.CANCELLED, OLD),
                job("old_pending", JobStatus.PENDING, OLD),
                job("old_running", JobStatus.RUNNING, OLD),
                job("old_delivering", JobStatus.DELIVERING, OLD),
                job("recent_completed", JobStatus.COMPLETED, RECENT),
                job("live_delivering", JobStatus.DELIVERING, OLD, RECENT),
            ],
        )
        conn.execute(
            insert(ReportMetadata.__table__),
            [
                {
                    "report_id": uuid4(),
                    "job_id": ids[job_label],
                    "project_id": "p",
                    "user_id": "u",
                    "script_format": ScriptFormat.FDX,
                    "processing_time_seconds": 1.0,
                    "created_at": OLD,
                }
                for job_label in ("old_completed", "old_delivering", "live_delivering")
            ],
        )
        conn.execute(
            insert(DeliveryDeadLetter.__table__),
            [
                {"id": uuid4(), "job_id": ids["old_failed"], "project_id": "p", "user_id": "u"}
                | {"reason": "delivery_failed", "created_at": created}
                for created in (OLD, RECENT)
            ],
        )
        conn.execute(
            insert(ApiKeyModel.__table__),
            [
                {"id": uuid4(), "user_id": "u", "key_hash": uuid4().hex * 2, "name": "k"}
                | {"is_active": active, "created_at": OLD, "expires_at": expires}
                for active, expires in ((True, NOW + timedelta(days=30)), (False, OLD))
            ],
        )
        conn.execute(
            insert(AuditLog.__table__),
            [
                {"id": uuid4(), "endpoint": "/", "method": "GET", "status_code": 200}
                | {"timestamp": at}
                for at in (OLD, RECENT)
            ],
        )

    engine = create_async_engine(
        make_url(TEST_POSTGRES_URL).set(database=migration_db.name), poolclass=NullPool
    )
    try:
        result = await purge_expired_metadata(
            async_sessionmaker(engine, expire_on_commit=False),
            retention_days=90,
            max_job_lifetime=timedelta(hours=16),
            now=NOW,
        )
    finally:
        await engine.dispose()

    assert result["deleted"] == {
        "report_metadata": 2,  # the live DELIVERING job's report stays
        "delivery_dead_letters": 1,
        "job_metadata": 3,
        "job_metadata_abandoned": 3,  # old pending/running/delivering
        "api_keys": 1,  # the active, unexpired key stays
        "audit_logs": 1,
    }
    assert result["abandoned_cleanup"] is True
    jobs = JobMetadata.__table__
    with migration_db.engine.connect() as conn:
        kept_jobs = set(conn.execute(select(jobs.c.job_id)).scalars())
        kept_report_jobs = set(conn.execute(select(ReportMetadata.__table__.c.job_id)).scalars())
        active_keys = conn.execute(select(ApiKeyModel.__table__.c.is_active)).scalars().all()
    assert kept_jobs == {ids["recent_completed"], ids["live_delivering"]}
    assert kept_report_jobs == {ids["live_delivering"]}
    assert active_keys == [True]
