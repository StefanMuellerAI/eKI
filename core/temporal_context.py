"""Temporal interceptor that carries ``request_id`` from the API into activities (M09).

Before M09 the worker logged without any correlation key: activities used
plain ``logging`` with ``job_id`` embedded in the message string, and the
``request_id`` set by the API middleware never left the API process.

This interceptor closes that gap without touching workflow signatures:

* **Client outbound** (API process): ``start_workflow`` adds a Temporal
  header ``eki-request-id`` taken from the logging ContextVar.
* **Workflow inbound/outbound** (sandbox): reads the header on
  ``execute_workflow`` and copies it onto every ``start_activity`` call.
  Only ``contextvars`` is used inside the sandbox -- no logging/structlog.
* **Activity inbound** (worker): sets the request_id ContextVar and binds
  ``request_id``, ``job_id``, ``workflow_id``, ``activity`` and ``attempt``
  as structlog context so every log line of the activity carries them.

Pflichtenheft §6: "strukturierte Logs mit Trace-IDs zur schnellen Fehlersuche".
"""

from __future__ import annotations

import contextvars
from collections.abc import Iterator
from contextlib import contextmanager
from typing import Any

from temporalio import activity, client, converter, worker, workflow

HEADER_REQUEST_ID = "eki-request-id"

# Workflow-side storage of the propagated request id. Plain ContextVar so it
# is deterministic and sandbox-safe.
_WORKFLOW_REQUEST_ID: contextvars.ContextVar[str] = contextvars.ContextVar(
    "eki_wf_request_id", default=""
)


def _header_value(headers: Any, payload_converter: converter.PayloadConverter) -> str:
    payload = (headers or {}).get(HEADER_REQUEST_ID)
    if not payload:
        return ""
    try:
        return str(payload_converter.from_payload(payload, str) or "")
    except Exception:
        return ""


def _with_header(headers: Any, value: str, payload_converter: converter.PayloadConverter) -> Any:
    if not value:
        return headers
    return {**(headers or {}), HEADER_REQUEST_ID: payload_converter.to_payload(value)}


class ContextPropagationInterceptor(client.Interceptor, worker.Interceptor):
    """Attach to ``Client.connect(interceptors=[...])``; the Worker inherits it."""

    def __init__(self, payload_converter: converter.PayloadConverter | None = None) -> None:
        self._payload_converter = payload_converter or converter.default().payload_converter

    # -- client side ---------------------------------------------------------
    def intercept_client(self, next: client.OutboundInterceptor) -> client.OutboundInterceptor:
        return _ClientOutbound(next, self._payload_converter)

    # -- worker side ---------------------------------------------------------
    def intercept_activity(
        self, next: worker.ActivityInboundInterceptor
    ) -> worker.ActivityInboundInterceptor:
        return _ActivityInbound(next)

    def workflow_interceptor_class(
        self, input: worker.WorkflowInterceptorClassInput
    ) -> type[worker.WorkflowInboundInterceptor] | None:
        return _WorkflowInbound


class _ClientOutbound(client.OutboundInterceptor):
    def __init__(
        self, next: client.OutboundInterceptor, payload_converter: converter.PayloadConverter
    ) -> None:
        super().__init__(next)
        self._payload_converter = payload_converter

    async def start_workflow(self, input: client.StartWorkflowInput) -> Any:
        from core.logging_config import get_request_id

        input.headers = _with_header(input.headers, get_request_id(), self._payload_converter)
        return await super().start_workflow(input)


class _WorkflowInbound(worker.WorkflowInboundInterceptor):
    def init(self, outbound: worker.WorkflowOutboundInterceptor) -> None:
        self.next.init(_WorkflowOutbound(outbound))

    async def execute_workflow(self, input: worker.ExecuteWorkflowInput) -> Any:
        rid = _header_value(input.headers, workflow.payload_converter())
        token = _WORKFLOW_REQUEST_ID.set(rid)
        try:
            return await self.next.execute_workflow(input)
        finally:
            _WORKFLOW_REQUEST_ID.reset(token)


class _WorkflowOutbound(worker.WorkflowOutboundInterceptor):
    def start_activity(self, input: worker.StartActivityInput) -> workflow.ActivityHandle:
        input.headers = _with_header(
            input.headers, _WORKFLOW_REQUEST_ID.get(), workflow.payload_converter()
        )
        return self.next.start_activity(input)

    def start_local_activity(
        self, input: worker.StartLocalActivityInput
    ) -> workflow.ActivityHandle:
        input.headers = _with_header(
            input.headers, _WORKFLOW_REQUEST_ID.get(), workflow.payload_converter()
        )
        return self.next.start_local_activity(input)

    async def start_child_workflow(
        self, input: worker.StartChildWorkflowInput
    ) -> workflow.ChildWorkflowHandle:
        input.headers = _with_header(
            input.headers, _WORKFLOW_REQUEST_ID.get(), workflow.payload_converter()
        )
        return await self.next.start_child_workflow(input)


@contextmanager
def activity_log_context(
    *, request_id: str, workflow_id: str, activity_type: str, attempt: int
) -> Iterator[None]:
    """Bind correlation fields for the duration of one activity execution.

    Lives outside the workflow sandbox (activities run in the plain worker
    process), so importing structlog / logging_config here is safe.
    """
    import structlog

    from core.logging_config import set_request_id

    set_request_id(request_id or None)
    structlog.contextvars.bind_contextvars(
        job_id=workflow_id,
        workflow_id=workflow_id,
        activity=activity_type,
        attempt=attempt,
    )
    try:
        yield
    finally:
        structlog.contextvars.unbind_contextvars("job_id", "workflow_id", "activity", "attempt")


class _ActivityInbound(worker.ActivityInboundInterceptor):
    async def execute_activity(self, input: worker.ExecuteActivityInput) -> Any:
        info = activity.info()
        rid = _header_value(input.headers, activity.payload_converter())
        with activity_log_context(
            request_id=rid,
            workflow_id=info.workflow_id,
            activity_type=info.activity_type,
            attempt=info.attempt,
        ):
            return await self.next.execute_activity(input)


__all__ = ["HEADER_REQUEST_ID", "ContextPropagationInterceptor", "activity_log_context"]
