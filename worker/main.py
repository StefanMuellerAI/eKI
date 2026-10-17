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
from core.logging_config import configure_logging
from core.metrics import set_build_info, start_metrics_server
from core.temporal_codec import EncryptionCodec, temporal_client_options
from core.tracing import configure_tracing, temporal_interceptors
from core.version import __version__
from workflows.activities import (
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
from workflows.maintenance import (
    KBCleanupWorkflow,
    MetadataCleanupWorkflow,
    ensure_kb_cleanup_schedule,
    ensure_metadata_cleanup_schedule,
    kb_cleanup_expired_activity,
    metadata_cleanup_expired_activity,
)
from workflows.security_check import SecurityCheckWorkflow

# M08: dieselbe zentrale Logging-Konfiguration wie im API-Prozess.
# Damit greifen Log-Hygiene-Filter und JSON-Format auch fuer Activities.
configure_logging(get_settings())
logger = logging.getLogger(__name__)


async def main() -> None:
    """Start the Temporal worker."""
    settings = get_settings()

    # M09: Worker-Metriken. Der Worker hat keinen FastAPI-Prozess, daher ein
    # eigener Prometheus-Exporter auf einem Port, der in docker-compose NICHT
    # auf den Host gemappt ist (nur Prometheus im eki-network scrapt ihn).
    if settings.metrics_enabled:
        start_metrics_server(settings.prometheus_port)
        logger.info("Worker metrics exporter listening on :%d", settings.prometheus_port)
    set_build_info(version=__version__, llm_provider=settings.llm_provider, role="worker")
    configure_tracing(settings, role="worker")

    logger.info(f"Connecting to Temporal at {settings.temporal_host}")

    try:
        # Connect to Temporal server
        # Interceptors: request_id/job_id-Korrelation in Activity-Logs und
        # (opt-in) OpenTelemetry-Spans ueber Workflow- und Activity-Grenzen.
        client = await Client.connect(
            settings.temporal_host,
            namespace=settings.temporal_namespace,
            interceptors=temporal_interceptors(settings),
            # Payload encryption (1.0.1): the Worker inherits this converter;
            # it must be identical to the API's (same API_SECRET_KEY).
            **temporal_client_options(settings.api_secret_key),
        )

        logger.info(f"Connected to Temporal namespace: {settings.temporal_namespace}")
        # Non-secret fingerprint: lets operators compare the API's and the
        # worker's API_SECRET_KEY without revealing it (OPERATIONS_GUIDE).
        logger.info(
            "Temporal payload encryption active: key id %s",
            EncryptionCodec(settings.api_secret_key).key_id,
        )

        # Create and start worker. Concurrency-Caps werden aus den Settings
        # gelesen (M07), damit Betrieb die Werte ohne Code-Änderung tunen
        # kann. Defaults entsprechen dem Stand vor M07.
        worker = Worker(
            client,
            task_queue=settings.temporal_task_queue,
            workflows=[SecurityCheckWorkflow, KBCleanupWorkflow, MetadataCleanupWorkflow],
            activities=[
                # FDX activities
                parse_fdx_activity,
                # PDF activities
                extract_pdf_text_activity,
                split_scenes_activity,
                structure_scene_llm_activity,
                aggregate_script_activity,
                # Shared activities
                analyze_scene_risk_activity,
                aggregate_report_activity,
                deliver_report_activity,
                update_job_status_activity,
                # M08 -- failure-branch helpers
                cleanup_buffer_activity,
                send_delivery_failed_webhook_activity,
                # M10 -- outbound hardening
                record_dead_letter_activity,
                check_report_retrieved_activity,
                # M09 -- maintenance
                kb_cleanup_expired_activity,
                # 1.0.1 -- metadata retention
                metadata_cleanup_expired_activity,
                # Document limit (MAX_CONCURRENT_DOCUMENTS)
                acquire_document_slot_activity,
                release_document_slot_activity,
            ],
            max_concurrent_workflow_tasks=settings.worker_max_concurrent_workflow_tasks,
            max_concurrent_activities=settings.worker_max_concurrent_activities,
        )

        logger.info(
            "Worker concurrency caps: workflow_tasks=%d, activities=%d, "
            "documents=%d (0 = no limit), ollama_requests=%d",
            settings.worker_max_concurrent_workflow_tasks,
            settings.worker_max_concurrent_activities,
            settings.max_concurrent_documents,
            settings.ollama_max_concurrent_requests,
        )

        # M09/1.0.1: Wartungs-Schedules anlegen bzw. an die Settings angleichen
        # (Cron, *_ENABLED -> pausiert). Fehler sind nicht fatal: der Worker
        # laeuft weiter, der naechste Start versucht es erneut.
        for name, ensure_schedule in (
            ("KB cleanup", ensure_kb_cleanup_schedule),
            ("Metadata cleanup", ensure_metadata_cleanup_schedule),
        ):
            try:
                await ensure_schedule(client, settings)
            except Exception as exc:
                logger.warning("%s schedule bootstrap failed (non-fatal): %s", name, exc)

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
