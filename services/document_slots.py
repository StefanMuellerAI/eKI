"""Document limit: how many security checks analyse at the same time.

Without a limit the worker interleaves every submitted screenplay: all jobs
share the one LLM, each one waits for a slot in the Ollama semaphore, and that
wait counts against the activity timeout. With several large documents all of
them finished late together -- or failed together.

The limit (``MAX_CONCURRENT_DOCUMENTS``) admits at most *N* jobs into
processing. Every other job waits as ``pending`` in a queue ordered by
priority (1 = highest), then by arrival. The workflow polls with durable
timers, so a waiting job holds no worker slot and no activity timeout runs.

State lives in Redis (three sorted sets under ``eki:docslot:``):

* ``holders`` -- job id -> lease expiry (epoch ms). A holder renews its lease
  while its LLM activities run; a job that disappears without releasing (e.g.
  a terminated workflow) frees its slot when the lease runs out.
* ``queue``   -- job id -> priority * 10**13 + arrival (epoch ms).
* ``seen``    -- job id -> time of the last poll. Waiting jobs that stopped
  polling (workflow gone) are dropped after ``stale_waiter_seconds``; a job
  that only paused (worker restart) re-enters with its original arrival time.

Acquire is one Lua script, so concurrent workers cannot both take the last
slot. Times are passed in by the caller (no ``TIME`` in the script).
"""

from __future__ import annotations

import time
from dataclasses import dataclass
from typing import Any

KEY_PREFIX = "eki:docslot:"

# A holder renews its lease every minute while an LLM activity runs. The gaps
# without an LLM activity (PDF text extraction with OCR, aggregate_script:
# each at most 3 x 5 min plus backoff) stay well below 30 min.
DEFAULT_LEASE_SECONDS = 30 * 60
# Workflows poll at least every 60 s; five minutes without a poll means the
# workflow is gone (or the worker is down -- then the job simply re-enters).
DEFAULT_STALE_WAITER_SECONDS = 5 * 60

# Queue score = priority * factor + arrival in epoch ms. 10**13 ms is the
# year 2286, so priority always dominates; 10 * 10**13 < 2**53 keeps the
# score exact in Redis' doubles.
_PRIORITY_FACTOR = 10**13

_ACQUIRE_SCRIPT = """
local holders, queue, seen = KEYS[1], KEYS[2], KEYS[3]
local job = ARGV[1]
local score = tonumber(ARGV[2])
local limit = tonumber(ARGV[3])
local now = tonumber(ARGV[4])
local lease = tonumber(ARGV[5])
local stale = tonumber(ARGV[6])

redis.call('ZREMRANGEBYSCORE', holders, '-inf', now)
local gone = redis.call('ZRANGEBYSCORE', seen, '-inf', now - stale)
for _, member in ipairs(gone) do
  redis.call('ZREM', queue, member)
  redis.call('ZREM', seen, member)
end

if redis.call('ZSCORE', holders, job) then
  redis.call('ZADD', holders, now + lease, job)
  redis.call('ZREM', queue, job)
  redis.call('ZREM', seen, job)
  return {1, 0, redis.call('ZCARD', holders), redis.call('ZCARD', queue)}
end

redis.call('ZADD', queue, 'NX', score, job)
redis.call('ZADD', seen, now, job)
local held = redis.call('ZCARD', holders)
local rank = redis.call('ZRANK', queue, job)
if held < limit and rank < (limit - held) then
  redis.call('ZREM', queue, job)
  redis.call('ZREM', seen, job)
  redis.call('ZADD', holders, now + lease, job)
  return {1, 0, held + 1, redis.call('ZCARD', queue)}
end
return {0, rank + 1, held, redis.call('ZCARD', queue)}
"""


@dataclass(frozen=True)
class SlotDecision:
    """Outcome of one acquire attempt."""

    granted: bool
    position: int  # 1-based place in the queue while waiting, 0 when granted
    in_use: int  # slots held after this attempt
    waiting: int  # jobs in the queue after this attempt


def queue_score(priority: int, enqueued_at_ms: int) -> int:
    """Order by priority (1 = first), then by arrival."""
    return min(10, max(1, int(priority))) * _PRIORITY_FACTOR + max(0, int(enqueued_at_ms))


def _now_ms() -> int:
    return int(time.time() * 1000)


class DocumentSlots:
    """Counting semaphore with a fair queue on top of a ``redis.asyncio`` client."""

    def __init__(
        self,
        redis_client: Any,
        *,
        key_prefix: str = KEY_PREFIX,
        lease_seconds: int = DEFAULT_LEASE_SECONDS,
        stale_waiter_seconds: int = DEFAULT_STALE_WAITER_SECONDS,
    ) -> None:
        self._redis = redis_client
        self.holders_key = f"{key_prefix}holders"
        self.queue_key = f"{key_prefix}queue"
        self.seen_key = f"{key_prefix}seen"
        self._lease_ms = int(lease_seconds * 1000)
        self._stale_ms = int(stale_waiter_seconds * 1000)

    async def acquire(
        self,
        job_id: str,
        *,
        priority: int,
        enqueued_at_ms: int,
        limit: int,
        now_ms: int | None = None,
    ) -> SlotDecision:
        """Take a slot if one is free and *job_id* is next in line, else queue it.

        Idempotent: a holder that calls again keeps its slot (and renews the
        lease); a waiting job keeps its place.
        """
        raw = await self._redis.eval(
            _ACQUIRE_SCRIPT,
            3,
            self.holders_key,
            self.queue_key,
            self.seen_key,
            job_id,
            queue_score(priority, enqueued_at_ms),
            max(1, int(limit)),
            _now_ms() if now_ms is None else int(now_ms),
            self._lease_ms,
            self._stale_ms,
        )
        granted, position, in_use, waiting = (int(v) for v in raw)
        return SlotDecision(
            granted=bool(granted), position=position, in_use=in_use, waiting=waiting
        )

    async def renew(self, job_id: str, *, now_ms: int | None = None) -> bool:
        """Extend the lease of a current holder; no-op for anyone else."""
        now = _now_ms() if now_ms is None else int(now_ms)
        changed = await self._redis.zadd(
            self.holders_key, {job_id: now + self._lease_ms}, xx=True, ch=True
        )
        return bool(changed)

    async def release(self, job_id: str) -> tuple[int, int]:
        """Give the slot back (or leave the queue). Returns (in use, waiting)."""
        async with self._redis.pipeline(transaction=True) as pipe:
            pipe.zrem(self.holders_key, job_id)
            pipe.zrem(self.queue_key, job_id)
            pipe.zrem(self.seen_key, job_id)
            pipe.zcard(self.holders_key)
            pipe.zcard(self.queue_key)
            results = await pipe.execute()
        return int(results[3]), int(results[4])


__all__ = [
    "DEFAULT_LEASE_SECONDS",
    "DEFAULT_STALE_WAITER_SECONDS",
    "KEY_PREFIX",
    "DocumentSlots",
    "SlotDecision",
    "queue_score",
]
