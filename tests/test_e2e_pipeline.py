"""End-to-end tests for the complete eKI pipeline, in-process.

Every other suite stubs at least one layer (API tests use a mock Temporal
client, workflow tests use stub activities, activity tests call activities in
isolation). These tests run the *real* production path in one event loop:

    HTTP API (FastAPI ``app`` via ASGITransport, real API-key auth)
      -> ``get_temporal_client`` overridden with the time-skipping test server's
         client, wrapped with ``temporal_interceptors`` like api/dependencies.py
      -> ``worker/main.py:main()`` itself (only ``Client.connect`` is redirected
         to the test server), i.e. exactly the production registration list of
         ``SecurityCheckWorkflow`` and the activities from workflows/activities.py
      -> real FDX / PDF parsers on the fixtures in tests/fixtures/
      -> LLM replaced ONLY at the provider boundary (``llm.factory.get_llm_provider``)
         by a deterministic fake implementing ``llm.base.BaseLLMProvider``
      -> real ``SecureBuffer`` on a real Redis (``TEST_REDIS_URL``, default db 4,
         flushed before and after every test) shared by API and activities
      -> DB via the conftest ``test_engine`` (SQLite in-memory, or PostgreSQL
         with ``TEST_POSTGRES_URL``)
      -> delivery: one-shot GET (pull) or multipart POST to a local ePro mock
         HTTP server on an ephemeral port (push), plus the delivery-failed webhook.

Scenarios mirror the Pflichtenheft acceptance tests (docs/UAT/UAT_TESTPLAN.md):
AT2 push + delete-on-delivery, AT3 pull one-shot + 410, AT4 hard-4xx failure
branch with dead letter + webhook, AT5 idempotency, plus processing failures,
the synchronous endpoint and the "only reference keys in Temporal" invariant.

Waiting is bounded and sleep-free: job status is polled once per completed
activity (observed through a worker interceptor), workflow results are awaited
with ``asyncio.timeout``; durable timers (6h pull TTL) are skipped by the
time-skipping server only while a result is awaited.
"""

from __future__ import annotations

import asyncio
import base64
import contextvars
import copy
import email.policy
import json
import os
import re
import threading
import uuid
from collections.abc import AsyncIterator, Awaitable, Callable, Iterator, Sequence
from dataclasses import dataclass, field
from email.parser import BytesParser
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any

import pytest
import redis
import redis.asyncio as aioredis
import sqlalchemy.ext.asyncio as sa_asyncio
from httpx import ASGITransport, AsyncClient
from redis.exceptions import LockError
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker
from temporalio import activity
from temporalio import client as temporal_client
from temporalio import worker as temporal_worker
from temporalio.client import Client, WorkflowExecutionStatus
from temporalio.testing import WorkflowEnvironment

import llm.factory
import worker.main as worker_main
from api.config import get_settings
from api.dependencies import get_redis, get_temporal_client
from api.main import app
from core.db_models import DeliveryDeadLetter, JobMetadata, ReportMetadata
from core.models import JobStatus, SyncSecurityCheckResponse
from core.tracing import temporal_interceptors
from core.version import __version__
from llm.base import BaseLLMProvider
from workflows import activities
from workflows import security_check as security_check_module
from workflows.security_check import SecurityCheckWorkflow

pytestmark = pytest.mark.temporal

FIXTURES = Path(__file__).parent / "fixtures"
TEST_REDIS_URL = os.environ.get("TEST_REDIS_URL", "redis://localhost:6379/4")

# The activities must never reach a real database: their configured URL is a
# sentinel that only the patched engine factory below accepts.
_SENTINEL_DATABASE_URL = "postgresql+asyncpg://e2e:never-used@127.0.0.1:9/e2e_sentinel_never_used"
_EPRO_TOKEN = "e2e-epro-service-token"  # nosec B105 - test credential for the local mock
_USER_ID = "test-user-123"  # owner of conftest's ``auth_headers`` key
_WAIT_SECONDS = 60.0  # upper bound for any single wait on the pipeline
_REDIS_LOCK_KEY = "eki-e2e-tests:lock"  # outside the eki:buf:* namespace
_REDIS_LOCK_SECONDS = 600


def _redis_skip_reason() -> str | None:
    """Return why the module must be skipped, or ``None`` if Redis is usable."""
    try:
        probe = redis.Redis.from_url(TEST_REDIS_URL, socket_connect_timeout=1, socket_timeout=1)
        try:
            probe.ping()
            db_index = int(probe.connection_pool.connection_kwargs.get("db", 0) or 0)
        finally:
            probe.close()
    except Exception as exc:  # pragma: no cover - depends on the environment
        return f"Redis not reachable at TEST_REDIS_URL={TEST_REDIS_URL} ({type(exc).__name__})"
    if db_index == 0:  # pragma: no cover - safety net against flushing a shared db
        return f"refusing to FLUSHDB Redis db 0 (TEST_REDIS_URL={TEST_REDIS_URL}); use a test db"
    return None


_SKIP_REASON = _redis_skip_reason()
if _SKIP_REASON:  # pragma: no cover - depends on the environment
    pytest.skip(_SKIP_REASON, allow_module_level=True)


# ---------------------------------------------------------------------------
# Deterministic fake LLM (the only replaced component)
# ---------------------------------------------------------------------------

_STUNT_EVIDENCE = "A stunt performer stands at the edge of a 15-meter cliff."

# Answers of the fake "risk analysis" call, keyed by the scene location the
# parser extracted. Mixed on purpose: explicit vs. missing rule_id / category /
# measure codes, so the taxonomy enrichment is exercised end to end.
_RISK_ANSWERS: dict[str, list[dict[str, Any]]] = {
    # tests/fixtures/fdx/stunt_heavy.fdx
    "CLIFF EDGE": [
        {
            "risk_class": "STUNTS",
            "rule_id": "SEC-P-001",
            "category": "PHYSICAL",
            "likelihood": 4,
            "impact": 5,
            "description": "E2E: cliff jump of a stunt performer",
            "recommendation": "E2E: rehearse with the stunt coordinator",
            "measure_codes": ["STUNT-REHEARSAL", "MEDICAL-STANDBY"],
            "confidence": 0.95,
            "evidence": _STUNT_EVIDENCE,
        },
        {
            "risk_class": "WATER",
            "category": "",
            "likelihood": 3,
            "impact": 4,
            "description": "E2E: jump into dark water",
            "recommendation": "E2E: position safety divers",
            "measure_codes": ["WATER-SAFETY"],
            "evidence": "Below, the dark water churns.",
        },
    ],
    "HIGHWAY": [
        {
            "risk_class": "VEHICLES",
            "category": "PHYSICAL",
            "likelihood": 3,
            "impact": 3,
            "description": "E2E: high-speed car chase",
            "recommendation": "E2E: closed road and precision drivers",
            "evidence": "Three cars race at high speed along the highway.",
        },
        {
            "risk_class": "FIRE",
            "category": "PHYSICAL",
            "likelihood": 2,
            "impact": 5,
            "description": "E2E: fuel tanker explosion",
            "recommendation": "E2E: pyrotechnician and fire brigade on set",
            "measure_codes": ["SFX-CLEARANCE", "FIRE-DEPT"],
            "evidence": "A massive explosion erupts behind them.",
        },
    ],
    "BURNING BUILDING": [
        {
            "risk_class": "FIRE",
            "category": "PHYSICAL",
            "likelihood": 4,
            "impact": 4,
            "description": "E2E: actors inside a burning set",
            "recommendation": "E2E: controlled gas fire only",
            "measure_codes": ["FIRE-DEPT"],
            "evidence": "Flames engulf the hallway.",
        },
        {
            "risk_class": "SMOKE_DUST",
            "category": "ENVIRONMENTAL",
            "likelihood": 2,
            "impact": 2,
            "description": "E2E: near-zero visibility",
            "recommendation": "E2E: non-toxic haze only",
            "measure_codes": ["VENTILATION"],
            "evidence": "Smoke makes visibility near zero.",
        },
    ],
    "ROOFTOP": [
        {
            "risk_class": "VIOLENCE",
            "category": "PSYCHOLOGICAL",
            "likelihood": 1,
            "impact": 1,
            "description": "E2E: depicted knife fight",
            "recommendation": "E2E: brief the cast",
            "measure_codes": ["PSY-BRIEFING"],
            "evidence": "Weapons are drawn.",
        },
    ],
    # tests/fixtures/pdf/simple_screenplay.pdf
    "LIVING ROOM": [
        {
            "risk_class": "ELECTRICAL",
            "category": "PHYSICAL",
            "likelihood": 2,
            "impact": 3,
            "description": "E2E: lighting rig near windows",
            "recommendation": "E2E: check cabling",
            "measure_codes": ["SFX-CLEARANCE"],
            "evidence": "sunlight streaming through windows",
        },
    ],
    "GARDEN": [
        {
            "risk_class": "ANIMALS",
            "category": "PHYSICAL",
            "likelihood": 1,
            "impact": 2,
            "description": "E2E: birds on location",
            "recommendation": "E2E: animal handler",
            "measure_codes": ["ANIMAL-HANDLER"],
            "evidence": "Birds sing in the trees.",
        },
    ],
}

# (scene_number, risk_class, category, rule_id, risk_level) the report must contain.
_FDX_EXPECTED = sorted(
    [
        ("1", "STUNTS", "PHYSICAL", "SEC-P-001", "critical"),  # 4x5=20
        ("1", "WATER", "PHYSICAL", "SEC-P-007", "high"),  # 3x4=12, category+rule auto-filled
        ("2", "VEHICLES", "PHYSICAL", "SEC-P-005", "medium"),  # 3x3=9
        ("2", "FIRE", "PHYSICAL", "SEC-P-008", "high"),  # 2x5=10
        ("3", "FIRE", "PHYSICAL", "SEC-P-008", "critical"),  # 4x4=16
        ("3", "SMOKE_DUST", "ENVIRONMENTAL", "SEC-E-003", "low"),  # 2x2=4
        ("4", "VIOLENCE", "PSYCHOLOGICAL", "SEC-Y-001", "info"),  # 1x1=1
    ]
)
_FDX_RISK_SUMMARY = {"critical": 2, "high": 2, "medium": 1, "low": 1, "info": 1}
_FDX_SCENES = [
    ("1", "CLIFF EDGE", "EXT", "NIGHT"),
    ("2", "HIGHWAY", "EXT", "DAY"),
    ("3", "BURNING BUILDING", "INT", "NIGHT"),
    ("4", "ROOFTOP", "EXT", "DAY"),
]
_PDF_EXPECTED = sorted(
    [
        ("1", "ELECTRICAL", "PHYSICAL", "SEC-P-009", "medium"),  # 2x3=6
        ("2", "ANIMALS", "PHYSICAL", "SEC-P-010", "low"),  # 1x2=2
    ]
)
_PDF_SCENES = [("1", "LIVING ROOM", "INT", "DAY"), ("2", "GARDEN", "EXT", "MORNING")]

# Script text that the fake LLM never echoes back: it must not show up in
# Temporal history or dead letters (only reference keys may travel there).
_FDX_UNQUOTED_SCRIPT_TEXT = [
    "Clear the zone! Jump on my mark!",
    "Safety divers are positioned in the water.",
    "A firefighter carries an unconscious child through the inferno.",
]

_HEADING_RE = re.compile(r"^(INT|EXT)\.\s+(.+?)\s+-\s+([A-Z]+)\s*$", re.MULTILINE)


def _between(text: str, start: str, end: str) -> str:
    head, _, rest = text.partition(start)
    return rest.partition(end)[0].strip() if rest else ""


class FakeLLMProvider(BaseLLMProvider):
    """Deterministic stand-in for the LLM, answering by JSON schema.

    * risk analysis schema (``findings``) -> ``_RISK_ANSWERS[location]``
    * PDF scene structuring schema (``location``) -> fields parsed from the heading
    * PDF preamble schema (``title``) -> a fixed title
    """

    def __init__(self) -> None:
        super().__init__({})
        self.risk_calls: list[dict[str, str]] = []
        self.structure_calls: list[str] = []
        self.preamble_calls = 0

    @property
    def provider_name(self) -> str:
        return "e2e_fake"

    async def health_check(self) -> bool:
        return True

    async def generate(self, prompt: str, system_prompt: str | None = None, **kwargs: Any) -> str:
        raise AssertionError("the pipeline is expected to use structured output only")

    async def generate_structured(
        self,
        prompt: str,
        schema: dict[str, Any],
        system_prompt: str | None = None,
        temperature: float = 0.2,
        **kwargs: Any,
    ) -> dict[str, Any]:
        props = schema.get("properties", {})
        if "findings" in props:
            return self._risk(prompt)
        if "location" in props:
            return self._structure(prompt)
        if "title" in props:
            self.preamble_calls += 1
            return {"title": "E2E Fake Title"}
        raise AssertionError(f"unexpected schema: {sorted(props)}")

    def _risk(self, prompt: str) -> dict[str, Any]:
        scene = re.search(r"^Scene: (.+)$", prompt, re.MULTILINE)
        location = re.search(r"^Location: (.+) \((.*)\)$", prompt, re.MULTILINE)
        tod = re.search(r"^Time of day: (.+)$", prompt, re.MULTILINE)
        assert scene and location and tod, "risk prompt lost its scene header"
        call = {
            "scene_number": scene.group(1).strip(),
            "location": location.group(1).strip(),
            "location_type": location.group(2).strip(),
            "time_of_day": tod.group(1).strip(),
            "scene_text": _between(prompt, "[SCENE START]", "[SCENE END]"),
        }
        self.risk_calls.append(call)
        return {"findings": copy.deepcopy(_RISK_ANSWERS.get(call["location"], []))}

    def _structure(self, prompt: str) -> dict[str, Any]:
        block = _between(prompt, "[SCENE START]", "[SCENE END]")
        self.structure_calls.append(block)
        heading = _HEADING_RE.search(block)
        assert heading, f"no scene heading in structuring prompt: {block[:80]!r}"
        lines = [ln.strip() for ln in block.splitlines()[1:] if ln.strip()]
        names = [ln for ln in lines if ln.isupper() and not ln.endswith(":") and "." not in ln]
        return {
            "location": heading.group(2),
            "location_type": heading.group(1),
            "time_of_day": heading.group(3),
            "characters": sorted(set(names)),
            "action_text": " ".join(ln for ln in lines if not ln.isupper()),
            "dialogue": [],
        }

    def risk_scenes(self) -> list[tuple[str, str, str, str]]:
        return [
            (c["scene_number"], c["location"], c["location_type"], c["time_of_day"])
            for c in self.risk_calls
        ]


# ---------------------------------------------------------------------------
# ePro mock: a real HTTP server on an ephemeral localhost port
# ---------------------------------------------------------------------------

_ASSESSMENT_PREFIX = "/api/eki/scl/set-risk-assessment/"
_WEBHOOK_PATH = "/api/eki/scl/delivery-failed"


@dataclass
class EproRequest:
    method: str
    path: str
    headers: dict[str, str]  # lower-cased names
    body: bytes

    def form(self) -> dict[str, dict[str, Any]]:
        """Parse the multipart/form-data body into ``{name: {filename, content_type, data}}``."""
        content_type = self.headers["content-type"]
        assert content_type.startswith("multipart/form-data"), content_type
        message = BytesParser(policy=email.policy.HTTP).parsebytes(
            f"Content-Type: {content_type}\r\n\r\n".encode() + self.body
        )
        parts: dict[str, dict[str, Any]] = {}
        for part in message.iter_parts():
            name = part.get_param("name", header="content-disposition")
            parts[str(name)] = {
                "filename": part.get_filename(),
                "content_type": part.get_content_type(),
                "data": part.get_payload(decode=True),
            }
        return parts

    def text_field(self, name: str) -> str:
        return self.form()[name]["data"].decode("utf-8")


class EproMock:
    """Records every request; answers the assessment endpoint with ``assessment_status``."""

    def __init__(self) -> None:
        self.requests: list[EproRequest] = []
        self.assessment_status = 201
        self._lock = threading.Lock()
        mock = self

        class _Handler(BaseHTTPRequestHandler):
            protocol_version = "HTTP/1.1"

            def do_POST(self) -> None:  # noqa: N802 - stdlib naming
                length = int(self.headers.get("Content-Length") or 0)
                body = self.rfile.read(length)
                request = EproRequest(
                    method="POST",
                    path=self.path,
                    headers={k.lower(): v for k, v in self.headers.items()},
                    body=body,
                )
                with mock._lock:
                    mock.requests.append(request)
                    status = mock.assessment_status
                if self.path.startswith(_ASSESSMENT_PREFIX):
                    ok = 200 <= status < 300
                    payload = {"status": ok, "message": "persisted" if ok else "rejected"}
                elif self.path == _WEBHOOK_PATH:
                    status, payload = 200, {"ok": True}
                else:
                    status, payload = 404, {"error": "unknown path"}
                data = json.dumps(payload).encode()
                self.send_response(status)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(data)))
                self.end_headers()
                self.wfile.write(data)

            def log_message(self, format: str, *args: Any) -> None:  # noqa: A002
                return None

        self._server = ThreadingHTTPServer(("127.0.0.1", 0), _Handler)
        self._thread = threading.Thread(target=self._server.serve_forever, daemon=True)

    @property
    def base_url(self) -> str:
        return f"http://127.0.0.1:{self._server.server_address[1]}/api"

    def start(self) -> None:
        self._thread.start()

    def stop(self) -> None:
        self._server.shutdown()
        self._server.server_close()
        self._thread.join(timeout=5)

    def deliveries(self) -> list[EproRequest]:
        with self._lock:
            return [r for r in self.requests if r.path.startswith(_ASSESSMENT_PREFIX)]

    def webhooks(self) -> list[EproRequest]:
        with self._lock:
            return [r for r in self.requests if r.path == _WEBHOOK_PATH]


# ---------------------------------------------------------------------------
# Temporal instrumentation (observation only, public interceptor API)
# ---------------------------------------------------------------------------


@dataclass
class StartedWorkflow:
    workflow_id: str
    task_queue: str
    args: Sequence[Any]
    handle: temporal_client.WorkflowHandle[Any, Any]


class _StartRecorder(temporal_client.Interceptor):
    """Records every ``start_workflow`` the API issues (and the returned handle)."""

    def __init__(self) -> None:
        self.starts: list[StartedWorkflow] = []

    def intercept_client(
        self, next: temporal_client.OutboundInterceptor
    ) -> temporal_client.OutboundInterceptor:
        return _StartRecorderOutbound(next, self)


class _StartRecorderOutbound(temporal_client.OutboundInterceptor):
    def __init__(self, next: temporal_client.OutboundInterceptor, rec: _StartRecorder) -> None:
        super().__init__(next)
        self._rec = rec

    async def start_workflow(
        self, input: temporal_client.StartWorkflowInput
    ) -> temporal_client.WorkflowHandle[Any, Any]:
        handle = await super().start_workflow(input)
        self._rec.starts.append(
            StartedWorkflow(
                workflow_id=input.id, task_queue=input.task_queue, args=input.args, handle=handle
            )
        )
        return handle


class ActivityProgress(temporal_client.Interceptor, temporal_worker.Interceptor):
    """Signals every finished activity attempt so tests can poll without sleeping.

    With a ``gate`` (SQLite mode only, see ``db_gate``) each activity runs under
    the same lock as the API requests.
    """

    def __init__(self, gate: asyncio.Lock | None = None) -> None:
        self.finished: list[str] = []
        self.gate = gate
        self._changed = asyncio.Event()

    def intercept_activity(
        self, next: temporal_worker.ActivityInboundInterceptor
    ) -> temporal_worker.ActivityInboundInterceptor:
        return _ActivityProgressInbound(next, self)

    def _record(self, name: str) -> None:
        self.finished.append(name)
        self._changed.set()

    async def wait_beyond(self, seen: int) -> int:
        """Wait until more than ``seen`` activity attempts finished; return the new count."""
        while len(self.finished) <= seen:
            self._changed.clear()
            await self._changed.wait()
        return len(self.finished)

    def count(self, name: str) -> int:
        return self.finished.count(name)


class _ActivityProgressInbound(temporal_worker.ActivityInboundInterceptor):
    def __init__(
        self, next: temporal_worker.ActivityInboundInterceptor, progress: ActivityProgress
    ) -> None:
        super().__init__(next)
        self._progress = progress

    async def execute_activity(self, input: temporal_worker.ExecuteActivityInput) -> Any:
        try:
            if self._progress.gate is None:
                return await super().execute_activity(input)
            async with self._progress.gate:
                return await super().execute_activity(input)
        finally:
            self._progress._record(activity.info().activity_type)


def _gated_app(asgi_app: Any, gate: asyncio.Lock | None) -> Any:
    """Run each API request under ``gate`` (no-op without one)."""
    if gate is None:
        return asgi_app

    async def _app(scope: Any, receive: Any, send: Any) -> None:
        async with gate:
            await asgi_app(scope, receive, send)

    return _app


def _client_from(base: Client, interceptors: Sequence[Any]) -> Client:
    """Clone the test-server client (keeping its time-skipping interceptors)."""
    config = base.config()
    config["interceptors"] = list(interceptors)
    return Client(**config)


# ---------------------------------------------------------------------------
# DB routing for the activities
# ---------------------------------------------------------------------------


class _NoDisposeEngine:
    """Returned by the patched ``_session_factory``; activities only call ``dispose()``."""

    async def dispose(self) -> None:
        return None


class _SharedAsyncEngine(AsyncEngine):
    """View on the conftest test engine whose ``dispose()`` keeps the shared pool alive."""

    async def dispose(self, close: bool = True) -> None:
        return None


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def epro() -> Iterator[EproMock]:
    mock = EproMock()
    mock.start()
    yield mock
    mock.stop()


@pytest.fixture
def e2e_settings(monkeypatch: pytest.MonkeyPatch, epro: EproMock) -> Any:
    """Point the (cached) Settings singleton at the test infrastructure."""
    settings = get_settings()
    overrides: dict[str, Any] = {
        "redis_url": TEST_REDIS_URL,
        "database_url": _SENTINEL_DATABASE_URL,
        "temporal_task_queue": f"e2e-{uuid.uuid4().hex}",
        "epro_base_url": epro.base_url,
        "epro_auth_token": _EPRO_TOKEN,
        "epro_webhook_url": epro.base_url + "/eki/scl/delivery-failed",
        "kb_cleanup_enabled": False,
        "kb_retrieval_enabled": False,
        "llm_parallel_enabled": False,
        "otel_enabled": False,
        "buffer_ttl_seconds": 21600,
    }
    for name, value in overrides.items():
        monkeypatch.setattr(settings, name, value)
    return settings


@pytest.fixture
async def redis_db() -> AsyncIterator[aioredis.Redis]:
    """Exclusive, empty Redis db for one test.

    The lock serialises concurrent pytest sessions that share ``TEST_REDIS_URL``
    (e.g. two developers or CI jobs on one Redis): without it one session's
    cleanup would wipe the other's in-flight buffer entries. Clearing deletes
    every key except the lock (equivalent to FLUSHDB for the test).
    """
    client = aioredis.from_url(TEST_REDIS_URL)
    lock = client.lock(_REDIS_LOCK_KEY, timeout=_REDIS_LOCK_SECONDS)
    if not await lock.acquire(blocking_timeout=_REDIS_LOCK_SECONDS):
        await client.aclose()
        pytest.fail(f"another test session holds {_REDIS_LOCK_KEY} on {TEST_REDIS_URL}")
    try:
        await _clear_redis(client)
        yield client
    finally:
        try:
            await _clear_redis(client)
        finally:
            try:
                await lock.release()
            except LockError:  # pragma: no cover - lock expired meanwhile
                pass
            await client.aclose()


async def _clear_redis(client: aioredis.Redis) -> None:
    keys = [k async for k in client.scan_iter(match="*") if k != _REDIS_LOCK_KEY.encode()]
    if keys:
        await client.delete(*keys)


@pytest.fixture
def fake_llm(monkeypatch: pytest.MonkeyPatch) -> FakeLLMProvider:
    fake = FakeLLMProvider()
    monkeypatch.setattr(llm.factory, "get_llm_provider", lambda settings: fake)
    return fake


@pytest.fixture
def db_gate(test_engine: Any) -> asyncio.Lock | None:
    """SQLite mode only: serialise API requests and activity executions.

    The conftest SQLite database is ONE in-memory aiosqlite connection shared by
    every session (and reused across the per-test event loops). Concurrent use
    by the API and an activity would interleave their transactions on that
    single connection and bind its internal mutex to this test's loop. In
    production (and in PostgreSQL mode) API and worker use separate
    connections, so PostgreSQL mode keeps full concurrency.
    """
    return asyncio.Lock() if test_engine.dialect.name == "sqlite" else None


@pytest.fixture
def db(test_engine: Any) -> async_sessionmaker[AsyncSession]:
    """Session factory for assertions (same database the API and activities use)."""
    return async_sessionmaker(test_engine, class_=AsyncSession, expire_on_commit=False)


@pytest.fixture
def activity_db(
    monkeypatch: pytest.MonkeyPatch,
    test_engine: Any,
    e2e_settings: Any,
    db: async_sessionmaker[AsyncSession],
) -> list[str]:
    """Route the activities' two DB entry points to the test engine.

    * ``workflows.activities._session_factory`` (delivery bookkeeping, dead letters)
    * ``create_async_engine(settings.database_url)`` inside ``update_job_status_activity``
    """
    monkeypatch.setattr(activities, "_session_factory", lambda: (_NoDisposeEngine(), db))
    shared = _SharedAsyncEngine(test_engine.sync_engine)
    real_create = sa_asyncio.create_async_engine
    routed: list[str] = []

    def _create(url: Any, *args: Any, **kwargs: Any) -> AsyncEngine:
        if str(url) == _SENTINEL_DATABASE_URL:
            routed.append(str(url))
            return shared
        return real_create(url, *args, **kwargs)

    monkeypatch.setattr(sa_asyncio, "create_async_engine", _create)
    return routed


@pytest.fixture
async def temporal_env() -> AsyncIterator[WorkflowEnvironment]:
    async with await WorkflowEnvironment.start_time_skipping() as env:
        yield env


@dataclass
class WorkerRun:
    connect_calls: list[dict[str, Any]]
    registration: dict[str, Any]
    progress: ActivityProgress


@pytest.fixture
async def pipeline_worker(
    monkeypatch: pytest.MonkeyPatch,
    temporal_env: WorkflowEnvironment,
    e2e_settings: Any,
    activity_db: list[str],
    fake_llm: FakeLLMProvider,
    redis_db: aioredis.Redis,
    db_gate: asyncio.Lock | None,
) -> AsyncIterator[WorkerRun]:
    """Run the production worker entrypoint ``worker/main.py:main()`` against the test server."""
    progress = ActivityProgress(db_gate)
    connect_calls: list[dict[str, Any]] = []
    workers: list[Any] = []
    env_client = temporal_env.client

    class _ConnectToTestServer:
        @staticmethod
        async def connect(target_host: str, **kwargs: Any) -> Client:
            connect_calls.append({"target_host": target_host, **kwargs})
            return _client_from(
                env_client,
                [*env_client.config()["interceptors"], *kwargs.get("interceptors", []), progress],
            )

    class _RecordingWorker(temporal_worker.Worker):
        def __init__(self, client: Client, **kwargs: Any) -> None:
            super().__init__(client, **kwargs)
            self.e2e_registration = dict(kwargs)
            workers.append(self)

    monkeypatch.setattr(worker_main, "Client", _ConnectToTestServer)
    monkeypatch.setattr(worker_main, "Worker", _RecordingWorker)
    monkeypatch.setattr(worker_main, "start_metrics_server", lambda port: None)
    monkeypatch.setattr(worker_main, "set_build_info", lambda **kwargs: None)

    # Fresh context: nothing the test task sets (e.g. the API middleware's
    # request_id ContextVar) can leak into activities except via Temporal headers.
    task = asyncio.create_task(worker_main.main(), context=contextvars.Context())
    async with asyncio.timeout(_WAIT_SECONDS):
        while not workers and not task.done():
            await asyncio.sleep(0)
    if task.done():
        task.result()  # surface start-up errors of worker/main.py
    assert workers, "worker/main.py:main() did not construct a Worker"
    try:
        yield WorkerRun(
            connect_calls=connect_calls,
            registration=workers[0].e2e_registration,
            progress=progress,
        )
    finally:
        await workers[0].shutdown()
        await asyncio.wait_for(task, timeout=30)


@dataclass
class Pipeline:
    http: AsyncClient
    starts: list[StartedWorkflow]
    progress: ActivityProgress
    epro: EproMock
    llm: FakeLLMProvider
    redis: aioredis.Redis
    db: async_sessionmaker[AsyncSession]
    settings: Any
    routed_engines: list[str] = field(default_factory=list)


@pytest.fixture
async def pipeline(
    override_get_db: None,
    e2e_settings: Any,
    redis_db: aioredis.Redis,
    temporal_env: WorkflowEnvironment,
    pipeline_worker: WorkerRun,
    epro: EproMock,
    fake_llm: FakeLLMProvider,
    db: async_sessionmaker[AsyncSession],
    activity_db: list[str],
    db_gate: asyncio.Lock | None,
) -> AsyncIterator[Pipeline]:
    recorder = _StartRecorder()
    env_client = temporal_env.client
    # Like api/dependencies.get_temporal_client: interceptors=temporal_interceptors(settings).
    api_temporal = _client_from(
        env_client,
        [recorder, *env_client.config()["interceptors"], *temporal_interceptors(e2e_settings)],
    )

    async def _override_temporal() -> AsyncIterator[Client]:
        yield api_temporal

    # Redis: the real ``get_redis`` dependency, reading the patched settings.redis_url.
    assert get_redis not in app.dependency_overrides, "a leaked get_redis override"
    app.dependency_overrides[get_temporal_client] = _override_temporal
    try:
        transport = ASGITransport(app=_gated_app(app, db_gate))
        async with AsyncClient(transport=transport, base_url="http://test") as http:
            yield Pipeline(
                http=http,
                starts=recorder.starts,
                progress=pipeline_worker.progress,
                epro=epro,
                llm=fake_llm,
                redis=redis_db,
                db=db,
                settings=e2e_settings,
                routed_engines=activity_db,
            )
    finally:
        app.dependency_overrides.pop(get_temporal_client, None)
        # A test that failed early may leave its workflow running; terminate it
        # so the worker does not shut down with live workflow coroutines.
        for start in recorder.starts:
            try:
                description = await start.handle.describe()
                if description.status == WorkflowExecutionStatus.RUNNING:
                    await start.handle.terminate(reason="e2e test teardown")
            except Exception:  # pragma: no cover - best-effort cleanup
                pass


@pytest.fixture
async def sync_api(
    override_get_db: None,
    e2e_settings: Any,
    redis_db: aioredis.Redis,
    fake_llm: FakeLLMProvider,
) -> AsyncIterator[AsyncClient]:
    """API client without any Temporal wiring (the sync endpoint must not need it)."""
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as http:
        yield http


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _fixture_b64(relative: str) -> str:
    return base64.b64encode((FIXTURES / relative).read_bytes()).decode("ascii")


def _project_id(tag: str) -> str:
    return f"e2e-{tag}-{uuid.uuid4().hex[:8]}"


async def _submit_json(
    p: Pipeline,
    headers: dict[str, str],
    *,
    fixture: str,
    script_format: str,
    project_id: str,
    delivery: str,
    request_id: str | None = None,
    **extra: Any,
) -> Any:
    body = {
        "script_content": _fixture_b64(fixture),
        "script_format": script_format,
        "project_id": project_id,
        "delivery": delivery,
        "script_id": 4711,
        **extra,
    }
    hdrs = {**headers, **({"X-Request-ID": request_id} if request_id else {})}
    return await p.http.post("/v1/security/check:async", json=body, headers=hdrs)


def _started(p: Pipeline, job_id: str) -> StartedWorkflow:
    mine = [s for s in p.starts if s.workflow_id == job_id]
    assert len(mine) == 1, f"expected exactly one workflow start for {job_id}, got {len(mine)}"
    return mine[0]


async def _workflow_result(p: Pipeline, job_id: str) -> dict[str, Any]:
    """Await the workflow result (the time-skipping handle skips durable timers)."""
    async with asyncio.timeout(_WAIT_SECONDS):
        return await _started(p, job_id).handle.result()


async def _poll_job_until(
    p: Pipeline, headers: dict[str, str], status_url: str, wanted: set[str]
) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    """GET the job status once per finished activity until it reaches ``wanted``."""
    observed: list[dict[str, Any]] = []
    seen = len(p.progress.finished)
    async with asyncio.timeout(_WAIT_SECONDS):
        while True:
            resp = await p.http.get(status_url, headers=headers)
            assert resp.status_code == 200, resp.text
            body = resp.json()
            observed.append(body)
            if body["status"] in wanted:
                return body, observed
            seen = await p.progress.wait_beyond(seen)


async def _buffer_keys(r: aioredis.Redis) -> list[str]:
    return sorted([k.decode() async for k in r.scan_iter(match="eki:buf:*")])


async def _residual_keys(r: aioredis.Redis) -> list[str]:
    """Every Redis key except the rate limiter's counters and the test lock."""
    return sorted(
        [
            k.decode()
            async for k in r.scan_iter(match="*")
            if not k.startswith(b"rate_limit:") and k != _REDIS_LOCK_KEY.encode()
        ]
    )


async def _history_bytes(p: Pipeline, job_id: str) -> bytes:
    history = await _started(p, job_id).handle.fetch_history()
    return b"".join(event.SerializeToString() for event in history.events)


def _signature(findings: list[dict[str, Any]]) -> list[tuple[str, str, str, str, str]]:
    return sorted(
        (f["scene_number"], f["risk_class"], f["category"], f["rule_id"], f["risk_level"])
        for f in findings
    )


async def _job_row(p: Pipeline, job_id: str) -> JobMetadata:
    async with p.db() as session:
        return (
            await session.execute(
                select(JobMetadata).where(JobMetadata.job_id == uuid.UUID(job_id))
            )
        ).scalar_one()


async def _report_rows(p: Pipeline, job_id: str) -> list[ReportMetadata]:
    async with p.db() as session:
        return list(
            (
                await session.execute(
                    select(ReportMetadata).where(ReportMetadata.job_id == uuid.UUID(job_id))
                )
            ).scalars()
        )


async def _dead_letters(p: Pipeline, job_id: str) -> list[DeliveryDeadLetter]:
    async with p.db() as session:
        return list(
            (
                await session.execute(
                    select(DeliveryDeadLetter).where(DeliveryDeadLetter.job_id == uuid.UUID(job_id))
                )
            ).scalars()
        )


def _assert_report_matches(
    report: dict[str, Any],
    *,
    report_id: str,
    project_id: str,
    script_format: str,
    expected: list[tuple[str, str, str, str, str]],
) -> None:
    assert report["report_id"] == report_id
    assert report["project_id"] == project_id
    assert report["script_format"] == script_format
    assert _signature(report["findings"]) == expected
    assert report["total_findings"] == len(expected)
    summary = dict.fromkeys(("critical", "high", "medium", "low", "info"), 0)
    for _, _, _, _, level in expected:
        summary[level] += 1
    assert report["risk_summary"] == summary
    assert report["metadata"]["taxonomy_version"] == "1.0"
    assert report["metadata"]["engine_version"] == __version__
    ids = [f["id"] for f in report["findings"]]
    assert len(set(ids)) == len(ids)
    for finding in report["findings"]:
        uuid.UUID(finding["id"])
        assert finding["description"].startswith("E2E: ")
        assert finding["evidence"]
        assert 0.0 < finding["confidence"] <= 1.0
        assert finding["measures"], f"no measures for {finding['risk_class']}"
        assert all(m["code"] and m["title"] for m in finding["measures"])


async def _run_pull_flow(
    p: Pipeline,
    headers: dict[str, str],
    resp: Any,
    before_fetch: Callable[[str, str], Awaitable[None]] | None = None,
) -> dict[str, Any]:
    """Poll an accepted job to ``completed``, fetch the report once, verify one-shot semantics.

    ``before_fetch(job_id, report_id)`` runs after completion, before the owner's GET.
    """
    assert resp.status_code == 202, resp.text
    accepted = resp.json()
    job_id = accepted["job_id"]
    assert accepted["status"] == "pending"
    assert accepted["status_url"] == f"/v1/security/jobs/{job_id}"
    assert "delivery=pull" in accepted["message"]

    start = _started(p, job_id)
    assert start.task_queue == p.settings.temporal_task_queue
    job_data = start.args[0]
    # Temporal only ever sees an opaque buffer reference, never the script.
    assert job_data["ref_key"].startswith("eki:buf:")
    assert "script_content" not in job_data
    assert job_data["job_id"] == job_id and job_data["delivery_mode"] == "pull"

    status, observed = await _poll_job_until(
        p, headers, accepted["status_url"], wanted={"completed", "failed"}
    )
    assert status["status"] == "completed", status
    progress = [o["progress_percentage"] or 0 for o in observed]
    assert progress == sorted(progress), f"progress went backwards: {progress}"
    assert status["progress_percentage"] == 100
    assert status["error_message"] is None
    report_id = status["report_id"]
    assert report_id is not None and report_id == job_data["report_id"]
    # Pull: the report is ready, but "delivered" only after the one-shot GET.
    assert status["metadata"]["delivery_mode"] == "pull"
    assert status["metadata"]["delivery_status"] == "pending"
    assert status["metadata"]["delivery_attempts"] == 1
    assert status["metadata"]["delivery_last_status_code"] is None

    # Exactly one encrypted entry is left: the report package for the GET.
    assert len(await _buffer_keys(p.redis)) == 1
    if before_fetch is not None:
        await before_fetch(job_id, report_id)
        assert len(await _buffer_keys(p.redis)) == 1

    first = await p.http.get(f"/v1/security/reports/{report_id}", headers=headers)
    assert first.status_code == 200, first.text
    assert first.headers.get("X-One-Shot") == "true"
    body = first.json()
    assert base64.b64decode(body["pdf_base64"]).startswith(b"%PDF")
    assert "invalidated" in body["message"]

    second = await p.http.get(f"/v1/security/reports/{report_id}", headers=headers)
    assert second.status_code == 410, second.text
    assert second.json()["error"] == "Gone"

    # Delete-on-delivery: no script, no intermediate result, no report left.
    assert await _buffer_keys(p.redis) == []
    assert await _residual_keys(p.redis) == []

    # Now let the 6h pull-TTL timer elapse: the watch sees the retrieval -> completed.
    result = await _workflow_result(p, job_id)
    assert result["status"] == "completed", result
    assert result["delivered"] is True
    assert result["delivery_mode"] == "pull"
    assert result["report_id"] == report_id
    assert p.progress.count("check_report_retrieved") == 1
    assert p.progress.count("cleanup_buffer") == 0

    final = await p.http.get(accepted["status_url"], headers=headers)
    assert final.status_code == 200
    final_body = final.json()
    assert final_body["status"] == "completed"
    assert final_body["metadata"]["delivery_status"] == "delivered"
    assert final_body["metadata"]["delivered_at"] is not None

    return {"job_id": job_id, "report_id": report_id, "report": body["report"]}


async def _assert_pull_db_state(
    p: Pipeline, job_id: str, report_id: str, *, project_id: str, fmt: str, total: int
) -> None:
    job = await _job_row(p, job_id)
    assert job.status == JobStatus.COMPLETED
    assert job.progress_percentage == 100
    assert str(job.report_id) == report_id
    assert job.user_id == _USER_ID
    assert job.project_id == project_id
    assert str(getattr(job.script_format, "value", job.script_format)) == fmt
    assert job.delivery_mode == "pull"
    assert job.delivery_status == "delivered"
    assert job.delivery_attempts == 1
    assert job.delivery_last_status_code is None
    assert job.delivered_at is not None
    assert job.error_message is None

    reports = await _report_rows(p, job_id)
    assert len(reports) == 1
    report = reports[0]
    assert str(report.report_id) == report_id
    assert report.user_id == _USER_ID and report.project_id == project_id
    assert report.delivery_mode == "pull"
    assert report.is_retrieved is True
    assert report.retrieved_at is not None
    assert report.total_findings == total
    assert report.processing_time_seconds >= 0
    assert (report.report_ref_key or "").startswith("eki:buf:")
    assert await p.redis.exists(report.report_ref_key) == 0

    assert await _dead_letters(p, job_id) == []
    # update_job_status_activity really wrote through the (routed) configured DB URL.
    assert p.routed_engines, "update_job_status_activity never reached the database"


# ---------------------------------------------------------------------------
# Wiring
# ---------------------------------------------------------------------------


async def test_worker_entrypoint_registers_everything_the_workflow_uses(
    pipeline_worker: WorkerRun, e2e_settings: Any
) -> None:
    """worker/main.py registers SecurityCheckWorkflow and every activity it can call."""
    registration = pipeline_worker.registration
    assert SecurityCheckWorkflow in registration["workflows"]
    assert registration["task_queue"] == e2e_settings.temporal_task_queue
    registered = {fn.__temporal_activity_definition.name for fn in registration["activities"]}
    used_by_workflow = {
        obj.__temporal_activity_definition.name
        for obj in vars(security_check_module).values()
        if hasattr(obj, "__temporal_activity_definition")
    }
    assert {"parse_fdx", "extract_pdf_text", "deliver_report", "record_dead_letter"} <= (
        used_by_workflow
    )
    assert used_by_workflow <= registered, used_by_workflow - registered

    (call,) = pipeline_worker.connect_calls
    assert call["target_host"] == e2e_settings.temporal_host
    assert call["namespace"] == e2e_settings.temporal_namespace
    assert any(type(i).__name__ == "ContextPropagationInterceptor" for i in call["interceptors"])


# ---------------------------------------------------------------------------
# a) Pull mode, FDX
# ---------------------------------------------------------------------------


async def test_pull_fdx_end_to_end_one_shot_report(
    pipeline: Pipeline, auth_headers: dict[str, str], auth_headers_user2: dict[str, str]
) -> None:
    """AT3: FDX -> parse -> fake LLM per scene -> report -> one-shot GET -> 410 -> buffer empty."""
    project_id = _project_id("pull-fdx")

    resp = await _submit_json(
        pipeline,
        auth_headers,
        fixture="fdx/stunt_heavy.fdx",
        script_format="fdx",
        project_id=project_id,
        delivery="pull",
        priority=3,
    )
    job_id = resp.json()["job_id"]

    async def _idor_probes(probe_job_id: str, probe_report_id: str) -> None:
        # Another tenant sees neither job nor report, and its probe does not
        # consume the one-shot retrieval.
        other = await pipeline.http.get(
            f"/v1/security/jobs/{probe_job_id}", headers=auth_headers_user2
        )
        assert other.status_code == 404
        other = await pipeline.http.get(
            f"/v1/security/reports/{probe_report_id}", headers=auth_headers_user2
        )
        assert other.status_code == 404

    outcome = await _run_pull_flow(pipeline, auth_headers, resp, before_fetch=_idor_probes)
    report = outcome["report"]
    _assert_report_matches(
        report,
        report_id=outcome["report_id"],
        project_id=project_id,
        script_format="fdx",
        expected=_FDX_EXPECTED,
    )
    assert report["risk_summary"] == _FDX_RISK_SUMMARY

    stunts = next(f for f in report["findings"] if f["risk_class"] == "STUNTS")
    assert stunts["likelihood"] == 4 and stunts["impact"] == 5
    assert [m["code"] for m in stunts["measures"]] == ["STUNT-REHEARSAL", "MEDICAL-STANDBY"]
    assert stunts["evidence"] == _STUNT_EVIDENCE
    assert stunts["confidence"] == 0.95
    vehicles = next(f for f in report["findings"] if f["risk_class"] == "VEHICLES")
    # No measure codes from the LLM -> catalog suggestions for the class.
    assert {"VEHICLE-SAFETY", "MEDICAL-STANDBY"} <= {m["code"] for m in vehicles["measures"]}
    assert vehicles["confidence"] == 0.8  # default when the LLM gives none

    # The parser's scenes reached the LLM in order, with their own text.
    assert pipeline.llm.risk_scenes() == _FDX_SCENES
    assert "15-meter cliff" in pipeline.llm.risk_calls[0]["scene_text"]
    assert "fuel tanker ignites" in pipeline.llm.risk_calls[1]["scene_text"]
    assert pipeline.llm.structure_calls == []  # FDX needs no LLM structuring

    await _assert_pull_db_state(
        pipeline,
        job_id,
        outcome["report_id"],
        project_id=project_id,
        fmt="fdx",
        total=len(_FDX_EXPECTED),
    )
    assert (await _job_row(pipeline, job_id)).priority == 3


async def test_pull_report_not_fetched_within_ttl_is_dead_lettered(
    pipeline: Pipeline, auth_headers: dict[str, str]
) -> None:
    """AT4 for pull: nobody fetches the report -> after the 6h TTL it is deleted,
    the job fails, a webhook is sent and a content-free dead letter is written."""
    project_id = _project_id("pull-ttl")
    resp = await _submit_json(
        pipeline,
        auth_headers,
        fixture="fdx/stunt_heavy.fdx",
        script_format="fdx",
        project_id=project_id,
        delivery="pull",
    )
    assert resp.status_code == 202, resp.text
    job_id = resp.json()["job_id"]
    report_id = _started(pipeline, job_id).args[0]["report_id"]

    # Awaiting the result lets the time-skipping server run the 6h timer at once.
    result = await _workflow_result(pipeline, job_id)
    assert result["status"] == "delivery_failed"
    assert result["failure_reason"] == "pull_ttl_expired"
    assert result["delivery_mode"] == "pull"
    assert pipeline.progress.count("check_report_retrieved") == 1
    assert pipeline.progress.count("cleanup_buffer") == 1

    assert await _buffer_keys(pipeline.redis) == []
    assert await _residual_keys(pipeline.redis) == []
    assert pipeline.epro.deliveries() == []  # pull never pushes
    (webhook,) = pipeline.epro.webhooks()
    body = json.loads(webhook.body)
    assert body["job_id"] == job_id and body["report_id"] == report_id
    assert body["reason"] == "pull_ttl_expired"

    status = (await pipeline.http.get(f"/v1/security/jobs/{job_id}", headers=auth_headers)).json()
    assert status["status"] == "failed"
    assert status["error_message"] == "delivery_failed:pull_ttl_expired"
    assert status["metadata"]["delivery_status"] == "dead_lettered"

    (dead_letter,) = await _dead_letters(pipeline, job_id)
    assert dead_letter.reason == "pull_ttl_expired"
    assert dead_letter.delivery_mode == "pull"
    assert str(dead_letter.report_id) == report_id
    assert dead_letter.last_status_code is None
    assert dead_letter.webhook_sent is True
    (report_row,) = await _report_rows(pipeline, job_id)
    assert report_row.is_retrieved is False


@pytest.mark.xfail(
    strict=True,
    reason=(
        "BUG: _record_attempt (workflows/activities.py) sets delivered_at when a pull report "
        "merely becomes ready; it stays set even if the report is never fetched (dead letter)"
    ),
)
async def test_pull_report_is_not_marked_delivered_before_retrieval(
    pipeline: Pipeline, auth_headers: dict[str, str]
) -> None:
    resp = await _submit_json(
        pipeline,
        auth_headers,
        fixture="fdx/simple_scene.fdx",
        script_format="fdx",
        project_id=_project_id("pull-ready"),
        delivery="pull",
    )
    assert resp.status_code == 202, resp.text
    status, _ = await _poll_job_until(
        pipeline, auth_headers, resp.json()["status_url"], wanted={"completed", "failed"}
    )
    assert status["status"] == "completed"
    assert status["metadata"]["delivery_status"] == "pending"
    assert status["metadata"]["delivered_at"] is None


# ---------------------------------------------------------------------------
# b) Pull mode, PDF (JSON base64 and multipart upload)
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("transport", ["json", "multipart"])
async def test_pull_pdf_end_to_end(
    pipeline: Pipeline, auth_headers: dict[str, str], transport: str
) -> None:
    """PDF -> text -> scene split -> fake LLM structuring -> risk -> report -> one-shot GET."""
    project_id = _project_id(f"pull-pdf-{transport}")
    if transport == "json":
        resp = await _submit_json(
            pipeline,
            auth_headers,
            fixture="pdf/simple_screenplay.pdf",
            script_format="pdf",
            project_id=project_id,
            delivery="pull",
        )
    else:
        resp = await pipeline.http.post(
            "/v1/security/check:async",
            files={
                "file": (
                    "simple_screenplay.pdf",
                    (FIXTURES / "pdf" / "simple_screenplay.pdf").read_bytes(),
                    "application/pdf",
                )
            },
            data={"project_id": project_id, "delivery": "pull", "script_id": "4711"},
            headers=auth_headers,
        )

    outcome = await _run_pull_flow(pipeline, auth_headers, resp)
    _assert_report_matches(
        outcome["report"],
        report_id=outcome["report_id"],
        project_id=project_id,
        script_format="pdf",
        expected=_PDF_EXPECTED,
    )

    # Preamble + two INT/EXT blocks went through the LLM structurer ...
    assert pipeline.llm.preamble_calls == 1
    assert [b.splitlines()[0] for b in pipeline.llm.structure_calls] == [
        "INT. LIVING ROOM - DAY",
        "EXT. GARDEN - MORNING",
    ]
    # ... and the aggregated scenes (numbered 1..n) into the risk analysis.
    assert pipeline.llm.risk_scenes() == _PDF_SCENES
    assert "sunlight streaming through windows" in pipeline.llm.risk_calls[0]["scene_text"]
    assert "Birds sing in the trees." in pipeline.llm.risk_calls[1]["scene_text"]

    await _assert_pull_db_state(
        pipeline,
        outcome["job_id"],
        outcome["report_id"],
        project_id=project_id,
        fmt="pdf",
        total=len(_PDF_EXPECTED),
    )


# ---------------------------------------------------------------------------
# c) Push mode -> ePro
# ---------------------------------------------------------------------------


async def test_push_fdx_delivers_to_epro_and_deletes_content(
    pipeline: Pipeline, auth_headers: dict[str, str]
) -> None:
    """AT2: multipart POST to ePro with PDF + idempotency/correlation headers, then nothing left."""
    project_id = _project_id("push")
    request_id = f"e2e-req-{uuid.uuid4().hex}"
    resp = await _submit_json(
        pipeline,
        auth_headers,
        fixture="fdx/stunt_heavy.fdx",
        script_format="fdx",
        project_id=project_id,
        delivery="push",
        request_id=request_id,
    )
    assert resp.status_code == 202, resp.text
    assert resp.headers["X-Request-ID"] == request_id
    job_id = resp.json()["job_id"]
    report_id = _started(pipeline, job_id).args[0]["report_id"]

    result = await _workflow_result(pipeline, job_id)
    assert result == {
        "status": "completed",
        "report_id": report_id,
        "total_findings": len(_FDX_EXPECTED),
        "delivered": True,
        "delivery_mode": "push",
        "delivery_attempts": 1,
        "workflow_id": job_id,
    }

    (delivery,) = pipeline.epro.deliveries()
    assert pipeline.epro.webhooks() == []
    assert delivery.path == f"{_ASSESSMENT_PREFIX}{project_id}"
    assert delivery.headers["authorization"] == f"Bearer {_EPRO_TOKEN}"
    assert delivery.headers["idempotency-key"] == report_id
    assert delivery.headers["x-eki-job-id"] == job_id
    assert delivery.headers["x-eki-attempt"] == "1"
    # request_id travelled API -> Temporal header -> activity -> ePro.
    assert delivery.headers["x-request-id"] == request_id

    form = delivery.form()
    assert set(form) == {"script_id", "status", "assessment", "file"}
    assert delivery.text_field("script_id") == "4711"
    assert delivery.text_field("status") == "1"  # critical/high findings present
    assessment = delivery.text_field("assessment")
    assert f"Project {project_id}" in assessment
    assert "Total findings: 7 (2 CRITICAL, 2 HIGH, 1 MEDIUM, 1 LOW, 1 INFO)" in assessment
    assert f"eKI v{__version__}" in assessment
    pdf = form["file"]
    assert pdf["filename"] == f"safety-check-result_{project_id}.pdf"
    assert pdf["content_type"] == "application/pdf"
    assert pdf["data"].startswith(b"%PDF") and len(pdf["data"]) > 1000

    status = (await pipeline.http.get(f"/v1/security/jobs/{job_id}", headers=auth_headers)).json()
    assert status["status"] == "completed"
    assert status["progress_percentage"] == 100
    assert status["report_id"] == report_id
    assert status["metadata"]["delivery_mode"] == "push"
    assert status["metadata"]["delivery_status"] == "delivered"
    assert status["metadata"]["delivery_attempts"] == 1
    assert status["metadata"]["delivery_last_status_code"] == 201
    assert status["metadata"]["delivered_at"] is not None

    # Delete-on-delivery right after the 2xx.
    assert await _buffer_keys(pipeline.redis) == []
    assert await _residual_keys(pipeline.redis) == []

    (report_row,) = await _report_rows(pipeline, job_id)
    assert report_row.delivery_mode == "push"
    assert report_row.total_findings == len(_FDX_EXPECTED)
    assert report_row.is_retrieved is False
    assert await _dead_letters(pipeline, job_id) == []
    assert pipeline.progress.count("check_report_retrieved") == 0  # no pull-TTL watch

    # The script itself never entered Temporal history (only reference keys).
    history = await _history_bytes(pipeline, job_id)
    assert _fixture_b64("fdx/stunt_heavy.fdx")[:120].encode() not in history
    for text in _FDX_UNQUOTED_SCRIPT_TEXT:
        assert text.encode() not in history, text


# ---------------------------------------------------------------------------
# d) Push hard failure (ePro answers 422)
# ---------------------------------------------------------------------------


async def test_push_hard_4xx_dead_letters_without_content(
    pipeline: Pipeline, auth_headers: dict[str, str], admin_headers: dict[str, str]
) -> None:
    """AT4 part B: 422 -> no retry -> buffer cleaned, job failed, webhook, content-free dead letter."""
    pipeline.epro.assessment_status = 422
    project_id = _project_id("push-422")
    resp = await _submit_json(
        pipeline,
        auth_headers,
        fixture="fdx/stunt_heavy.fdx",
        script_format="fdx",
        project_id=project_id,
        delivery="push",
    )
    assert resp.status_code == 202, resp.text
    job_id = resp.json()["job_id"]
    report_id = _started(pipeline, job_id).args[0]["report_id"]

    result = await _workflow_result(pipeline, job_id)
    assert result["status"] == "delivery_failed"
    assert result["failure_reason"] == "hard_4xx"
    assert result["delivery_mode"] == "push"
    assert result["report_id"] == report_id

    assert len(pipeline.epro.deliveries()) == 1  # hard 4xx is not retried
    assert pipeline.progress.count("deliver_report") == 1
    (webhook,) = pipeline.epro.webhooks()
    assert json.loads(webhook.body) == {
        "job_id": job_id,
        "report_id": report_id,
        "reason": "hard_4xx",
        "attempts": 1,
    }
    assert webhook.headers["authorization"] == f"Bearer {_EPRO_TOKEN}"

    status = (await pipeline.http.get(f"/v1/security/jobs/{job_id}", headers=auth_headers)).json()
    assert status["status"] == "failed"
    assert status["error_message"] == "delivery_failed:hard_4xx"
    assert status["metadata"]["delivery_status"] == "dead_lettered"
    assert status["metadata"]["delivery_attempts"] == 1
    assert status["metadata"]["delivery_last_status_code"] == 422
    assert status["metadata"]["delivered_at"] is None

    # Failure branch removed the report from the buffer; nothing else is left.
    assert await _buffer_keys(pipeline.redis) == []
    assert await _residual_keys(pipeline.redis) == []

    listing = await pipeline.http.get(
        "/v1/ops/dead-letters", params={"acknowledged": "false"}, headers=admin_headers
    )
    assert listing.status_code == 200, listing.text
    (dead_letter,) = [d for d in listing.json()["items"] if d["job_id"] == job_id]
    assert dead_letter["report_id"] == report_id
    assert dead_letter["project_id"] == project_id
    assert dead_letter["user_id"] == _USER_ID
    assert dead_letter["delivery_mode"] == "push"
    assert dead_letter["reason"] == "hard_4xx"
    assert dead_letter["attempts"] == 1
    assert dead_letter["last_status_code"] == 422
    assert dead_letter["webhook_sent"] is True
    assert dead_letter["acknowledged_at"] is None

    # Privacy: the persisted dead letter holds metadata only.
    (row,) = await _dead_letters(pipeline, job_id)
    assert row.last_error_type is None and row.note is None
    stored = " ".join(str(getattr(row, c.key)) for c in DeliveryDeadLetter.__table__.columns)
    forbidden = [
        *_FDX_UNQUOTED_SCRIPT_TEXT,
        _STUNT_EVIDENCE,
        "E2E:",
        "CLIFF EDGE",
        "Total findings",
        "eki:buf:",
    ]
    for text in forbidden:
        assert text not in stored, text


# ---------------------------------------------------------------------------
# e) Idempotent resubmission
# ---------------------------------------------------------------------------


async def test_idempotent_resubmission_starts_one_workflow_and_delivers_once(
    pipeline: Pipeline, auth_headers: dict[str, str]
) -> None:
    """AT5: same Idempotency-Key -> same job_id, one workflow, exactly one ePro delivery."""
    project_id = _project_id("idem")
    key = f"e2e-idem-{uuid.uuid4().hex}"
    first = await _submit_json(
        pipeline,
        auth_headers,
        fixture="fdx/stunt_heavy.fdx",
        script_format="fdx",
        project_id=project_id,
        delivery="push",
        idempotency_key=key,
    )
    second = await _submit_json(
        pipeline,
        auth_headers,
        fixture="fdx/stunt_heavy.fdx",
        script_format="fdx",
        project_id=project_id,
        delivery="push",
        idempotency_key=key,
    )
    assert first.status_code == 202 and second.status_code == 202, (first.text, second.text)
    job_id = first.json()["job_id"]
    assert second.json()["job_id"] == job_id
    assert "idempotency" in second.json()["message"].lower()
    assert len(pipeline.starts) == 1  # no second workflow

    result = await _workflow_result(pipeline, job_id)
    assert result["status"] == "completed" and result["delivered"] is True

    # Re-submitting after completion still maps to the same job.
    third = await _submit_json(
        pipeline,
        auth_headers,
        fixture="fdx/stunt_heavy.fdx",
        script_format="fdx",
        project_id=project_id,
        delivery="push",
        idempotency_key=key,
    )
    assert third.status_code == 202
    assert third.json()["job_id"] == job_id
    assert third.json()["status"] == "completed"
    assert len(pipeline.starts) == 1

    (delivery,) = pipeline.epro.deliveries()
    assert delivery.headers["idempotency-key"] == result["report_id"]
    # Nothing left: in particular the duplicates never parked their script in
    # the buffer (no workflow would ever have deleted it).
    assert await _buffer_keys(pipeline.redis) == []
    async with pipeline.db() as session:
        rows = (
            await session.execute(
                select(func.count())
                .select_from(JobMetadata)
                .where(JobMetadata.user_id == _USER_ID, JobMetadata.idempotency_key == key)
            )
        ).scalar_one()
    assert rows == 1


# ---------------------------------------------------------------------------
# f) Synchronous endpoint (no Temporal)
# ---------------------------------------------------------------------------


async def test_sync_check_contract_without_temporal(
    sync_api: AsyncClient, auth_headers: dict[str, str], redis_db: aioredis.Redis, db: Any
) -> None:
    """The sync endpoint answers in-request, needs no workflow engine and leaves nothing behind."""
    project_id = _project_id("sync")
    body = {
        "script_content": _fixture_b64("fdx/stunt_heavy.fdx"),
        "script_format": "fdx",
        "project_id": project_id,
    }
    unauthenticated = await sync_api.post("/v1/security/check", json=body)
    assert unauthenticated.status_code in (401, 403)

    resp = await sync_api.post("/v1/security/check", json=body, headers=auth_headers)
    assert resp.status_code == 200, resp.text
    parsed = SyncSecurityCheckResponse.model_validate(resp.json())
    report = parsed.report
    assert report.project_id == project_id
    assert report.script_format.value == "fdx"
    assert report.total_findings == len(report.findings)
    assert sum(report.risk_summary.values()) == report.total_findings

    invalid = await sync_api.post(
        "/v1/security/check",
        json={**body, "script_content": "%%% not base64 %%%"},
        headers=auth_headers,
    )
    assert invalid.status_code == 422

    assert await _residual_keys(redis_db) == []
    async with db() as session:
        jobs = (
            await session.execute(
                select(func.count())
                .select_from(JobMetadata)
                .where(JobMetadata.project_id == project_id)
            )
        ).scalar_one()
    assert jobs == 0


@pytest.mark.xfail(
    strict=True,
    reason=(
        "BUG: POST /v1/security/check returns a hard-coded M01 stub report "
        "(api/routers/security.py security_check_sync) instead of analysing the script"
    ),
)
async def test_sync_check_returns_the_real_analysis(
    sync_api: AsyncClient, auth_headers: dict[str, str], fake_llm: FakeLLMProvider
) -> None:
    project_id = _project_id("sync-real")
    resp = await sync_api.post(
        "/v1/security/check",
        json={
            "script_content": _fixture_b64("fdx/stunt_heavy.fdx"),
            "script_format": "fdx",
            "project_id": project_id,
        },
        headers=auth_headers,
    )
    assert resp.status_code == 200, resp.text
    report = resp.json()["report"]
    assert not report["metadata"].get("stub"), "sync endpoint answered with stub data"
    assert fake_llm.risk_scenes() == _FDX_SCENES
    assert _signature(report["findings"]) == _FDX_EXPECTED
    assert report["risk_summary"] == _FDX_RISK_SUMMARY


# ---------------------------------------------------------------------------
# g) Malformed input -> processing failure
# ---------------------------------------------------------------------------

_MALFORMED_SCRIPT_TEXT = ["This XML is malformed.", "INT. ROOM - DAY"]


async def _run_malformed(p: Pipeline, headers: dict[str, str]) -> tuple[str, dict[str, Any]]:
    resp = await _submit_json(
        p,
        headers,
        fixture="fdx/malformed.fdx",
        script_format="fdx",
        project_id=_project_id("malformed"),
        delivery="pull",
    )
    assert resp.status_code == 202, resp.text  # syntactically valid request
    job_id = resp.json()["job_id"]
    return job_id, await _workflow_result(p, job_id)


async def test_malformed_fdx_fails_job_with_sanitized_error(
    pipeline: Pipeline, auth_headers: dict[str, str]
) -> None:
    job_id, result = await _run_malformed(pipeline, auth_headers)
    assert result["status"] == "failed"
    assert result["workflow_id"] == job_id
    assert pipeline.progress.count("parse_fdx") >= 1
    assert pipeline.progress.count("analyze_scene_risk") == 0
    assert pipeline.progress.count("deliver_report") == 0
    assert pipeline.llm.risk_calls == []

    status = (await pipeline.http.get(f"/v1/security/jobs/{job_id}", headers=auth_headers)).json()
    assert status["status"] == "failed"
    assert status["report_id"] is None
    error = status["error_message"]
    assert error, "failed job without error_message"
    assert len(error) <= 1000
    for text in [*_MALFORMED_SCRIPT_TEXT, "eki:buf:", "Traceback"]:
        assert text not in error, text
    assert status["metadata"]["delivery_status"] == "pending"
    assert status["metadata"]["delivery_attempts"] == 0

    assert await _report_rows(pipeline, job_id) == []
    assert await _dead_letters(pipeline, job_id) == []  # not a delivery failure
    history = await _history_bytes(pipeline, job_id)
    for text in _MALFORMED_SCRIPT_TEXT:
        assert text.encode() not in history, text


@pytest.mark.xfail(
    strict=True,
    reason=(
        "BUG: on a processing failure the uploaded script stays encrypted in the "
        "SecureBuffer until its 6h TTL (no cleanup in SecurityCheckWorkflow.run's except branch)"
    ),
)
async def test_malformed_fdx_leaves_no_content_in_buffer(
    pipeline: Pipeline, auth_headers: dict[str, str]
) -> None:
    _, result = await _run_malformed(pipeline, auth_headers)
    assert result["status"] == "failed"
    assert await _buffer_keys(pipeline.redis) == []


# ---------------------------------------------------------------------------
# Further defects found by the end-to-end run
# ---------------------------------------------------------------------------


@pytest.mark.xfail(
    strict=True,
    reason=(
        "BUG: analyze_scene_risk_activity returns the findings (descriptions and evidence "
        "quotes from the script) as plain activity results, so they are persisted in "
        "Temporal history instead of travelling as SecureBuffer reference keys"
    ),
)
async def test_findings_do_not_travel_through_temporal_history(
    pipeline: Pipeline, auth_headers: dict[str, str]
) -> None:
    resp = await _submit_json(
        pipeline,
        auth_headers,
        fixture="fdx/stunt_heavy.fdx",
        script_format="fdx",
        project_id=_project_id("history"),
        delivery="push",
    )
    job_id = resp.json()["job_id"]
    result = await _workflow_result(pipeline, job_id)
    assert result["status"] == "completed"
    history = await _history_bytes(pipeline, job_id)
    assert _STUNT_EVIDENCE.encode() not in history
    assert b"E2E: cliff jump of a stunt performer" not in history


@pytest.mark.xfail(
    strict=True,
    reason=(
        "BUG: GET /v1/security/reports/{id} answers 200 with an empty placeholder report "
        "(0 findings) when the buffered report is gone, e.g. after push delivery"
    ),
)
async def test_report_get_after_push_delivery_does_not_fabricate_an_empty_report(
    pipeline: Pipeline, auth_headers: dict[str, str]
) -> None:
    resp = await _submit_json(
        pipeline,
        auth_headers,
        fixture="fdx/stunt_heavy.fdx",
        script_format="fdx",
        project_id=_project_id("push-get"),
        delivery="push",
    )
    job_id = resp.json()["job_id"]
    result = await _workflow_result(pipeline, job_id)
    assert result["status"] == "completed" and result["delivered"] is True

    got = await pipeline.http.get(
        f"/v1/security/reports/{result['report_id']}", headers=auth_headers
    )
    assert got.status_code in (404, 410), (
        f"{got.status_code}: total_findings={got.json().get('report', {}).get('total_findings')}"
    )


@pytest.mark.xfail(
    strict=True,
    reason=(
        "BUG: 'delivery' is not validated against the OpenAPI enum [pull, push]; e.g. 'PUSH' "
        "is accepted, never pushed and skips the pull-TTL watch (no dead letter/webhook)"
    ),
)
async def test_unknown_delivery_mode_is_rejected(
    pipeline: Pipeline, auth_headers: dict[str, str]
) -> None:
    resp = await _submit_json(
        pipeline,
        auth_headers,
        fixture="fdx/simple_scene.fdx",
        script_format="fdx",
        project_id=_project_id("delivery-enum"),
        delivery="PUSH",
    )
    assert resp.status_code == 422, resp.text
    assert pipeline.starts == []


@pytest.mark.xfail(
    strict=True,
    reason=(
        "BUG: multipart project_id is not validated (JSON enforces ^[a-zA-Z0-9_-]+$) and is "
        "interpolated into the ePro push URL path"
    ),
)
async def test_multipart_project_id_cannot_steer_the_epro_push_url(
    pipeline: Pipeline, auth_headers: dict[str, str]
) -> None:
    resp = await pipeline.http.post(
        "/v1/security/check:async",
        files={
            "file": ("scene.fdx", (FIXTURES / "fdx" / "simple_scene.fdx").read_bytes(), "text/xml")
        },
        data={"project_id": "75/../../../admin/hijack", "delivery": "push"},
        headers=auth_headers,
    )
    assert resp.status_code == 422, resp.text
    assert pipeline.starts == []
