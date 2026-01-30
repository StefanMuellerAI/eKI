"""Temporal activities for security check workflow."""

import logging
from datetime import timedelta
from typing import Any

from temporalio import activity

logger = logging.getLogger(__name__)


@activity.defn(name="parse_script")
async def parse_script_activity(script_data: dict[str, Any]) -> dict[str, Any]:
    """
    Parse script content and extract scenes.

    Args:
        script_data: Dict containing script_content, script_format, etc.

    Returns:
        Dict with parsed scenes and metadata.

    Note:
        M01 stub - real implementation in M02 (FDX) and M03 (PDF).
    """
    logger.info(f"Parsing script format: {script_data.get('script_format')}")

    # Stub: Return mock parsed data
    return {
        "scenes": [],
        "total_scenes": 0,
        "parsing_time_seconds": 0.1,
        "metadata": {"stub": True},
    }


@activity.defn(name="analyze_risks")
async def analyze_risks_activity(parsed_data: dict[str, Any]) -> dict[str, Any]:
    """
    Analyze parsed script for security risks using LLM.

    Args:
        parsed_data: Parsed script data from parse_script_activity.

    Returns:
        Dict with risk findings and analysis results.

    Note:
        M01 stub - real implementation in M06 (LLM integration).
    """
    logger.info("Analyzing risks with LLM")

    # Stub: Return mock risk analysis
    return {
        "findings": [],
        "risk_summary": {
            "critical": 0,
            "high": 0,
            "medium": 0,
            "low": 0,
            "info": 1,
        },
        "total_findings": 1,
        "analysis_time_seconds": 0.5,
        "metadata": {"stub": True, "llm_provider": "stub"},
    }


@activity.defn(name="generate_report")
async def generate_report_activity(
    analysis_data: dict[str, Any], job_metadata: dict[str, Any]
) -> dict[str, Any]:
    """
    Generate final security report from analysis data.

    Args:
        analysis_data: Risk analysis results.
        job_metadata: Job metadata (project_id, user_id, etc.).

    Returns:
        Dict with complete report structure.

    Note:
        M01 stub - full report generation in M04 (Risiko-Taxonomie & Scoring).
    """
    logger.info(f"Generating report for project: {job_metadata.get('project_id')}")

    # Stub: Return mock report
    return {
        "report_id": job_metadata.get("report_id"),
        "project_id": job_metadata.get("project_id"),
        "findings": analysis_data.get("findings", []),
        "risk_summary": analysis_data.get("risk_summary", {}),
        "total_findings": analysis_data.get("total_findings", 0),
        "processing_time_seconds": 1.0,
        "metadata": {"stub": True},
    }


@activity.defn(name="deliver_report")
async def deliver_report_activity(
    report: dict[str, Any], callback_url: str | None = None
) -> dict[str, Any]:
    """
    Deliver report to eProjekt system (write-through).

    Args:
        report: Generated security report.
        callback_url: Optional callback URL for async notification.

    Returns:
        Dict with delivery status.

    Note:
        M01 stub - real eProjekt integration in later milestones.
    """
    logger.info(f"Delivering report {report.get('report_id')} to eProjekt")

    if callback_url:
        logger.info(f"Callback URL provided: {callback_url}")

    # Stub: Return mock delivery status
    return {
        "delivered": True,
        "delivery_time_seconds": 0.2,
        "callback_sent": callback_url is not None,
        "metadata": {"stub": True},
    }


# Activity configuration for retry policies
activity_config = {
    "start_to_close_timeout": timedelta(minutes=10),
    "retry_policy": {
        "maximum_attempts": 3,
        "initial_interval": timedelta(seconds=1),
        "maximum_interval": timedelta(seconds=30),
        "backoff_coefficient": 2.0,
    },
}
