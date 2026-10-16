"""Maintenance workflows + activities (M09, 1.0.1).

* **KB TTL cleanup** (schedule ``eki-kb-cleanup``, ``KB_CLEANUP_*``): deletes
  expired knowledge-base documents (M06/M08 left this as a manual one-liner).
* **Metadata retention** (schedule ``eki-metadata-cleanup``,
  ``METADATA_RETENTION_DAYS`` / ``METADATA_CLEANUP_*``): deletes job/report
  metadata, dead letters, unusable API keys and audit logs once their latest
  timestamp is older than the retention period (see ``purge_expired_metadata``).

Both run as Temporal *Schedules* (cron) so no extra cron container is needed
and the run history is visible in the Temporal UI.

``worker/main.py`` reconciles both schedules on every worker start
(``ensure_kb_cleanup_schedule`` / ``ensure_metadata_cleanup_schedule``): a
missing schedule is created, an existing one is updated when its cron, its
enabled state (``*_ENABLED=false`` pauses it) or its target differ from the
settings. The Temporal server compiles cron strings into calendar specs and
does not return the original string, so the configured cron is also stored as
a ``# eki-cron=<cron>`` comment, which the server keeps on the calendar.
"""

from __future__ import annotations

import logging
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any

from temporalio import activity, workflow
from temporalio.client import (
    Client,
    Schedule,
    ScheduleActionStartWorkflow,
    ScheduleAlreadyRunningError,
    ScheduleOverlapPolicy,
    SchedulePolicy,
    ScheduleSpec,
    ScheduleState,
    ScheduleUpdate,
    ScheduleUpdateInput,
)
from temporalio.common import RetryPolicy
from temporalio.service import RPCError, RPCStatusCode

logger = logging.getLogger(__name__)

KB_CLEANUP_SCHEDULE_ID = "eki-kb-cleanup"
KB_CLEANUP_WORKFLOW_ID = "eki-kb-cleanup-run"

METADATA_CLEANUP_SCHEDULE_ID = "eki-metadata-cleanup"
METADATA_CLEANUP_WORKFLOW_ID = "eki-metadata-cleanup-run"
# Rows per DELETE transaction; keeps locks and WAL bursts small.
METADATA_CLEANUP_BATCH_SIZE = 1000
# Tables the retention cleanup deletes from.
METADATA_TABLES = (
    "report_metadata",
    "delivery_dead_letters",
    "job_metadata",
    "api_keys",
    "audit_logs",
)
# Keys of the result's ``deleted`` counts (= ``table`` label of the metric), in
# deletion order. There are no foreign keys between the tables; rows that
# reference a job (reports, dead letters) simply go before the jobs.
# ``job_metadata`` counts terminal jobs, ``job_metadata_abandoned`` jobs that
# were still pending/running/delivering (both are rows of ``job_metadata``).
METADATA_CLEANUP_COUNTS = (
    "report_metadata",
    "delivery_dead_letters",
    "job_metadata",
    "job_metadata_abandoned",
    "api_keys",
    "audit_logs",
)

# Marker the schedule bootstrap writes next to the cron expression.
_CRON_MARKER = "eki-cron="


# ===========================================================================
# KB TTL cleanup
# ===========================================================================


@activity.defn(name="kb_cleanup_expired")
async def kb_cleanup_expired_activity(_payload: dict[str, Any] | None = None) -> dict[str, Any]:
    """Delete expired KB documents and refresh the ``eki_kb_documents`` gauge."""
    from sqlalchemy import func, select
    from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

    from api.config import get_settings
    from core import metrics
    from core.db_models import KnowledgeDocument
    from llm.factory import get_llm_provider
    from services.knowledge_base import KnowledgeBaseService

    settings = get_settings()
    engine = create_async_engine(str(settings.database_url))
    session_factory = async_sessionmaker(engine, expire_on_commit=False)

    try:
        async with session_factory() as session:
            kb = KnowledgeBaseService(
                db=session,
                llm=get_llm_provider(settings),
                secret_key=settings.api_secret_key,
            )
            removed = await kb.cleanup_expired()
            remaining = (
                await session.execute(select(func.count()).select_from(KnowledgeDocument))
            ).scalar_one()
    finally:
        await engine.dispose()

    metrics.KB_CLEANUP_REMOVED_TOTAL.inc(removed)
    metrics.KB_DOCUMENTS.set(int(remaining))
    logger.info("KB TTL cleanup: removed=%d remaining=%d", removed, remaining)
    return {"removed": int(removed), "remaining": int(remaining)}


@workflow.defn(name="KBCleanupWorkflow")
class KBCleanupWorkflow:
    """One scheduled run of the KB TTL cleanup."""

    @workflow.run
    async def run(self) -> dict[str, Any]:
        return await workflow.execute_activity(
            kb_cleanup_expired_activity,
            {},
            start_to_close_timeout=timedelta(minutes=5),
            retry_policy=RetryPolicy(maximum_attempts=3, initial_interval=timedelta(seconds=10)),
        )


# ===========================================================================
# Metadata retention
# ===========================================================================


async def purge_expired_metadata(
    session_factory: Callable[[], Any],
    *,
    retention_days: int,
    max_job_lifetime: timedelta,
    batch_size: int = METADATA_CLEANUP_BATCH_SIZE,
    now: datetime | None = None,
) -> dict[str, Any]:
    """Delete metadata whose latest timestamp is older than ``now - retention_days``.

    A row is deleted only when **all** of its timestamps lie before the cutoff
    (``NULL`` = never happened), in this order:

    * ``report_metadata``: ``created_at``, ``retrieved_at``; kept while its job
      is still live (pending/running/delivering and not abandoned).
    * ``delivery_dead_letters``: ``created_at``, ``acknowledged_at``
      (acknowledged or not).
    * ``job_metadata``: terminal jobs (completed, failed, cancelled) by
      ``created_at`` and ``updated_at``.
    * ``job_metadata_abandoned``: jobs still pending/running/delivering whose
      ``created_at`` and ``updated_at`` are older than the cutoff. No workflow
      lives longer than *max_job_lifetime* (execution timeout + pull TTL), so
      such a row can only belong to a workflow that ended without updating
      the database. Deleted only while ``cutoff <= now - max_job_lifetime``;
      otherwise (misconfiguration) they are kept and an error is logged.
    * ``api_keys``: only keys that can no longer authenticate (deactivated, or
      expired before the cutoff); ``created_at``, ``last_used_at``. An active
      key that has not expired is never deleted.
    * ``audit_logs``: ``timestamp``.

    Every batch of ``batch_size`` rows is its own transaction, and the delete
    re-checks the conditions so a row that changed in between (e.g. a
    re-activated key or a job that made progress) survives. Only counts are
    logged and returned.
    """
    from sqlalchemy import ColumnElement, and_, exists, not_, or_

    from core import metrics
    from core.db_models import (
        ApiKeyModel,
        AuditLog,
        DeliveryDeadLetter,
        JobMetadata,
        ReportMetadata,
    )
    from core.models import JobStatus

    if retention_days < 1:
        raise ValueError("retention_days must be >= 1")
    if batch_size < 1:
        raise ValueError("batch_size must be >= 1")
    now = now or datetime.now(UTC)
    if now.tzinfo is None:
        raise ValueError("now must be timezone-aware")
    cutoff = now - timedelta(days=retention_days)
    # Guard: a job untouched since the cutoff can only be abandoned if no
    # workflow can live that long. Never treat a possibly live job as abandoned.
    abandoned_cleanup = cutoff <= now - max_job_lifetime

    active_job_statuses = (JobStatus.PENDING, JobStatus.RUNNING, JobStatus.DELIVERING)
    terminal_job_statuses = (JobStatus.COMPLETED, JobStatus.FAILED, JobStatus.CANCELLED)
    jobs = JobMetadata.__table__
    reports = ReportMetadata.__table__
    dead_letters = DeliveryDeadLetter.__table__
    keys = ApiKeyModel.__table__
    audit = AuditLog.__table__

    def before_cutoff(column: Any) -> Any:
        """Nullable timestamp: ``NULL`` (never happened) counts as old."""
        return or_(column.is_(None), column < cutoff)

    untouched_since_cutoff = and_(jobs.c.created_at < cutoff, jobs.c.updated_at < cutoff)
    live_job: ColumnElement[bool] = jobs.c.status.in_(active_job_statuses)
    if abandoned_cleanup:
        live_job = and_(live_job, not_(untouched_since_cutoff))

    plan: list[tuple[str, Any, Any, Sequence[Any]]] = [
        (
            "report_metadata",
            reports,
            reports.c.report_id,
            (
                reports.c.created_at < cutoff,
                before_cutoff(reports.c.retrieved_at),
                ~exists().where(jobs.c.job_id == reports.c.job_id, live_job),
            ),
        ),
        (
            "delivery_dead_letters",
            dead_letters,
            dead_letters.c.id,
            (dead_letters.c.created_at < cutoff, before_cutoff(dead_letters.c.acknowledged_at)),
        ),
        (
            "job_metadata",
            jobs,
            jobs.c.job_id,
            (jobs.c.status.in_(terminal_job_statuses), untouched_since_cutoff),
        ),
        (
            "job_metadata_abandoned",
            jobs,
            jobs.c.job_id,
            (jobs.c.status.in_(active_job_statuses), untouched_since_cutoff),
        ),
        (
            "api_keys",
            keys,
            keys.c.id,
            (
                or_(keys.c.is_active.is_(False), keys.c.expires_at < cutoff),
                keys.c.created_at < cutoff,
                before_cutoff(keys.c.last_used_at),
            ),
        ),
        ("audit_logs", audit, audit.c.id, (audit.c.timestamp < cutoff,)),
    ]

    deleted: dict[str, int] = {}
    for name, table, primary_key, conditions in plan:
        if name == "job_metadata_abandoned" and not abandoned_cleanup:
            deleted[name] = 0
            logger.error(
                "Metadata retention cleanup: abandoned jobs NOT deleted -- the cutoff "
                "(%d days) is not older than the maximum job lifetime (%s = workflow "
                "execution timeout + buffer TTL); raise METADATA_RETENTION_DAYS",
                retention_days,
                max_job_lifetime,
            )
            continue
        deleted[name] = await _delete_in_batches(
            session_factory, table, primary_key, conditions, batch_size
        )
        metrics.METADATA_CLEANUP_DELETED_TOTAL.labels(table=name).inc(deleted[name])

    logger.info(
        "Metadata retention cleanup: retention_days=%d cutoff=%s deleted %s",
        retention_days,
        cutoff.isoformat(),
        " ".join(f"{name}={count}" for name, count in deleted.items()),
    )
    if deleted["job_metadata_abandoned"]:
        logger.warning(
            "Metadata retention cleanup: deleted %d abandoned job(s) that were still "
            "pending/running/delivering without any update since the cutoff",
            deleted["job_metadata_abandoned"],
        )
    return {
        "retention_days": retention_days,
        "cutoff": cutoff.isoformat(),
        "deleted": deleted,
        "abandoned_cleanup": abandoned_cleanup,
    }


async def _delete_in_batches(
    session_factory: Callable[[], Any],
    table: Any,
    primary_key: Any,
    conditions: Sequence[Any],
    batch_size: int,
) -> int:
    """Delete matching rows ``batch_size`` at a time, one transaction per batch."""
    from sqlalchemy import delete, select

    total = 0
    while True:
        async with session_factory() as session:
            ids = (
                (await session.execute(select(primary_key).where(*conditions).limit(batch_size)))
                .scalars()
                .all()
            )
            if not ids:
                return total
            result = await session.execute(delete(table).where(primary_key.in_(ids), *conditions))
            await session.commit()
        total += max(int(result.rowcount or 0), 0)
        if len(ids) < batch_size:
            return total


@activity.defn(name="metadata_cleanup_expired")
async def metadata_cleanup_expired_activity(
    _payload: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Apply ``METADATA_RETENTION_DAYS`` to the metadata tables (counts only)."""
    from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

    from api.config import get_settings

    settings = get_settings()
    # Longest possible workflow: execution timeout (10 h) covers processing,
    # push retries and the pull TTL watch; the buffer TTL (6 h) is added as margin.
    max_job_lifetime = timedelta(
        seconds=int(settings.temporal_workflow_execution_timeout) + int(settings.buffer_ttl_seconds)
    )
    engine = create_async_engine(str(settings.database_url))
    try:
        return await purge_expired_metadata(
            async_sessionmaker(engine, expire_on_commit=False),
            retention_days=int(settings.metadata_retention_days),
            max_job_lifetime=max_job_lifetime,
        )
    finally:
        await engine.dispose()


@workflow.defn(name="MetadataCleanupWorkflow")
class MetadataCleanupWorkflow:
    """One scheduled run of the metadata retention cleanup."""

    @workflow.run
    async def run(self) -> dict[str, Any]:
        return await workflow.execute_activity(
            metadata_cleanup_expired_activity,
            {},
            start_to_close_timeout=timedelta(minutes=15),
            retry_policy=RetryPolicy(maximum_attempts=3, initial_interval=timedelta(seconds=30)),
        )


# ===========================================================================
# Schedule bootstrap (create or reconcile on every worker start)
# ===========================================================================


@dataclass(frozen=True)
class _ManagedSchedule:
    schedule_id: str
    workflow_name: str
    workflow_run: Callable[..., Any]
    workflow_id: str
    execution_timeout: timedelta
    setting_prefix: str  # settings ``<prefix>_enabled`` / ``<prefix>_cron``
    default_cron: str


_KB_CLEANUP = _ManagedSchedule(
    schedule_id=KB_CLEANUP_SCHEDULE_ID,
    workflow_name="KBCleanupWorkflow",
    workflow_run=KBCleanupWorkflow.run,
    workflow_id=KB_CLEANUP_WORKFLOW_ID,
    execution_timeout=timedelta(minutes=30),
    setting_prefix="kb_cleanup",
    default_cron="0 3 * * *",
)
_METADATA_CLEANUP = _ManagedSchedule(
    schedule_id=METADATA_CLEANUP_SCHEDULE_ID,
    workflow_name="MetadataCleanupWorkflow",
    workflow_run=MetadataCleanupWorkflow.run,
    workflow_id=METADATA_CLEANUP_WORKFLOW_ID,
    execution_timeout=timedelta(hours=1),
    setting_prefix="metadata_cleanup",
    default_cron="30 3 * * *",
)


def _cron_expression(cron: str) -> str:
    return f"{cron} # {_CRON_MARKER}{cron}"


def _build_schedule(
    managed: _ManagedSchedule, *, cron: str, enabled: bool, task_queue: str
) -> Schedule:
    return Schedule(
        action=ScheduleActionStartWorkflow(
            managed.workflow_run,
            id=managed.workflow_id,
            task_queue=task_queue,
            execution_timeout=managed.execution_timeout,
        ),
        spec=ScheduleSpec(cron_expressions=[_cron_expression(cron)]),
        policy=SchedulePolicy(overlap=ScheduleOverlapPolicy.SKIP),
        state=ScheduleState(
            paused=not enabled,
            note=None if enabled else f"{managed.setting_prefix.upper()}_ENABLED=false",
        ),
    )


def _schedule_is_current(
    current: Schedule, managed: _ManagedSchedule, *, cron: str, enabled: bool, task_queue: str
) -> bool:
    """True when *current* already has the configured cron, state and target."""
    spec = current.spec
    if spec.cron_expressions:  # a server that keeps the raw strings
        cron_matches = list(spec.cron_expressions) == [_cron_expression(cron)]
        cron_matches = cron_matches and not spec.calendars
    else:  # Temporal: cron compiled into calendars, comment preserved
        cron_matches = [c.comment for c in spec.calendars] == [f"{_CRON_MARKER}{cron}"]
    action = current.action
    target_matches = (
        isinstance(action, ScheduleActionStartWorkflow)
        and action.workflow == managed.workflow_name
        and action.task_queue == task_queue
    )
    return (
        cron_matches
        and not spec.intervals
        and target_matches
        and bool(current.state.paused) == (not enabled)
    )


async def _ensure_schedule(client: Client, settings: Any, managed: _ManagedSchedule) -> bool:
    """Create *managed* if missing, otherwise update it when it drifted from the settings.

    Returns ``True`` when the schedule exists and is active after the call,
    ``False`` when it is disabled (``<PREFIX>_ENABLED=false``: an existing
    schedule is paused, a missing one is not created).
    """
    prefix = managed.setting_prefix
    enabled = bool(getattr(settings, f"{prefix}_enabled", True))
    cron = str(getattr(settings, f"{prefix}_cron", managed.default_cron))
    task_queue = str(settings.temporal_task_queue)
    desired = _build_schedule(managed, cron=cron, enabled=enabled, task_queue=task_queue)
    changed = False

    def _updater(update_input: ScheduleUpdateInput) -> ScheduleUpdate | None:
        nonlocal changed
        current = update_input.description.schedule
        if _schedule_is_current(
            current, managed, cron=cron, enabled=enabled, task_queue=task_queue
        ):
            return None
        changed = True
        return ScheduleUpdate(schedule=desired)

    try:
        await client.get_schedule_handle(managed.schedule_id).update(_updater)
    except RPCError as exc:
        if exc.status != RPCStatusCode.NOT_FOUND:
            raise
    else:
        logger.info(
            "Schedule %s %s: cron=%r paused=%s",
            managed.schedule_id,
            "updated" if changed else "up to date",
            cron,
            not enabled,
        )
        return enabled

    if not enabled:
        logger.info(
            "Schedule %s not created (%s_ENABLED=false)", managed.schedule_id, prefix.upper()
        )
        return False
    try:
        await client.create_schedule(managed.schedule_id, desired)
        logger.info("Schedule %s created: cron=%r", managed.schedule_id, cron)
    except ScheduleAlreadyRunningError:  # another worker created it in the meantime
        logger.info("Schedule %s already exists", managed.schedule_id)
    return True


async def ensure_kb_cleanup_schedule(client: Client, settings: Any) -> bool:
    """Create or reconcile the KB TTL cleanup schedule (``KB_CLEANUP_*``)."""
    return await _ensure_schedule(client, settings, _KB_CLEANUP)


async def ensure_metadata_cleanup_schedule(client: Client, settings: Any) -> bool:
    """Create or reconcile the metadata retention schedule (``METADATA_CLEANUP_*``)."""
    return await _ensure_schedule(client, settings, _METADATA_CLEANUP)


__all__ = [
    "KB_CLEANUP_SCHEDULE_ID",
    "METADATA_CLEANUP_COUNTS",
    "METADATA_CLEANUP_SCHEDULE_ID",
    "METADATA_TABLES",
    "KBCleanupWorkflow",
    "MetadataCleanupWorkflow",
    "ensure_kb_cleanup_schedule",
    "ensure_metadata_cleanup_schedule",
    "kb_cleanup_expired_activity",
    "metadata_cleanup_expired_activity",
    "purge_expired_metadata",
]
