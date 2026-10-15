"""Edge-case and boundary tests for the public HTTP surface (release 1.0 hardening).

Covers the limits that are actually enforced by ``core/models.py``,
``api/routers/security.py``, ``api/dependencies.py`` and ``api/rate_limiting.py``:
payload size/encoding, field boundaries, header handling, SSRF filtering of
``callback_url``, UUID path parameters, Bearer parsing, rate limiting, the
one-shot report contract and the error envelope.

Tests marked "regression (W5 #n)" pin bugs found by this suite and fixed for
1.0 (previously strict xfails); ``..._documented`` tests pin surprising but
intended behaviour so that changing it is a deliberate decision.

Runs against SQLite (default) and PostgreSQL (``TEST_POSTGRES_URL``).  Every
test that writes rows uses its own freshly created API key/user, because the
database is session-scoped and idempotency keys / one-shot state are per user.
"""

import asyncio
import base64
import hashlib
import secrets
from datetime import UTC, datetime, timedelta
from unittest.mock import AsyncMock
from uuid import UUID, uuid4

import pytest
from sqlalchemy import select, update

from api.config import get_settings
from api.main import app
from core.db_models import ApiKeyModel, JobMetadata, ReportMetadata
from core.models import JobStatus
from services.report_generator import build_report_dict
from services.secure_buffer import SecureBuffer
from tests.conftest import MINIMAL_PDF_BYTES, requires_postgres

SYNC = "/v1/security/check"
ASYNC = "/v1/security/check:async"
NIL_UUID = "00000000-0000-0000-0000-000000000000"
UNAVAILABLE = (
    "Report is no longer available (expired or already delivered). URL is no longer valid."
)

FDX = b'<?xml version="1.0" encoding="UTF-8"?><FinalDraft><Content/></FinalDraft>'
ENVELOPE_KEYS = {"error", "message", "details", "request_id", "timestamp"}

# Encoded (base64) length limit on ``script_content``.  Because the limit is on
# the *encoded* string, the effective decoded limit is 3/4 of it (7.5 MiB) --
# the "Decoded script exceeds 10 MiB" check in the model can never trigger.
ENCODED_LIMIT = 10_485_760
DECODED_MAX_VIA_JSON = ENCODED_LIMIT // 4 * 3  # 7_864_320 bytes

# ---------------------------------------------------------------------------
# Helpers & fixtures
# ---------------------------------------------------------------------------


def b64(data: bytes) -> str:
    return base64.b64encode(data).decode("ascii")


def payload(**overrides: object) -> dict:
    body: dict = {"script_content": b64(FDX), "script_format": "fdx", "project_id": "edge-proj"}
    body.update(overrides)
    return body


def assert_envelope(resp, status: int, error: str, message: str | None = None) -> dict:
    """Assert status code + the project's ErrorResponse envelope (api/main.py)."""
    assert resp.status_code == status, resp.text
    body = resp.json()
    assert set(body) == ENVELOPE_KEYS, body
    assert body["error"] == error
    assert isinstance(body["details"], list)
    if message is not None:
        assert message in body["message"], body["message"]
    return body


def _naive_utc_now() -> datetime:
    return datetime.now(UTC).replace(tzinfo=None)


async def _create_key(db_session, *, user_id: str, active: bool = True, expired: bool = False):
    api_key = f"eki_{secrets.token_hex(32)}"
    now = _naive_utc_now()
    db_session.add(
        ApiKeyModel(
            id=uuid4(),
            user_id=user_id,
            organization_id="edge-org",
            key_hash=hashlib.sha256(api_key.encode()).hexdigest(),
            name="edge key",
            is_active=active,
            created_at=now - timedelta(days=2),
            expires_at=now - timedelta(days=1) if expired else now + timedelta(days=365),
            usage_count=0,
        )
    )
    await db_session.commit()
    return api_key


@pytest.fixture
async def edge_user(db_session) -> tuple[str, str]:
    """A fresh (api_key, user_id) per test -- isolates per-user DB state."""
    user_id = f"edge-{uuid4().hex[:12]}"
    return await _create_key(db_session, user_id=user_id), user_id


@pytest.fixture
def headers(edge_user) -> dict[str, str]:
    return {"Authorization": f"Bearer {edge_user[0]}"}


@pytest.fixture
def rate_limits():
    """Override the ``get_settings`` dependency used by the rate limiters."""

    def _apply(**overrides: object) -> None:
        custom = get_settings().model_copy(update={"rate_limit_enabled": True, **overrides})
        app.dependency_overrides[get_settings] = lambda: custom

    yield _apply
    app.dependency_overrides.pop(get_settings, None)


async def _create_job(db_session, user_id: str) -> UUID:
    job_id = uuid4()
    db_session.add(
        JobMetadata(
            job_id=job_id,
            project_id="edge-proj",
            script_format="fdx",
            status=JobStatus.PENDING,
            user_id=user_id,
            priority=5,
            delivery_mode="pull",
        )
    )
    await db_session.commit()
    return job_id


async def _create_report(db_session, redis, user_id: str) -> tuple[UUID, str]:
    """ReportMetadata row + encrypted report package in the (mock) SecureBuffer."""
    report_id = uuid4()
    buffer = SecureBuffer(redis, secret_key=get_settings().api_secret_key)
    report = build_report_dict(
        report_id=str(report_id),
        project_id="edge-proj",
        script_format="fdx",
        findings=[],
        processing_time_seconds=0.5,
    )
    ref_key = await buffer.store({"report": report, "pdf_base64": b64(b"%PDF-1.4")})
    db_session.add(
        ReportMetadata(
            report_id=report_id,
            job_id=uuid4(),
            project_id="edge-proj",
            user_id=user_id,
            script_format="fdx",
            is_retrieved=False,
            total_findings=0,
            processing_time_seconds=0.5,
            report_ref_key=ref_key,
        )
    )
    await db_session.commit()
    return report_id, ref_key


# ---------------------------------------------------------------------------
# Script content: size limits, emptiness, base64, magic bytes
# ---------------------------------------------------------------------------


class TestScriptContentLimits:
    @pytest.mark.parametrize(
        ("decoded_size", "expected"),
        [(DECODED_MAX_VIA_JSON, 200), (DECODED_MAX_VIA_JSON + 1, 422)],
        ids=["encoded-exactly-at-limit", "encoded-limit-plus-one-block"],
    )
    def test_json_limit_is_on_the_encoded_length(self, client, headers, decoded_size, expected):
        content = b64(b"a" * decoded_size)
        resp = client.post(SYNC, json=payload(script_content=content), headers=headers)
        if expected == 200:
            assert len(content) == ENCODED_LIMIT
            assert resp.status_code == 200, resp.text
        else:
            assert len(content) == ENCODED_LIMIT + 4
            assert_envelope(
                resp,
                422,
                "ValidationError",
                "script_content: String should have at most 10485760 characters",
            )

    @pytest.mark.parametrize(("extra", "expected"), [(0, 200), (1, 413)], ids=["at", "plus-1"])
    def test_multipart_limit_is_on_raw_bytes(self, client, headers, monkeypatch, extra, expected):
        custom = get_settings().model_copy(update={"max_upload_size_bytes": 1024})
        monkeypatch.setattr("api.routers.security.get_settings", lambda: custom)
        body = b"x" * (1024 + extra)
        resp = client.post(
            SYNC, files={"file": ("s.fdx", body, "application/xml")}, headers=headers
        )
        if expected == 200:
            assert resp.status_code == 200, resp.text
        else:
            assert_envelope(resp, 413, "PayloadTooLarge", "File exceeds maximum size of 1024 bytes")

    def test_multipart_default_limit_exactly_10_mib(self, client, headers):
        limit = get_settings().max_upload_size_bytes
        assert limit == 10 * 1024 * 1024
        ok = client.post(
            SYNC, files={"file": ("s.fdx", b"a" * limit, "application/xml")}, headers=headers
        )
        assert ok.status_code == 200, ok.text
        too_big = client.post(
            SYNC, files={"file": ("s.fdx", b"a" * (limit + 1), "application/xml")}, headers=headers
        )
        assert_envelope(too_big, 413, "PayloadTooLarge", f"maximum size of {limit} bytes")

    @pytest.mark.parametrize(
        ("content", "message"),
        [
            ("", "script_content: String should have at least 1 character"),
            (" ", "Script content cannot be empty"),
            ("   \t\r\n ", "Script content cannot be empty"),
        ],
        ids=["empty", "single-space", "mixed-whitespace"],
    )
    def test_empty_or_whitespace_content_rejected(self, client, headers, content, message):
        resp = client.post(SYNC, json=payload(script_content=content), headers=headers)
        assert_envelope(resp, 422, "ValidationError", message)

    def test_base64_of_whitespace_only_script_is_accepted(self, client, headers):
        # Surprising but current contract: emptiness is checked on the *encoded*
        # string only; a script that decodes to blanks is accepted (fails later).
        resp = client.post(SYNC, json=payload(script_content=b64(b"   \n")), headers=headers)
        assert resp.status_code == 200, resp.text

    @pytest.mark.parametrize(
        ("content", "message"),
        [
            ("QUJ", "Invalid base64 encoding"),
            ("QQ===", "Invalid base64 encoding"),
            ("====", "Invalid base64 encoding"),
            ("-_-_", "Invalid base64 encoding"),
            ("QUJD\nREVG", "Invalid base64 encoding"),
            ("QUJD\r\nREVG", "Invalid base64 encoding"),
            ("QUJD REVG", "Invalid base64 encoding"),
            (" QUJD", "Invalid base64 encoding"),
            ("QUJD\x00", "Invalid base64 encoding"),
            ("QUJDé", "string argument should contain only ASCII characters"),
            ("QUJD🔥", "string argument should contain only ASCII characters"),
        ],
        ids=[
            "missing-padding",
            "excess-padding",
            "padding-only",
            "urlsafe-alphabet",
            "mime-newline",
            "mime-crlf",
            "embedded-space",
            "leading-space",
            "nul-byte",
            "non-ascii-latin1",
            "non-ascii-emoji",
        ],
    )
    def test_invalid_base64_variants(self, client, headers, content, message):
        resp = client.post(SYNC, json=payload(script_content=content), headers=headers)
        assert_envelope(resp, 422, "ValidationError", message)


class TestMagicBytes:
    @pytest.mark.parametrize(
        ("fmt", "raw", "message"),
        [
            ("pdf", FDX, "File does not appear to be a valid PDF"),
            ("pdf", b"%pdf-1.4\n", "File does not appear to be a valid PDF"),
            ("pdf", b" %PDF-1.4\n", "File does not appear to be a valid PDF"),
            ("pdf", b"\xef\xbb\xbf%PDF-1.4\n", "File does not appear to be a valid PDF"),
            (
                "fdx",
                b"%PDF-1.7\n%\xe2\xe3\xcf\xd3\n",
                "FDX script does not appear to be valid text",
            ),
            ("fdx", '<FinalDraft x="ä"/>'.encode("latin-1"), "does not appear to be valid text"),
            (
                "fdx",
                '<?xml version="1.0" encoding="UTF-16"?><FinalDraft/>'.encode("utf-16"),
                "null bytes",
            ),
        ],
        ids=[
            "fdx-declared-pdf",
            "pdf-lowercase-magic",
            "pdf-leading-space",
            "pdf-with-bom",
            "binary-pdf-declared-fdx",
            "latin1-fdx",
            "utf16-fdx",
        ],
    )
    def test_json_content_must_match_declared_format(self, client, headers, fmt, raw, message):
        resp = client.post(
            SYNC, json=payload(script_format=fmt, script_content=b64(raw)), headers=headers
        )
        assert_envelope(resp, 422, "ValidationError", message)

    @pytest.mark.parametrize(
        ("fmt", "raw"),
        [
            ("pdf", b"%PDF"),  # bare magic is enough at the API boundary
            ("fdx", MINIMAL_PDF_BYTES),  # ASCII-only PDF passes the FDX "is text" check
            ("fdx", b"just some text, not XML"),  # XML validity is checked by the worker
            ("fdx", b"\xef\xbb\xbf" + FDX),  # UTF-8 BOM is valid UTF-8
            ("fdx", b"a" * 1000 + b"\x00"),  # NUL check only covers the first 1000 bytes
        ],
        ids=["pdf-magic-only", "ascii-pdf-as-fdx", "non-xml-fdx", "utf8-bom-fdx", "late-nul-fdx"],
    )
    def test_lenient_api_boundary_accepts(self, client, headers, fmt, raw):
        # Documented leniency: the API only sniffs; the parser rejects later.
        resp = client.post(
            SYNC, json=payload(script_format=fmt, script_content=b64(raw)), headers=headers
        )
        assert resp.status_code == 200, resp.text

    def test_fdx_multibyte_char_straddling_byte_1000(self, client, headers):
        # Regression (W5 #8): the UTF-8 sniff of the first 1000 bytes rejected a
        # valid script when a multi-byte character straddled the boundary.
        prefix = b'<FinalDraft><Content><Paragraph Type="Action"><Text>'
        raw = prefix + b"a" * (999 - len(prefix)) + "ü".encode() + b"</Text></Paragraph>"
        raw += b"</Content></FinalDraft>"
        assert raw[999:1001] == "ü".encode()
        resp = client.post(SYNC, json=payload(script_content=b64(raw)), headers=headers)
        assert resp.status_code == 200, resp.text

    @pytest.mark.parametrize(
        ("filename", "raw", "status", "message"),
        [
            ("s.pdf", b"hello", 400, "Uploaded file does not appear to be a valid PDF"),
            ("s.pdf", b"\n%PDF-1.4", 400, "Uploaded file does not appear to be a valid PDF"),
            ("s.txt", b"text", 400, "Unsupported file type. Allowed: .fdx, .pdf"),
            ("s.pdf.exe", MINIMAL_PDF_BYTES, 400, "Unsupported file type"),
            ("s", FDX, 400, "Unsupported file type"),
            ("s.FDX", FDX, 200, None),
            ("S.PDF", MINIMAL_PDF_BYTES, 200, None),
            ("Drehbuch Über 🎬.fdx", FDX, 200, None),
        ],
        ids=[
            "pdf-no-magic",
            "pdf-leading-newline",
            "txt",
            "double-extension",
            "no-extension",
            "upper-fdx",
            "upper-pdf",
            "unicode-filename",
        ],
    )
    def test_multipart_extension_and_magic(self, client, headers, filename, raw, status, message):
        resp = client.post(
            SYNC, files={"file": (filename, raw, "application/octet-stream")}, headers=headers
        )
        if status == 200:
            assert resp.status_code == 200, resp.text
            expected_fmt = "pdf" if filename.lower().endswith(".pdf") else "fdx"
            assert resp.json()["report"]["script_format"] == expected_fmt
        else:
            assert_envelope(resp, status, "BadRequest", message)


# ---------------------------------------------------------------------------
# priority / delivery / script_id
# ---------------------------------------------------------------------------


class TestPriority:
    @pytest.mark.parametrize(
        ("value", "status", "stored_or_message"),
        [
            (1, 202, 1),
            (10, 202, 10),
            ("5", 202, 5),
            (5.0, 202, 5),
            (True, 202, 1),  # pydantic lax mode: bool -> int (surprising)
            (0, 422, "priority: Input should be greater than or equal to 1"),
            (-1, 422, "priority: Input should be greater than or equal to 1"),
            (11, 422, "priority: Input should be less than or equal to 10"),
            (5.5, 422, "got a number with a fractional part"),
            (None, 422, "priority: Input should be a valid integer"),
            ("abc", 422, "unable to parse string as an integer"),
            ("", 422, "unable to parse string as an integer"),
            ([5], 422, "priority: Input should be a valid integer"),
            (1e20, 422, "priority:"),
        ],
        ids=lambda v: repr(v),
    )
    def test_json_priority(self, client, headers, mock_temporal, value, status, stored_or_message):
        resp = client.post(ASYNC, json=payload(priority=value), headers=headers)
        if status == 202:
            assert resp.status_code == 202, resp.text
            assert mock_temporal.started[-1]["args"][1]["priority"] == stored_or_message
        else:
            assert_envelope(resp, 422, "ValidationError", stored_or_message)
            assert mock_temporal.started == []

    @pytest.mark.parametrize(
        ("value", "status", "stored_or_message"),
        [
            ("1", 202, 1),
            ("10", 202, 10),
            (" 5 ", 202, 5),
            ("٥", 202, 5),  # int() accepts Unicode digits (surprising)
            ("1_0", 202, 10),  # int() accepts underscores (surprising)
            ("0", 422, "priority: must be between 1 and 10"),
            ("11", 422, "priority: must be between 1 and 10"),
            ("-1", 422, "priority: must be between 1 and 10"),
            ("5.5", 422, "priority: must be an integer between 1 and 10"),
            ("", 422, "priority: must be an integer between 1 and 10"),
            ("0x5", 422, "priority: must be an integer between 1 and 10"),
        ],
        ids=lambda v: repr(v),
    )
    def test_multipart_priority(
        self, client, headers, mock_temporal, value, status, stored_or_message
    ):
        resp = client.post(
            ASYNC,
            files={"file": ("s.fdx", FDX, "application/xml")},
            data={"project_id": "edge-proj", "priority": value},
            headers=headers,
        )
        if status == 202:
            assert resp.status_code == 202, resp.text
            assert mock_temporal.started[-1]["args"][1]["priority"] == stored_or_message
        else:
            assert_envelope(resp, 422, "ValidationError", stored_or_message)

    def test_sync_endpoint_ignores_priority(self, client, headers):
        resp = client.post(SYNC, json=payload(priority=999), headers=headers)
        assert resp.status_code == 200, resp.text


def _post_delivery(client, headers, transport: str, value):
    if transport == "json":
        return client.post(ASYNC, json=payload(delivery=value), headers=headers)
    return client.post(
        ASYNC,
        files={"file": ("s.fdx", FDX, "application/xml")},
        data={"project_id": "edge-proj", "delivery": value},
        headers=headers,
    )


class TestDelivery:
    """Regression (W5 #3): ``delivery`` is normalised (strip + lower) and must be
    ``pull`` or ``push``; unknown values used to be stored verbatim (skipping the
    pull-TTL watch) and >10 characters hit VARCHAR(10) -> 503 on PostgreSQL."""

    @pytest.mark.parametrize("transport", ["json", "multipart"])
    @pytest.mark.parametrize(
        ("value", "stored"),
        [
            ("pull", "pull"),
            ("push", "push"),
            ("PUSH", "push"),
            ("Push", "push"),
            ("pull ", "pull"),
            (" PuLl\t", "pull"),
            ("", "pull"),  # blank == omitted == default
            ("   ", "pull"),
        ],
        ids=repr,
    )
    def test_valid_and_normalised_values(
        self, client, headers, mock_temporal, transport, value, stored
    ):
        resp = _post_delivery(client, headers, transport, value)
        assert resp.status_code == 202, resp.text
        assert mock_temporal.started[-1]["args"][1]["delivery_mode"] == stored
        assert resp.json()["message"] == f"Security check job started (delivery={stored})"

    @pytest.mark.parametrize("transport", ["json", "multipart"])
    @pytest.mark.parametrize(
        "value", ["foo", "pul", "pushy", "push-push-push", "pu\x00sh", "p u l l"], ids=repr
    )
    def test_unknown_values_rejected(self, client, headers, mock_temporal, transport, value):
        resp = _post_delivery(client, headers, transport, value)
        assert_envelope(resp, 422, "ValidationError", "delivery: Input should be 'pull' or 'push'")
        assert mock_temporal.started == []

    @pytest.mark.parametrize("value", [None, 5, ["push"]], ids=repr)
    def test_non_string_delivery_rejected(self, client, headers, value):
        resp = client.post(ASYNC, json=payload(delivery=value), headers=headers)
        assert_envelope(resp, 422, "ValidationError", "delivery: Input should be 'pull' or 'push'")


class TestScriptId:
    @pytest.mark.parametrize(
        ("value", "stored"), [(-1, -1), (0, 0), ("7", 7), (2**31 - 1, 2**31 - 1)]
    )
    def test_json_script_id_accepted(self, client, headers, mock_temporal, value, stored):
        resp = client.post(ASYNC, json=payload(script_id=value), headers=headers)
        assert resp.status_code == 202, resp.text
        assert mock_temporal.started[-1]["args"][1]["script_id"] == stored

    @pytest.mark.parametrize("value", ["abc", 1.5, [1]], ids=repr)
    def test_json_script_id_rejected(self, client, headers, value):
        resp = client.post(ASYNC, json=payload(script_id=value), headers=headers)
        assert_envelope(resp, 422, "ValidationError", "script_id:")

    @pytest.mark.parametrize(
        ("value", "message"),
        [
            (2**31, "script_id: Input should be less than or equal to 2147483647"),
            (2**63, "script_id: Input should be less than or equal to 2147483647"),
            (-(2**31) - 1, "script_id: Input should be greater than or equal to -2147483648"),
        ],
        ids=repr,
    )
    def test_json_script_id_outside_int32_rejected(self, client, headers, value, message):
        # Regression (W5 #5): used to reach the INTEGER column -> 503 (PG) / 500 (SQLite).
        resp = client.post(ASYNC, json=payload(script_id=value), headers=headers)
        assert_envelope(resp, 422, "ValidationError", message)

    def test_int32_bounds_accepted(self, client, headers, mock_temporal):
        for value in (-(2**31), 2**31 - 1):
            assert (
                client.post(ASYNC, json=payload(script_id=value), headers=headers).status_code
                == 202
            )


# ---------------------------------------------------------------------------
# project_id / metadata / idempotency_key
# ---------------------------------------------------------------------------


class TestStringFieldBoundaries:
    @pytest.mark.parametrize(
        "value", ["a" * 100, "a", "A-z_0-9"], ids=["max-100", "min-1", "charset"]
    )
    def test_project_id_accepted(self, client, headers, value):
        resp = client.post(SYNC, json=payload(project_id=value), headers=headers)
        assert resp.status_code == 200, resp.text
        assert resp.json()["report"]["project_id"] == value

    @pytest.mark.parametrize(
        "value",
        [
            "a" * 101,
            "",
            " abc",
            "abc ",
            "abc\n",
            "a b",
            "Prüfung",
            "проект",
            "😀",
            "a\x00",
            "a.b",
            "a/b",
        ],
        ids=repr,
    )
    def test_project_id_rejected(self, client, headers, value):
        resp = client.post(SYNC, json=payload(project_id=value), headers=headers)
        assert_envelope(resp, 422, "ValidationError", "project_id")

    @pytest.mark.parametrize(
        "metadata",
        [
            {"k" * 50: "v"},
            {"k": "v" * 1000},
            {"k": "😀" * 1000},
            {f"k{i}": i for i in range(50)},
            {"rtl": "مرحبا בעולם", "emoji": "🎬🔥", "ctl": "a\x00b\r\nc", "n": None},
            {"f": 1.5, "b": False, "i": -(2**62)},
        ],
        ids=["key-50", "value-1000", "emoji-1000", "50-keys", "unicode-ctl", "scalars"],
    )
    def test_metadata_accepted_and_forwarded_verbatim(
        self, client, headers, mock_temporal, metadata
    ):
        resp = client.post(ASYNC, json=payload(metadata=metadata), headers=headers)
        assert resp.status_code == 202, resp.text
        assert mock_temporal.started[-1]["args"][1]["metadata"] == metadata

    @pytest.mark.parametrize(
        ("metadata", "message"),
        [
            ({"k" * 51: "v"}, "Invalid metadata key format"),
            ({"": "v"}, "Invalid metadata key format"),
            ({"schlüssel": "v"}, "Invalid metadata key format"),
            ({"a b": "v"}, "Invalid metadata key format"),
            ({"k": "v" * 1001}, "Metadata value too long for key 'k'"),
            ({"k": {"nested": 1}}, "Invalid metadata value type for key 'k'"),
            ({"k": [1, 2]}, "Invalid metadata value type for key 'k'"),
            ({f"k{i}": i for i in range(51)}, "Too many metadata fields (max 50)"),
            (["not", "a", "dict"], "metadata: Input should be a valid dictionary"),
        ],
        ids=[
            "key-51",
            "key-empty",
            "key-umlaut",
            "key-space",
            "value-1001",
            "nested",
            "list",
            "51-keys",
            "not-object",
        ],
    )
    def test_metadata_rejected(self, client, headers, metadata, message):
        resp = client.post(SYNC, json=payload(metadata=metadata), headers=headers)
        assert_envelope(resp, 422, "ValidationError", message)

    @pytest.mark.parametrize(
        "key",
        ["k" * 255, "🔑" * 255, " ", "  spaced key  "],
        ids=["max-255", "emoji-255", "space", "padded"],
    )
    def test_idempotency_key_accepted_and_deduplicates(self, client, headers, mock_temporal, key):
        first = client.post(ASYNC, json=payload(idempotency_key=key), headers=headers)
        second = client.post(ASYNC, json=payload(idempotency_key=key), headers=headers)
        assert first.status_code == 202, first.text
        assert second.status_code == 202, second.text
        assert second.json()["job_id"] == first.json()["job_id"]
        assert second.json()["message"] == "Existing job returned (idempotency key matched)"
        assert len(mock_temporal.started) == 1

    def test_idempotency_key_too_long(self, client, headers):
        resp = client.post(ASYNC, json=payload(idempotency_key="k" * 256), headers=headers)
        assert_envelope(
            resp, 422, "ValidationError", "idempotency_key: String should have at most 255"
        )

    @pytest.mark.parametrize("transport", ["json", "multipart"])
    async def test_empty_idempotency_key_means_no_key(
        self, client, db_session, headers, mock_temporal, transport
    ):
        # Regression (W5 #6): "" was stored as a real key but treated as "no key",
        # so the second request hit the unique constraint -> 503.  Now "" is
        # normalised to None: two independent jobs, nothing stored.
        def post():
            if transport == "json":
                return client.post(ASYNC, json=payload(idempotency_key=""), headers=headers)
            return client.post(
                ASYNC,
                files={"file": ("s.fdx", FDX, "application/xml")},
                data={"idempotency_key": ""},
                headers=headers,
            )

        first, second = post(), post()
        assert (first.status_code, second.status_code) == (202, 202), second.text
        job_ids = {UUID(first.json()["job_id"]), UUID(second.json()["job_id"])}
        assert len(job_ids) == 2
        rows = (
            await db_session.execute(
                select(JobMetadata.idempotency_key).where(JobMetadata.job_id.in_(job_ids))
            )
        ).scalars()
        assert list(rows) == [None, None]

    @pytest.mark.parametrize(
        "key", ["a\x00b", "tab\tkey", "line\nbreak", "cr\r", "del\x7f", "c1\x85"], ids=repr
    )
    def test_control_chars_in_idempotency_key_rejected(self, client, headers, key):
        # Regression (W5 #7): NUL reached PostgreSQL -> 503.
        resp = client.post(ASYNC, json=payload(idempotency_key=key), headers=headers)
        assert_envelope(
            resp,
            422,
            "ValidationError",
            "idempotency_key: Value error, idempotency_key must not contain control characters",
        )


# ---------------------------------------------------------------------------
# Multipart form handling
# ---------------------------------------------------------------------------


class TestMultipart:
    @pytest.mark.parametrize(
        ("kwargs", "message"),
        [
            (
                {"files": {"project_id": (None, "p")}},  # multipart with only a text field
                "Multipart request must include a 'file' field",
            ),
            (
                {"files": {"upload": ("s.fdx", FDX, "application/xml")}},
                "Multipart request must include a 'file' field",
            ),
            (
                {"files": {"file": (None, "plain text")}},
                "Multipart request must include a 'file' field",
            ),
            (
                {"files": {"file": ("", FDX, "application/xml")}},
                "Multipart request must include a 'file' field",
            ),
            ({"files": {"file": ("s.fdx", b"", "application/xml")}}, "Uploaded file is empty"),
        ],
        ids=["no-file", "wrong-field-name", "file-as-text-field", "no-filename", "empty-file"],
    )
    def test_bad_file_field(self, client, headers, kwargs, message):
        resp = client.post(ASYNC, headers=headers, **kwargs)
        assert_envelope(resp, 400, "BadRequest", message)

    def test_two_file_fields_last_one_wins(self, client, headers, mock_temporal):
        # Starlette's FormData.get returns the last value -> the PDF decides.
        files = [
            ("file", ("a.fdx", FDX, "application/xml")),
            ("file", ("b.pdf", MINIMAL_PDF_BYTES, "application/pdf")),
        ]
        resp = client.post(ASYNC, files=files, headers=headers)
        assert resp.status_code == 202, resp.text
        assert mock_temporal.started[-1]["args"][1]["script_format"] == "pdf"
        # ...and a broken last file fails even though the first one is valid.
        broken_last = [files[1], ("file", ("c.pdf", b"not a pdf", "application/pdf"))]
        resp = client.post(ASYNC, files=broken_last, headers=headers)
        assert_envelope(resp, 400, "BadRequest", "does not appear to be a valid PDF")

    def test_multipart_without_boundary_is_400(self, client, headers):
        resp = client.post(
            ASYNC, content=b"garbage", headers={**headers, "Content-Type": "multipart/form-data"}
        )
        assert_envelope(resp, 400, "BadRequest", "Missing boundary in multipart.")

    def test_missing_project_id_falls_back_to_unknown_documented(
        self, client, headers, mock_temporal
    ):
        # project_id is optional for multipart (documented contract); the router
        # stores the same "unknown" placeholder as before the validation fix.
        resp = client.post(
            ASYNC, files={"file": ("s.fdx", FDX, "application/xml")}, headers=headers
        )
        assert resp.status_code == 202, resp.text
        assert mock_temporal.started[-1]["args"][1]["project_id"] == "unknown"

    def test_text_field_sent_as_file_part_is_422(self, client, headers):
        files = {"file": ("s.fdx", FDX, "application/xml"), "project_id": ("p.txt", b"p")}
        resp = client.post(ASYNC, files=files, headers=headers)
        assert_envelope(
            resp, 422, "ValidationError", "project_id: must be a text field, not a file"
        )

    @pytest.mark.parametrize(
        ("data", "file", "message"),
        [
            ({"project_id": "'; DROP TABLE job_metadata; --"}, ("s.fdx", FDX), "project_id:"),
            ({"project_id": "75/../../../admin/hijack"}, ("s.fdx", FDX), "project_id:"),
            ({"project_id": "x" * 101}, ("s.fdx", FDX), "project_id: String should have at most"),
            ({"project_id": "x" * 300}, ("s.fdx", FDX), "project_id: String should have at most"),
            ({"project_id": "Prüfung"}, ("s.fdx", FDX), "project_id: String should match pattern"),
            ({"idempotency_key": "i" * 256}, ("s.fdx", FDX), "idempotency_key: String should"),
            ({"idempotency_key": "a\x00b"}, ("s.fdx", FDX), "must not contain control characters"),
            ({"script_format": "docx"}, ("s.fdx", FDX), "script_format: Input should be"),
            ({"script_format": "PDF"}, ("s.fdx", FDX), "script_format: Input should be"),
            ({"script_format": "pdf"}, ("s.fdx", FDX), "File does not appear to be a valid PDF"),
            ({"script_id": "abc"}, ("s.fdx", FDX), "script_id: Input should be a valid integer"),
            ({"script_id": str(2**31)}, ("s.fdx", FDX), "script_id: Input should be less than"),
            ({}, ("s.fdx", '<FinalDraft x="1"/>'.encode("utf-16")), "FDX script contains invalid"),
            ({}, ("s.fdx", b"\xff\xfe<FinalDraft/>"), "FDX script does not appear to be valid"),
        ],
        ids=[
            "project-sql",
            "project-path-traversal",
            "project-101",
            "project-300",
            "project-umlaut",
            "idem-256",
            "idem-nul",
            "format-docx",
            "format-upper",
            "format-pdf-for-fdx",
            "script-id-text",
            "script-id-int32-overflow",
            "utf16-content",
            "non-utf8-content",
        ],
    )
    def test_multipart_fields_validated_like_json(
        self, client, headers, mock_temporal, data, file, message
    ):
        # Regression (W5 #4): the multipart path used to bypass the Pydantic model
        # (500 for script_format/script_id, unchecked project_id that can steer
        # the ePro push URL, PG 503 for long values, %PDF check bypass).
        resp = client.post(
            ASYNC, files={"file": (*file, "application/octet-stream")}, data=data, headers=headers
        )
        assert_envelope(resp, 422, "ValidationError", message)
        assert mock_temporal.started == []

    def test_multipart_fields_accepted_and_normalised(self, client, headers, mock_temporal):
        key = f"mp-{uuid4().hex}"
        resp = client.post(
            ASYNC,
            files={"file": ("s.pdf", MINIMAL_PDF_BYTES, "application/pdf")},
            data={
                "project_id": "edge-proj",
                "script_format": "pdf",
                "script_id": "-1",
                "delivery": " PUSH ",
                "idempotency_key": key,
                "priority": "3",
            },
            headers=headers,
        )
        assert resp.status_code == 202, resp.text
        job_data = mock_temporal.started[-1]["args"][1]
        assert (job_data["project_id"], job_data["script_format"], job_data["script_id"]) == (
            "edge-proj",
            "pdf",
            -1,
        )
        assert (job_data["delivery_mode"], job_data["priority"]) == ("push", 3)

    def test_multipart_beyond_json_encoded_limit_is_accepted(self, client, headers):
        # The JSON base64-length limit must not apply to uploads (raw size only).
        raw = b"a" * (DECODED_MAX_VIA_JSON + 1)
        resp = client.post(SYNC, files={"file": ("s.fdx", raw, "application/xml")}, headers=headers)
        assert resp.status_code == 200, resp.text


# ---------------------------------------------------------------------------
# Malformed bodies / content types
# ---------------------------------------------------------------------------


_BAD_JSON = "Request body must be valid JSON (or multipart/form-data with a 'file' field)"


class TestMalformedBodies:
    """Regression (W5 #2): these used to raise inside ``_resolve_json`` -> 500."""

    @pytest.mark.parametrize("url", [SYNC, ASYNC])
    @pytest.mark.parametrize(
        ("content", "content_type"),
        [
            (b"{bad json", "application/json"),
            (b"", "application/json"),
            (b"   ", "application/json"),
            (b"\xff\xfe{", "application/json"),
            (b'{"a": NaN', "application/json"),
            (b"a=b&c=d", "application/x-www-form-urlencoded"),
            (b"[" * 100_000 + b"]" * 100_000, "application/json"),
        ],
        ids=["syntax", "empty", "blank", "non-utf8", "truncated", "urlencoded", "deep-nesting"],
    )
    def test_unparseable_body_is_400(self, client, headers, url, content, content_type):
        resp = client.post(url, content=content, headers={**headers, "Content-Type": content_type})
        assert_envelope(resp, 400, "BadRequest", _BAD_JSON)

    @pytest.mark.parametrize("url", [SYNC, ASYNC])
    @pytest.mark.parametrize(
        ("content", "type_name"),
        [(b"[1, 2, 3]", "list"), (b"null", "NoneType"), (b'"a string"', "str"), (b"42", "int")],
        ids=["array", "null", "string", "number"],
    )
    def test_non_object_json_is_422(self, client, headers, url, content, type_name):
        resp = client.post(
            url, content=content, headers={**headers, "Content-Type": "application/json"}
        )
        assert_envelope(
            resp,
            422,
            "ValidationError",
            f"Request validation failed: body: JSON object expected, got {type_name}",
        )

    @pytest.mark.parametrize("content_type", ["text/plain", "application/xml", None])
    def test_json_body_accepted_regardless_of_content_type(self, client, headers, content_type):
        # Lenient: anything that is not multipart is parsed as JSON.
        import json

        hdrs = dict(headers)
        if content_type:
            hdrs["Content-Type"] = content_type
        resp = client.post(SYNC, content=json.dumps(payload()).encode(), headers=hdrs)
        assert resp.status_code == 200, resp.text

    def test_unknown_fields_are_ignored(self, client, headers):
        resp = client.post(
            SYNC, json=payload(unexpected="x", __proto__={"admin": True}), headers=headers
        )
        assert resp.status_code == 200, resp.text


# ---------------------------------------------------------------------------
# Headers: actor headers, request id, oversized values
# ---------------------------------------------------------------------------


class TestActorHeaders:
    def test_missing_and_blank_actor_user_are_equivalent(self, client, headers, edge_user):
        missing = client.post(SYNC, json=payload(), headers=headers)
        blank = client.post(SYNC, json=payload(), headers={**headers, "X-Actor-User-Id": ""})
        matching = client.post(
            SYNC, json=payload(), headers={**headers, "X-Actor-User-Id": edge_user[1]}
        )
        for resp in (missing, blank, matching):
            assert resp.status_code == 200, resp.text
            assert resp.json()["report"]["metadata"]["user_id"] == edge_user[1]

    @pytest.mark.parametrize(
        "value",
        [
            " ",
            "{user} ",
            "{USER}",
            "Jürgen".encode(),
            "😀".encode(),
            "مستخدم".encode(),
            "u" * 16_384,
        ],
        ids=["space", "trailing-space", "case", "utf8", "emoji", "rtl", "16kb"],
    )
    def test_actor_user_mismatch_is_403(self, client, headers, edge_user, value):
        if isinstance(value, str):
            value = value.replace("{user}", edge_user[1]).replace("{USER}", edge_user[1].upper())
        resp = client.post(SYNC, json=payload(), headers={**headers, "X-Actor-User-Id": value})
        assert_envelope(
            resp, 403, "Forbidden", "X-Actor-User-Id does not match authenticated API key user"
        )

    @pytest.mark.parametrize(
        "raw",
        ["Prüfung 😀".encode(), "مشروع".encode(), b"p" * 16_384, b"tab\there"],
        ids=["utf8", "rtl", "16kb", "tab"],
    )
    def test_actor_project_header_is_unvalidated_latin1(self, client, headers, raw):
        # Surprising: the header is not validated and is echoed as Latin-1
        # (mojibake for UTF-8 input) into the sync report metadata.
        resp = client.post(SYNC, json=payload(), headers={**headers, "X-Actor-Project-Id": raw})
        assert resp.status_code == 200, resp.text
        assert resp.json()["report"]["metadata"]["project_id"] == raw.decode("latin-1")

    def test_huge_authorization_header_is_plain_401(self, client):
        resp = client.get(
            f"/v1/security/jobs/{NIL_UUID}", headers={"Authorization": "Bearer " + "x" * 70_000}
        )
        assert_envelope(resp, 401, "Unauthorized", "Invalid or expired API key")


class TestRequestId:
    @pytest.mark.parametrize(
        "sent", [None, "abc-123", "   ", "r" * 2048], ids=["absent", "given", "blank", "long"]
    )
    def test_error_envelope_request_id_matches_response_header(self, client, headers, sent):
        hdrs = dict(headers)
        if sent is not None:
            hdrs["X-Request-ID"] = sent
        resp = client.post(SYNC, json=payload(project_id="bad id"), headers=hdrs)
        body = assert_envelope(resp, 422, "ValidationError")
        assert resp.headers["X-Request-ID"]
        assert body["request_id"] == resp.headers["X-Request-ID"]
        if sent and sent.strip():
            assert body["request_id"] == sent


# ---------------------------------------------------------------------------
# SSRF: callback_url
# ---------------------------------------------------------------------------

PRIVATE = "Callback URL cannot point to private/internal IP addresses"
NOT_ALLOWED = "Callback URL domain not allowed"
HTTPS_ONLY = "Callback URL must use HTTPS"
BAD_SCHEME = "URL scheme should be 'http' or 'https'"


class TestCallbackUrlSsrf:
    @pytest.mark.parametrize(
        ("url", "message"),
        [
            ("https://[::1]/cb", PRIVATE),
            ("https://[::]/cb", PRIVATE),
            ("https://[fe80::1]/cb", PRIVATE),
            ("https://[fc00::1]/cb", PRIVATE),
            ("https://2130706433/cb", PRIVATE),  # decimal 127.0.0.1
            ("https://0177.0.0.1/cb", PRIVATE),  # octal
            ("https://0x7f.1/cb", PRIVATE),  # hex + short form
            ("https://127.1/cb", PRIVATE),
            ("https://0.0.0.0/cb", PRIVATE),
            ("https://169.254.169.254/latest/meta-data", PRIVATE),
            ("https://10.0.0.1/cb", PRIVATE),
            ("https://224.0.0.1/cb", PRIVATE),
            ("https://epro.filmakademie.de@127.0.0.1/cb", PRIVATE),  # userinfo trick
            ("https://localhost./cb", NOT_ALLOWED),
            ("https://localhost/cb", NOT_ALLOWED),
            ("https://8.8.8.8/cb", NOT_ALLOWED),
            ("https://100.64.0.1/cb", NOT_ALLOWED),  # CGNAT: not "private" but not allowed
            ("https://epro.filmakademie.de.evil.com/cb", NOT_ALLOWED),
            ("https://evil.com#@epro.filmakademie.de", NOT_ALLOWED),
            ("https://evil.com\\@epro.filmakademie.de", NOT_ALLOWED),
            ("https://epro.filmakademie.de%2f@evil.com/", NOT_ALLOWED),
            ("https://еpro.filmakademie.de/cb", NOT_ALLOWED),  # Cyrillic 'е' (IDN homograph)
            ("https://xn--pro-5cd.filmakademie.de/cb", NOT_ALLOWED),
            ("http://epro.filmakademie.de/cb", HTTPS_ONLY),
            ("ftp://epro.filmakademie.de/cb", BAD_SCHEME),
            ("file:///etc/passwd", BAD_SCHEME),
            ("gopher://epro.filmakademie.de/", BAD_SCHEME),
            ("javascript:alert(1)", BAD_SCHEME),
            ("https://", "Input should be a valid URL"),
            ("", "Input should be a valid URL"),
            ("https://epro.filmakademie.de/" + "a" * 3000, "URL should have at most 2083"),
        ],
        ids=lambda v: v if isinstance(v, str) and len(v) < 60 else None,
    )
    def test_rejected(self, client, headers, url, message):
        resp = client.post(SYNC, json=payload(callback_url=url), headers=headers)
        assert_envelope(resp, 422, "ValidationError", message)
        assert "callback_url" in resp.json()["message"]

    @pytest.mark.parametrize(
        "url",
        ["https://[::ffff:127.0.0.1]/cb", "https://[::ffff:7f00:1]/cb"],
        ids=["mapped", "mapped-hex"],
    )
    def test_ipv4_mapped_ipv6_rejected(self, client, headers, url):
        # Message depends on the Python version's ipaddress semantics; either
        # way the domain allow-list blocks it.
        resp = client.post(SYNC, json=payload(callback_url=url), headers=headers)
        body = assert_envelope(resp, 422, "ValidationError")
        assert PRIVATE in body["message"] or NOT_ALLOWED in body["message"]

    @pytest.mark.parametrize(
        "url",
        [
            "https://epro.filmakademie.de/cb",
            "https://staging.epro.filmakademie.de/x?y=1",
            "https://EPRO.FILMAKADEMIE.DE./cb",  # case + trailing dot normalised
            "https://127.0.0.1@epro.filmakademie.de/",  # userinfo, real host is allowed
            "https://epro.filmakademie.de:8443/cb",  # surprising: any port is allowed
        ],
    )
    def test_allowed(self, client, headers, url):
        resp = client.post(SYNC, json=payload(callback_url=url), headers=headers)
        assert resp.status_code == 200, resp.text


# ---------------------------------------------------------------------------
# UUID path params & IDOR
# ---------------------------------------------------------------------------

UUID_FORMATS = {
    "upper": lambda u: str(u).upper(),
    "no-hyphens": lambda u: u.hex,
    "braces": lambda u: "{" + str(u) + "}",
    "urn": lambda u: f"urn:uuid:{u}",
}


class TestUuidPathParams:
    @pytest.mark.parametrize("fmt", list(UUID_FORMATS))
    async def test_job_id_alternate_spellings_resolve(
        self, client, db_session, edge_user, headers, fmt
    ):
        job_id = await _create_job(db_session, edge_user[1])
        resp = client.get(f"/v1/security/jobs/{UUID_FORMATS[fmt](job_id)}", headers=headers)
        assert resp.status_code == 200, resp.text
        assert resp.json()["job_id"] == str(job_id)

    @pytest.mark.parametrize("fmt", ["upper", "no-hyphens"])
    async def test_report_id_alternate_spellings_resolve(
        self, client, db_session, mock_redis, edge_user, headers, fmt
    ):
        report_id, _ = await _create_report(db_session, mock_redis, edge_user[1])
        resp = client.get(f"/v1/security/reports/{UUID_FORMATS[fmt](report_id)}", headers=headers)
        assert resp.status_code == 200, resp.text
        assert resp.json()["report"]["report_id"] == str(report_id)

    @pytest.mark.parametrize("kind", ["jobs", "reports"])
    @pytest.mark.parametrize(
        "value",
        [
            "not-a-uuid",
            "123e4567-e89b-12d3-a456-42661417400",
            "123e4567-e89b-12d3-a456-4266141740000",
            "123e4567-e89b-12d3-a456-42661417400g",
            "%20123e4567-e89b-12d3-a456-426614174000",
            "123e4567-e89b-12d3-a456-426614174000%00",
            "' OR 1=1 --",
            "x" * 5000,
        ],
        ids=["word", "short", "long", "non-hex", "leading-space", "nul", "sqli", "5000-chars"],
    )
    def test_invalid_uuid_is_422_not_500(self, client, headers, kind, value):
        resp = client.get(f"/v1/security/{kind}/{value}", headers=headers)
        body = assert_envelope(resp, 422, "ValidationError", "Request validation failed")
        field = "path.job_id" if kind == "jobs" else "path.report_id"
        assert body["details"][0]["field"] == field
        assert body["details"][0]["error_code"] == "uuid_parsing"

    @pytest.mark.parametrize("kind", ["jobs", "reports"])
    @pytest.mark.parametrize("value", [NIL_UUID, "ffffffff-ffff-ffff-ffff-ffffffffffff"])
    def test_well_formed_unknown_uuid_is_404(self, client, headers, kind, value):
        resp = client.get(f"/v1/security/{kind}/{value}", headers=headers)
        assert_envelope(resp, 404, "NotFound", "not found or access denied")

    @pytest.mark.parametrize("fmt", ["canonical", *UUID_FORMATS])
    async def test_foreign_job_indistinguishable_from_missing(
        self, client, db_session, headers, fmt
    ):
        other_job = await _create_job(db_session, f"other-{uuid4().hex[:8]}")
        spell = UUID_FORMATS.get(fmt, str)
        foreign = client.get(f"/v1/security/jobs/{spell(other_job)}", headers=headers)
        missing = client.get(f"/v1/security/jobs/{spell(uuid4())}", headers=headers)

        def strip(resp) -> dict:
            return {k: v for k, v in resp.json().items() if k not in ("timestamp", "request_id")}

        assert foreign.status_code == missing.status_code == 404
        assert strip(foreign) == strip(missing)

    async def test_foreign_report_attempt_does_not_consume_one_shot(
        self, client, db_session, mock_redis, edge_user, headers
    ):
        report_id, ref_key = await _create_report(db_session, mock_redis, edge_user[1])
        intruder_key = await _create_key(db_session, user_id=f"intruder-{uuid4().hex[:8]}")
        intruder = client.get(
            f"/v1/security/reports/{report_id}", headers={"Authorization": f"Bearer {intruder_key}"}
        )
        assert_envelope(intruder, 404, "NotFound", "Report not found or access denied")
        assert await mock_redis.exists(ref_key) == 1  # still there

        owner = client.get(f"/v1/security/reports/{report_id}", headers=headers)
        assert owner.status_code == 200, owner.text
        assert owner.headers["X-One-Shot"] == "true"


# ---------------------------------------------------------------------------
# Authorization header parsing & key-existence leaks
# ---------------------------------------------------------------------------


class TestAuthorizationHeader:
    @pytest.mark.parametrize(
        "template", ["Bearer {k}", "bearer {k}", "BEARER {k}", "Bearer  {k}", "Bearer {k} "]
    )
    def test_accepted_scheme_spellings(self, client, edge_user, template):
        resp = client.get(
            f"/v1/security/jobs/{NIL_UUID}",
            headers={"Authorization": template.format(k=edge_user[0])},
        )
        # Authenticated -> reaches the handler -> 404 for the unknown job.
        assert_envelope(resp, 404, "NotFound")

    @pytest.mark.parametrize(
        ("template", "message"),
        [
            ("{k}", "Not authenticated"),
            ("Bearer", "Not authenticated"),
            ("Bearer ", "Not authenticated"),
            ("Basic {k}", "Not authenticated"),
            ("Token {k}", "Not authenticated"),
            (" Bearer {k}", "Not authenticated"),
            ("Bearer\t{k}", "Not authenticated"),
            ("Bearer eki_" + "0" * 64, "Invalid or expired API key"),
            ("Bearer {K}", "Invalid or expired API key"),
            ("Bearer {k}x", "Invalid or expired API key"),
            ("Bearer {k}\x00", "Invalid or expired API key"),
        ],
        ids=[
            "no-scheme",
            "scheme-only",
            "empty-token",
            "basic",
            "token",
            "leading-space",
            "tab",
            "wrong-key",
            "upper-key",
            "suffix",
            "nul",
        ],
    )
    def test_rejected(self, client, edge_user, template, message):
        value = template.format(k=edge_user[0], K=edge_user[0].upper())
        resp = client.get(f"/v1/security/jobs/{NIL_UUID}", headers={"Authorization": value})
        assert_envelope(resp, 401, "Unauthorized", message)
        assert resp.headers["WWW-Authenticate"] == "Bearer"

    async def test_unknown_expired_and_inactive_keys_are_indistinguishable(
        self, client, db_session
    ):
        expired = await _create_key(db_session, user_id="edge-expired", expired=True)
        inactive = await _create_key(db_session, user_id="edge-inactive", active=False)
        unknown = f"eki_{secrets.token_hex(32)}"

        def probe(key: str):
            resp = client.post(SYNC, json=payload(), headers={"Authorization": f"Bearer {key}"})
            body = resp.json()
            return (
                resp.status_code,
                body["error"],
                body["message"],
                resp.headers.get("WWW-Authenticate"),
            )

        results = {probe(k) for k in (unknown, expired, inactive)}
        assert results == {(401, "Unauthorized", "Invalid or expired API key", "Bearer")}


# ---------------------------------------------------------------------------
# Rate limiting
# ---------------------------------------------------------------------------


class TestRateLimiting:
    URL = f"/v1/security/jobs/{NIL_UUID}"

    def test_ip_limit_n_allowed_then_429(self, client, headers, rate_limits):
        rate_limits(rate_limit_per_minute=3, rate_limit_per_hour=1000)
        assert [client.get(self.URL, headers=headers).status_code for _ in range(3)] == [404] * 3
        blocked = client.get(self.URL, headers=headers)
        assert_envelope(
            blocked,
            429,
            "RateLimitExceeded",
            "Rate limit exceeded. Maximum 3 requests per 60 seconds.",
        )
        assert int(blocked.headers["Retry-After"]) > 0

    def test_unauthenticated_requests_count_against_ip_limit(self, client, headers, rate_limits):
        rate_limits(rate_limit_per_minute=2, rate_limit_per_hour=1000)
        bad = {"Authorization": "Bearer eki_wrong"}
        assert [client.get(self.URL, headers=bad).status_code for _ in range(2)] == [401, 401]
        # The limiter runs before auth: a valid key from the same IP is now blocked.
        assert_envelope(client.get(self.URL, headers=headers), 429, "RateLimitExceeded")

    def test_zero_limit_is_clamped_to_one(self, client, headers, rate_limits):
        rate_limits(rate_limit_per_minute=0, rate_limit_per_hour=1000)
        assert client.get(self.URL, headers=headers).status_code == 404
        assert_envelope(
            client.get(self.URL, headers=headers), 429, "RateLimitExceeded", "Maximum 1 requests"
        )

    def test_api_key_hourly_limit(self, client, headers, rate_limits):
        rate_limits(rate_limit_per_minute=1000, rate_limit_per_hour=2)
        assert [client.get(self.URL, headers=headers).status_code for _ in range(2)] == [404, 404]
        blocked = client.get(self.URL, headers=headers)
        assert_envelope(
            blocked,
            429,
            "RateLimitExceeded",
            "Rate limit exceeded. Maximum 2 requests per 60 minutes.",
        )
        assert int(blocked.headers["Retry-After"]) > 0

    async def test_api_key_limit_is_per_key(self, client, db_session, headers, rate_limits):
        rate_limits(rate_limit_per_minute=1000, rate_limit_per_hour=1)
        other = {"Authorization": f"Bearer {await _create_key(db_session, user_id='edge-other')}"}
        assert client.get(self.URL, headers=headers).status_code == 404
        assert client.get(self.URL, headers=headers).status_code == 429
        # A different key from the same IP has its own hourly bucket.
        assert client.get(self.URL, headers=other).status_code == 404
        assert client.get(self.URL, headers=other).status_code == 429

    @pytest.mark.parametrize(
        "template", ["bearer {k}", "BEARER {k}", "Bearer  {k}", "Bearer {k} ", "bEaReR   {k}  "]
    )
    def test_api_key_limit_not_bypassable_by_header_spelling(
        self, client, edge_user, rate_limits, template
    ):
        rate_limits(rate_limit_per_minute=1000, rate_limit_per_hour=2)
        canonical = {"Authorization": f"Bearer {edge_user[0]}"}
        for _ in range(2):
            assert client.get(self.URL, headers=canonical).status_code == 404
        # Regression (W5 #9): every spelling that authenticates shares the bucket.
        variant = {"Authorization": template.format(k=edge_user[0])}
        assert_envelope(
            client.get(self.URL, headers=variant), 429, "RateLimitExceeded", "per 60 minutes"
        )

    def test_disabled_rate_limit_allows_burst(self, client, headers, rate_limits):
        rate_limits(rate_limit_enabled=False, rate_limit_per_minute=1, rate_limit_per_hour=1)
        assert {client.get(self.URL, headers=headers).status_code for _ in range(5)} == {404}


# ---------------------------------------------------------------------------
# One-shot report retrieval
# ---------------------------------------------------------------------------


class TestOneShotReport:
    async def test_double_fetch_second_is_410_without_content(
        self, client, db_session, mock_redis, edge_user, headers
    ):
        report_id, ref_key = await _create_report(db_session, mock_redis, edge_user[1])
        first = client.get(f"/v1/security/reports/{report_id}", headers=headers)
        assert first.status_code == 200, first.text
        assert first.json()["pdf_base64"] == b64(b"%PDF-1.4")
        assert await mock_redis.exists(ref_key) == 0  # deleted after delivery

        second = client.get(f"/v1/security/reports/{report_id}", headers=headers)
        body = assert_envelope(
            second, 410, "Gone", "Report already retrieved. URL is no longer valid."
        )
        assert "report" not in body and "pdf_base64" not in body

    @requires_postgres
    async def test_concurrent_double_fetch_exactly_one_wins(
        self, async_client, db_session, mock_redis, edge_user, headers
    ):
        report_id, _ = await _create_report(db_session, mock_redis, edge_user[1])
        url = f"/v1/security/reports/{report_id}"
        responses = await asyncio.gather(
            *(async_client.get(url, headers=headers) for _ in range(4))
        )
        codes = sorted(r.status_code for r in responses)
        assert codes == [200, 410, 410, 410], [r.text for r in responses]

    @pytest.mark.parametrize("cause", ["ttl-expired", "no-ref-key", "undecryptable"])
    async def test_unavailable_report_is_410_and_not_consumed(
        self, client, db_session, mock_redis, edge_user, headers, cause
    ):
        """Lead decision (W4/W5): a report that is no longer in the buffer is not
        fabricated as an empty "0 findings" report and not marked delivered."""
        report_id, ref_key = await _create_report(db_session, mock_redis, edge_user[1])
        job_id = await _create_job(db_session, edge_user[1])
        await db_session.execute(
            update(JobMetadata).where(JobMetadata.job_id == job_id).values(report_id=report_id)
        )
        if cause == "ttl-expired":
            await mock_redis.delete(ref_key)
        elif cause == "no-ref-key":
            await db_session.execute(
                update(ReportMetadata)
                .where(ReportMetadata.report_id == report_id)
                .values(report_ref_key=None)
            )
        else:
            mock_redis._store[ref_key] = b"not-a-fernet-token"
        await db_session.commit()

        for _ in range(2):  # stays 410 with the same message, no 200 ever
            resp = client.get(f"/v1/security/reports/{report_id}", headers=headers)
            body = assert_envelope(resp, 410, "Gone", UNAVAILABLE)
            assert "report" not in body
            assert "X-One-Shot" not in resp.headers

        db_session.expire_all()
        report = await db_session.get(ReportMetadata, report_id)
        job = await db_session.get(JobMetadata, job_id)
        assert (report.is_retrieved, report.retrieved_at) == (False, None)
        assert (job.delivery_status, job.delivered_at) == ("pending", None)

    async def test_redis_outage_is_503_and_report_stays_retrievable(
        self, client, db_session, mock_redis, edge_user, headers
    ):
        from redis.exceptions import ConnectionError as RedisConnectionError

        report_id, _ = await _create_report(db_session, mock_redis, edge_user[1])
        real_get = mock_redis.get
        mock_redis.get = AsyncMock(side_effect=RedisConnectionError("down"))
        resp = client.get(f"/v1/security/reports/{report_id}", headers=headers)
        assert_envelope(resp, 503, "ServiceUnavailable", "Cache service is temporarily unavailable")

        mock_redis.get = real_get
        resp = client.get(f"/v1/security/reports/{report_id}", headers=headers)
        assert resp.status_code == 200, resp.text
        assert resp.headers["X-One-Shot"] == "true"


# ---------------------------------------------------------------------------
# Error envelope & no script-content echo
# ---------------------------------------------------------------------------

MARKER = b"GEHEIMES-DREHBUCH-EXPLOSION-SZENE-42"
MARKER_FDX = (
    b"<FinalDraft><Content><Paragraph><Text>"
    + MARKER
    + b"</Text></Paragraph></Content></FinalDraft>"
)


def _marker_payload(**overrides: object) -> dict:
    return payload(script_content=b64(MARKER_FDX), **overrides)


ERROR_CASES = {
    "400-empty-upload": (
        lambda c, h: c.post(ASYNC, files={"file": ("s.fdx", b"", "application/xml")}, headers=h),
        400,
        "BadRequest",
    ),
    "400-unsupported-upload": (
        lambda c, h: c.post(ASYNC, files={"file": ("s.doc", MARKER_FDX, "text/plain")}, headers=h),
        400,
        "BadRequest",
    ),
    "401-no-auth": (lambda c, h: c.post(SYNC, json=_marker_payload()), 401, "Unauthorized"),
    "401-bad-key": (
        lambda c, h: c.post(SYNC, json=_marker_payload(), headers={"Authorization": "Bearer x"}),
        401,
        "Unauthorized",
    ),
    "403-actor": (
        lambda c, h: c.post(SYNC, json=_marker_payload(), headers={**h, "X-Actor-User-Id": "x"}),
        403,
        "Forbidden",
    ),
    "404-job": (lambda c, h: c.get(f"/v1/security/jobs/{uuid4()}", headers=h), 404, "NotFound"),
    "422-project": (
        lambda c, h: c.post(SYNC, json=_marker_payload(project_id="bad id"), headers=h),
        422,
        "ValidationError",
    ),
    "422-format": (
        lambda c, h: c.post(SYNC, json=_marker_payload(script_format="docx"), headers=h),
        422,
        "ValidationError",
    ),
    "422-priority": (
        lambda c, h: c.post(ASYNC, json=_marker_payload(priority=99), headers=h),
        422,
        "ValidationError",
    ),
    "422-callback": (
        lambda c, h: c.post(SYNC, json=_marker_payload(callback_url="https://x.y"), headers=h),
        422,
        "ValidationError",
    ),
    "422-raw-content": (
        lambda c, h: c.post(SYNC, json=payload(script_content=MARKER.decode() + "!"), headers=h),
        422,
        "ValidationError",
    ),
    "422-content-as-pdf": (
        lambda c, h: c.post(SYNC, json=_marker_payload(script_format="pdf"), headers=h),
        422,
        "ValidationError",
    ),
    "422-path": (lambda c, h: c.get("/v1/security/jobs/nope", headers=h), 422, "ValidationError"),
}


class TestErrorEnvelope:
    @pytest.mark.parametrize("case", list(ERROR_CASES))
    def test_envelope_and_no_script_echo(self, client, headers, case):
        make, status, error = ERROR_CASES[case]
        resp = make(client, headers)
        assert_envelope(resp, status, error)
        assert MARKER.decode() not in resp.text
        assert b64(MARKER_FDX) not in resp.text

    async def test_workflow_start_failure_is_503_envelope(
        self, client, headers, mock_temporal, mock_redis
    ):
        mock_temporal.start_workflow = AsyncMock(side_effect=RuntimeError(MARKER.decode()))
        resp = client.post(ASYNC, json=_marker_payload(), headers=headers)
        assert_envelope(
            resp,
            503,
            "ServiceUnavailableException",
            "Security check could not be started. The workflow engine is temporarily unavailable.",
        )
        assert MARKER.decode() not in resp.text
        assert await mock_redis.keys("eki:buf:") == []  # buffer cleaned up

    @pytest.mark.parametrize(
        ("method", "url", "kwargs", "status", "error", "message"),
        [
            ("get", "/v1/does-not-exist", {}, 404, "NotFound", "Not Found"),
            ("put", SYNC, {}, 405, "MethodNotAllowed", "Method Not Allowed"),
            ("get", SYNC, {}, 405, "MethodNotAllowed", "Method Not Allowed"),
            (
                "post",
                ASYNC,
                {"content": b"x", "headers": {"Content-Type": "multipart/form-data"}},
                400,
                "BadRequest",
                "Missing boundary in multipart.",
            ),
        ],
        ids=["unknown-route", "put", "get-on-post", "multipart-no-boundary"],
    )
    def test_framework_errors_use_envelope(
        self, client, headers, method, url, kwargs, status, error, message
    ):
        # Regression (W5 #10): Starlette-raised errors returned {"detail": ...}.
        hdrs = {**headers, **kwargs.pop("headers", {})}
        resp = getattr(client, method)(url, headers=hdrs, **kwargs)
        body = assert_envelope(resp, status, error, message)
        assert body["request_id"] == resp.headers["X-Request-ID"]
        if status == 405:
            assert resp.headers["Allow"] == "POST"


# ---------------------------------------------------------------------------
# Ops (admin) query parameters
# ---------------------------------------------------------------------------


class TestOpsQueryBounds:
    @pytest.mark.parametrize(
        ("query", "status"),
        [
            ("limit=1", 200),
            ("limit=500", 200),
            ("limit=0", 422),
            ("limit=501", 422),
            ("offset=0", 200),
            ("offset=-1", 422),
            (f"offset={2**63 - 1}", 200),
            ("project_id=" + "x" * 255, 200),
            ("project_id=" + "x" * 256, 422),
            ("status=pending", 200),
            ("status=PENDING", 422),
            ("delivery_status=bogus", 422),
        ],
    )
    def test_jobs_listing_bounds(self, client, admin_headers, query, status):
        resp = client.get(f"/v1/ops/jobs?{query}", headers=admin_headers)
        if status == 200:
            assert resp.status_code == 200, resp.text
        else:
            assert_envelope(resp, 422, "ValidationError")

    @pytest.mark.parametrize("path", ["/v1/ops/jobs", "/v1/ops/dead-letters"])
    @pytest.mark.parametrize("offset", [2**63, 10**30])
    def test_offset_beyond_bigint_is_422(self, client, admin_headers, path, offset):
        # Regression (W5 #11): used to reach the driver -> 503 (PG) / 500 (SQLite).
        resp = client.get(f"{path}?offset={offset}", headers=admin_headers)
        body = assert_envelope(resp, 422, "ValidationError", "Request validation failed")
        assert body["details"][0]["field"] == "query.offset"
        assert "less than or equal to 9223372036854775807" in body["details"][0]["message"]
