"""Temporal activities for security check workflow.

Each activity fetches sensitive data from the encrypted SecureBuffer (Redis),
processes it, stores the result back, and returns only the new reference key
plus metadata.  No screenplay content flows through Temporal history.
"""

import base64
import logging
from datetime import timedelta
from typing import Any

import redis.asyncio as aioredis
from temporalio import activity

from api.config import get_settings
from services.secure_buffer import SecureBuffer

logger = logging.getLogger(__name__)


def _get_buffer() -> SecureBuffer:
    """Create a SecureBuffer bound to the global Redis + secret key."""
    settings = get_settings()
    redis_client = aioredis.from_url(
        str(settings.redis_url),
        decode_responses=False,
    )
    return SecureBuffer(
        redis_client,
        secret_key=settings.api_secret_key,
        default_ttl=settings.buffer_ttl_seconds,
    )


@activity.defn(name="parse_script")
async def parse_script_activity(job_data: dict[str, Any]) -> dict[str, Any]:
    """Parse script content and extract scenes.

    Fetches encrypted script from Redis, parses it with the appropriate parser,
    stores the parsed result back in Redis, and returns only metadata plus the
    new reference key.
    """
    ref_key = job_data.get("ref_key", "")
    script_format = job_data.get("script_format", "fdx")
    logger.info("Parsing script format=%s", script_format)

    buffer = _get_buffer()

    try:
        # Fetch encrypted script content from Redis
        raw_data = await buffer.retrieve(ref_key)
        b64_content = raw_data["script_content"]
        content_bytes = base64.b64decode(b64_content)

        # Parse with the appropriate parser
        from parsers.base import get_parser

        parser = get_parser(script_format)
        parsed = parser.parse(content_bytes)

        # Store parsed result back in Redis (never in Temporal history)
        parsed_ref = await buffer.store(parsed.model_dump(mode="json"))

        # Delete raw script content -- no longer needed
        await buffer.delete(ref_key)

        return {
            "parsed_ref_key": parsed_ref,
            "total_scenes": parsed.total_scenes,
            "total_characters": len(parsed.characters),
            "parsing_time_seconds": parsed.parsing_time_seconds,
        }

    except Exception:
        logger.exception("parse_script_activity failed")
        raise


@activity.defn(name="analyze_risks")
async def analyze_risks_activity(job_data: dict[str, Any]) -> dict[str, Any]:
    """Analyze parsed script for security risks using LLM.

    M02 stub -- real LLM integration in M06.
    """
    parsed_ref_key = job_data.get("parsed_ref_key", "")
    logger.info("Analyzing risks (stub)")

    buffer = _get_buffer()

    # Fetch parsed data from Redis to confirm it exists
    _parsed = await buffer.retrieve(parsed_ref_key)

    # Stub: store mock analysis in Redis
    analysis = {
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
    analysis_ref = await buffer.store(analysis)

    # Clean up parsed data from Redis
    await buffer.delete(parsed_ref_key)

    return {
        "analysis_ref_key": analysis_ref,
        "total_findings": 1,
        "analysis_time_seconds": 0.5,
    }


@activity.defn(name="generate_report")
async def generate_report_activity(
    analysis_data: dict[str, Any], job_metadata: dict[str, Any]
) -> dict[str, Any]:
    """Generate final security report from analysis data.

    M02 stub -- full report generation in M04.
    """
    analysis_ref_key = analysis_data.get("analysis_ref_key", "")
    logger.info("Generating report for project=%s", job_metadata.get("project_id"))

    buffer = _get_buffer()

    _analysis = await buffer.retrieve(analysis_ref_key)

    report = {
        "report_id": job_metadata.get("report_id"),
        "project_id": job_metadata.get("project_id"),
        "findings": _analysis.get("findings", []),
        "risk_summary": _analysis.get("risk_summary", {}),
        "total_findings": _analysis.get("total_findings", 0),
        "processing_time_seconds": 1.0,
        "metadata": {"stub": True},
    }
    report_ref = await buffer.store(report)

    await buffer.delete(analysis_ref_key)

    return {
        "report_ref_key": report_ref,
        "report_id": job_metadata.get("report_id"),
        "total_findings": report["total_findings"],
    }


@activity.defn(name="deliver_report")
async def deliver_report_activity(
    report_data: dict[str, Any], callback_url: str | None = None
) -> dict[str, Any]:
    """Deliver report to eProjekt system (write-through).

    M02 stub -- real eProjekt integration in later milestones.
    """
    report_ref_key = report_data.get("report_ref_key", "")
    logger.info("Delivering report %s to eProjekt", report_data.get("report_id"))

    buffer = _get_buffer()

    _report = await buffer.retrieve(report_ref_key)

    if callback_url:
        logger.info("Callback URL provided: %s", callback_url)

    # After successful delivery, delete all remaining buffer keys
    await buffer.delete(report_ref_key)

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
