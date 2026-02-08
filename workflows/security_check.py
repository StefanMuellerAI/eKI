"""Temporal workflows for FDX and PDF security check processing.

The main ``SecurityCheckWorkflow`` routes to format-specific logic:
- FDX: parse -> analyze per scene -> aggregate report -> deliver
- PDF: extract text -> split -> LLM structure per scene -> aggregate script
       -> analyze per scene -> aggregate report -> deliver

Activities exchange only encrypted Redis reference keys.
"""

import logging
from datetime import timedelta
from typing import Any

from temporalio import workflow
from temporalio.common import RetryPolicy

with workflow.unsafe.imports_passed_through():
    from workflows.activities import (
        aggregate_report_activity,
        aggregate_script_activity,
        analyze_scene_risk_activity,
        deliver_report_activity,
        extract_pdf_text_activity,
        parse_fdx_activity,
        split_scenes_activity,
        structure_scene_llm_activity,
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
    maximum_attempts=5,
    initial_interval=timedelta(seconds=2),
    maximum_interval=timedelta(minutes=1),
    backoff_coefficient=2.0,
)


@workflow.defn(name="SecurityCheckWorkflow")
class SecurityCheckWorkflow:
    """Routes to FDX or PDF pipeline based on script_format."""

    @workflow.run
    async def run(self, job_data: dict[str, Any]) -> dict[str, Any]:
        workflow_id = workflow.info().workflow_id
        fmt = job_data.get("script_format", "fdx")
        logger.info("SecurityCheckWorkflow %s started (format=%s)", workflow_id, fmt)

        try:
            if fmt == "pdf":
                return await self._run_pdf(job_data, workflow_id)
            else:
                return await self._run_fdx(job_data, workflow_id)
        except Exception as e:
            logger.error("Workflow %s failed: %s", workflow_id, e, exc_info=True)
            return {"status": "failed", "error": str(e), "workflow_id": workflow_id}

    # ------------------------------------------------------------------
    # FDX Pipeline
    # ------------------------------------------------------------------

    async def _run_fdx(self, job_data: dict[str, Any], workflow_id: str) -> dict[str, Any]:
        """FDX: parse -> risk per scene -> aggregate report -> deliver."""

        # Step 1: Parse FDX (no LLM needed)
        parsed_result = await workflow.execute_activity(
            parse_fdx_activity,
            job_data,
            start_to_close_timeout=timedelta(minutes=10),
            retry_policy=_RETRY_STANDARD,
        )
        logger.info("FDX parsed: %d scenes", parsed_result.get("total_scenes", 0))

        # Step 2: Analyze risk per scene
        all_findings = await self._analyze_scenes(
            parsed_result["parsed_ref_key"],
            parsed_result["total_scenes"],
        )

        # Step 3 & 4: Aggregate report and deliver
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
        logger.info("Split into %d blocks (%d scenes)", block_count, scene_count)

        # Step 3: LLM structuring per block (sequential)
        scene_ref_keys: list[str] = []
        title: str | None = None

        for i in range(block_count):
            llm_result = await workflow.execute_activity(
                structure_scene_llm_activity,
                {
                    "blocks_ref_key": split_result["blocks_ref_key"],
                    "block_index": i,
                },
                start_to_close_timeout=timedelta(minutes=5),
                retry_policy=_RETRY_LLM,
            )

            if llm_result.get("is_preamble"):
                title = llm_result.get("title")
                logger.info("Preamble processed, title=%s", title)
            else:
                scene_ref_keys.append(llm_result["scene_ref_key"])

        logger.info("LLM structured %d scenes", len(scene_ref_keys))

        # Step 4: Aggregate into ParsedScript (programmatic, no LLM)
        agg_result = await workflow.execute_activity(
            aggregate_script_activity,
            {
                "scene_ref_keys": scene_ref_keys,
                "title": title,
                "ocr_pages_skipped": text_result.get("ocr_pages_skipped", []),
                "blocks_ref_key": split_result["blocks_ref_key"],
            },
            start_to_close_timeout=timedelta(minutes=5),
            retry_policy=_RETRY_STANDARD,
        )
        logger.info(
            "Aggregated: %d scenes, confidence=%.2f",
            agg_result["total_scenes"],
            agg_result.get("overall_confidence", 0),
        )

        # Step 5: Analyze risk per scene
        all_findings = await self._analyze_scenes(
            agg_result["parsed_ref_key"],
            agg_result["total_scenes"],
        )

        # Step 6 & 7: Aggregate report and deliver
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
        self, parsed_ref_key: str, total_scenes: int
    ) -> list[dict[str, Any]]:
        """Call ``analyze_scene_risk_activity`` for each scene."""
        all_findings: list[dict[str, Any]] = []
        for i in range(total_scenes):
            finding = await workflow.execute_activity(
                analyze_scene_risk_activity,
                {"parsed_ref_key": parsed_ref_key, "scene_index": i},
                start_to_close_timeout=timedelta(minutes=5),
                retry_policy=_RETRY_LLM,
            )
            all_findings.append(finding)
        return all_findings

    async def _report_and_deliver(
        self,
        all_findings: list[dict[str, Any]],
        parsed_ref_key: str,
        job_data: dict[str, Any],
        workflow_id: str,
    ) -> dict[str, Any]:
        """Aggregate findings into report and deliver."""
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

        delivery_result = await workflow.execute_activity(
            deliver_report_activity,
            args=[report_result, job_data.get("callback_url")],
            start_to_close_timeout=timedelta(minutes=5),
            retry_policy=_RETRY_DELIVERY,
        )
        logger.info("Report delivered: %s", delivery_result.get("delivered"))

        return {
            "status": "completed",
            "report_id": report_result.get("report_id"),
            "total_findings": report_result.get("total_findings"),
            "delivered": delivery_result.get("delivered"),
            "workflow_id": workflow_id,
        }
