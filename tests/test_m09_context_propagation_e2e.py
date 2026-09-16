"""M09 -- request_id propagation through a real Temporal test server.

Uses ``temporalio.testing.WorkflowEnvironment`` (time-skipping test server,
downloaded on first use). Marked ``temporal`` so it can be deselected in
offline environments: ``pytest -m "not temporal"``.
"""

from __future__ import annotations

import uuid
from datetime import timedelta

import pytest
from temporalio import activity, workflow
from temporalio.testing import WorkflowEnvironment
from temporalio.worker import Worker

# This module doubles as the workflow module and is therefore imported into
# the workflow sandbox; logging libraries must be passed through.
with workflow.unsafe.imports_passed_through():
    import structlog

    from core.logging_config import get_request_id, set_request_id
    from core.temporal_context import ContextPropagationInterceptor

pytestmark = pytest.mark.temporal


@activity.defn(name="m09_probe")
async def probe_activity(_: str) -> dict:
    ctx = structlog.contextvars.get_contextvars()
    return {
        "request_id": get_request_id(),
        "job_id": ctx.get("job_id"),
        "activity": ctx.get("activity"),
        "attempt": ctx.get("attempt"),
    }


@workflow.defn(name="M09ProbeWorkflow")
class ProbeWorkflow:
    @workflow.run
    async def run(self, arg: str) -> dict:
        return await workflow.execute_activity(
            probe_activity,
            arg,
            start_to_close_timeout=timedelta(seconds=10),
        )


@pytest.mark.asyncio
async def test_request_id_reaches_activity_logs():
    async with await WorkflowEnvironment.start_time_skipping() as env:
        client = env.client
        # The interceptor is attached like in api/dependencies.py and worker/main.py.
        client = client.__class__(
            **{**client.config(), "interceptors": [ContextPropagationInterceptor()]}
        )

        task_queue = f"m09-{uuid.uuid4().hex}"
        workflow_id = f"job-{uuid.uuid4()}"
        set_request_id("req-propagated-42")

        async with Worker(
            client,
            task_queue=task_queue,
            workflows=[ProbeWorkflow],
            activities=[probe_activity],
        ):
            result = await client.execute_workflow(
                ProbeWorkflow.run,
                "x",
                id=workflow_id,
                task_queue=task_queue,
            )

    assert result["request_id"] == "req-propagated-42"
    assert result["job_id"] == workflow_id
    assert result["activity"] == "m09_probe"
    assert result["attempt"] == 1
