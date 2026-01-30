"""Temporal worker process for executing workflows and activities."""

import asyncio
import logging
import sys
from pathlib import Path

# Add parent directory to path for imports
sys.path.insert(0, str(Path(__file__).parent.parent))

from temporalio.client import Client
from temporalio.worker import Worker

from api.config import get_settings
from workflows.activities import (
    analyze_risks_activity,
    deliver_report_activity,
    generate_report_activity,
    parse_script_activity,
)
from workflows.security_check import SecurityCheckWorkflow

# Configure logging
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
)
logger = logging.getLogger(__name__)


async def main() -> None:
    """Start the Temporal worker."""
    settings = get_settings()

    logger.info(f"Connecting to Temporal at {settings.temporal_host}")

    try:
        # Connect to Temporal server
        client = await Client.connect(
            settings.temporal_host,
            namespace=settings.temporal_namespace,
        )

        logger.info(f"Connected to Temporal namespace: {settings.temporal_namespace}")

        # Create and start worker
        worker = Worker(
            client,
            task_queue=settings.temporal_task_queue,
            workflows=[SecurityCheckWorkflow],
            activities=[
                parse_script_activity,
                analyze_risks_activity,
                generate_report_activity,
                deliver_report_activity,
            ],
            max_concurrent_workflow_tasks=10,
            max_concurrent_activities=20,
        )

        logger.info(f"Starting worker on task queue: {settings.temporal_task_queue}")

        # Run worker
        await worker.run()

    except KeyboardInterrupt:
        logger.info("Worker shutdown requested")
    except Exception as e:
        logger.error(f"Worker error: {e}", exc_info=True)
        raise


if __name__ == "__main__":
    asyncio.run(main())
