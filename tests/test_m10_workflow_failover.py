"""M10 -- failover scenarios of SecurityCheckWorkflow on a time-skipping Temporal server.

The real workflow (``workflows/security_check.py``) runs against stub
activities registered under the production activity *names*. Time-skipping
lets the 6h retry window and the 6h pull TTL elapse in milliseconds.

Marker ``temporal``: needs the Temporal test server binary (downloaded once).
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass, field
from typing import Any

import pytest
from temporalio import activity, workflow
from temporalio.testing import WorkflowEnvironment
from temporalio.worker import Worker

with workflow.unsafe.imports_passed_through():
    from workflows.security_check import SecurityCheckWorkflow

pytestmark = pytest.mark.temporal


@dataclass
class Recorder:
    """Collects activity invocations so tests can assert the failure branch."""

    calls: dict[str, list[Any]] = field(default_factory=lambda: {})
    deliver_behaviour: str = "success"  # success | hard_4xx | transient | pull
    report_retrieved: bool = True

    def add(self, name: str, payload: Any) -> None:
        self.calls.setdefault(name, []).append(payload)

    def count(self, name: str) -> int:
        return len(self.calls.get(name, []))


def build_stub_activities(rec: Recorder) -> list[Any]:
    """Stub activities with the production names used by SecurityCheckWorkflow."""

    @activity.defn(name="parse_fdx")
    async def parse_fdx(job_data: dict) -> dict:
        rec.add("parse_fdx", job_data)
        return {"parsed_ref_key": "parsed", "total_scenes": 1, "total_characters": 1}

    @activity.defn(name="analyze_scene_risk")
    async def analyze(job_data: dict) -> dict:
        rec.add("analyze_scene_risk", job_data)
        return {"scene_index": 0, "scene_number": "1", "findings": []}

    @activity.defn(name="aggregate_report")
    async def aggregate(job_data: dict, job_metadata: dict) -> dict:
        rec.add("aggregate_report", job_metadata)
        return {
            "report_ref_key": "report-ref",
            "report_id": job_metadata.get("report_id"),
            "total_findings": 0,
        }

    @activity.defn(name="deliver_report")
    async def deliver(report_data: dict, delivery_config: dict) -> dict:
        rec.add("deliver_report", {"attempt": activity.info().attempt, **delivery_config})
        if rec.deliver_behaviour == "hard_4xx":
            return {
                "delivered": False,
                "delivery_mode": "push",
                "hard_fail": True,
                "status_code": 422,
                "attempts_used": activity.info().attempt,
            }
        if rec.deliver_behaviour == "transient":
            raise RuntimeError("ePro 503")
        if rec.deliver_behaviour == "pull":
            return {"delivered": True, "delivery_mode": "pull", "attempts_used": 1}
        return {
            "delivered": True,
            "delivery_mode": "push",
            "status_code": 201,
            "attempts_used": activity.info().attempt,
        }

    @activity.defn(name="update_job_status")
    async def update_status(payload: dict) -> dict:
        rec.add("update_job_status", payload)
        return {"updated": True}

    @activity.defn(name="cleanup_buffer")
    async def cleanup(payload: dict) -> dict:
        rec.add("cleanup_buffer", payload)
        return {"deleted": len(payload.get("ref_keys", []))}

    @activity.defn(name="send_delivery_failed_webhook")
    async def webhook(payload: dict) -> dict:
        rec.add("send_delivery_failed_webhook", payload)
        return {"sent": True, "status_code": 200, "attempts_used": 1}

    @activity.defn(name="record_dead_letter")
    async def dead_letter(payload: dict) -> dict:
        rec.add("record_dead_letter", payload)
        return {"recorded": True, "dead_letter_id": str(uuid.uuid4())}

    @activity.defn(name="check_report_retrieved")
    async def check_retrieved(payload: dict) -> dict:
        rec.add("check_report_retrieved", payload)
        return {"found": True, "retrieved": rec.report_retrieved}

    return [
        parse_fdx,
        analyze,
        aggregate,
        deliver,
        update_status,
        cleanup,
        webhook,
        dead_letter,
        check_retrieved,
    ]


def _job_data(delivery_mode: str, ttl_seconds: int = 21600) -> dict[str, Any]:
    job_id = str(uuid.uuid4())
    return {
        "ref_key": "script-ref",
        "script_format": "fdx",
        "project_id": "75",
        "job_id": job_id,
        "report_id": str(uuid.uuid4()),
        "user_id": "u1",
        "priority": 5,
        "delivery_mode": delivery_mode,
        "metadata": {},
        "script_id": 7,
        "llm_parallel_enabled": False,
        "pdf_structure_concurrency": 1,
        "risk_analysis_concurrency": 1,
        "llm_activity_timeout_seconds": 600,
        "buffer_ttl_seconds": ttl_seconds,
    }


async def _run(rec: Recorder, job_data: dict[str, Any]) -> dict[str, Any]:
    async with await WorkflowEnvironment.start_time_skipping() as env:
        queue = f"m10-{uuid.uuid4().hex}"
        async with Worker(
            env.client,
            task_queue=queue,
            workflows=[SecurityCheckWorkflow],
            activities=build_stub_activities(rec),
        ):
            return await env.client.execute_workflow(
                SecurityCheckWorkflow.run,
                job_data,
                id=job_data["job_id"],
                task_queue=queue,
            )


def _statuses(rec: Recorder) -> list[str]:
    return [p["status"] for p in rec.calls.get("update_job_status", [])]


@pytest.mark.asyncio
async def test_push_success_completes_without_failure_branch():
    rec = Recorder(deliver_behaviour="success")
    result = await _run(rec, _job_data("push"))
    assert result["status"] == "completed"
    assert result["delivered"] is True
    assert rec.count("cleanup_buffer") == 0
    assert rec.count("record_dead_letter") == 0
    assert rec.count("send_delivery_failed_webhook") == 0
    assert rec.count("check_report_retrieved") == 0  # push: no TTL watch
    assert _statuses(rec)[0] == "running"


@pytest.mark.asyncio
async def test_push_hard_4xx_runs_failure_branch_with_dead_letter():
    rec = Recorder(deliver_behaviour="hard_4xx")
    result = await _run(rec, _job_data("push"))

    assert result["status"] == "delivery_failed"
    assert result["failure_reason"] == "hard_4xx"
    assert rec.count("deliver_report") == 1  # no retry on hard fail
    assert rec.calls["cleanup_buffer"][0]["ref_keys"] == ["report-ref"]
    assert "failed" in _statuses(rec)
    failed = [p for p in rec.calls["update_job_status"] if p["status"] == "failed"][0]
    assert failed["error_message"] == "delivery_failed:hard_4xx"
    webhook = rec.calls["send_delivery_failed_webhook"][0]
    assert webhook["reason"] == "hard_4xx" and webhook["attempts"] == 1
    dl = rec.calls["record_dead_letter"][0]
    assert dl["reason"] == "hard_4xx"
    assert dl["last_status_code"] == 422
    assert dl["webhook_sent"] is True
    assert dl["delivery_mode"] == "push"


@pytest.mark.asyncio
async def test_push_transient_errors_exhaust_6h_window_then_dead_letter():
    rec = Recorder(deliver_behaviour="transient")
    result = await _run(rec, _job_data("push"))

    assert result["status"] == "delivery_failed"
    assert result["failure_reason"] == "retry_window_exhausted"
    # Exponential backoff 2s -> cap 10min over 6h: many attempts, but bounded.
    assert 10 <= rec.count("deliver_report") <= 60
    assert rec.count("cleanup_buffer") == 1
    assert rec.calls["record_dead_letter"][0]["reason"] == "retry_window_exhausted"
    assert rec.calls["send_delivery_failed_webhook"][0]["reason"] == "retry_window_exhausted"


@pytest.mark.asyncio
async def test_pull_not_retrieved_within_ttl_is_dead_lettered():
    rec = Recorder(deliver_behaviour="pull", report_retrieved=False)
    result = await _run(rec, _job_data("pull", ttl_seconds=21600))

    assert result["status"] == "delivery_failed"
    assert result["failure_reason"] == "pull_ttl_expired"
    assert result["delivery_mode"] == "pull"
    assert rec.count("check_report_retrieved") == 1
    assert rec.calls["cleanup_buffer"][0]["ref_keys"] == ["report-ref"]
    assert rec.calls["record_dead_letter"][0]["reason"] == "pull_ttl_expired"
    assert rec.calls["record_dead_letter"][0]["delivery_mode"] == "pull"
    assert rec.count("send_delivery_failed_webhook") == 1


@pytest.mark.asyncio
async def test_pull_retrieved_within_ttl_completes_cleanly():
    rec = Recorder(deliver_behaviour="pull", report_retrieved=True)
    result = await _run(rec, _job_data("pull", ttl_seconds=21600))

    assert result["status"] == "completed"
    assert rec.count("check_report_retrieved") == 1
    assert rec.count("cleanup_buffer") == 0
    assert rec.count("record_dead_letter") == 0
