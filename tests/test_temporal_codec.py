"""Decision 10a -- Temporal payload encryption (``core/temporal_codec.py``).

Unit tests for the AES-256-GCM codec (round trip, plaintext pass-through,
wrong key, tampering, key id, encoded failure attributes), the wiring of the
production clients, and -- on the Temporal time-skipping test server -- the
property the codec exists for: the *stored* workflow history contains none of
the plaintext that went through workflow input, activity results, headers or
failure messages, while workflows keep working and replaying. Histories
written before 1.0.1 (plaintext) must keep replaying with the new converter,
including a workflow that was in flight across the deploy.
"""

from __future__ import annotations

import ast
import asyncio
import uuid
from collections.abc import Iterator
from datetime import timedelta
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock, patch

import pytest
from google.protobuf.message import Message
from temporalio import activity, workflow
from temporalio.api.common.v1 import Payload
from temporalio.api.enums.v1 import EventType
from temporalio.api.failure.v1 import Failure
from temporalio.client import Client, WorkflowFailureError, WorkflowHistory
from temporalio.common import HeaderCodecBehavior, RetryPolicy
from temporalio.converter import DataConverter, DefaultFailureConverterWithEncodedAttributes
from temporalio.exceptions import ActivityError, ApplicationError
from temporalio.testing import WorkflowEnvironment
from temporalio.worker import Replayer, Worker

# This module defines workflows and is therefore re-imported inside the
# workflow sandbox: project modules are passed through.
with workflow.unsafe.imports_passed_through():
    from core.logging_config import get_request_id, set_request_id
    from core.temporal_codec import (
        CODEC_KEY_LABEL,
        ENCRYPTED_ENCODING,
        HEADER_CODEC_BEHAVIOR,
        METADATA_KEY_ID,
        EncryptionCodec,
        PayloadDecryptionError,
        build_data_converter,
        codec_key_id,
        derive_codec_key,
        temporal_client_options,
    )
    from core.temporal_context import ContextPropagationInterceptor
    from workflows.security_check import SecurityCheckWorkflow

SECRET = "codec-test-api-secret-key-0123456789abcdef"  # nosec B105 - test key
OTHER_SECRET = "a-different-api-secret-key-fedcba9876543210"  # nosec B105 - test key

# Plaintext markers: each travels through one kind of payload.
INPUT_MARKER = "Titel: Das vertrauliche Drehbuch 7f3a"
RESULT_MARKER = "activity-result-marker-c41d"
FAILURE_MARKER = "activity-failure-message-9b2e"
DETAIL_MARKER = "activity-failure-detail-5e8a"
HEADER_MARKER = "request-id-header-marker-2d6c"
WF_FAILURE_MARKER = "workflow-failure-message-0aa1"
ALL_MARKERS = [INPUT_MARKER, RESULT_MARKER, FAILURE_MARKER, DETAIL_MARKER, HEADER_MARKER]


def _payload(value: str, encoding: bytes = b"json/plain") -> Payload:
    return Payload(metadata={"encoding": encoding}, data=value.encode("utf-8"))


def _payloads_in(message: Message) -> Iterator[Payload]:
    """Every ``Payload`` nested anywhere in *message* (repeated and map fields included)."""
    for field, value in message.ListFields():
        if field.type != field.TYPE_MESSAGE:
            continue
        if isinstance(value, Message):
            items = [value]
        elif hasattr(value, "keys"):  # map field
            items = list(value.values())
        else:  # repeated field
            items = list(value)
        for item in items:
            if isinstance(item, Payload):
                yield item
            elif isinstance(item, Message):
                yield from _payloads_in(item)


def _history_bytes(history: WorkflowHistory) -> bytes:
    return b"".join(event.SerializeToString() for event in history.events)


def _history_payloads(history: WorkflowHistory) -> list[Payload]:
    return [p for event in history.events for p in _payloads_in(event)]


def _events(history: WorkflowHistory, event_type: int) -> list[Any]:
    return [e for e in history.events if e.event_type == event_type]


# ===================================================================
# Key derivation and key id
# ===================================================================


class TestKeyDerivation:
    def test_key_is_deterministic_256_bit_and_secret_dependent(self):
        key = derive_codec_key(SECRET)
        assert len(key) == 32
        assert derive_codec_key(SECRET) == key
        assert derive_codec_key(OTHER_SECRET) != key

    def test_key_is_independent_of_the_buffer_and_kb_keys(self):
        """Own label: the codec key is neither the SecureBuffer nor the KB Fernet key."""
        import base64

        from services.knowledge_base import _derive_kb_fernet_key
        from services.secure_buffer import _derive_fernet_key

        key = derive_codec_key(SECRET)
        assert key != base64.urlsafe_b64decode(_derive_fernet_key(SECRET))
        assert key != base64.urlsafe_b64decode(_derive_kb_fernet_key(SECRET))
        assert CODEC_KEY_LABEL == b"eki-temporal-codec-v1"

    async def test_known_answer_keeps_the_format_stable_across_releases(self):
        """Payloads written by 1.0.1 must decode in every later release.

        Key = HKDF-SHA256(ikm=secret, salt=None, info="eki-temporal-codec-v1")
        (RFC 5869, checked by hand with hmac), key id = HMAC-SHA256(key,
        "eki-temporal-codec-key-id")[:16 hex]; the ciphertext was produced once by
        1.0.1. Changing label, KDF, AAD or layout breaks this test -- and with it
        the decoding of every open workflow.
        """
        secret = "golden-api-secret-key-for-format-stability"  # nosec B105 - test vector
        assert derive_codec_key(secret).hex() == (
            "b58ca9cf4e5c04341aa6b7282f8f99c45925a617725da7a68ab820736bc1364a"
        )
        codec = EncryptionCodec(secret)
        assert codec.key_id == "eki-v1-e5700ee5b7771486"
        golden = Payload(
            metadata={"encoding": b"binary/encrypted", METADATA_KEY_ID: b"eki-v1-e5700ee5b7771486"},
            data=bytes.fromhex(
                "adf26e9e564415d95f20aa26813a6a7f49794be63b1110652706488dd5f788274f66a473"
                "42bb33a29bbdfc71fb183bdad1a8505b4501ed3985d9fe1f366133f3d4e8689a5790cf10"
                "3f455757a5"
            ),
        )
        assert await codec.decode([golden]) == [_payload('{"project_id":"golden"}')]

    def test_empty_secret_is_rejected(self):
        with pytest.raises(ValueError, match="API_SECRET_KEY is empty"):
            derive_codec_key("")

    def test_key_id_is_a_stable_fingerprint_without_key_material(self):
        key = derive_codec_key(SECRET)
        key_id = codec_key_id(key)
        assert key_id == codec_key_id(derive_codec_key(SECRET))
        assert key_id != codec_key_id(derive_codec_key(OTHER_SECRET))
        assert key_id.startswith("eki-v1-") and len(key_id) == len("eki-v1-") + 16
        int(key_id.removeprefix("eki-v1-"), 16)  # hex
        assert key.hex()[:16] not in key_id
        assert SECRET not in key_id

    def test_repr_shows_the_key_id_only(self):
        codec = EncryptionCodec(SECRET)
        assert repr(codec) == f"EncryptionCodec(key_id={codec.key_id!r})"
        assert SECRET not in repr(codec)
        assert derive_codec_key(SECRET).hex() not in repr(codec)


# ===================================================================
# Codec: encode / decode
# ===================================================================


class TestEncryptionCodec:
    async def test_round_trip_restores_metadata_and_data(self):
        codec = EncryptionCodec(SECRET)
        originals = [
            _payload('{"title": "' + INPUT_MARKER + '"}'),
            Payload(metadata={"encoding": b"binary/null"}),
            Payload(
                metadata={"encoding": b"binary/plain", "messageType": b"x"}, data=bytes(range(256))
            ),
            _payload('"Umlaute äöü ß – ✓"'),
        ]

        encoded = await codec.encode(originals)
        decoded = await codec.decode(encoded)

        assert decoded == originals
        assert len(encoded) == len(originals)
        for payload in encoded:
            assert dict(payload.metadata) == {
                "encoding": ENCRYPTED_ENCODING,
                METADATA_KEY_ID: codec.key_id.encode(),
            }
        assert INPUT_MARKER.encode() not in encoded[0].SerializeToString()

    async def test_each_encryption_uses_a_fresh_nonce(self):
        codec = EncryptionCodec(SECRET)
        first, second = await codec.encode([_payload("same"), _payload("same")])
        assert first.data != second.data
        assert first.data[:12] != second.data[:12]

    async def test_plaintext_payloads_pass_through_unchanged(self):
        """Histories from before 1.0.1 are plaintext and must still decode."""
        codec = EncryptionCodec(SECRET)
        legacy = [_payload('"legacy"'), Payload(), Payload(metadata={"encoding": b"binary/null"})]
        assert await codec.decode(legacy) == legacy

    async def test_mixed_sequence_keeps_order(self):
        codec = EncryptionCodec(SECRET)
        (sealed,) = await codec.encode([_payload('"new"')])
        legacy = _payload('"old"')
        assert await codec.decode([legacy, sealed, legacy]) == [
            legacy,
            _payload('"new"'),
            legacy,
        ]

    async def test_already_encrypted_payload_is_not_wrapped_twice(self):
        codec = EncryptionCodec(SECRET)
        (sealed,) = await codec.encode([_payload('"once"')])
        (again,) = await codec.encode([sealed])
        assert again == sealed
        assert await codec.decode([again]) == [_payload('"once"')]

    async def test_wrong_key_fails_loudly_without_content(self):
        (sealed,) = await EncryptionCodec(SECRET).encode([_payload(INPUT_MARKER)])
        other = EncryptionCodec(OTHER_SECRET)

        with pytest.raises(PayloadDecryptionError) as exc_info:
            await other.decode([sealed])

        message = str(exc_info.value)
        assert EncryptionCodec(SECRET).key_id in message
        assert other.key_id in message
        assert "API_SECRET_KEY" in message
        assert INPUT_MARKER not in message
        assert SECRET not in message and OTHER_SECRET not in message

    async def test_forged_key_id_with_wrong_key_fails_authentication(self):
        """Even with a matching key id, a ciphertext from another key does not decrypt."""
        (sealed,) = await EncryptionCodec(SECRET).encode([_payload(INPUT_MARKER)])
        other = EncryptionCodec(OTHER_SECRET)
        sealed.metadata[METADATA_KEY_ID] = other.key_id.encode()

        with pytest.raises(PayloadDecryptionError, match="failed authentication"):
            await other.decode([sealed])

    async def test_tampered_ciphertext_fails_authentication(self):
        codec = EncryptionCodec(SECRET)
        (sealed,) = await codec.encode([_payload(INPUT_MARKER)])
        data = bytearray(sealed.data)
        data[20] ^= 0x01
        sealed.data = bytes(data)

        with pytest.raises(PayloadDecryptionError, match="failed authentication") as exc_info:
            await codec.decode([sealed])
        assert exc_info.value.__cause__ is None  # no chained crypto internals
        assert INPUT_MARKER not in str(exc_info.value)

    async def test_missing_key_id_is_rejected(self):
        codec = EncryptionCodec(SECRET)
        (sealed,) = await codec.encode([_payload('"x"')])
        del sealed.metadata[METADATA_KEY_ID]

        with pytest.raises(PayloadDecryptionError, match="key id ''"):
            await codec.decode([sealed])

    async def test_reported_foreign_key_id_is_truncated(self):
        codec = EncryptionCodec(SECRET)
        (sealed,) = await codec.encode([_payload('"x"')])
        sealed.metadata[METADATA_KEY_ID] = b"k" * 500

        with pytest.raises(PayloadDecryptionError) as exc_info:
            await codec.decode([sealed])
        assert "k" * 64 in str(exc_info.value)
        assert "k" * 65 not in str(exc_info.value)

    async def test_truncated_ciphertext_is_rejected(self):
        codec = EncryptionCodec(SECRET)
        (sealed,) = await codec.encode([_payload('"x"')])
        sealed.data = sealed.data[:27]  # shorter than nonce + tag

        with pytest.raises(PayloadDecryptionError, match="truncated"):
            await codec.decode([sealed])

    async def test_authentic_ciphertext_that_is_no_payload_is_rejected(self):
        codec = EncryptionCodec(SECRET)
        nonce = b"\x00" * 12
        sealed = Payload(
            metadata={"encoding": ENCRYPTED_ENCODING, METADATA_KEY_ID: codec.key_id.encode()},
            data=nonce + codec._aesgcm.encrypt(nonce, b"\xff\xff\xff", codec._aad),
        )

        with pytest.raises(PayloadDecryptionError, match="not a valid Payload"):
            await codec.decode([sealed])


# ===================================================================
# DataConverter: values and failures
# ===================================================================


class TestDataConverter:
    async def test_values_round_trip_and_are_sealed(self):
        converter = build_data_converter(SECRET)
        value = {"project_id": "75", "metadata": {"title": INPUT_MARKER}}

        payloads = await converter.encode([value])

        assert all(EncryptionCodec.is_encrypted(p) for p in payloads)
        assert INPUT_MARKER.encode() not in b"".join(p.SerializeToString() for p in payloads)
        assert await converter.decode(payloads, [dict]) == [value]

    async def test_failure_message_stack_trace_and_details_are_encrypted(self):
        converter = build_data_converter(SECRET)
        try:
            try:
                raise ValueError(f"inner {DETAIL_MARKER}")
            except ValueError as inner:
                raise ApplicationError(
                    FAILURE_MARKER, {"detail": DETAIL_MARKER}, type="ProbeFailure"
                ) from inner
        except ApplicationError as err:
            failure = Failure()
            await converter.encode_failure(err, failure)

        raw = failure.SerializeToString()
        for marker in (FAILURE_MARKER, DETAIL_MARKER, "test_temporal_codec.py"):
            assert marker.encode() not in raw, marker
        assert failure.message == "Encoded failure"
        assert failure.stack_trace == ""
        assert failure.application_failure_info.type == "ProbeFailure"  # type stays readable
        assert EncryptionCodec.is_encrypted(failure.encoded_attributes)
        assert failure.cause.message == "Encoded failure"  # the cause chain as well
        assert all(EncryptionCodec.is_encrypted(p) for p in _payloads_in(failure))

        decoded = await converter.decode_failure(failure)
        assert isinstance(decoded, ApplicationError)
        assert decoded.message == FAILURE_MARKER
        assert decoded.type == "ProbeFailure"
        assert decoded.details == ({"detail": DETAIL_MARKER},)
        assert "test_temporal_codec.py" in decoded.failure.stack_trace
        assert isinstance(decoded.__cause__, ApplicationError)
        assert decoded.__cause__.message == f"inner {DETAIL_MARKER}"

    def test_build_data_converter_wiring(self):
        converter = build_data_converter(SECRET)
        assert isinstance(converter, DataConverter)
        assert isinstance(converter.payload_codec, EncryptionCodec)
        assert converter.payload_codec.key_id == codec_key_id(derive_codec_key(SECRET))
        assert converter.failure_converter_class is DefaultFailureConverterWithEncodedAttributes
        assert converter.failure_converter._encode_common_attributes is True

    def test_client_options_also_encode_headers(self):
        options = temporal_client_options(SECRET)
        assert set(options) == {"data_converter", "header_codec_behavior"}
        assert options["header_codec_behavior"] is HeaderCodecBehavior.CODEC
        assert HEADER_CODEC_BEHAVIOR is HeaderCodecBehavior.CODEC
        assert isinstance(options["data_converter"].payload_codec, EncryptionCodec)


# ===================================================================
# Production wiring
# ===================================================================


def _assert_production_options(kwargs: dict[str, Any], secret: str) -> None:
    converter = kwargs["data_converter"]
    assert isinstance(converter.payload_codec, EncryptionCodec)
    assert converter.payload_codec.key_id == codec_key_id(derive_codec_key(secret))
    assert converter.failure_converter_class is DefaultFailureConverterWithEncodedAttributes
    assert kwargs["header_codec_behavior"] is HeaderCodecBehavior.CODEC


class TestProductionWiring:
    async def test_api_client_uses_the_codec(self):
        from api import dependencies
        from api.config import get_settings

        connect = AsyncMock(return_value=object())
        with patch.object(dependencies.TemporalClient, "connect", new=connect):
            gen = dependencies.get_temporal_client()
            await gen.__anext__()
            await gen.aclose()

        _assert_production_options(connect.await_args.kwargs, get_settings().api_secret_key)

    async def test_readiness_probe_client_uses_the_codec(self):
        from fastapi import Response

        from api.config import get_settings
        from api.routers import health

        check_health = AsyncMock(return_value=True)
        connect = AsyncMock(
            return_value=SimpleNamespace(service_client=SimpleNamespace(check_health=check_health))
        )
        response = Response()
        with patch.object(health.TemporalClient, "connect", new=connect):
            body = await health.readiness_check(
                response,
                db=SimpleNamespace(execute=AsyncMock()),
                redis=SimpleNamespace(ping=AsyncMock()),
                _api_key=SimpleNamespace(),
            )

        assert body.services == {"database": True, "redis": True, "temporal": True}
        check_health.assert_awaited_once()
        _assert_production_options(connect.await_args.kwargs, get_settings().api_secret_key)

    async def test_readiness_reports_temporal_down_when_the_codec_key_is_unusable(
        self, monkeypatch
    ):
        from fastapi import Response

        from api.config import get_settings
        from api.routers import health

        monkeypatch.setattr(get_settings(), "api_secret_key", "")
        connect = AsyncMock()
        response = Response()
        with patch.object(health.TemporalClient, "connect", new=connect):
            body = await health.readiness_check(
                response,
                db=SimpleNamespace(execute=AsyncMock()),
                redis=SimpleNamespace(ping=AsyncMock()),
                _api_key=SimpleNamespace(),
            )

        assert body.services["temporal"] is False
        assert response.status_code == 503
        connect.assert_not_awaited()

    def test_every_temporal_client_in_production_code_uses_the_codec(self):
        """AST guard: a new ``Client.connect``/``Client(...)`` without the codec fails here."""
        repo_root = Path(__file__).resolve().parent.parent
        sites: dict[str, bool] = {}
        for directory in (
            "api",
            "core",
            "db",
            "llm",
            "parsers",
            "scripts",
            "services",
            "worker",
            "workflows",
        ):
            for path in sorted((repo_root / directory).rglob("*.py")):
                tree = ast.parse(path.read_text(encoding="utf-8"))
                for node in ast.walk(tree):
                    if not isinstance(node, ast.Call):
                        continue
                    func = node.func
                    is_connect = (
                        isinstance(func, ast.Attribute)
                        and func.attr == "connect"
                        and isinstance(func.value, ast.Name)
                        and func.value.id in {"Client", "TemporalClient"}
                    )
                    is_ctor = isinstance(func, ast.Name) and func.id in {
                        "Client",
                        "TemporalClient",
                    }
                    if not (is_connect or is_ctor):
                        continue
                    uses_codec = any(
                        kw.arg is None
                        and isinstance(kw.value, ast.Call)
                        and isinstance(kw.value.func, ast.Name)
                        and kw.value.func.id == "temporal_client_options"
                        for kw in node.keywords
                    )
                    sites[f"{path.relative_to(repo_root)}:{node.lineno}"] = uses_codec
        files = {site.split(":")[0] for site in sites}
        assert {"api/dependencies.py", "api/routers/health.py", "worker/main.py"} <= files
        assert all(sites.values()), [site for site, ok in sites.items() if not ok]


# ===================================================================
# Temporal test server: stored history, replay, migration
# ===================================================================


@activity.defn(name="codec_probe_echo")
async def echo_activity(job: dict) -> dict:
    return {"echo": job["title"], "result": RESULT_MARKER, "request_id": get_request_id()}


@activity.defn(name="codec_probe_fail")
async def failing_activity(title: str) -> str:
    raise ApplicationError(
        f"{FAILURE_MARKER}: {title}", DETAIL_MARKER, type="ProbeFailure", non_retryable=True
    )


@workflow.defn(name="CodecProbeWorkflow")
class CodecProbeWorkflow:
    @workflow.run
    async def run(self, job: dict) -> dict:
        echoed = await workflow.execute_activity(
            echo_activity, job, start_to_close_timeout=timedelta(seconds=30)
        )
        caught: dict[str, Any] = {}
        try:
            await workflow.execute_activity(
                failing_activity,
                job["title"],
                start_to_close_timeout=timedelta(seconds=30),
                retry_policy=RetryPolicy(maximum_attempts=1),
            )
        except ActivityError as err:
            cause = err.cause
            assert isinstance(cause, ApplicationError)
            caught = {"message": cause.message, "details": list(cause.details), "type": cause.type}
        if job.get("fail_workflow"):
            raise ApplicationError(WF_FAILURE_MARKER, type="ProbeWorkflowFailure")
        return {"echoed": echoed, "caught": caught}


def _client(env: WorkflowEnvironment, options: dict[str, Any]) -> Client:
    """Clone the test-server client (keeps its time-skipping interceptors)."""
    config = env.client.config()
    return Client(
        **{
            **config,
            "interceptors": [*config["interceptors"], ContextPropagationInterceptor()],
            **options,
        }
    )


async def _run_probe(
    env: WorkflowEnvironment, options: dict[str, Any], *, fail_workflow: bool = False
) -> tuple[dict | None, WorkflowFailureError | None, WorkflowHistory]:
    client = _client(env, options)
    queue = f"codec-{uuid.uuid4().hex}"
    set_request_id(HEADER_MARKER)
    try:
        async with Worker(
            client,
            task_queue=queue,
            workflows=[CodecProbeWorkflow],
            activities=[echo_activity, failing_activity],
        ):
            handle = await client.start_workflow(
                CodecProbeWorkflow.run,
                {"title": INPUT_MARKER, "fail_workflow": fail_workflow},
                id=f"codec-{uuid.uuid4().hex}",
                task_queue=queue,
            )
            result: dict | None = None
            error: WorkflowFailureError | None = None
            try:
                result = await handle.result()
            except WorkflowFailureError as err:
                error = err
            history = await handle.fetch_history()
    finally:
        set_request_id(None)
    return result, error, history


def _probe_replayer(secret: str) -> Replayer:
    return Replayer(
        workflows=[CodecProbeWorkflow],
        interceptors=[ContextPropagationInterceptor()],
        **temporal_client_options(secret),
    )


@pytest.mark.temporal
class TestStoredHistory:
    async def test_history_holds_no_plaintext_and_the_workflow_still_works(self, caplog):
        async with await WorkflowEnvironment.start_time_skipping() as env:
            result, error, history = await _run_probe(env, temporal_client_options(SECRET))

        # The workflow saw every value decrypted: input, activity result,
        # propagated header and the failure message/details of the activity.
        assert error is None
        assert result == {
            "echoed": {"echo": INPUT_MARKER, "result": RESULT_MARKER, "request_id": HEADER_MARKER},
            "caught": {
                "message": f"{FAILURE_MARKER}: {INPUT_MARKER}",
                "details": [DETAIL_MARKER],
                "type": "ProbeFailure",
            },
        }

        raw = _history_bytes(history)
        for marker in ALL_MARKERS:
            assert marker.encode() not in raw, marker
        payloads = _history_payloads(history)
        assert len(payloads) >= 6  # input, header, 2 results, failure attributes, details
        assert all(EncryptionCodec.is_encrypted(p) for p in payloads), [
            dict(p.metadata) for p in payloads if not EncryptionCodec.is_encrypted(p)
        ]
        key_id = EncryptionCodec(SECRET).key_id.encode()
        assert {p.metadata[METADATA_KEY_ID] for p in payloads} == {key_id}
        (failed,) = _events(history, EventType.EVENT_TYPE_ACTIVITY_TASK_FAILED)
        failure = failed.activity_task_failed_event_attributes.failure
        assert failure.message == "Encoded failure"
        assert failure.stack_trace == ""
        assert failure.application_failure_info.type == "ProbeFailure"

        # The encrypted history replays with the production converter ...
        await _probe_replayer(SECRET).replay_workflow(history)
        # ... but not with another API_SECRET_KEY: the workflow task fails (and
        # is retried by a real worker) and the worker log names both key ids.
        replay = await _probe_replayer(OTHER_SECRET).replay_workflow(
            history, raise_on_replay_failure=False
        )
        assert replay.replay_failure is not None
        logged = [
            r.exc_info[1]
            for r in caplog.records
            if r.exc_info and isinstance(r.exc_info[1], PayloadDecryptionError)
        ]
        assert logged, "the decryption error must reach the worker log"
        assert EncryptionCodec(OTHER_SECRET).key_id in str(logged[0])
        assert INPUT_MARKER not in str(logged[0])

    async def test_without_the_codec_the_same_history_is_plaintext(self):
        """Control: the scan above would see the markers if they were stored in clear."""
        async with await WorkflowEnvironment.start_time_skipping() as env:
            result, error, history = await _run_probe(env, {})

        assert error is None and result is not None
        raw = _history_bytes(history)
        for marker in ALL_MARKERS:
            assert marker.encode() in raw, marker
        assert not any(EncryptionCodec.is_encrypted(p) for p in _history_payloads(history))

    async def test_workflow_failure_message_is_encrypted_and_reaches_the_client(self):
        async with await WorkflowEnvironment.start_time_skipping() as env:
            result, error, history = await _run_probe(
                env, temporal_client_options(SECRET), fail_workflow=True
            )

        assert result is None
        assert error is not None
        assert isinstance(error.cause, ApplicationError)
        assert error.cause.message == WF_FAILURE_MARKER
        assert error.cause.type == "ProbeWorkflowFailure"
        (failed,) = _events(history, EventType.EVENT_TYPE_WORKFLOW_EXECUTION_FAILED)
        assert failed.workflow_execution_failed_event_attributes.failure.message == (
            "Encoded failure"
        )
        raw = _history_bytes(history)
        for marker in [*ALL_MARKERS, WF_FAILURE_MARKER]:
            assert marker.encode() not in raw, marker


# -------------------------------------------------------------------
# SecurityCheckWorkflow: plaintext histories and in-flight migration
# -------------------------------------------------------------------

_PROJECT_MARKER = "codec-project-6c1e"


def _security_stubs(timer_started: asyncio.Event | None = None) -> list[Any]:
    """Stub activities under the production names (pull path with TTL watch)."""

    @activity.defn(name="parse_fdx")
    async def parse_fdx(job_data: dict) -> dict:
        return {"parsed_ref_key": "eki:buf:parsed", "total_scenes": 1, "total_characters": 1}

    @activity.defn(name="analyze_scene_risk")
    async def analyze(job_data: dict) -> dict:
        return {"scene_index": 0, "scene_number": "1", "findings_ref_key": "eki:buf:f0"}

    @activity.defn(name="aggregate_report")
    async def aggregate(job_data: dict, job_metadata: dict) -> dict:
        return {
            "report_ref_key": "eki:buf:report",
            "report_id": job_metadata.get("report_id"),
            "total_findings": 0,
        }

    @activity.defn(name="deliver_report")
    async def deliver(report_data: dict, delivery_config: dict) -> dict:
        return {"delivered": True, "delivery_mode": "pull", "attempts_used": 1}

    @activity.defn(name="update_job_status")
    async def update_status(payload: dict) -> dict:
        return {"updated": True}

    @activity.defn(name="check_report_retrieved")
    async def check_retrieved(payload: dict) -> dict:
        return {"found": True, "retrieved": True}

    return [parse_fdx, analyze, aggregate, deliver, update_status, check_retrieved]


def _security_job() -> dict[str, Any]:
    return {
        "ref_key": "eki:buf:script",
        "script_format": "fdx",
        "project_id": _PROJECT_MARKER,
        "job_id": str(uuid.uuid4()),
        "report_id": str(uuid.uuid4()),
        "user_id": "codec-user",
        "priority": 5,
        "delivery_mode": "pull",
        "metadata": {"title": INPUT_MARKER},
        "script_id": 7,
        "llm_parallel_enabled": False,
        "pdf_structure_concurrency": 1,
        "risk_analysis_concurrency": 1,
        "llm_activity_timeout_seconds": 600,
        "buffer_ttl_seconds": 21600,
    }


def _security_replayer(options: dict[str, Any]) -> Replayer:
    return Replayer(
        workflows=[SecurityCheckWorkflow],
        interceptors=[ContextPropagationInterceptor()],
        **options,
    )


async def _timer_started(handle: Any) -> None:
    while True:
        async for event in handle.fetch_history_events():
            if event.event_type == EventType.EVENT_TYPE_TIMER_STARTED:
                return
        await asyncio.sleep(0)


@pytest.mark.temporal
class TestSecurityCheckWorkflowAcrossTheDeploy:
    async def test_pre_codec_plaintext_history_replays_with_the_new_converter(self):
        async with await WorkflowEnvironment.start_time_skipping() as env:
            client = _client(env, {})  # 1.0.0: default converter
            queue = f"codec-plain-{uuid.uuid4().hex}"
            job = _security_job()
            async with Worker(
                client,
                task_queue=queue,
                workflows=[SecurityCheckWorkflow],
                activities=_security_stubs(),
            ):
                handle = await client.start_workflow(
                    SecurityCheckWorkflow.run, job, id=job["job_id"], task_queue=queue
                )
                result = await handle.result()
                history = await handle.fetch_history()

        assert result["status"] == "completed"
        assert _PROJECT_MARKER.encode() in _history_bytes(history)  # really plaintext
        await _security_replayer(temporal_client_options(SECRET)).replay_workflow(history)

    async def test_workflow_in_flight_across_the_deploy_finishes_and_replays(self):
        """Started by a 1.0.0 API/worker, finished by a 1.0.1 worker: a mixed history."""
        async with await WorkflowEnvironment.start_time_skipping() as env:
            job = _security_job()
            queue = f"codec-mixed-{uuid.uuid4().hex}"

            old_client = _client(env, {})
            async with Worker(
                old_client,
                task_queue=queue,
                workflows=[SecurityCheckWorkflow],
                activities=_security_stubs(),
                max_cached_workflows=0,  # no sticky queue: the new worker takes over at once
            ):
                handle = await old_client.start_workflow(
                    SecurityCheckWorkflow.run, job, id=job["job_id"], task_queue=queue
                )
                # Delivered (pull), now waiting in the 6 h pull-TTL watch timer.
                await asyncio.wait_for(_timer_started(handle), timeout=30)

            new_client = _client(env, temporal_client_options(SECRET))
            async with Worker(
                new_client,
                task_queue=queue,
                workflows=[SecurityCheckWorkflow],
                activities=_security_stubs(),
            ):
                # A fetched handle does not unlock time skipping; let the TTL elapse.
                await env.sleep(timedelta(hours=7))
                result = await new_client.get_workflow_handle(job["job_id"]).result()
                history = await handle.fetch_history()

        assert result["status"] == "completed"
        (started,) = _events(history, EventType.EVENT_TYPE_WORKFLOW_EXECUTION_STARTED)
        (start_input,) = started.workflow_execution_started_event_attributes.input.payloads
        assert not EncryptionCodec.is_encrypted(start_input)  # written by 1.0.0
        (completed,) = _events(history, EventType.EVENT_TYPE_WORKFLOW_EXECUTION_COMPLETED)
        (output,) = completed.workflow_execution_completed_event_attributes.result.payloads
        assert EncryptionCodec.is_encrypted(output)  # written by 1.0.1
        assert b'"completed"' not in output.SerializeToString()

        await _security_replayer(temporal_client_options(SECRET)).replay_workflow(history)
        # A 1.0.0 worker (no codec) cannot read what 1.0.1 wrote: deploy the
        # worker together with / before the API, never roll it back with open workflows.
        replay = await _security_replayer({}).replay_workflow(
            history, raise_on_replay_failure=False
        )
        assert replay.replay_failure is not None
