"""Maintenance workflow + activities (M09).

Currently hosts the Knowledge-Base TTL cleanup that was left as a manual
one-liner in M06/M08. It runs as a Temporal *Schedule* (cron) so no extra
cron container is needed and the run history is visible in the Temporal UI.

Schedule bootstrap is idempotent (``ensure_kb_cleanup_schedule``) and is
invoked from ``worker/main.py`` on every worker start.
"""

from __future__ import annotations

import logging
from datetime import timedelta
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
)
from temporalio.common import RetryPolicy

logger = logging.getLogger(__name__)

KB_CLEANUP_SCHEDULE_ID = "eki-kb-cleanup"
KB_CLEANUP_WORKFLOW_ID = "eki-kb-cleanup-run"


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


async def ensure_kb_cleanup_schedule(client: Client, settings: Any) -> bool:
    """Create the daily KB cleanup schedule if it does not exist yet.

    Returns ``True`` when a schedule exists after the call (created or
    pre-existing), ``False`` when disabled via ``KB_CLEANUP_ENABLED=false``.
    """
    if not getattr(settings, "kb_cleanup_enabled", True):
        logger.info("KB cleanup schedule disabled (KB_CLEANUP_ENABLED=false)")
        return False

    cron = str(getattr(settings, "kb_cleanup_cron", "0 3 * * *"))
    try:
        await client.create_schedule(
            KB_CLEANUP_SCHEDULE_ID,
            Schedule(
                action=ScheduleActionStartWorkflow(
                    KBCleanupWorkflow.run,
                    id=KB_CLEANUP_WORKFLOW_ID,
                    task_queue=settings.temporal_task_queue,
                    execution_timeout=timedelta(minutes=30),
                ),
                spec=ScheduleSpec(cron_expressions=[cron]),
                policy=SchedulePolicy(overlap=ScheduleOverlapPolicy.SKIP),
            ),
        )
        logger.info("KB cleanup schedule created: id=%s cron=%r", KB_CLEANUP_SCHEDULE_ID, cron)
    except ScheduleAlreadyRunningError:
        logger.info("KB cleanup schedule already exists: id=%s", KB_CLEANUP_SCHEDULE_ID)
    return True


__all__ = [
    "KB_CLEANUP_SCHEDULE_ID",
    "KBCleanupWorkflow",
    "ensure_kb_cleanup_schedule",
    "kb_cleanup_expired_activity",
]
