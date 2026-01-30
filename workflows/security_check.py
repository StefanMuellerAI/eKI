"""Temporal workflow for security check processing."""

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
    """
    Main workflow for processing security checks.

    Orchestrates the following steps:
    1. Parse script content (FDX or PDF)
    2. Analyze risks using LLM
    3. Generate structured report
    4. Deliver report to eProjekt (write-through)

    Workflow execution timeout: 40 minutes (configurable)
    """

    @workflow.run
    async def run(self, job_data: dict[str, Any]) -> dict[str, Any]:
        """
        Execute the security check workflow.

        Args:
            job_data: Dict containing:
                - script_content: Base64-encoded script
                - script_format: 'fdx' or 'pdf'
                - project_id: eProjekt project ID
                - job_id: Unique job identifier
                - callback_url: Optional callback URL
                - user_id: Actor user ID
                - metadata: Additional metadata

        Returns:
            Dict with workflow execution results.
        """
        workflow_id = workflow.info().workflow_id
        logger.info(f"Starting security check workflow: {workflow_id}")

        try:
            # Step 1: Parse script
            parsed_data = await workflow.execute_activity(
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

            logger.info(f"Parsed {parsed_data.get('total_scenes', 0)} scenes")

            # Step 2: Analyze risks
            analysis_data = await workflow.execute_activity(
                analyze_risks_activity,
                parsed_data,
                start_to_close_timeout=timedelta(minutes=15),
                retry_policy=RetryPolicy(
                    maximum_attempts=3,
                    initial_interval=timedelta(seconds=2),
                    maximum_interval=timedelta(minutes=1),
                    backoff_coefficient=2.0,
                ),
            )

            logger.info(f"Found {analysis_data.get('total_findings', 0)} risk findings")

            # Step 3: Generate report
            job_metadata = {
                "report_id": job_data.get("report_id"),
                "project_id": job_data.get("project_id"),
                "user_id": job_data.get("user_id"),
                "script_format": job_data.get("script_format"),
            }

            report = await workflow.execute_activity(
                generate_report_activity,
                args=[analysis_data, job_metadata],
                start_to_close_timeout=timedelta(minutes=5),
                retry_policy=RetryPolicy(
                    maximum_attempts=3,
                    initial_interval=timedelta(seconds=1),
                    maximum_interval=timedelta(seconds=20),
                    backoff_coefficient=2.0,
                ),
            )

            logger.info(f"Generated report: {report.get('report_id')}")

            # Step 4: Deliver report to eProjekt
            delivery_result = await workflow.execute_activity(
                deliver_report_activity,
                args=[report, job_data.get("callback_url")],
                start_to_close_timeout=timedelta(minutes=5),
                retry_policy=RetryPolicy(
                    maximum_attempts=5,
                    initial_interval=timedelta(seconds=2),
                    maximum_interval=timedelta(minutes=1),
                    backoff_coefficient=2.0,
                ),
            )

            logger.info(f"Report delivered: {delivery_result.get('delivered')}")

            # Return workflow result
            return {
                "status": "completed",
                "report_id": report.get("report_id"),
                "total_findings": report.get("total_findings"),
                "delivered": delivery_result.get("delivered"),
                "workflow_id": workflow_id,
            }

        except Exception as e:
            logger.error(f"Workflow failed: {str(e)}", exc_info=True)
            return {
                "status": "failed",
                "error": str(e),
                "workflow_id": workflow_id,
            }
