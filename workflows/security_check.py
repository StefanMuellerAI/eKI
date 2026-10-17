"""Temporal workflows for FDX and PDF security check processing.

The main ``SecurityCheckWorkflow`` routes to format-specific logic:
- FDX: parse -> analyze per scene -> aggregate report -> deliver
- PDF: extract text -> split -> LLM structure per scene -> aggregate script
       -> analyze per scene -> aggregate report -> deliver

Activities exchange only encrypted Redis reference keys.

M07: Die LLM-Strukturierungs- und Risikoanalyse-Schleifen können wahlweise
parallelisiert werden. Geschützt wird das durch zwei Schalter, die per
job_data deterministisch in den Workflow getragen werden:

* ``llm_parallel_enabled``        -> Master-Flag (Default false)
* ``pdf_structure_concurrency``   -> per-Workflow-Cap für structure_scene_llm
* ``risk_analysis_concurrency``   -> per-Workflow-Cap für analyze_scene_risk

Zusätzlich greift der prozessweite Ollama-Semaphore in ``llm/ollama.py``,
damit auch parallele Workflows die LLM-Last nie über
``ollama_max_concurrent_requests`` heben können.

M13 (Release 1.0 hardening) -- replay safety
-------------------------------------------
Workflows that are already running must replay unchanged after a deploy.
Every M13 change that adds/removes Temporal commands on a path that may
already exist in recorded histories is guarded by ``workflow.patched``:

* ``_PATCH_PARALLEL_DRAIN``  -- parallel loop drains in-flight activities on
  the first failure instead of failing fast (no orphaned activities, no
  progress update after the job was marked failed).
* ``_PATCH_FAILURE_CLEANUP`` -- processing failures delete the uploaded
  script and all intermediate SecureBuffer refs (cleanup_buffer activity).
* ``_PATCH_CANCELLATION``    -- a workflow cancel marks the job ``cancelled``
  (plus buffer cleanup) and re-raises instead of being recorded as failure.
* ``_PATCH_FAILURE_WEBHOOK`` -- processing failures also send the content-free
  ``security.delivery.failed`` webhook (the report can never be delivered).

All other M13 changes only alter activity *arguments/results* or activity
code, which Temporal does not compare during replay.

Document limit (``MAX_CONCURRENT_DOCUMENTS``)
--------------------------------------------
A job whose ``job_data`` carries ``max_concurrent_documents > 0`` first waits
for a processing slot (``acquire_document_slot``, polled with durable timers,
status stays ``pending``) and gives it back after the risk analysis or when it
fails / is cancelled (``release_document_slot``). The step is gated by the
workflow *input*, not by ``workflow.patched``: histories recorded before the
change never carry the key, so they replay without the new commands.
"""

import asyncio
import logging
from collections.abc import Awaitable, Callable
from datetime import timedelta
from typing import Any

from temporalio import workflow
from temporalio.common import RetryPolicy
from temporalio.exceptions import ActivityError, ApplicationError, is_cancelled_exception

with workflow.unsafe.imports_passed_through():
    from workflows.activities import (
        CONTENT_FREE_ERROR_TYPES,
        ERROR_TYPE_BUFFER_EXPIRED,
        ERROR_TYPE_INVALID_CALLBACK_URL,
        ERROR_TYPE_INVALID_PROJECT_ID,
        ERROR_TYPE_PARSING_FAILED,
        ERROR_TYPE_PROMPT_BLOCKED,
        ERROR_TYPE_QUEUE_TIMEOUT,
        acquire_document_slot_activity,
        aggregate_report_activity,
        aggregate_script_activity,
        analyze_scene_risk_activity,
        check_report_retrieved_activity,
        cleanup_buffer_activity,
        deliver_report_activity,
        extract_pdf_text_activity,
        parse_fdx_activity,
        record_dead_letter_activity,
        release_document_slot_activity,
        send_delivery_failed_webhook_activity,
        split_scenes_activity,
        structure_scene_llm_activity,
        update_job_status_activity,
    )

logger = logging.getLogger(__name__)

# Shared retry policies
_RETRY_STANDARD = RetryPolicy(
    maximum_attempts=3,
    initial_interval=timedelta(seconds=1),
    maximum_interval=timedelta(seconds=30),
    backoff_coefficient=2.0,
)
_RETRY_LLM = RetryPolicy(
    maximum_attempts=2,
    initial_interval=timedelta(seconds=5),
    maximum_interval=timedelta(minutes=1),
    backoff_coefficient=2.0,
)
_RETRY_DELIVERY = RetryPolicy(
    initial_interval=timedelta(seconds=2),
    maximum_interval=timedelta(minutes=10),
    backoff_coefficient=2.0,
)
# M13: terminal status updates (failed/cancelled) must survive a short DB
# outage -- update_job_status now raises on DB errors instead of returning
# {"updated": False}. ~3.5 min of retries before the update is given up.
_RETRY_STATUS_TERMINAL = RetryPolicy(
    maximum_attempts=8,
    initial_interval=timedelta(seconds=2),
    maximum_interval=timedelta(minutes=1),
    backoff_coefficient=2.0,
)
# M08: ``maximum_attempts`` bewusst NICHT gesetzt. Die Begrenzung erfolgt
# ueber das ``schedule_to_close_timeout`` von 6 Stunden im Aufrufer von
# deliver_report_activity (Pflichtenheft Abnahmetest 4). 4xx-Hard-Fails
# werden in der Activity selbst erkannt und ohne Retry direkt
# zurueckgegeben (siehe ``deliver_report_activity``).
_DELIVERY_SCHEDULE_TO_CLOSE = timedelta(hours=6)
_DELIVERY_START_TO_CLOSE_PER_ATTEMPT = timedelta(minutes=5)
# M10: Pull-Modus -- so lange darf der Report unabgeholt im Buffer liegen,
# bevor Loeschung + Webhook + Dead Letter greifen (Pflichtenheft Abnahmetest 4
# gilt fuer beide Lieferwege). Der Wert wird beim Workflow-Start in job_data
# eingefroren (``buffer_ttl_seconds``); Fallback = 6h.
_DEFAULT_PULL_TTL_SECONDS = 6 * 3600

# M13 replay-safety patch ids (see module docstring).
_PATCH_PARALLEL_DRAIN = "eki-m13-parallel-drain-on-failure"
_PATCH_FAILURE_CLEANUP = "eki-m13-cleanup-on-processing-failure"
_PATCH_CANCELLATION = "eki-m13-cancelled-job-status"
_PATCH_FAILURE_WEBHOOK = "eki-m13-processing-failure-webhook"

# Non-retryable delivery failures that are not an exhausted retry window.
_DELIVERY_FAILURE_REASONS = {
    ERROR_TYPE_BUFFER_EXPIRED: "report_expired",
    ERROR_TYPE_INVALID_PROJECT_ID: "invalid_project_id",
    # 1.0.1: callback_url failed the delivery-time policy check (host no longer
    # allow-listed, resolves to a private address, ...). Same command sequence
    # as every other non-retryable delivery failure -> replay-safe.
    ERROR_TYPE_INVALID_CALLBACK_URL: "invalid_callback_url",
}
_TERMINAL_STATUSES = frozenset({"failed", "cancelled"})

# Document limit: a waiting job polls for a slot, starting after 5 s and
# doubling up to once a minute (bounded history: ~10 events per poll).
_SLOT_POLL_INITIAL = timedelta(seconds=5)
_SLOT_POLL_MAX = timedelta(seconds=60)
_SLOT_ACTIVITY_TIMEOUT = timedelta(seconds=30)
_DEFAULT_QUEUE_TIMEOUT_SECONDS = 7200

# ``reason`` of the security.delivery.failed webhook for processing failures.
_PROCESSING_FAILURE_REASONS = {
    ERROR_TYPE_PROMPT_BLOCKED: "prompt_blocked",
    ERROR_TYPE_PARSING_FAILED: "parsing_failed",
}
_PROCESSING_FAILURE_DEFAULT_REASON = "processing_failed"


def _processing_failure_reason(exc: BaseException) -> str:
    cause = exc.cause if isinstance(exc, ActivityError) else exc
    cause_type = cause.type if isinstance(cause, ApplicationError) else None
    return _PROCESSING_FAILURE_REASONS.get(cause_type or "", _PROCESSING_FAILURE_DEFAULT_REASON)


def _failure_message(exc: BaseException) -> str:
    """Content-free ``error_message`` for a failed job (M13).

    Activity failures of a type from ``CONTENT_FREE_ERROR_TYPES`` carry a
    message written for the user (e.g. "Parsing fehlgeschlagen: ...",
    "Szene 4: vom Prompt-Injection-Filter blockiert ..."); everything else is
    reported by activity name and exception class only, never by message.
    """
    cause = exc.cause if isinstance(exc, ActivityError) else exc
    if isinstance(cause, ApplicationError) and cause.type in CONTENT_FREE_ERROR_TYPES:
        return cause.message
    if isinstance(exc, ActivityError):
        if isinstance(cause, ApplicationError) and cause.type:
            kind = cause.type
        elif cause is not None:
            kind = type(cause).__name__
        else:
            kind = "unknown"
        return f"{exc.activity_type or 'Activity'} fehlgeschlagen ({kind})"
    return f"Workflow-Fehler ({type(exc).__name__})"


def _resolve_concurrency(job_data: dict[str, Any], key: str) -> int:
    """Return the effective concurrency for a per-scene loop.

    Wenn ``llm_parallel_enabled`` nicht explizit True ist, wird IMMER 1
    zurückgegeben -- damit greift der strikt sequenzielle Pfad und das
    Verhalten ist bytewise identisch zu M06. Das gilt auch für
    Workflows, die noch ohne die neuen Felder gestartet wurden (sicheres
    Default-Verhalten).
    """
    if not job_data.get("llm_parallel_enabled", False):
        return 1
    value = job_data.get(key, 1) or 1
    try:
        value = int(value)
    except (TypeError, ValueError):
        return 1
    return max(1, value)


def _resolve_activity_timeout(job_data: dict[str, Any]) -> timedelta:
    """Return the start_to_close_timeout for LLM activities.

    Default 600s entspricht der vor M07 hardcodeten 5-Minuten-Marke,
    verdoppelt, um Backoff bei voller Ollama-Queue abzufedern. Liest
    aus job_data, damit der Workflow deterministisch bleibt.
    """
    seconds = job_data.get("llm_activity_timeout_seconds", 600) or 600
    try:
        seconds = int(seconds)
    except (TypeError, ValueError):
        seconds = 600
    return timedelta(seconds=max(60, seconds))


def _document_limit_enabled(job_data: dict[str, Any]) -> bool:
    """True when the API froze an active document limit into this job.

    Jobs started before the document limit existed (or with
    ``MAX_CONCURRENT_DOCUMENTS=0``) skip the queue entirely.
    """
    try:
        return int(job_data.get("max_concurrent_documents") or 0) > 0
    except (TypeError, ValueError):
        return False


def _resolve_queue_timeout(job_data: dict[str, Any]) -> timedelta:
    """Maximum time a job waits for a processing slot (frozen at job start)."""
    seconds = (
        job_data.get("document_queue_timeout_seconds", _DEFAULT_QUEUE_TIMEOUT_SECONDS)
        or _DEFAULT_QUEUE_TIMEOUT_SECONDS
    )
    try:
        seconds = int(seconds)
    except (TypeError, ValueError):
        seconds = _DEFAULT_QUEUE_TIMEOUT_SECONDS
    return timedelta(seconds=max(60, seconds))


@workflow.defn(name="SecurityCheckWorkflow")
class SecurityCheckWorkflow:
    """Routes to FDX or PDF pipeline based on script_format."""

    def __init__(self) -> None:
        # SecureBuffer refs produced so far (M13 cleanup on failure/cancel).
        self._buffer_refs: list[str] = []
        self._report_ref = ""
        # True once the delivery outcome is decided; from then on the job's
        # terminal state is owned by the delivery logic, not by cancellation.
        self._delivery_settled = False
        # Document limit: set while this job holds or waits for a slot.
        self._slot_job_id = ""
        self._slot_pending_release = False

    def _track(self, *refs: Any) -> None:
        for ref in refs:
            if ref and isinstance(ref, str) and ref not in self._buffer_refs:
                self._buffer_refs.append(ref)

    async def _run_indexed(
        self,
        total: int,
        concurrency: int,
        factory: Callable[[int], Awaitable[Any]],
        progress_cb: Callable[[int], Awaitable[None]] | None = None,
    ) -> list[Any]:
        """Execute ``factory(i)`` for i in range(total), preserving order.

        * Bei ``concurrency <= 1`` läuft die Schleife strikt sequenziell --
          bytewise identisch zum Pfad vor M07: erst factory(i) awaiten,
          danach progress_cb awaiten, dann nächste Iteration.
        * Bei ``concurrency > 1`` werden bis zu ``concurrency`` Tasks
          gleichzeitig gestartet, indexerhaltend per asyncio.gather.
          Reihenfolge der Ergebnisliste entspricht 0..total-1.

        progress_cb (optional, async) wird nach jedem erfolgreich
        abgeschlossenen Task mit der Anzahl bereits fertiger Tasks
        aufgerufen. Im sequenziellen Pfad ist done == i + 1; im parallelen
        Pfad entspricht done der Eintreffe-Reihenfolge, was für ein
        prozentuales Progress-Update vollkommen ausreicht.
        """
        results: list[Any] = [None] * total

        if concurrency <= 1:
            for i in range(total):
                results[i] = await factory(i)
                if progress_cb is not None:
                    await progress_cb(i + 1)
            return results

        sem = asyncio.Semaphore(concurrency)
        completed = 0
        draining = False
        failures: list[BaseException] = []

        async def _bounded(idx: int) -> None:
            nonlocal completed, draining
            async with sem:
                if draining:
                    return  # M13: no new work after the first failure
                try:
                    results[idx] = await factory(idx)
                except Exception as exc:
                    # Pre-M13 histories: fail fast (siblings keep running).
                    if not draining and not workflow.patched(_PATCH_PARALLEL_DRAIN):
                        raise
                    # M13: remember the failure, let in-flight siblings finish,
                    # start nothing new -- the caller sees the first failure
                    # only once no activity of this loop is outstanding.
                    draining = True
                    failures.append(exc)
                    return
                completed += 1
                if progress_cb is not None and not draining:
                    await progress_cb(completed)

        await asyncio.gather(*[_bounded(i) for i in range(total)])
        if failures:
            raise failures[0]
        return results

    async def _update_job(
        self,
        job_id: str,
        status: str,
        progress: int | None = None,
        error: str | None = None,
    ) -> None:
        """Best-effort job status update -- never fails the workflow.

        Only a workflow cancellation (M13) is re-raised so it is not lost.
        Terminal statuses use the longer ``_RETRY_STATUS_TERMINAL`` policy.
        """
        if not job_id:
            return
        payload: dict[str, Any] = {"job_id": job_id, "status": status}
        if progress is not None:
            payload["progress_percentage"] = progress
        if error is not None:
            payload["error_message"] = error
        try:
            await workflow.execute_activity(
                update_job_status_activity,
                payload,
                start_to_close_timeout=timedelta(seconds=30),
                retry_policy=(
                    _RETRY_STATUS_TERMINAL if status in _TERMINAL_STATUSES else _RETRY_STANDARD
                ),
            )
        except Exception as exc:
            if (
                not self._delivery_settled
                and is_cancelled_exception(exc)
                and workflow.patched(_PATCH_CANCELLATION)
            ):
                raise  # a cancel landing on a progress update must not be lost
            logger.warning("Job status update failed (non-fatal): %s", type(exc).__name__)

    async def _cleanup_buffers(self, job_id: str) -> None:
        """Best-effort delete of every SecureBuffer ref this run produced (M13).

        Never raises -- it must not mask the original failure; the Redis TTL
        stays the safety net.
        """
        refs = list(self._buffer_refs)
        if self._report_ref and not self._delivery_settled:
            refs.append(self._report_ref)
        if not refs:
            return
        try:
            await workflow.execute_activity(
                cleanup_buffer_activity,
                {"ref_keys": refs},
                start_to_close_timeout=timedelta(seconds=30),
                retry_policy=_RETRY_STANDARD,
            )
        except Exception as exc:
            logger.warning(
                "Buffer cleanup after failure failed (non-fatal, TTL reaps): job=%s exc_type=%s",
                job_id,
                type(exc).__name__,
            )

    async def _acquire_document_slot(self, job_data: dict[str, Any], job_id: str) -> None:
        """Wait (status ``pending``) until the document limit admits this job.

        Polls ``acquire_document_slot`` with durable timers (5 s, doubling to
        60 s): a waiting job holds no worker slot and no activity timeout
        runs. Gives up after ``document_queue_timeout_seconds`` with the
        content-free ``DocumentQueueTimeout`` (job ``failed``).
        """
        timeout = _resolve_queue_timeout(job_data)
        deadline = workflow.now() + timeout
        payload = {
            "job_id": job_id,
            "priority": job_data.get("priority", 5),
            # Arrival = first poll; deterministic on replay.
            "enqueued_at_ms": int(workflow.now().timestamp() * 1000),
        }
        self._slot_job_id = job_id
        self._slot_pending_release = True
        delay = _SLOT_POLL_INITIAL
        while True:
            result: dict[str, Any] = await workflow.execute_activity(
                acquire_document_slot_activity,
                payload,
                start_to_close_timeout=_SLOT_ACTIVITY_TIMEOUT,
                retry_policy=_RETRY_STANDARD,
            )
            if result.get("granted"):
                return
            remaining = deadline - workflow.now()
            if remaining <= timedelta(0):
                minutes = int(timeout.total_seconds() // 60)
                raise ApplicationError(
                    f"Kein freier Verarbeitungsplatz innerhalb von {minutes} min "
                    f"(Dokument-Limit {result.get('limit')}, Warteposition "
                    f"{result.get('position')}) – Job bitte später neu einreichen",
                    type=ERROR_TYPE_QUEUE_TIMEOUT,
                    non_retryable=True,
                )
            await workflow.sleep(min(delay, remaining))
            delay = min(delay * 2, _SLOT_POLL_MAX)

    async def _release_document_slot(self) -> None:
        """Give the slot back / leave the queue (best effort; the lease is the safety net).

        A cancellation that lands on the release is re-raised and the release
        stays pending, so the cancellation branch of ``run`` retries it.
        """
        if not self._slot_pending_release:
            return
        try:
            await workflow.execute_activity(
                release_document_slot_activity,
                {"job_id": self._slot_job_id},
                start_to_close_timeout=_SLOT_ACTIVITY_TIMEOUT,
                retry_policy=_RETRY_STANDARD,
            )
        except Exception as exc:
            if is_cancelled_exception(exc):
                raise
            logger.warning(
                "Document slot release failed (non-fatal, lease expires): job=%s exc_type=%s",
                self._slot_job_id,
                type(exc).__name__,
            )
        self._slot_pending_release = False

    @workflow.run
    async def run(self, job_data: dict[str, Any]) -> dict[str, Any]:
        workflow_id = workflow.info().workflow_id
        fmt = job_data.get("script_format", "fdx")
        job_id = job_data.get("job_id", "")
        logger.info("SecurityCheckWorkflow %s started (format=%s)", workflow_id, fmt)

        self._track(job_data.get("ref_key", ""))

        try:
            if _document_limit_enabled(job_data):
                # Keyed by workflow id (= job_id): the LLM activities renew the
                # lease under activity.info().workflow_id.
                await self._acquire_document_slot(job_data, workflow_id)
            await self._update_job(job_id, "running", progress=0)
            if fmt == "pdf":
                return await self._run_pdf(job_data, workflow_id)
            else:
                return await self._run_fdx(job_data, workflow_id)
        except (Exception, asyncio.CancelledError) as e:
            if is_cancelled_exception(e) and workflow.patched(_PATCH_CANCELLATION):
                # M13: an operator cancel is not a processing failure.
                logger.warning("Workflow %s cancelled", workflow_id)
                await self._release_document_slot()
                if not self._delivery_settled:
                    await self._cleanup_buffers(job_id)
                    await self._update_job(
                        job_id, "cancelled", error="Job abgebrochen (Workflow-Cancel)"
                    )
                raise
            if isinstance(e, asyncio.CancelledError):
                raise
            error = _failure_message(e)
            logger.error("Workflow %s failed: %s", workflow_id, error, exc_info=True)
            await self._release_document_slot()
            if workflow.patched(_PATCH_FAILURE_CLEANUP):
                # M13: no script content may stay in the eKI after the job ended.
                await self._cleanup_buffers(job_id)
            await self._update_job(job_id, "failed", error=error)
            if workflow.patched(_PATCH_FAILURE_WEBHOOK):
                # M13: tell ePro that this report will never arrive (opt-in,
                # the activity is a no-op without EPRO_WEBHOOK_URL).
                await self._send_failure_webhook(
                    job_id=job_id,
                    report_id=str(job_data.get("report_id") or ""),
                    reason=_processing_failure_reason(e),
                )
            return {"status": "failed", "error": error, "workflow_id": workflow_id}

    async def _send_failure_webhook(self, *, job_id: str, report_id: str, reason: str) -> None:
        """Best-effort security.delivery.failed webhook for a processing failure."""
        try:
            await workflow.execute_activity(
                send_delivery_failed_webhook_activity,
                # Same content-free payload as the delivery-failure branch;
                # no delivery was attempted, hence attempts=0.
                {"job_id": job_id, "report_id": report_id, "reason": reason, "attempts": 0},
                start_to_close_timeout=timedelta(minutes=2),
                retry_policy=_RETRY_STANDARD,
            )
        except Exception as exc:
            logger.warning(
                "Processing-failure webhook dispatch failed (non-fatal): job=%s exc_type=%s",
                job_id,
                type(exc).__name__,
            )

    # ------------------------------------------------------------------
    # FDX Pipeline
    # ------------------------------------------------------------------

    async def _run_fdx(self, job_data: dict[str, Any], workflow_id: str) -> dict[str, Any]:
        """FDX: parse -> risk per scene -> aggregate report -> deliver."""
        job_id = job_data.get("job_id", "")

        # Step 1: Parse FDX (no LLM needed)
        parsed_result = await workflow.execute_activity(
            parse_fdx_activity,
            job_data,
            start_to_close_timeout=timedelta(minutes=10),
            retry_policy=_RETRY_STANDARD,
        )
        self._track(parsed_result.get("parsed_ref_key"))
        total = parsed_result.get("total_scenes", 0)
        logger.info("FDX parsed: %d scenes", total)
        await self._update_job(job_id, "running", progress=10)

        # Step 2: Analyze risk per scene
        all_findings = await self._analyze_scenes(
            parsed_result["parsed_ref_key"],
            total,
            job_data=job_data,
            job_id=job_id,
            progress_range=(10, 90),
        )
        await self._release_document_slot()  # LLM work done: next document may start

        # Step 3 & 4: Aggregate report and deliver
        await self._update_job(job_id, "running", progress=90)
        return await self._report_and_deliver(
            all_findings,
            parsed_result["parsed_ref_key"],
            job_data,
            workflow_id,
        )

    # ------------------------------------------------------------------
    # PDF Pipeline
    # ------------------------------------------------------------------

    async def _run_pdf(self, job_data: dict[str, Any], workflow_id: str) -> dict[str, Any]:
        """PDF: extract -> split -> LLM structure -> aggregate -> risk -> report -> deliver."""
        job_id = job_data.get("job_id", "")

        # Step 1: Extract text from PDF
        text_result = await workflow.execute_activity(
            extract_pdf_text_activity,
            job_data,
            start_to_close_timeout=timedelta(minutes=5),
            retry_policy=_RETRY_STANDARD,
        )
        self._track(text_result.get("text_ref_key"))
        logger.info("PDF text extracted: %d chars", text_result.get("text_length", 0))

        # Step 2: Split at INT/EXT markers
        split_result = await workflow.execute_activity(
            split_scenes_activity,
            text_result,
            start_to_close_timeout=timedelta(minutes=2),
            retry_policy=_RETRY_STANDARD,
        )
        self._track(split_result.get("blocks_ref_key"))
        scene_count = split_result["scene_count"]
        block_count = split_result["block_count"]
        used_page_fallback = split_result.get("used_page_fallback", False)
        if used_page_fallback:
            logger.warning(
                "No INT/EXT markers found -- using page-by-page fallback (%d pages)",
                scene_count,
            )
        else:
            logger.info("Split into %d blocks (%d scenes)", block_count, scene_count)
        await self._update_job(job_id, "running", progress=5)

        # Step 3: LLM structuring per block -- progress 8% -> 50%.
        # Optionaler Parallel-Pfad (M07): per pdf_structure_concurrency und
        # gating durch llm_parallel_enabled. Default = strikt sequenziell.
        structure_concurrency = _resolve_concurrency(job_data, "pdf_structure_concurrency")
        activity_timeout = _resolve_activity_timeout(job_data)
        blocks_ref_key = split_result["blocks_ref_key"]
        last_reported_pct = 8

        async def _structure_block(i: int) -> dict[str, Any]:
            result: dict[str, Any] = await workflow.execute_activity(
                structure_scene_llm_activity,
                {
                    "blocks_ref_key": blocks_ref_key,
                    "block_index": i,
                    "used_page_fallback": used_page_fallback,
                },
                start_to_close_timeout=activity_timeout,
                retry_policy=_RETRY_LLM,
            )
            self._track(result.get("scene_ref_key"))
            return result

        async def _report_structure_progress(done: int) -> None:
            nonlocal last_reported_pct
            if block_count <= 0:
                return
            pct = 8 + int(42 * done / block_count)
            if pct >= last_reported_pct + 10 or done == block_count:
                await self._update_job(job_id, "running", progress=pct)
                last_reported_pct = pct

        llm_results = await self._run_indexed(
            total=block_count,
            concurrency=structure_concurrency,
            factory=_structure_block,
            progress_cb=_report_structure_progress,
        )

        scene_ref_keys: list[str] = []
        title: str | None = None
        for llm_result in llm_results:
            if llm_result.get("is_preamble"):
                title = llm_result.get("title")
                logger.info("Preamble processed, title=%s", title)
            else:
                scene_ref_keys.append(llm_result["scene_ref_key"])

        logger.info(
            "LLM structured %d scenes (concurrency=%d)",
            len(scene_ref_keys),
            structure_concurrency,
        )

        # Collect extraction warnings
        extraction_warnings = text_result.get("extraction_warnings", [])
        if used_page_fallback:
            extraction_warnings.append(
                "No scene markers (INT/EXT) found. Falling back to page-by-page splitting."
            )

        # Step 4: Aggregate into ParsedScript (programmatic, no LLM)
        agg_result = await workflow.execute_activity(
            aggregate_script_activity,
            {
                "scene_ref_keys": scene_ref_keys,
                "title": title,
                "ocr_pages_skipped": text_result.get("ocr_pages_skipped", []),
                "ocr_pages_done": text_result.get("ocr_pages_done", []),
                "blocks_ref_key": split_result["blocks_ref_key"],
                "used_page_fallback": used_page_fallback,
                "extra_warnings": extraction_warnings,
            },
            start_to_close_timeout=timedelta(minutes=5),
            retry_policy=_RETRY_STANDARD,
        )
        self._track(agg_result.get("parsed_ref_key"))
        logger.info(
            "Aggregated: %d scenes, confidence=%.2f",
            agg_result["total_scenes"],
            agg_result.get("overall_confidence", 0),
        )
        await self._update_job(job_id, "running", progress=52)

        # Step 5: Analyze risk per scene -- progress 52% -> 92%
        all_findings = await self._analyze_scenes(
            agg_result["parsed_ref_key"],
            agg_result["total_scenes"],
            job_data=job_data,
            job_id=job_id,
            progress_range=(52, 92),
        )
        await self._release_document_slot()  # LLM work done: next document may start

        # Step 6 & 7: Aggregate report and deliver
        await self._update_job(job_id, "running", progress=95)
        return await self._report_and_deliver(
            all_findings,
            agg_result["parsed_ref_key"],
            job_data,
            workflow_id,
        )

    # ------------------------------------------------------------------
    # Shared: per-scene risk analysis + report + deliver
    # ------------------------------------------------------------------

    async def _analyze_scenes(
        self,
        parsed_ref_key: str,
        total_scenes: int,
        *,
        job_data: dict[str, Any],
        job_id: str = "",
        progress_range: tuple[int, int] = (10, 90),
    ) -> list[dict[str, Any]]:
        """Call ``analyze_scene_risk_activity`` for each scene.

        Optional parallelisiert (M07): per risk_analysis_concurrency und
        gating durch llm_parallel_enabled. Default = strikt sequenziell
        (Verhalten bytewise identisch zu M06). Reihenfolge der Findings
        bleibt fest an scene_index gebunden -- der Report sortiert intern
        nach scene_number/scene_index, das ändert sich durch M07 nicht.
        """
        start_pct, end_pct = progress_range
        last_reported_pct = start_pct
        concurrency = _resolve_concurrency(job_data, "risk_analysis_concurrency")
        activity_timeout = _resolve_activity_timeout(job_data)

        async def _analyze(i: int) -> dict[str, Any]:
            result: dict[str, Any] = await workflow.execute_activity(
                analyze_scene_risk_activity,
                {"parsed_ref_key": parsed_ref_key, "scene_index": i},
                start_to_close_timeout=activity_timeout,
                retry_policy=_RETRY_LLM,
            )
            # M13: findings travel as SecureBuffer refs (older results inline).
            self._track(result.get("findings_ref_key"))
            return result

        async def _report(done: int) -> None:
            nonlocal last_reported_pct
            if total_scenes <= 0 or not job_id:
                return
            pct = start_pct + int((end_pct - start_pct) * done / total_scenes)
            if pct >= last_reported_pct + 10 or done == total_scenes:
                await self._update_job(job_id, "running", progress=pct)
                last_reported_pct = pct

        return await self._run_indexed(
            total=total_scenes,
            concurrency=concurrency,
            factory=_analyze,
            progress_cb=_report,
        )

    async def _report_and_deliver(
        self,
        all_findings: list[dict[str, Any]],
        parsed_ref_key: str,
        job_data: dict[str, Any],
        workflow_id: str,
    ) -> dict[str, Any]:
        """Aggregate findings into report (with PDF), then deliver.

        M08: Der Push-Delivery-Pfad wird durch ein
        ``schedule_to_close_timeout`` von 6 Stunden begrenzt
        (Pflichtenheft Abnahmetest 4). Innerhalb dieses Fensters retried
        Temporal die Activity mit exponentiellem Backoff. Schlaegt die
        Zustellung endgueltig fehl (5xx ueber 6h hinweg oder 4xx-Hard-Fail),
        wird der Failure-Branch ausgefuehrt: Buffer-Cleanup, Job-Status auf
        ``failed`` und optionaler security.delivery.failed-Webhook.
        """
        job_metadata = {
            "report_id": job_data.get("report_id"),
            "project_id": job_data.get("project_id"),
            "user_id": job_data.get("user_id"),
            "script_format": job_data.get("script_format"),
            # M13: lets aggregate_report compute the real processing time.
            "job_id": job_data.get("job_id"),
        }

        report_result = await workflow.execute_activity(
            aggregate_report_activity,
            args=[
                {"all_findings": all_findings, "parsed_ref_key": parsed_ref_key},
                job_metadata,
            ],
            start_to_close_timeout=timedelta(minutes=5),
            retry_policy=_RETRY_STANDARD,
        )
        self._report_ref = str(report_result.get("report_ref_key") or "")
        logger.info("Report generated: %s", report_result.get("report_id"))

        # Delivery config includes mode, job metadata for DB updates
        delivery_config = {
            "delivery_mode": job_data.get("delivery_mode", "pull"),
            "job_id": job_data.get("job_id"),
            "project_id": job_data.get("project_id"),
            "user_id": job_data.get("user_id"),
            "script_format": job_data.get("script_format"),
            "script_id": job_data.get("script_id"),
            # 1.0.1 (Decision 10b): optional per-job push target. Only the
            # activity *input* grows (absent in pre-1.0.1 histories -> None ->
            # EPRO_BASE_URL default); no command changes, so replay-safe
            # without workflow.patched.
            "callback_url": job_data.get("callback_url"),
        }
        job_id = job_data.get("job_id", "")
        report_id = report_result.get("report_id", "")
        report_ref_key = report_result.get("report_ref_key", "")

        delivery_result: dict[str, Any] | None = None
        delivery_failure_reason: str | None = None
        delivery_attempts: int = 0
        delivery_mode = str(delivery_config.get("delivery_mode") or "pull")

        try:
            result: dict[str, Any] = await workflow.execute_activity(
                deliver_report_activity,
                args=[report_result, delivery_config],
                start_to_close_timeout=_DELIVERY_START_TO_CLOSE_PER_ATTEMPT,
                schedule_to_close_timeout=_DELIVERY_SCHEDULE_TO_CLOSE,
                retry_policy=_RETRY_DELIVERY,
            )
            delivery_result = result
            logger.info(
                "Report delivered: %s (mode=%s, attempts=%s)",
                result.get("delivered"),
                result.get("delivery_mode"),
                result.get("attempts_used"),
            )
        except Exception as exc:
            if is_cancelled_exception(exc) and workflow.patched(_PATCH_CANCELLATION):
                raise  # M13: handled as cancellation in run()
            # Retry-Fenster (6h) erschoepft -- Pflichtenheft Abnahmetest 4 --
            # oder (M13) ein nicht wiederholbarer Fehler (Report abgelaufen,
            # ungueltige project_id). Wir loggen nur den Exception-Typ, NICHT
            # die Nachricht, damit keine Reportinhalte ins Log gelangen koennen.
            cause = exc.cause if isinstance(exc, ActivityError) else None
            cause_type = cause.type if isinstance(cause, ApplicationError) else None
            delivery_failure_reason = _DELIVERY_FAILURE_REASONS.get(
                cause_type or "", "retry_window_exhausted"
            )
            delivery_attempts = -1  # echte Zahl steht in job_metadata.delivery_attempts
            logger.error(
                "Report delivery failed definitively (%s): job=%s report=%s exc_type=%s",
                delivery_failure_reason,
                job_id,
                report_id,
                type(exc).__name__,
            )
        self._delivery_settled = True

        # 4xx-Hard-Fail: Activity hat ohne Retry zurueckgegeben.
        if (
            delivery_failure_reason is None
            and delivery_result is not None
            and not delivery_result.get("delivered", False)
            and delivery_result.get("delivery_mode") == "push"
        ):
            delivery_failure_reason = (
                "hard_4xx" if delivery_result.get("hard_fail") else "delivery_failed"
            )
            delivery_attempts = int(delivery_result.get("attempts_used", 1))

        if delivery_failure_reason is not None:
            return await self._handle_delivery_failure(
                job_id=job_id,
                report_id=report_id,
                report_ref_key=report_ref_key,
                reason=delivery_failure_reason,
                attempts=delivery_attempts,
                workflow_id=workflow_id,
                total_findings=report_result.get("total_findings", 0),
                # M13: was hard-coded "push" -> pull jobs were dead-lettered as push.
                delivery_mode=delivery_mode,
                last_status_code=(delivery_result or {}).get("status_code"),
            )

        # M10: Pull-TTL-Watch. Der Workflow bleibt (als durable Timer) offen,
        # bis der Report abgeholt wurde oder die Buffer-TTL abgelaufen ist.
        # Nicht abgeholt => Loeschung + Dead Letter + Webhook (Abnahmetest 4).
        if delivery_mode == "pull" and delivery_result is not None:
            ttl_seconds = int(job_data.get("buffer_ttl_seconds") or _DEFAULT_PULL_TTL_SECONDS)
            pull_result = await self._watch_pull_retrieval(
                job_id=job_id,
                report_id=report_id,
                report_ref_key=report_ref_key,
                ttl_seconds=ttl_seconds,
                workflow_id=workflow_id,
                total_findings=report_result.get("total_findings", 0),
            )
            if pull_result is not None:
                return pull_result

        return {
            "status": "completed",
            "report_id": report_result.get("report_id"),
            "total_findings": report_result.get("total_findings"),
            "delivered": delivery_result.get("delivered") if delivery_result else False,
            "delivery_mode": delivery_result.get("delivery_mode") if delivery_result else None,
            "delivery_attempts": (delivery_result or {}).get("attempts_used"),
            "workflow_id": workflow_id,
        }

    async def _watch_pull_retrieval(
        self,
        *,
        job_id: str,
        report_id: str,
        report_ref_key: str,
        ttl_seconds: int,
        workflow_id: str,
        total_findings: int,
    ) -> dict[str, Any] | None:
        """Wait for the pull TTL, then verify the one-shot GET happened.

        Returns ``None`` when the report was retrieved (happy path) or a
        ``delivery_failed`` result after running the failure branch.
        """
        await workflow.sleep(timedelta(seconds=max(1, ttl_seconds)))

        try:
            check = await workflow.execute_activity(
                check_report_retrieved_activity,
                {"report_id": report_id},
                start_to_close_timeout=timedelta(seconds=30),
                retry_policy=_RETRY_STANDARD,
            )
        except Exception as exc:
            logger.warning(
                "Pull retrieval check failed (assuming retrieved, TTL reaps buffer): "
                "job=%s exc_type=%s",
                job_id,
                type(exc).__name__,
            )
            return None

        if check.get("retrieved", False):
            return None

        logger.warning(
            "Pull report not retrieved within TTL, running failure branch: job=%s report=%s",
            job_id,
            report_id,
        )
        return await self._handle_delivery_failure(
            job_id=job_id,
            report_id=report_id,
            report_ref_key=report_ref_key,
            reason="pull_ttl_expired",
            attempts=0,
            workflow_id=workflow_id,
            total_findings=total_findings,
            delivery_mode="pull",
            last_status_code=None,
        )

    async def _handle_delivery_failure(
        self,
        *,
        job_id: str,
        report_id: str,
        report_ref_key: str,
        reason: str,
        attempts: int,
        workflow_id: str,
        total_findings: int,
        delivery_mode: str = "push",
        last_status_code: int | None = None,
    ) -> dict[str, Any]:
        """Centralised handling of definitive delivery failures (M08 + M10).

        Macht in dieser Reihenfolge:
        1. Buffer-Cleanup (Inhalt aus eKI loeschen).
        2. JobMetadata auf ``failed`` setzen, mit inhaltsarmer Fehlermeldung.
        3. Dead Letter persistieren (M10, inhaltsarm, fuer Ops/Alerting).
        4. security.delivery.failed Webhook (opt-in via EPRO_WEBHOOK_URL).

        Alle drei Schritte sind best-effort. Schlaegt einer fehl, fahren
        die anderen trotzdem fort, weil die Information moeglichst auch
        ohne perfekten Pfad ankommen soll.
        """
        # 1) Buffer-Cleanup -- expliziter Aufruf der dedizierten
        # cleanup_buffer_activity (M08). Damit ist sichergestellt, dass
        # der Report nach erschoepftem Retry oder 4xx-Hard-Fail nicht im
        # Redis verbleibt (Pflichtenheft Abnahmetest 4).
        if report_ref_key:
            try:
                await workflow.execute_activity(
                    cleanup_buffer_activity,
                    {"ref_keys": [report_ref_key]},
                    start_to_close_timeout=timedelta(seconds=30),
                    retry_policy=_RETRY_STANDARD,
                )
            except Exception as exc:
                logger.warning(
                    "Failure-branch buffer cleanup failed (non-fatal): job=%s exc_type=%s",
                    job_id,
                    type(exc).__name__,
                )

        # 2) Job-Status auf failed
        await self._update_job(
            job_id,
            "failed",
            error=f"delivery_failed:{reason}",
        )

        # 3) Webhook (opt-in)
        webhook_sent = False
        try:
            webhook_result = await workflow.execute_activity(
                send_delivery_failed_webhook_activity,
                {
                    "job_id": job_id,
                    "report_id": report_id,
                    "reason": reason,
                    "attempts": max(1, attempts) if attempts >= 0 else 0,
                },
                start_to_close_timeout=timedelta(minutes=2),
                retry_policy=_RETRY_STANDARD,
            )
            webhook_sent = bool((webhook_result or {}).get("sent", False))
        except Exception as exc:
            logger.warning(
                "Failure-branch webhook dispatch failed (non-fatal): job=%s exc_type=%s",
                job_id,
                type(exc).__name__,
            )

        # 4) Dead Letter (M10) -- nach dem Webhook, damit webhook_sent stimmt.
        try:
            await workflow.execute_activity(
                record_dead_letter_activity,
                {
                    "job_id": job_id,
                    "report_id": report_id,
                    "reason": reason,
                    "attempts": attempts if attempts >= 0 else None,
                    "delivery_mode": delivery_mode,
                    "last_status_code": last_status_code,
                    "webhook_sent": webhook_sent,
                },
                start_to_close_timeout=timedelta(seconds=30),
                retry_policy=_RETRY_STANDARD,
            )
        except Exception as exc:
            logger.warning(
                "Failure-branch dead-letter record failed (non-fatal): job=%s exc_type=%s",
                job_id,
                type(exc).__name__,
            )

        return {
            "status": "delivery_failed",
            "report_id": report_id,
            "total_findings": total_findings,
            "delivered": False,
            "delivery_mode": delivery_mode,
            "failure_reason": reason,
            "workflow_id": workflow_id,
        }
