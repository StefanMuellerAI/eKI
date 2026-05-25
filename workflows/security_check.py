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
"""

import asyncio
import logging
from datetime import timedelta
from typing import Any, Awaitable, Callable

from temporalio import workflow
from temporalio.common import RetryPolicy

with workflow.unsafe.imports_passed_through():
    from workflows.activities import (
        aggregate_report_activity,
        aggregate_script_activity,
        analyze_scene_risk_activity,
        cleanup_buffer_activity,
        deliver_report_activity,
        extract_pdf_text_activity,
        parse_fdx_activity,
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
# M08: ``maximum_attempts`` bewusst NICHT gesetzt. Die Begrenzung erfolgt
# ueber das ``schedule_to_close_timeout`` von 6 Stunden im Aufrufer von
# deliver_report_activity (Pflichtenheft Abnahmetest 4). 4xx-Hard-Fails
# werden in der Activity selbst erkannt und ohne Retry direkt
# zurueckgegeben (siehe ``deliver_report_activity``).
_DELIVERY_SCHEDULE_TO_CLOSE = timedelta(hours=6)
_DELIVERY_START_TO_CLOSE_PER_ATTEMPT = timedelta(minutes=5)


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


@workflow.defn(name="SecurityCheckWorkflow")
class SecurityCheckWorkflow:
    """Routes to FDX or PDF pipeline based on script_format."""

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

        async def _bounded(idx: int) -> None:
            nonlocal completed
            async with sem:
                results[idx] = await factory(idx)
                completed += 1
                if progress_cb is not None:
                    await progress_cb(completed)

        await asyncio.gather(*[_bounded(i) for i in range(total)])
        return results

    async def _update_job(
        self, job_id: str, status: str, progress: int | None = None, error: str | None = None,
    ) -> None:
        """Best-effort job status update -- never fails the workflow."""
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
                retry_policy=_RETRY_STANDARD,
            )
        except Exception as exc:
            logger.warning("Job status update failed (non-fatal): %s", exc)

    @workflow.run
    async def run(self, job_data: dict[str, Any]) -> dict[str, Any]:
        workflow_id = workflow.info().workflow_id
        fmt = job_data.get("script_format", "fdx")
        job_id = job_data.get("job_id", "")
        logger.info("SecurityCheckWorkflow %s started (format=%s)", workflow_id, fmt)

        await self._update_job(job_id, "running", progress=0)

        try:
            if fmt == "pdf":
                return await self._run_pdf(job_data, workflow_id)
            else:
                return await self._run_fdx(job_data, workflow_id)
        except Exception as e:
            logger.error("Workflow %s failed: %s", workflow_id, e, exc_info=True)
            await self._update_job(job_id, "failed", error=str(e))
            return {"status": "failed", "error": str(e), "workflow_id": workflow_id}

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
        logger.info("PDF text extracted: %d chars", text_result.get("text_length", 0))

        # Step 2: Split at INT/EXT markers
        split_result = await workflow.execute_activity(
            split_scenes_activity,
            text_result,
            start_to_close_timeout=timedelta(minutes=2),
            retry_policy=_RETRY_STANDARD,
        )
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
        structure_concurrency = _resolve_concurrency(
            job_data, "pdf_structure_concurrency"
        )
        activity_timeout = _resolve_activity_timeout(job_data)
        blocks_ref_key = split_result["blocks_ref_key"]
        last_reported_pct = 8

        async def _structure_block(i: int) -> dict[str, Any]:
            return await workflow.execute_activity(
                structure_scene_llm_activity,
                {
                    "blocks_ref_key": blocks_ref_key,
                    "block_index": i,
                    "used_page_fallback": used_page_fallback,
                },
                start_to_close_timeout=activity_timeout,
                retry_policy=_RETRY_LLM,
            )

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
            len(scene_ref_keys), structure_concurrency,
        )

        # Collect extraction warnings
        extraction_warnings = text_result.get("extraction_warnings", [])
        if used_page_fallback:
            extraction_warnings.append(
                "No scene markers (INT/EXT) found. "
                "Falling back to page-by-page splitting."
            )

        # Step 4: Aggregate into ParsedScript (programmatic, no LLM)
        agg_result = await workflow.execute_activity(
            aggregate_script_activity,
            {
                "scene_ref_keys": scene_ref_keys,
                "title": title,
                "ocr_pages_skipped": text_result.get("ocr_pages_skipped", []),
                "blocks_ref_key": split_result["blocks_ref_key"],
                "used_page_fallback": used_page_fallback,
                "extra_warnings": extraction_warnings,
            },
            start_to_close_timeout=timedelta(minutes=5),
            retry_policy=_RETRY_STANDARD,
        )
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
            return await workflow.execute_activity(
                analyze_scene_risk_activity,
                {"parsed_ref_key": parsed_ref_key, "scene_index": i},
                start_to_close_timeout=activity_timeout,
                retry_policy=_RETRY_LLM,
            )

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
        logger.info("Report generated: %s", report_result.get("report_id"))

        # Delivery config includes mode, job metadata for DB updates
        delivery_config = {
            "delivery_mode": job_data.get("delivery_mode", "pull"),
            "job_id": job_data.get("job_id"),
            "project_id": job_data.get("project_id"),
            "user_id": job_data.get("user_id"),
            "script_format": job_data.get("script_format"),
            "script_id": job_data.get("script_id"),
        }
        job_id = job_data.get("job_id", "")
        report_id = report_result.get("report_id", "")
        report_ref_key = report_result.get("report_ref_key", "")

        delivery_result: dict[str, Any] | None = None
        delivery_failure_reason: str | None = None
        delivery_attempts: int = 0

        try:
            delivery_result = await workflow.execute_activity(
                deliver_report_activity,
                args=[report_result, delivery_config],
                start_to_close_timeout=_DELIVERY_START_TO_CLOSE_PER_ATTEMPT,
                schedule_to_close_timeout=_DELIVERY_SCHEDULE_TO_CLOSE,
                retry_policy=_RETRY_DELIVERY,
            )
            logger.info(
                "Report delivered: %s (mode=%s)",
                delivery_result.get("delivered"),
                delivery_result.get("delivery_mode"),
            )
        except Exception as exc:
            # Retry-Fenster (6h) erschoepft -- Pflichtenheft Abnahmetest 4.
            # Wir loggen nur den Exception-Typ, NICHT die Nachricht, damit
            # keine Reportinhalte ins Log gelangen koennen.
            delivery_failure_reason = "retry_window_exhausted"
            delivery_attempts = -1  # unbekannt von hier aus
            logger.error(
                "Report delivery exhausted 6h retry window: job=%s "
                "report=%s exc_type=%s",
                job_id, report_id, type(exc).__name__,
            )

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
            )

        return {
            "status": "completed",
            "report_id": report_result.get("report_id"),
            "total_findings": report_result.get("total_findings"),
            "delivered": delivery_result.get("delivered") if delivery_result else False,
            "delivery_mode": delivery_result.get("delivery_mode") if delivery_result else None,
            "workflow_id": workflow_id,
        }

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
    ) -> dict[str, Any]:
        """Centralised handling of definitive delivery failures (M08).

        Macht in dieser Reihenfolge:
        1. Buffer-Cleanup (Inhalt aus eKI loeschen).
        2. JobMetadata auf ``failed`` setzen, mit inhaltsarmer Fehlermeldung.
        3. security.delivery.failed Webhook (opt-in via EPRO_WEBHOOK_URL).

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
                    "Failure-branch buffer cleanup failed (non-fatal): "
                    "job=%s exc_type=%s",
                    job_id, type(exc).__name__,
                )

        # 2) Job-Status auf failed
        await self._update_job(
            job_id,
            "failed",
            error=f"delivery_failed:{reason}",
        )

        # 3) Webhook (opt-in)
        try:
            await workflow.execute_activity(
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
        except Exception as exc:
            logger.warning(
                "Failure-branch webhook dispatch failed (non-fatal): "
                "job=%s exc_type=%s",
                job_id, type(exc).__name__,
            )

        return {
            "status": "delivery_failed",
            "report_id": report_id,
            "total_findings": total_findings,
            "delivered": False,
            "delivery_mode": "push",
            "failure_reason": reason,
            "workflow_id": workflow_id,
        }
