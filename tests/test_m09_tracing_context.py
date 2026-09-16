"""M09 -- OpenTelemetry wiring and request_id propagation into Temporal activities."""

from __future__ import annotations

from types import SimpleNamespace

import pytest
import structlog
from temporalio import converter

from core import tracing
from core.logging_config import get_request_id, set_request_id
from core.temporal_context import (
    HEADER_REQUEST_ID,
    ContextPropagationInterceptor,
    _header_value,
    _with_header,
    activity_log_context,
)


class TestTracingGate:
    def test_disabled_by_default_is_noop(self):
        settings = SimpleNamespace(otel_enabled=False)
        assert tracing.is_enabled(settings) is False
        assert tracing.configure_tracing(settings, role="api") is False

    def test_interceptors_without_otel_contain_only_context_propagation(self):
        interceptors = tracing.temporal_interceptors(SimpleNamespace(otel_enabled=False))
        assert len(interceptors) == 1
        assert isinstance(interceptors[0], ContextPropagationInterceptor)

    def test_interceptors_with_otel_add_tracing_interceptor(self):
        from temporalio.contrib.opentelemetry import TracingInterceptor

        interceptors = tracing.temporal_interceptors(SimpleNamespace(otel_enabled=True))
        assert any(isinstance(i, TracingInterceptor) for i in interceptors)
        assert isinstance(interceptors[0], ContextPropagationInterceptor)

    def test_configure_tracing_enabled_installs_provider(self, monkeypatch):
        """Provider + instrumentation are installed; exporter swapped for in-memory."""
        from opentelemetry.exporter.otlp.proto.http import trace_exporter
        from opentelemetry.sdk.trace.export.in_memory_span_exporter import (
            InMemorySpanExporter,
        )

        monkeypatch.setattr(
            trace_exporter, "OTLPSpanExporter", lambda endpoint: InMemorySpanExporter()
        )
        tracing.reset_for_testing()
        settings = SimpleNamespace(
            otel_enabled=True,
            otel_service_name="eki-test",
            otel_exporter_otlp_endpoint="http://collector:4318",
            env="test",
        )
        assert tracing.configure_tracing(settings, role="worker") is True
        # second call is idempotent
        assert tracing.configure_tracing(settings, role="worker") is True
        tracing.reset_for_testing()


class TestHeaderRoundtrip:
    def test_with_header_and_header_value_roundtrip(self):
        pc = converter.default().payload_converter
        headers = _with_header({}, "req-123", pc)
        assert HEADER_REQUEST_ID in headers
        assert _header_value(headers, pc) == "req-123"

    def test_empty_value_does_not_add_header(self):
        pc = converter.default().payload_converter
        assert _with_header({"x": 1}, "", pc) == {"x": 1}
        assert _header_value({}, pc) == ""
        assert _header_value(None, pc) == ""


class TestActivityLogContext:
    def test_binds_and_unbinds_structlog_context(self):
        structlog.contextvars.clear_contextvars()
        set_request_id("outer")
        with activity_log_context(
            request_id="req-abc", workflow_id="job-1", activity_type="parse_fdx", attempt=2
        ):
            ctx = structlog.contextvars.get_contextvars()
            assert ctx["job_id"] == "job-1"
            assert ctx["workflow_id"] == "job-1"
            assert ctx["activity"] == "parse_fdx"
            assert ctx["attempt"] == 2
            assert get_request_id() == "req-abc"
        assert "job_id" not in structlog.contextvars.get_contextvars()

    def test_missing_request_id_generates_one(self):
        with activity_log_context(request_id="", workflow_id="job-2", activity_type="x", attempt=1):
            assert get_request_id()  # generated uuid4 hex

    @pytest.mark.asyncio
    async def test_log_line_contains_correlation_fields(self, capsys):
        """End-to-end: stdlib logger inside the context renders job_id/request_id."""
        import logging

        from core.logging_config import configure_logging

        configure_logging(SimpleNamespace(log_level="INFO", log_format="json"))
        log = logging.getLogger("tests.m09.correlation")
        with activity_log_context(
            request_id="req-e2e", workflow_id="job-e2e", activity_type="analyze", attempt=1
        ):
            log.info("hello from activity")
        out = capsys.readouterr().out
        assert '"request_id": "req-e2e"' in out
        assert '"job_id": "job-e2e"' in out
        assert '"activity": "analyze"' in out
