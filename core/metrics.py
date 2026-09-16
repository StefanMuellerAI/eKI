"""Prometheus metrics registry shared by the API and the Temporal worker (M09).

All business metrics of the eKI live here so both processes export the same
metric families:

* the API exposes ``GET /metrics`` (API-key protected, see ``api/main.py``),
* the worker starts a plain HTTP exporter on ``settings.prometheus_port``
  that is reachable only inside the Docker network (``worker/main.py``).

Naming follows the Prometheus conventions (``eki_`` prefix, base units,
``_total`` for counters). Label cardinality is deliberately bounded: no
job ids, no project ids, no free text -- only enumerated dimensions.

Pflichtenheft references: §4.1 Observability (Prometheus/Grafana), §6
"Dashboards mit Kennzahlen (Zustellraten, Latenzen)", §5 Leistungsziele
(job duration SLOs, see ``docs/M09_SLO.md``).
"""

from __future__ import annotations

import time
from collections.abc import AsyncIterator, Iterable
from contextlib import asynccontextmanager
from typing import Any

from prometheus_client import (
    REGISTRY,
    CollectorRegistry,
    Counter,
    Gauge,
    Histogram,
    Info,
    start_http_server,
)

# ---------------------------------------------------------------------------
# Bucket layouts
# ---------------------------------------------------------------------------

# HTTP handlers: sub-second to a few seconds (uploads of 10 MB PDFs).
_HTTP_BUCKETS = (0.005, 0.01, 0.025, 0.05, 0.1, 0.25, 0.5, 1.0, 2.5, 5.0, 10.0, 30.0)

# LLM calls: seconds to the 600 s activity timeout.
_LLM_BUCKETS = (0.5, 1, 2, 5, 10, 20, 30, 60, 120, 180, 300, 600)

# Whole jobs: Pflichtenheft §5 SLOs are 10 / 60 / 120 minutes, so the buckets
# straddle those thresholds (values in seconds).
_JOB_BUCKETS = (30, 60, 120, 300, 600, 900, 1800, 2700, 3600, 5400, 7200, 10800)

# Outbound delivery: a single push attempt is bounded by ``epro_timeout``.
_DELIVERY_BUCKETS = (0.1, 0.25, 0.5, 1, 2, 5, 10, 30, 60, 120, 300)


def _existing(name: str, registry: CollectorRegistry) -> Any | None:
    """Return an already registered collector for *name* (idempotent imports)."""
    return registry._names_to_collectors.get(name)  # noqa: SLF001 - documented private API


def _counter(name: str, doc: str, labels: Iterable[str] = ()) -> Counter:
    return _existing(name, REGISTRY) or Counter(name, doc, list(labels))


def _histogram(name: str, doc: str, labels: Iterable[str], buckets: tuple) -> Histogram:
    return _existing(name, REGISTRY) or Histogram(name, doc, list(labels), buckets=buckets)


def _gauge(name: str, doc: str, labels: Iterable[str] = ()) -> Gauge:
    return _existing(name, REGISTRY) or Gauge(name, doc, list(labels))


def _info(name: str, doc: str) -> Info:
    return _existing(f"{name}_info", REGISTRY) or Info(name, doc)


# ---------------------------------------------------------------------------
# HTTP (API process)
# ---------------------------------------------------------------------------

HTTP_REQUESTS_TOTAL = _counter(
    "eki_http_requests_total",
    "HTTP requests handled by the eKI API, by route template and status code.",
    ["method", "route", "status"],
)
HTTP_REQUEST_DURATION = _histogram(
    "eki_http_request_duration_seconds",
    "HTTP request latency by route template.",
    ["method", "route"],
    _HTTP_BUCKETS,
)
HTTP_REQUESTS_IN_FLIGHT = _gauge(
    "eki_http_requests_in_flight",
    "HTTP requests currently being processed.",
)

# ---------------------------------------------------------------------------
# Jobs / workflows
# ---------------------------------------------------------------------------

JOBS_TOTAL = _counter(
    "eki_jobs_total",
    "Security-check jobs by script format and terminal transition "
    "(started, completed, failed, delivery_failed).",
    ["script_format", "status"],
)
JOB_DURATION = _histogram(
    "eki_job_duration_seconds",
    "Wall-clock duration of a job from creation to terminal status.",
    ["script_format"],
    _JOB_BUCKETS,
)
SCENES_PROCESSED_TOTAL = _counter(
    "eki_scenes_processed_total",
    "Scenes that went through a pipeline stage (structure, risk).",
    ["stage"],
)
FINDINGS_TOTAL = _counter(
    "eki_findings_total",
    "Risk findings emitted by the risk analysis, by severity.",
    ["severity"],
)

# ---------------------------------------------------------------------------
# LLM
# ---------------------------------------------------------------------------

LLM_REQUESTS_TOTAL = _counter(
    "eki_llm_requests_total",
    "LLM provider calls by operation and outcome (success, error).",
    ["provider", "operation", "outcome"],
)
LLM_REQUEST_DURATION = _histogram(
    "eki_llm_request_duration_seconds",
    "LLM provider call latency (transport time, excluding queue wait).",
    ["provider", "operation"],
    _LLM_BUCKETS,
)
LLM_QUEUE_WAIT = _histogram(
    "eki_llm_queue_wait_seconds",
    "Time spent waiting for a slot in the process-wide Ollama concurrency cap.",
    ["provider"],
    _LLM_BUCKETS,
)
LLM_INFLIGHT = _gauge(
    "eki_llm_requests_in_flight",
    "LLM calls currently executing (after acquiring a slot).",
    ["provider"],
)
PROMPT_SANITIZER_HITS_TOTAL = _counter(
    "eki_prompt_sanitizer_hits_total",
    "Prompts matching a dangerous pattern, by action taken (blocked, allowed).",
    ["action"],
)

# ---------------------------------------------------------------------------
# Outbound delivery (Push / Pull) + retries
# ---------------------------------------------------------------------------

DELIVERY_ATTEMPTS_TOTAL = _counter(
    "eki_delivery_attempts_total",
    "Delivery attempts by mode and outcome "
    "(success, hard_fail, retryable, transport_error, pull_ready).",
    ["mode", "outcome"],
)
DELIVERY_DURATION = _histogram(
    "eki_delivery_duration_seconds",
    "Duration of a single push attempt to ePro.",
    ["mode"],
    _DELIVERY_BUCKETS,
)
DELIVERY_FAILURES_TOTAL = _counter(
    "eki_delivery_failures_total",
    "Deliveries that entered the failure branch, by reason.",
    ["reason"],
)
WEBHOOK_SENT_TOTAL = _counter(
    "eki_webhook_sent_total",
    "security.delivery.failed webhook results (sent, failed, disabled).",
    ["outcome"],
)
REPORT_RETRIEVALS_TOTAL = _counter(
    "eki_report_retrievals_total",
    "One-shot report retrievals by outcome (success, not_found, gone, expired).",
    ["outcome"],
)
BUFFER_DELETES_TOTAL = _counter(
    "eki_buffer_deletes_total",
    "SecureBuffer keys deleted, by trigger (push, pull, cleanup).",
    ["source"],
)
DEAD_LETTERS_UNACKNOWLEDGED = _gauge(
    "eki_dead_letters_unacknowledged",
    "Delivery dead letters not yet acknowledged by operations.",
)

# ---------------------------------------------------------------------------
# Knowledge base
# ---------------------------------------------------------------------------

KB_DOCUMENTS = _gauge(
    "eki_kb_documents",
    "Documents currently stored in the knowledge base.",
)
KB_RETRIEVAL_HITS_TOTAL = _counter(
    "eki_kb_retrieval_hits_total",
    "KB chunks injected into risk-analysis prompts.",
)
KB_CLEANUP_REMOVED_TOTAL = _counter(
    "eki_kb_cleanup_removed_total",
    "Expired KB documents removed by the TTL cleanup schedule.",
)

# ---------------------------------------------------------------------------
# Build info
# ---------------------------------------------------------------------------

BUILD_INFO = _info(
    "eki_build",
    "Static build information (version, llm provider, process role).",
)


def set_build_info(*, version: str, llm_provider: str, role: str) -> None:
    """Publish static build metadata as an Info metric."""
    BUILD_INFO.info({"version": version, "llm_provider": llm_provider, "role": role})


# ---------------------------------------------------------------------------
# Helpers used by instrumented code paths
# ---------------------------------------------------------------------------


@asynccontextmanager
async def observe_llm_call(provider: str, operation: str) -> AsyncIterator[None]:
    """Time one LLM transport call and record its outcome.

    Usage::

        async with observe_llm_call("ollama", "generate_structured"):
            response = await client.post(...)
    """
    LLM_INFLIGHT.labels(provider=provider).inc()
    started = time.perf_counter()
    outcome = "success"
    try:
        yield
    except BaseException:
        outcome = "error"
        raise
    finally:
        LLM_INFLIGHT.labels(provider=provider).dec()
        LLM_REQUEST_DURATION.labels(provider=provider, operation=operation).observe(
            time.perf_counter() - started
        )
        LLM_REQUESTS_TOTAL.labels(provider=provider, operation=operation, outcome=outcome).inc()


def record_job_terminal(script_format: str, status: str, duration_seconds: float | None) -> None:
    """Count a terminal job transition and observe its duration (if known)."""
    JOBS_TOTAL.labels(script_format=script_format or "unknown", status=status).inc()
    if duration_seconds is not None and duration_seconds >= 0:
        JOB_DURATION.labels(script_format=script_format or "unknown").observe(duration_seconds)


def start_metrics_server(port: int, addr: str = "0.0.0.0") -> None:  # nosec B104
    """Expose the default registry over plain HTTP (worker process only).

    The worker has no FastAPI app, so it publishes metrics on a dedicated
    port that is *not* mapped to the host in ``docker-compose.yml``; only
    Prometheus inside the ``eki-network`` can scrape it.
    """
    start_http_server(port, addr=addr)


__all__ = [
    "BUFFER_DELETES_TOTAL",
    "BUILD_INFO",
    "DEAD_LETTERS_UNACKNOWLEDGED",
    "DELIVERY_ATTEMPTS_TOTAL",
    "DELIVERY_DURATION",
    "DELIVERY_FAILURES_TOTAL",
    "FINDINGS_TOTAL",
    "HTTP_REQUESTS_IN_FLIGHT",
    "HTTP_REQUESTS_TOTAL",
    "HTTP_REQUEST_DURATION",
    "JOBS_TOTAL",
    "JOB_DURATION",
    "KB_CLEANUP_REMOVED_TOTAL",
    "KB_DOCUMENTS",
    "KB_RETRIEVAL_HITS_TOTAL",
    "LLM_INFLIGHT",
    "LLM_QUEUE_WAIT",
    "LLM_REQUESTS_TOTAL",
    "LLM_REQUEST_DURATION",
    "PROMPT_SANITIZER_HITS_TOTAL",
    "REPORT_RETRIEVALS_TOTAL",
    "SCENES_PROCESSED_TOTAL",
    "WEBHOOK_SENT_TOTAL",
    "observe_llm_call",
    "record_job_terminal",
    "set_build_info",
    "start_metrics_server",
]
