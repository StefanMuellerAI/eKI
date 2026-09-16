"""OpenTelemetry tracing setup for API and worker (M09).

Pflichtenheft §4.1 lists "OpenTelemetry (Tracing)" as part of the
observability stack. Before M09 the ``otel_*`` settings existed but nothing
consumed them. This module wires:

* a ``TracerProvider`` with an OTLP/HTTP exporter (Jaeger / OTel Collector),
* FastAPI, SQLAlchemy and httpx auto-instrumentation,
* the Temporal ``TracingInterceptor`` so a trace started by an HTTP request
  continues through ``start_workflow`` into every activity.

Everything is gated by ``settings.otel_enabled`` (default ``false``) so a
deployment without a collector never pays for exporter retries. All setup
failures are logged and swallowed: tracing must never take the service down.
"""

from __future__ import annotations

import logging
from typing import Any

logger = logging.getLogger(__name__)

_CONFIGURED_ROLE: str | None = None


def is_enabled(settings: Any) -> bool:
    return bool(getattr(settings, "otel_enabled", False))


def configure_tracing(settings: Any, *, role: str, app: Any | None = None) -> bool:
    """Install the global tracer provider and auto-instrumentation.

    Returns ``True`` when tracing is active. Idempotent per process: a
    second call only (re-)instruments the FastAPI ``app`` if one is given.
    """
    global _CONFIGURED_ROLE

    if not is_enabled(settings):
        return False

    try:
        from opentelemetry import trace
        from opentelemetry.exporter.otlp.proto.http.trace_exporter import OTLPSpanExporter
        from opentelemetry.instrumentation.httpx import HTTPXClientInstrumentor
        from opentelemetry.instrumentation.sqlalchemy import SQLAlchemyInstrumentor
        from opentelemetry.sdk.resources import SERVICE_NAME, SERVICE_VERSION, Resource
        from opentelemetry.sdk.trace import TracerProvider
        from opentelemetry.sdk.trace.export import BatchSpanProcessor

        from core.version import __version__

        if _CONFIGURED_ROLE is None:
            service_name = f"{getattr(settings, 'otel_service_name', 'eki-api')}-{role}"
            resource = Resource.create(
                {
                    SERVICE_NAME: service_name,
                    SERVICE_VERSION: __version__,
                    "deployment.environment": str(getattr(settings, "env", "development")),
                    "eki.role": role,
                }
            )
            provider = TracerProvider(resource=resource)
            endpoint = str(getattr(settings, "otel_exporter_otlp_endpoint", "")).rstrip("/")
            exporter = OTLPSpanExporter(endpoint=f"{endpoint}/v1/traces")
            provider.add_span_processor(BatchSpanProcessor(exporter))
            trace.set_tracer_provider(provider)

            # Patches create_engine / create_async_engine, so the ad-hoc
            # engines built inside activities are covered as well.
            SQLAlchemyInstrumentor().instrument(enable_commenter=False)
            HTTPXClientInstrumentor().instrument()
            _CONFIGURED_ROLE = role
            logger.info("OpenTelemetry tracing enabled: service=%s otlp=%s", service_name, endpoint)

        if app is not None:
            from opentelemetry.instrumentation.fastapi import FastAPIInstrumentor

            FastAPIInstrumentor.instrument_app(app, excluded_urls="health,metrics")

        return True
    except Exception as exc:  # pragma: no cover - defensive, depends on env
        logger.warning("OpenTelemetry setup failed, continuing without tracing: %s", exc)
        return False


def temporal_interceptors(settings: Any) -> list[Any]:
    """Return the Temporal interceptors to attach to ``Client.connect``.

    Always contains the eKI context-propagation interceptor (request_id /
    job_id into activity logs); the OpenTelemetry ``TracingInterceptor`` is
    appended only when tracing is enabled.
    """
    from core.temporal_context import ContextPropagationInterceptor

    interceptors: list[Any] = [ContextPropagationInterceptor()]
    if is_enabled(settings):
        try:
            from temporalio.contrib.opentelemetry import TracingInterceptor

            interceptors.append(TracingInterceptor())
        except Exception as exc:  # pragma: no cover
            logger.warning("Temporal TracingInterceptor unavailable: %s", exc)
    return interceptors


def reset_for_testing() -> None:
    global _CONFIGURED_ROLE
    _CONFIGURED_ROLE = None


__all__ = ["configure_tracing", "is_enabled", "temporal_interceptors", "reset_for_testing"]
