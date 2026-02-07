"""Temporal workflow for security check processing.

Activities exchange encrypted Redis reference keys -- never raw screenplay
content -- so that Temporal's workflow history contains only metadata.
"""

import logging
from datetime import timedelta
from typing import Any

from temporalio import workflow
from temporalio.common import RetryPolicy

with workflow.unsafe.imports_passed_through():
    from workflows.activities import (
        analyze_risks_activity,
        deliver_report_activity,
        generate_report_activity,
        parse_script_activity,
    )

logger = logging.getLogger(__name__)


@workflow.defn(name="SecurityCheckWorkflow")
class SecurityCheckWorkflow:
    """Main workflow for processing security checks.

    Orchestrates the following steps:
    1. Parse script content (FDX or PDF)
    2. Analyze risks using LLM
    3. Generate structured report
    4. Deliver report to eProjekt (write-through)

    All steps pass only Redis reference keys for sensitive data.
    """

    @workflow.run
    async def run(self, job_data: dict[str, Any]) -> dict[str, Any]:
        """Execute the security check workflow.

        Args:
            job_data: Dict containing:
                - ref_key: Redis reference to encrypted script content
                - script_format: 'fdx' or 'pdf'
                - project_id: eProjekt project ID
                - job_id: Unique job identifier
                - report_id: Pre-generated report ID
                - callback_url: Optional callback URL
                - user_id: Actor user ID
                - metadata: Additional metadata
        """
        workflow_id = workflow.info().workflow_id
        logger.info("Starting security check workflow: %s", workflow_id)

        try:
            # Step 1: Parse script (fetch from Redis, parse, store result in Redis)
            parsed_result = await workflow.execute_activity(
                parse_script_activity,
                job_data,
                start_to_close_timeout=timedelta(minutes=10),
                retry_policy=RetryPolicy(
                    maximum_attempts=3,
                    initial_interval=timedelta(seconds=1),
                    maximum_interval=timedelta(seconds=30),
                    backoff_coefficient=2.0,
                ),
            )

            logger.info(
                "Parsed %d scenes in %.2fs",
                parsed_result.get("total_scenes", 0),
                parsed_result.get("parsing_time_seconds", 0),
            )

            # Step 2: Analyze risks
            analysis_input = {
                "parsed_ref_key": parsed_result["parsed_ref_key"],
                "project_id": job_data.get("project_id"),
                "user_id": job_data.get("user_id"),
            }
            analysis_result = await workflow.execute_activity(
                analyze_risks_activity,
                analysis_input,
                start_to_close_timeout=timedelta(minutes=15),
                retry_policy=RetryPolicy(
                    maximum_attempts=3,
                    initial_interval=timedelta(seconds=2),
                    maximum_interval=timedelta(minutes=1),
                    backoff_coefficient=2.0,
                ),
            )

            logger.info("Found %d risk findings", analysis_result.get("total_findings", 0))

            # Step 3: Generate report
            job_metadata = {
                "report_id": job_data.get("report_id"),
                "project_id": job_data.get("project_id"),
                "user_id": job_data.get("user_id"),
                "script_format": job_data.get("script_format"),
            }
            report_result = await workflow.execute_activity(
                generate_report_activity,
                args=[analysis_result, job_metadata],
                start_to_close_timeout=timedelta(minutes=5),
                retry_policy=RetryPolicy(
                    maximum_attempts=3,
                    initial_interval=timedelta(seconds=1),
                    maximum_interval=timedelta(seconds=20),
                    backoff_coefficient=2.0,
                ),
            )

            logger.info("Generated report: %s", report_result.get("report_id"))

            # Step 4: Deliver report to eProjekt
            delivery_result = await workflow.execute_activity(
                deliver_report_activity,
                args=[report_result, job_data.get("callback_url")],
                start_to_close_timeout=timedelta(minutes=5),
                retry_policy=RetryPolicy(
                    maximum_attempts=5,
                    initial_interval=timedelta(seconds=2),
                    maximum_interval=timedelta(minutes=1),
                    backoff_coefficient=2.0,
                ),
            )

            logger.info("Report delivered: %s", delivery_result.get("delivered"))

            return {
                "status": "completed",
                "report_id": report_result.get("report_id"),
                "total_findings": report_result.get("total_findings"),
                "delivered": delivery_result.get("delivered"),
                "workflow_id": workflow_id,
            }

        except Exception as e:
            logger.error("Workflow failed: %s", str(e), exc_info=True)
            return {
                "status": "failed",
                "error": str(e),
                "workflow_id": workflow_id,
            }
