"""Document limit (``MAX_CONCURRENT_DOCUMENTS``) and the LLM keep-alive.

* ``services/document_slots.py`` against a real Redis (``TEST_REDIS_URL``,
  default ``redis://localhost:6379/4``; skipped when unreachable). Every test
  uses its own key prefix, nothing else in the database is touched.
* the ``acquire_document_slot`` / ``release_document_slot`` activities,
* ``_llm_activity_guard``: heartbeats, lease renewal, clean shutdown -- and,
  on the Temporal test server, that an LLM attempt is cancelled as soon as its
  workflow has ended instead of running on for nobody.
"""

from __future__ import annotations

import asyncio
import functools
import logging
import os
import time
import uuid
from collections.abc import AsyncIterator
from datetime import timedelta
from types import SimpleNamespace
from typing import Any

import pytest
import redis
import redis.asyncio as aioredis
from prometheus_client import REGISTRY
from temporalio import activity, workflow
from temporalio.common import RetryPolicy
from temporalio.exceptions import ApplicationError
from temporalio.testing import ActivityEnvironment, WorkflowEnvironment
from temporalio.worker import Worker

from services import document_slots as slots_module
from services.document_slots import DocumentSlots, queue_score
from workflows import activities

TEST_REDIS_URL = os.environ.get("TEST_REDIS_URL", "redis://localhost:6379/4")


def _redis_skip_reason() -> str | None:
    try:
        probe = redis.Redis.from_url(TEST_REDIS_URL, socket_connect_timeout=1, socket_timeout=1)
        try:
            probe.ping()
        finally:
            probe.close()
    except Exception as exc:  # pragma: no cover - depends on the environment
        return f"Redis not reachable at TEST_REDIS_URL={TEST_REDIS_URL} ({type(exc).__name__})"
    return None


requires_redis = pytest.mark.skipif(
    _redis_skip_reason() is not None, reason=_redis_skip_reason() or ""
)

NOW = 1_800_000_000_000  # fixed epoch ms, tests pass time explicitly


@pytest.fixture
async def redis_client() -> AsyncIterator[aioredis.Redis]:
    client = aioredis.from_url(TEST_REDIS_URL)
    try:
        yield client
    finally:
        await client.aclose()


@pytest.fixture
async def slots(redis_client: aioredis.Redis) -> AsyncIterator[DocumentSlots]:
    prefix = f"eki-test:docslot:{uuid.uuid4().hex}:"
    yield DocumentSlots(redis_client, key_prefix=prefix, lease_seconds=60, stale_waiter_seconds=30)
    keys = [k async for k in redis_client.scan_iter(match=f"{prefix}*")]
    if keys:
        await redis_client.delete(*keys)


async def _take(s: DocumentSlots, job: str, *, prio: int = 5, at: int = NOW, now: int = NOW):
    return await s.acquire(job, priority=prio, enqueued_at_ms=at, limit=1, now_ms=now)


# ===================================================================
# Queue order
# ===================================================================


class TestQueueScore:
    def test_priority_dominates_arrival(self):
        assert queue_score(1, NOW + 10**9) < queue_score(2, NOW)

    def test_arrival_orders_equal_priority(self):
        assert queue_score(5, NOW) < queue_score(5, NOW + 1)

    @pytest.mark.parametrize(("raw", "clamped"), [(0, 1), (-3, 1), (11, 10), (99, 10)])
    def test_priority_is_clamped_to_api_range(self, raw, clamped):
        assert queue_score(raw, NOW) == queue_score(clamped, NOW)

    def test_score_stays_exact_in_a_double(self):
        assert queue_score(10, NOW) < 2**53


# ===================================================================
# DocumentSlots on a real Redis
# ===================================================================


@requires_redis
@pytest.mark.asyncio
class TestDocumentSlots:
    async def test_first_job_gets_the_slot_others_queue_in_order(self, slots):
        first = await _take(slots, "a", at=NOW)
        second = await _take(slots, "b", at=NOW + 1)
        third = await _take(slots, "c", at=NOW + 2)

        assert (first.granted, first.in_use) == (True, 1)
        assert (second.granted, second.position) == (False, 1)
        assert (third.granted, third.position, third.waiting) == (False, 2, 2)

    async def test_release_admits_the_head_of_the_queue_only(self, slots):
        await _take(slots, "a", at=NOW)
        await _take(slots, "b", at=NOW + 1)
        await _take(slots, "c", at=NOW + 2)

        assert await slots.release("a") == (0, 2)
        # "c" polls first but must not overtake "b".
        assert not (await _take(slots, "c", at=NOW + 2)).granted
        assert (await _take(slots, "b", at=NOW + 1)).granted
        assert (await _take(slots, "c", at=NOW + 2)).position == 1

    async def test_higher_priority_overtakes_earlier_arrivals(self, slots):
        await _take(slots, "holder")
        await _take(slots, "normal", prio=5, at=NOW)
        urgent = await _take(slots, "urgent", prio=1, at=NOW + 10_000)

        assert urgent.position == 1
        await slots.release("holder")
        assert not (await _take(slots, "normal", prio=5, at=NOW)).granted
        assert (await _take(slots, "urgent", prio=1, at=NOW + 10_000)).granted

    async def test_limit_two_admits_two_jobs(self, slots):
        results = [
            await slots.acquire(j, priority=5, enqueued_at_ms=NOW + i, limit=2, now_ms=NOW)
            for i, j in enumerate(["a", "b", "c"])
        ]

        assert [r.granted for r in results] == [True, True, False]
        assert results[2].in_use == 2

    async def test_acquire_is_idempotent_for_holder_and_waiter(self, slots):
        await _take(slots, "a")
        await _take(slots, "b", at=NOW + 1)

        again = await _take(slots, "a", now=NOW + 1000)
        still = await _take(slots, "b", at=NOW + 1, now=NOW + 1000)

        assert (again.granted, again.in_use) == (True, 1)
        assert (still.granted, still.position, still.waiting) == (False, 1, 1)

    async def test_expired_lease_frees_the_slot(self, slots):
        await _take(slots, "dead")
        await _take(slots, "next", at=NOW + 1)

        later = NOW + 61_000  # lease 60 s
        decision = await _take(slots, "next", at=NOW + 1, now=later)

        assert decision.granted

    async def test_renew_extends_only_a_current_lease(self, slots, redis_client):
        await _take(slots, "a")
        await _take(slots, "b", at=NOW + 1)

        assert await slots.renew("a", now_ms=NOW + 50_000) is True
        assert await slots.renew("b", now_ms=NOW + 50_000) is False  # waiting, not holding
        assert await redis_client.zscore(slots.holders_key, "b") is None
        # Renewed at +50 s -> still valid at +61 s.
        assert not (await _take(slots, "b", at=NOW + 1, now=NOW + 61_000)).granted

    async def test_stale_waiter_is_dropped_and_rejoins_at_its_old_place(self, slots):
        await _take(slots, "holder")
        await _take(slots, "gone", at=NOW + 1)
        await _take(slots, "alive", at=NOW + 2)

        # "alive" keeps polling, "gone" stops: after 30 s it no longer blocks.
        later = NOW + 31_000
        alive = await _take(slots, "alive", at=NOW + 2, now=later)
        assert (alive.position, alive.waiting) == (1, 1)
        # A paused (not dead) job re-enters with its original arrival time.
        back = await _take(slots, "gone", at=NOW + 1, now=later)
        assert back.position == 1
        assert (await _take(slots, "alive", at=NOW + 2, now=later)).position == 2

    async def test_release_also_leaves_the_queue(self, slots, redis_client):
        await _take(slots, "a")
        await _take(slots, "b", at=NOW + 1)

        assert await slots.release("b") == (1, 0)
        assert await redis_client.zcard(slots.queue_key) == 0
        assert await redis_client.zcard(slots.seen_key) == 0

    async def test_released_keys_disappear_from_redis(self, slots, redis_client):
        await _take(slots, "a")
        await slots.release("a")

        for key in (slots.holders_key, slots.queue_key, slots.seen_key):
            assert await redis_client.exists(key) == 0

    async def test_concurrent_acquires_grant_exactly_one(self, slots, redis_client):
        jobs = [f"job-{i:02d}" for i in range(25)]
        decisions = await asyncio.gather(*[_take(slots, j, at=NOW + i) for i, j in enumerate(jobs)])

        assert sum(d.granted for d in decisions) == 1
        holder = next(j for j, d in zip(jobs, decisions, strict=True) if d.granted)
        assert await redis_client.zrange(slots.holders_key, 0, -1) == [holder.encode()]
        # Everyone else waits, ordered by arrival regardless of call order.
        queued = [m.decode() for m in await redis_client.zrange(slots.queue_key, 0, -1)]
        assert queued == [j for j in jobs if j != holder]


# ===================================================================
# Activities
# ===================================================================


def _settings(limit: int, redis_url: str = TEST_REDIS_URL) -> SimpleNamespace:
    return SimpleNamespace(max_concurrent_documents=limit, redis_url=redis_url)


def _gauge(name: str) -> float:
    return float(REGISTRY.get_sample_value(name) or 0.0)


@pytest.mark.asyncio
class TestSlotActivities:
    async def test_limit_zero_grants_without_redis(self, monkeypatch):
        monkeypatch.setattr(activities, "get_settings", lambda: _settings(0, "redis://invalid:1/0"))

        result = await activities.acquire_document_slot_activity({"job_id": "j"})

        assert result["granted"] is True and result["limit"] == 0

    async def test_missing_job_id_is_a_final_error(self, monkeypatch):
        monkeypatch.setattr(activities, "get_settings", lambda: _settings(1))

        with pytest.raises(ApplicationError) as err:
            await activities.acquire_document_slot_activity({})

        assert err.value.non_retryable

    async def test_release_without_job_id_is_a_no_op(self):
        assert await activities.release_document_slot_activity({}) == {"released": False}

    @requires_redis
    async def test_acquire_and_release_round_trip(self, monkeypatch, redis_client):
        prefix = f"eki-test:docslot:{uuid.uuid4().hex}:"
        monkeypatch.setattr(activities, "get_settings", lambda: _settings(1))
        monkeypatch.setattr(
            activities, "DocumentSlots", functools.partial(DocumentSlots, key_prefix=prefix)
        )
        now_ms = int(time.time() * 1000)

        first = await activities.acquire_document_slot_activity(
            {"job_id": "a", "priority": 5, "enqueued_at_ms": now_ms}
        )
        second = await activities.acquire_document_slot_activity(
            {"job_id": "b", "priority": "not-a-number", "enqueued_at_ms": now_ms + 1}
        )

        assert first == {"granted": True, "position": 0, "limit": 1, "in_use": 1, "waiting": 0}
        assert (second["granted"], second["position"], second["waiting"]) == (False, 1, 1)
        assert _gauge("eki_document_queue_length") == 1
        assert _gauge("eki_document_slots_in_use") == 1

        released = await activities.release_document_slot_activity({"job_id": "a"})
        assert released == {"released": True, "in_use": 0, "waiting": 1}
        assert (await activities.acquire_document_slot_activity({"job_id": "b"}))["granted"]
        await activities.release_document_slot_activity({"job_id": "b"})
        assert [k async for k in redis_client.scan_iter(match=f"{prefix}*")] == []

    async def test_renewal_errors_are_swallowed(self, monkeypatch, caplog):
        monkeypatch.setattr(
            activities, "get_settings", lambda: _settings(1, "redis://127.0.0.1:1/0")
        )
        caplog.set_level(logging.WARNING, logger="workflows.activities")

        await activities._renew_document_slot("job-x")

        assert "lease renewal failed (non-fatal): job=job-x" in caplog.text

    async def test_renewal_skipped_without_limit(self, monkeypatch):
        def _no_redis(_settings: Any) -> Any:
            raise AssertionError("must not connect")

        monkeypatch.setattr(activities, "get_settings", lambda: _settings(0))
        monkeypatch.setattr(activities, "_slot_redis", _no_redis)

        await activities._renew_document_slot("job-x")

    @pytest.mark.parametrize("raw", [None, "x", object()])
    def test_slot_limit_tolerates_bad_values(self, raw):
        assert activities._slot_limit(SimpleNamespace(max_concurrent_documents=raw)) == 0


# ===================================================================
# _llm_activity_guard (heartbeats + lease renewal)
# ===================================================================


@pytest.mark.asyncio
class TestLlmActivityGuard:
    async def test_no_op_outside_an_activity(self):
        async with activities._llm_activity_guard():
            pass

    async def test_heartbeats_and_renews_while_the_body_runs(self, monkeypatch):
        monkeypatch.setattr(activities, "_HEARTBEAT_INTERVAL_SECONDS", 0.01)
        monkeypatch.setattr(activities, "_SLOT_RENEW_INTERVAL_SECONDS", 0.05)
        renewed: list[str] = []

        async def _renew(job_id: str) -> None:
            renewed.append(job_id)

        monkeypatch.setattr(activities, "_renew_document_slot", _renew)
        beats: list[Any] = []
        env = ActivityEnvironment()
        env.on_heartbeat = lambda *details: beats.append(details)

        async def body() -> str:
            async with activities._llm_activity_guard():
                await asyncio.sleep(0.2)
            return "done"

        assert await env.run(body) == "done"
        assert len(beats) >= 5
        assert 2 <= len(renewed) <= 6
        assert set(renewed) == {"test"}  # ActivityEnvironment's workflow id
        settled = len(beats)
        await asyncio.sleep(0.05)
        assert len(beats) == settled, "keep-alive must stop with the activity"

    async def test_body_result_and_errors_pass_through(self, monkeypatch):
        monkeypatch.setattr(activities, "_renew_document_slot", lambda _j: asyncio.sleep(0))
        env = ActivityEnvironment()

        async def failing() -> None:
            async with activities._llm_activity_guard():
                raise ValueError("boom")

        with pytest.raises(ValueError, match="boom"):
            await env.run(failing)

    async def test_stops_even_if_a_renewal_swallows_cancellation(self, monkeypatch):
        """Regression: a cancel landing inside a Redis call was swallowed and the
        activity never completed. The guard stops the keeper by event instead."""
        monkeypatch.setattr(activities, "_HEARTBEAT_INTERVAL_SECONDS", 0.01)

        async def _stubborn_renew(_job_id: str) -> None:
            try:
                await asyncio.sleep(0.2)
            except asyncio.CancelledError:  # what the Redis client effectively did
                await asyncio.sleep(0.2)

        monkeypatch.setattr(activities, "_renew_document_slot", _stubborn_renew)
        env = ActivityEnvironment()

        async def body() -> str:
            async with activities._llm_activity_guard():
                await asyncio.sleep(0.01)
            return "done"

        started = time.monotonic()
        assert await asyncio.wait_for(env.run(body), timeout=5) == "done"
        assert time.monotonic() - started < 1.0

    async def test_keeper_that_does_not_stop_is_cancelled(self, monkeypatch, caplog):
        monkeypatch.setattr(activities, "_KEEPER_STOP_TIMEOUT_SECONDS", 0.05)

        async def _hanging_renew(_job_id: str) -> None:
            await asyncio.sleep(10)

        monkeypatch.setattr(activities, "_renew_document_slot", _hanging_renew)
        caplog.set_level(logging.WARNING, logger="workflows.activities")
        env = ActivityEnvironment()

        async def body() -> str:
            async with activities._llm_activity_guard():
                await asyncio.sleep(0)
            return "done"

        assert await asyncio.wait_for(env.run(body), timeout=5) == "done"
        assert "keep-alive did not stop in time" in caplog.text

    async def test_keeper_error_is_logged_not_raised(self, monkeypatch, caplog):
        async def _broken_renew(_job_id: str) -> None:
            raise RuntimeError("unexpected")

        monkeypatch.setattr(activities, "_renew_document_slot", _broken_renew)
        caplog.set_level(logging.WARNING, logger="workflows.activities")
        env = ActivityEnvironment()

        async def body() -> str:
            async with activities._llm_activity_guard():
                await asyncio.sleep(0.01)
            return "done"

        assert await env.run(body) == "done"
        assert "keep-alive ended with an error (non-fatal)" in caplog.text
        assert "RuntimeError" in caplog.text


# ===================================================================
# Temporal: an LLM attempt of an ended workflow is cancelled (heartbeat)
# ===================================================================

_attempt_started = asyncio.Event()
_attempt_cancelled = asyncio.Event()


@activity.defn(name="slow_llm_probe")
async def slow_llm_probe() -> str:
    """Stands in for an LLM activity stuck behind the Ollama semaphore."""
    async with activities._llm_activity_guard():
        _attempt_started.set()
        try:
            await asyncio.sleep(3600)
        except asyncio.CancelledError:
            _attempt_cancelled.set()
            raise
    return "never"


@workflow.defn(name="SlowLlmProbeWorkflow", sandboxed=False)
class SlowLlmProbeWorkflow:
    @workflow.run
    async def run(self) -> str:
        return await workflow.execute_activity(
            slow_llm_probe,
            start_to_close_timeout=timedelta(minutes=10),  # like LLM_ACTIVITY_TIMEOUT_SECONDS
            retry_policy=RetryPolicy(maximum_attempts=1),
        )


@pytest.mark.temporal
@pytest.mark.asyncio
async def test_llm_attempt_of_an_ended_workflow_is_cancelled_promptly(monkeypatch):
    """Without heartbeats the attempt ran on until its own 10-minute timeout,
    still sending its request to Ollama after the job had ended."""
    monkeypatch.setattr(activities, "_HEARTBEAT_INTERVAL_SECONDS", 0.1)
    monkeypatch.setattr(activities, "get_settings", lambda: _settings(0))  # no lease renewal
    _attempt_started.clear()
    _attempt_cancelled.clear()

    async with await WorkflowEnvironment.start_time_skipping() as env:
        queue = f"slow-llm-{uuid.uuid4().hex}"
        async with Worker(
            env.client,
            task_queue=queue,
            workflows=[SlowLlmProbeWorkflow],
            activities=[slow_llm_probe],
            # Production uses the SDK default (30 s); shortened for the test.
            default_heartbeat_throttle_interval=timedelta(milliseconds=200),
            max_heartbeat_throttle_interval=timedelta(milliseconds=200),
        ):
            handle = await env.client.start_workflow(
                SlowLlmProbeWorkflow.run, id=f"slow-{uuid.uuid4().hex}", task_queue=queue
            )
            await asyncio.wait_for(_attempt_started.wait(), timeout=20)
            await handle.terminate("job ended")  # e.g. failed elsewhere / operator
            await asyncio.wait_for(_attempt_cancelled.wait(), timeout=10)


def test_module_defaults_are_sane():
    assert slots_module.DEFAULT_LEASE_SECONDS == 1800
    assert slots_module.DEFAULT_STALE_WAITER_SECONDS == 300
    assert activities._HEARTBEAT_INTERVAL_SECONDS < activities._SLOT_RENEW_INTERVAL_SECONDS
