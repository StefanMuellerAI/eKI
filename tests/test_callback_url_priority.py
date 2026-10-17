"""1.0.1 Decision 10b -- ``callback_url`` push target and Temporal ``priority``.

* ``core/callback_url.py``: static policy, address policy, DNS check.
* Settings: ``CALLBACK_URL_ALLOWED_HOSTS``, ``TEMPORAL_PRIORITY_LEVELS``.
* API: priority -> ``start_workflow(priority=Priority(...))``, callback_url in job_data.
* ``deliver_report_activity``: push to the callback URL with the default push's
  payload/headers/token, re-validation at delivery time, DNS-rebinding defence
  (connection pinned to the validated address; TLS still verified against the
  host name -- proven with a real local TLS server).
* ``SecurityCheckWorkflow``: callback_url reaches the activity; a delivery-time
  policy violation ends in the failure branch with ``invalid_callback_url``.

No real network: DNS is injected (``activities._resolve_callback_host``), HTTP
goes to mocks or to a TLS server on 127.0.0.1.
"""

from __future__ import annotations

import datetime as dt
import ipaddress
import json
import socket
import ssl
import threading
import uuid
from collections.abc import Iterator
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import httpx
import pytest
from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.x509.oid import NameOID
from pydantic import ValidationError
from temporalio import activity, workflow
from temporalio.common import Priority
from temporalio.exceptions import ApplicationError
from temporalio.testing import WorkflowEnvironment
from temporalio.worker import Worker

import core.callback_url as cb
from api.config import Settings, get_settings
from api.routers.security import temporal_priority_key
from core.models import AsyncSecurityCheckRequest
from workflows import activities
from workflows.activities import ERROR_TYPE_INVALID_CALLBACK_URL, deliver_report_activity

with workflow.unsafe.imports_passed_through():
    from workflows.security_check import SecurityCheckWorkflow

FDX = b'<?xml version="1.0" encoding="UTF-8"?><FinalDraft><Content/></FinalDraft>'
CALLBACK = "https://epro.filmakademie.de/api/eki/scl/set-risk-assessment/cb-77?src=eki"
PUBLIC_V4 = "93.184.216.34"  # never contacted: transport is mocked or redirected
PUBLIC_V6 = "2606:2800:220:1:248:1893:25c8:1946"


def _b64(data: bytes) -> str:
    import base64

    return base64.b64encode(data).decode("ascii")


# ===========================================================================
# core/callback_url.py -- pure policy
# ===========================================================================


class TestAddressPolicy:
    @pytest.mark.parametrize(
        "address",
        [
            "10.1.2.3",
            "172.16.0.1",
            "192.168.1.1",
            "127.0.0.1",
            "169.254.169.254",
            "100.64.0.1",  # CGNAT: not "private", not global either
            "192.0.2.10",  # documentation
            "0.0.0.0",
            "224.0.0.1",
            "240.0.0.1",
            "255.255.255.255",
            "::1",
            "::",
            "fe80::1",
            "fc00::1",
            "ff02::1",
            "::ffff:127.0.0.1",
            "::ffff:10.0.0.1",
            "2002:7f00:1::1",  # 6to4 wrapping 127.0.0.1
            "2001:db8::1",
        ],
    )
    def test_forbidden(self, address):
        assert cb.is_forbidden_address(ipaddress.ip_address(address)) is True

    @pytest.mark.parametrize("address", [PUBLIC_V4, "8.8.8.8", PUBLIC_V6, "::ffff:93.184.216.34"])
    def test_public(self, address):
        assert cb.is_forbidden_address(ipaddress.ip_address(address)) is False


class TestStaticCheck:
    ALLOWED = ("epro.filmakademie.de", "STAGING.epro.filmakademie.de.")

    @pytest.mark.parametrize(
        ("url", "message"),
        [
            ("http://epro.filmakademie.de/cb", cb.MSG_HTTPS_ONLY),
            ("ftp://epro.filmakademie.de/cb", cb.MSG_HTTPS_ONLY),
            ("https:///no-host", cb.MSG_NO_HOST),
            ("https://10.0.0.1/cb", cb.MSG_PRIVATE_IP),
            ("https://[::1]/cb", cb.MSG_PRIVATE_IP),
            ("https://epro.filmakademie.de@127.0.0.1/cb", cb.MSG_PRIVATE_IP),
            ("https://user@epro.filmakademie.de/cb", cb.MSG_CREDENTIALS),
            ("https://:pw@epro.filmakademie.de/cb", cb.MSG_CREDENTIALS),
            ("https://evil.example/cb", cb.MSG_NOT_ALLOWED),
            ("https://epro.filmakademie.de.evil.example/cb", cb.MSG_NOT_ALLOWED),
            ("https://8.8.8.8/cb", cb.MSG_NOT_ALLOWED),
        ],
    )
    def test_rejected(self, url, message):
        with pytest.raises(cb.CallbackUrlError, match=message.replace(".", r"\.")):
            cb.check_callback_url(url, self.ALLOWED)

    def test_not_allowed_message_lists_the_normalised_allow_list(self):
        with pytest.raises(cb.CallbackUrlError) as exc_info:
            cb.check_callback_url("https://evil.example/", self.ALLOWED)
        assert str(exc_info.value) == (
            "Callback URL domain not allowed. Allowed: epro.filmakademie.de, "
            "staging.epro.filmakademie.de"
        )

    def test_empty_allow_list_rejects_everything(self):
        with pytest.raises(cb.CallbackUrlError, match=r"Allowed: \(none configured\)"):
            cb.check_callback_url("https://epro.filmakademie.de/", ["", "  "])

    @pytest.mark.parametrize(
        ("url", "host"),
        [
            ("https://epro.filmakademie.de/cb", "epro.filmakademie.de"),
            ("HTTPS://Staging.Epro.Filmakademie.De.:8443/x?y=1#f", "staging.epro.filmakademie.de"),
        ],
    )
    def test_allowed_returns_normalised_host(self, url, host):
        assert cb.check_callback_url(url, self.ALLOWED) == host

    def test_public_ip_literal_can_be_allow_listed_explicitly(self):
        assert cb.check_callback_url(f"https://{PUBLIC_V4}/cb", [PUBLIC_V4]) == PUBLIC_V4


@pytest.mark.asyncio
class TestResolution:
    @staticmethod
    def _resolver(*answers: str, exc: Exception | None = None) -> AsyncMock:
        return AsyncMock(side_effect=exc) if exc else AsyncMock(return_value=list(answers))

    async def test_all_public_answers_are_returned_normalised(self):
        resolver = self._resolver(PUBLIC_V4, f"{PUBLIC_V6}%eth0", "2606:2800:0220:0001::1")
        got = await cb.resolve_public_addresses("epro.filmakademie.de", 443, resolver)
        assert got == [PUBLIC_V4, PUBLIC_V6, "2606:2800:220:1::1"]
        resolver.assert_awaited_once_with("epro.filmakademie.de", 443)

    @pytest.mark.parametrize(
        "answers",
        [["10.0.0.5"], [PUBLIC_V4, "127.0.0.1"], ["::ffff:192.168.0.1"], ["not-an-ip"]],
        ids=["private", "mixed", "mapped-private", "garbage"],
    )
    async def test_any_non_public_answer_is_a_policy_violation(self, answers):
        with pytest.raises(cb.CallbackUrlError, match="resolves to a private/internal"):
            await cb.resolve_public_addresses("h", 443, self._resolver(*answers))

    async def test_empty_answer_is_transient(self):
        with pytest.raises(OSError, match="no addresses"):
            await cb.resolve_public_addresses("h", 443, self._resolver())

    async def test_resolver_errors_propagate_as_transient(self):
        with pytest.raises(socket.gaierror):
            await cb.resolve_public_addresses(
                "h", 443, self._resolver(exc=socket.gaierror(-2, "Name or service not known"))
            )

    async def test_system_resolver_on_an_ip_literal_needs_no_dns(self):
        assert await cb.system_resolver("127.0.0.1", 443) == ["127.0.0.1"]


# ===========================================================================
# Settings
# ===========================================================================


class TestSettings:
    def test_defaults(self):
        settings = Settings(_env_file=None)
        assert settings.callback_url_allowed_hosts == [
            "epro.filmakademie.de",
            "staging.epro.filmakademie.de",
        ]
        assert settings.temporal_priority_levels == 10

    @pytest.mark.parametrize(
        ("raw", "expected"),
        [
            (" EPRO.Example.org. , ,b.example ", ["b.example", "epro.example.org"]),
            ('["A.example", "a.example."]', ["a.example"]),
            ("", []),
        ],
        ids=["csv", "json", "empty"],
    )
    def test_allowed_hosts_from_env(self, monkeypatch, raw, expected):
        monkeypatch.setenv("CALLBACK_URL_ALLOWED_HOSTS", raw)
        assert Settings(_env_file=None).callback_url_allowed_hosts == expected

    def test_allowed_hosts_invalid_json_is_rejected(self, monkeypatch):
        monkeypatch.setenv("CALLBACK_URL_ALLOWED_HOSTS", '["epro.example.org"')
        with pytest.raises(ValidationError, match="callback_url_allowed_hosts"):
            Settings(_env_file=None)

    def test_allowed_hosts_accepts_python_collections(self):
        settings = Settings(_env_file=None, callback_url_allowed_hosts=("B.example", "a.example."))
        assert settings.callback_url_allowed_hosts == ["a.example", "b.example"]

    def test_allowed_hosts_must_be_a_list(self):
        with pytest.raises(ValidationError, match="callback_url_allowed_hosts"):
            Settings(_env_file=None, callback_url_allowed_hosts=5)

    @pytest.mark.parametrize("value", [0, 101])
    def test_priority_levels_bounds(self, value):
        with pytest.raises(ValidationError):
            Settings(_env_file=None, temporal_priority_levels=value)


# ===========================================================================
# Priority mapping + API wiring
# ===========================================================================


class TestPriorityMapping:
    def test_identity_with_the_shipped_ten_levels(self):
        assert [temporal_priority_key(p, 10) for p in range(1, 11)] == list(range(1, 11))

    def test_default_five_levels_of_the_temporal_server(self):
        assert [temporal_priority_key(p, 5) for p in range(1, 11)] == [
            1, 1, 2, 2, 3, 3, 4, 4, 5, 5
        ]  # fmt: skip
        assert temporal_priority_key(5, 5) == (5 + 1) // 2  # API default == server default

    @pytest.mark.parametrize("levels", [1, 2, 3, 7, 20, 100])
    def test_monotonic_and_covers_both_ends(self, levels):
        keys = [temporal_priority_key(p, levels) for p in range(1, 11)]
        assert keys == sorted(keys)
        assert keys[0] == 1
        assert 1 <= keys[-1] <= levels
        if levels >= 10:
            assert len(set(keys)) == 10  # no two API priorities collapse

    def test_out_of_range_inputs_are_clamped(self):
        assert temporal_priority_key(0, 10) == 1
        assert temporal_priority_key(99, 10) == 10
        assert temporal_priority_key(5, 0) == 1


class TestApiWiring:
    @pytest.mark.parametrize(
        ("priority", "levels", "key"), [(1, 10, 1), (5, 10, 5), (10, 10, 10), (2, 5, 1)]
    )
    def test_json_priority_becomes_temporal_priority(
        self, client, auth_headers, mock_temporal, monkeypatch, priority, levels, key
    ):
        monkeypatch.setattr(get_settings(), "temporal_priority_levels", levels)
        body = {
            "script_content": _b64(FDX),
            "script_format": "fdx",
            "project_id": "prio",
            "priority": priority,
        }
        resp = client.post("/v1/security/check:async", json=body, headers=auth_headers)
        assert resp.status_code == 202, resp.text
        started = mock_temporal.started[-1]
        assert started["kwargs"]["priority"] == Priority(priority_key=key)
        assert started["args"][1]["priority"] == priority  # API value kept for audit

    def test_multipart_default_priority_is_five(self, client, auth_headers, mock_temporal):
        resp = client.post(
            "/v1/security/check:async",
            files={"file": ("s.fdx", FDX, "application/xml")},
            headers=auth_headers,
        )
        assert resp.status_code == 202, resp.text
        assert mock_temporal.started[-1]["kwargs"]["priority"] == Priority(priority_key=5)

    def test_multipart_callback_url_reaches_job_data(self, client, auth_headers, mock_temporal):
        resp = client.post(
            "/v1/security/check:async",
            files={"file": ("s.fdx", FDX, "application/xml")},
            data={"project_id": "cb-mp", "callback_url": CALLBACK, "priority": "1"},
            headers=auth_headers,
        )
        assert resp.status_code == 202, resp.text
        started = mock_temporal.started[-1]
        job_data = started["args"][1]
        assert (job_data["callback_url"], job_data["delivery_mode"]) == (CALLBACK, "push")
        assert started["kwargs"]["priority"] == Priority(priority_key=1)

    def test_document_limit_is_frozen_into_job_data(self, client, auth_headers, mock_temporal):
        resp = client.post(
            "/v1/security/check:async",
            json={"script_content": _b64(FDX), "script_format": "fdx", "project_id": "dl"},
            headers=auth_headers,
        )
        assert resp.status_code == 202, resp.text
        job_data = mock_temporal.started[-1]["args"][1]
        assert job_data["max_concurrent_documents"] == 1
        assert job_data["document_queue_timeout_seconds"] == 7200

    def test_non_mapping_input_is_left_to_pydantic(self):
        with pytest.raises(ValidationError, match="valid dictionary"):
            AsyncSecurityCheckRequest.model_validate(["not", "a", "dict"])

    def test_model_keeps_explicit_push_and_rejects_pull(self):
        base = {"script_content": _b64(FDX), "script_format": "fdx", "project_id": "p"}
        assert AsyncSecurityCheckRequest(**base, callback_url=CALLBACK).delivery == "push"
        assert AsyncSecurityCheckRequest(**base).delivery == "pull"
        with pytest.raises(ValidationError, match="contradicts callback_url"):
            AsyncSecurityCheckRequest(**base, callback_url=CALLBACK, delivery="PULL")


# ===========================================================================
# deliver_report_activity -- push to callback_url
# ===========================================================================


def _package() -> dict[str, Any]:
    return {
        "report": {
            "report_id": str(uuid.uuid4()),
            "project_id": "75",
            "findings": [],
            "risk_summary": {"critical": 0, "high": 0, "medium": 0, "low": 0, "info": 0},
            "total_findings": 0,
        },
        "pdf_base64": _b64(b"%PDF-1.4 callback"),
    }


def _settings(**overrides: Any) -> SimpleNamespace:
    base = {
        "epro_base_url": "https://epro.example/api",
        "epro_auth_token": "tok",
        "epro_timeout": 5,
        "redis_url": "redis://x",
        "api_secret_key": "s" * 32,
        "buffer_ttl_seconds": 21600,
        "database_url": "postgresql+asyncpg://x",
        "callback_url_allowed_hosts": ["epro.filmakademie.de"],
    }
    base.update(overrides)
    return SimpleNamespace(**base)


def _buffer() -> MagicMock:
    buf = MagicMock()
    buf.retrieve = AsyncMock(return_value=_package())
    buf.delete = AsyncMock(return_value=1)
    return buf


def _cfg(callback_url: str | None = CALLBACK, mode: str = "push") -> dict[str, Any]:
    return {
        "delivery_mode": mode,
        "job_id": "job-cb-1",
        "project_id": "75",
        "user_id": "u1",
        "script_format": "fdx",
        "script_id": 7,
        "callback_url": callback_url,
    }


def _form(request: httpx.Request) -> dict[str, bytes]:
    """Parse a multipart body into {field: payload} (boundary-independent)."""
    import email.parser
    import email.policy

    message = email.parser.BytesParser(policy=email.policy.HTTP).parsebytes(
        f"Content-Type: {request.headers['content-type']}\r\n\r\n".encode() + request.content
    )
    return {
        str(part.get_param("name", header="content-disposition")): part.get_payload(decode=True)
        for part in message.iter_parts()
    }


@pytest.fixture
def bookkeeping():
    with (
        patch.object(activities, "_mark_delivering", new=AsyncMock()) as mark,
        patch.object(activities, "_record_attempt", new=AsyncMock()) as record,
        patch.object(activities, "_current_attempt", return_value=3),
        patch.object(activities, "_get_buffer", side_effect=lambda: _buffer()),
    ):
        yield mark, record


@pytest.fixture
def epro_http(monkeypatch) -> list[httpx.Request]:
    """Every push goes through an httpx MockTransport answering 201."""
    seen: list[httpx.Request] = []

    def _handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return httpx.Response(201, json={"status": True, "message": "persisted"})

    monkeypatch.setattr(
        activities,
        "_push_http_client",
        lambda timeout: httpx.AsyncClient(timeout=timeout, transport=httpx.MockTransport(_handler)),
    )
    return seen


@pytest.fixture
def resolver(monkeypatch) -> AsyncMock:
    mock = AsyncMock(return_value=[PUBLIC_V4])
    monkeypatch.setattr(activities, "_resolve_callback_host", mock)
    return mock


@pytest.mark.asyncio
class TestCallbackPush:
    async def test_same_payload_headers_and_token_as_the_default_push(
        self, bookkeeping, epro_http, resolver
    ):
        mark, record = bookkeeping
        report = {"report_ref_key": "k", "report_id": "rep-1", "total_findings": 0}
        with patch("api.config.get_settings", return_value=_settings()):
            default = await deliver_report_activity(report, _cfg(callback_url=None))
            pushed = await deliver_report_activity(report, _cfg())

        assert default["delivered"] is pushed["delivered"] is True
        assert pushed["status_code"] == 201 and pushed["attempts_used"] == 3
        to_default, to_callback = epro_http
        resolver.assert_awaited_once_with("epro.filmakademie.de", 443)

        # Target: the callback URL itself, connection pinned to the validated IP.
        assert str(to_default.url) == "https://epro.example/api/eki/scl/set-risk-assessment/75"
        assert str(to_callback.url) == (
            f"https://{PUBLIC_V4}/api/eki/scl/set-risk-assessment/cb-77?src=eki"
        )
        assert to_callback.headers["host"] == "epro.filmakademie.de"
        assert to_callback.extensions["sni_hostname"] == "epro.filmakademie.de"
        assert "sni_hostname" not in to_default.extensions

        # Same headers (bearer token, idempotency, correlation) and same payload.
        for name in ("authorization", "idempotency-key", "x-eki-job-id", "x-eki-attempt"):
            assert to_callback.headers[name] == to_default.headers[name], name
        assert to_callback.headers["authorization"] == "Bearer tok"
        assert to_callback.headers["idempotency-key"] == "rep-1"
        assert _form(to_callback) == _form(to_default)
        assert set(_form(to_callback)) == {"script_id", "status", "assessment", "file"}

        assert [c.kwargs["status_code"] for c in record.await_args_list] == [201, 201]
        assert mark.await_count == 2

    async def test_port_and_ipv6_answer(self, bookkeeping, epro_http, resolver):
        resolver.return_value = [PUBLIC_V6, PUBLIC_V4]
        url = "https://EPRO.filmakademie.de:8443/cb?x=1"
        with patch("api.config.get_settings", return_value=_settings()):
            result = await deliver_report_activity(
                {"report_ref_key": "k", "report_id": "r"}, _cfg(url)
            )
        assert result["delivered"] is True
        (request,) = epro_http
        resolver.assert_awaited_once_with("epro.filmakademie.de", 8443)
        assert str(request.url) == f"https://[{PUBLIC_V6}]:8443/cb?x=1"
        assert request.headers["host"] == "epro.filmakademie.de:8443"
        assert request.extensions["sni_hostname"] == "epro.filmakademie.de"

    @pytest.mark.parametrize(
        ("url", "allowed", "reason"),
        [
            (CALLBACK, ["staging.epro.filmakademie.de"], "domain not allowed"),
            (CALLBACK, [], "(none configured)"),
            ("http://epro.filmakademie.de/cb", ["epro.filmakademie.de"], "must use HTTPS"),
            ("https://u:p@epro.filmakademie.de/cb", ["epro.filmakademie.de"], "credentials"),
        ],
        ids=["host-removed", "feature-disabled", "http", "credentials"],
    )
    async def test_delivery_time_policy_violation_is_final_and_sends_nothing(
        self, bookkeeping, epro_http, resolver, url, allowed, reason
    ):
        mark, record = bookkeeping
        settings = _settings(callback_url_allowed_hosts=allowed)
        with patch("api.config.get_settings", return_value=settings):
            with pytest.raises(ApplicationError) as exc_info:
                await deliver_report_activity({"report_ref_key": "k", "report_id": "r"}, _cfg(url))
        assert exc_info.value.type == ERROR_TYPE_INVALID_CALLBACK_URL
        assert exc_info.value.non_retryable is True
        assert reason in exc_info.value.message
        assert epro_http == []
        resolver.assert_not_awaited()
        mark.assert_not_awaited()
        record.assert_not_awaited()

    @pytest.mark.parametrize(
        "answers", [["10.0.0.7"], [PUBLIC_V4, "127.0.0.1"], ["169.254.169.254"]], ids=repr
    )
    async def test_private_dns_answer_is_final_and_sends_nothing(
        self, bookkeeping, epro_http, resolver, answers
    ):
        _, record = bookkeeping
        resolver.return_value = answers
        with patch("api.config.get_settings", return_value=_settings()):
            with pytest.raises(ApplicationError) as exc_info:
                await deliver_report_activity({"report_ref_key": "k", "report_id": "r"}, _cfg())
        assert exc_info.value.type == ERROR_TYPE_INVALID_CALLBACK_URL
        assert exc_info.value.non_retryable is True
        assert "resolves to a private/internal" in exc_info.value.message
        assert epro_http == []
        record.assert_not_awaited()

    async def test_dns_failure_is_transient_and_counted(self, bookkeeping, epro_http, resolver):
        from prometheus_client import REGISTRY

        _, record = bookkeeping
        resolver.side_effect = socket.gaierror(-3, "Temporary failure in name resolution")
        labels = {"mode": "push", "outcome": "transport_error"}
        before = REGISTRY.get_sample_value("eki_delivery_attempts_total", labels) or 0.0
        with patch("api.config.get_settings", return_value=_settings()):
            with pytest.raises(socket.gaierror):
                await deliver_report_activity({"report_ref_key": "k", "report_id": "r"}, _cfg())
        assert REGISTRY.get_sample_value("eki_delivery_attempts_total", labels) == before + 1
        assert record.await_args.kwargs["status_code"] is None
        assert record.await_args.kwargs["delivered"] is False
        assert epro_http == []

    async def test_pull_mode_ignores_a_callback_url(self, bookkeeping, epro_http, resolver):
        with patch(
            "api.config.get_settings", return_value=_settings(callback_url_allowed_hosts=[])
        ):
            result = await deliver_report_activity(
                {"report_ref_key": "k", "report_id": "r"}, _cfg(mode="pull")
            )
        assert result["delivery_mode"] == "pull" and result["delivered"] is True
        resolver.assert_not_awaited()
        assert epro_http == []


# ---------------------------------------------------------------------------
# Real TLS: pinning keeps certificate verification on the host name
# ---------------------------------------------------------------------------


def _issue(host: str) -> tuple[bytes, bytes, bytes]:
    """(ca_pem, leaf_cert_pem, leaf_key_pem) for ``host`` from a throwaway CA."""
    now = dt.datetime.now(dt.UTC)
    ca_key = ec.generate_private_key(ec.SECP256R1())
    ca_name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "eKI test CA")])
    ca = (
        x509.CertificateBuilder()
        .subject_name(ca_name)
        .issuer_name(ca_name)
        .public_key(ca_key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(now - dt.timedelta(minutes=5))
        .not_valid_after(now + dt.timedelta(hours=1))
        .add_extension(x509.BasicConstraints(ca=True, path_length=None), critical=True)
        .add_extension(
            x509.KeyUsage(
                digital_signature=True,
                content_commitment=False,
                key_encipherment=False,
                data_encipherment=False,
                key_agreement=False,
                key_cert_sign=True,
                crl_sign=True,
                encipher_only=False,
                decipher_only=False,
            ),
            critical=True,
        )
        .add_extension(
            x509.SubjectKeyIdentifier.from_public_key(ca_key.public_key()), critical=False
        )
        .sign(ca_key, hashes.SHA256())
    )
    leaf_key = ec.generate_private_key(ec.SECP256R1())
    leaf = (
        x509.CertificateBuilder()
        .subject_name(x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, host)]))
        .issuer_name(ca_name)
        .public_key(leaf_key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(now - dt.timedelta(minutes=5))
        .not_valid_after(now + dt.timedelta(hours=1))
        .add_extension(x509.SubjectAlternativeName([x509.DNSName(host)]), critical=False)
        .add_extension(x509.ExtendedKeyUsage([x509.oid.ExtendedKeyUsageOID.SERVER_AUTH]), False)
        .add_extension(
            x509.AuthorityKeyIdentifier.from_issuer_public_key(ca_key.public_key()), False
        )
        .sign(ca_key, hashes.SHA256())
    )
    pem = serialization.Encoding.PEM
    return (
        ca.public_bytes(pem),
        leaf.public_bytes(pem),
        leaf_key.private_bytes(
            pem, serialization.PrivateFormat.PKCS8, serialization.NoEncryption()
        ),
    )


class _TlsEpro:
    def __init__(self, tmp: Path, cert_host: str) -> None:
        self.ca_pem, cert_pem, key_pem = _issue(cert_host)
        (tmp / "leaf.pem").write_bytes(cert_pem)
        (tmp / "leaf.key").write_bytes(key_pem)
        self.requests: list[tuple[str, dict[str, str]]] = []
        self.sni: list[str | None] = []
        context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
        context.load_cert_chain(tmp / "leaf.pem", tmp / "leaf.key")
        context.sni_callback = lambda _sock, name, _ctx: self.sni.append(name)
        outer = self

        class _Handler(BaseHTTPRequestHandler):
            protocol_version = "HTTP/1.1"

            def do_POST(self) -> None:  # noqa: N802 - stdlib naming
                self.rfile.read(int(self.headers.get("Content-Length") or 0))
                outer.requests.append((self.path, {k.lower(): v for k, v in self.headers.items()}))
                body = json.dumps({"status": True, "message": "persisted"}).encode()
                self.send_response(201)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def log_message(self, format: str, *args: Any) -> None:  # noqa: A002
                return None

            def handle(self) -> None:
                try:
                    super().handle()
                except (ssl.SSLError, ConnectionResetError):  # client rejected our cert
                    pass

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), _Handler)
        self.server.socket = context.wrap_socket(self.server.socket, server_side=True)
        self.port = self.server.server_address[1]
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()

    def close(self) -> None:
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=5)


@pytest.fixture
def tls_epro(tmp_path) -> Iterator[Any]:
    servers: list[_TlsEpro] = []

    def _start(cert_host: str) -> _TlsEpro:
        servers.append(_TlsEpro(tmp_path, cert_host))
        return servers[-1]

    yield _start
    for server in servers:
        server.close()


@pytest.fixture
def pinned_to_loopback(monkeypatch):
    """Route the callback host to the local TLS server.

    The address policy is relaxed for 127.0.0.1 ONLY in these tests -- the
    production policy forbids loopback (see TestAddressPolicy) -- so that the
    real TLS handshake against the pinned address can be exercised offline.
    """
    real = cb.is_forbidden_address
    monkeypatch.setattr(cb, "is_forbidden_address", lambda ip: str(ip) != "127.0.0.1" and real(ip))
    monkeypatch.setattr(activities, "_resolve_callback_host", AsyncMock(return_value=["127.0.0.1"]))


def _trusting_client(ca_pem: bytes):
    context = ssl.create_default_context(cadata=ca_pem.decode("ascii"))
    return lambda timeout: httpx.AsyncClient(timeout=timeout, verify=context)


@pytest.mark.asyncio
class TestPinnedTls:
    async def test_tls_is_verified_against_the_callback_host_not_the_ip(
        self, bookkeeping, tls_epro, pinned_to_loopback, monkeypatch
    ):
        server = tls_epro("epro-callback.test")
        monkeypatch.setattr(activities, "_push_http_client", _trusting_client(server.ca_pem))
        url = f"https://epro-callback.test:{server.port}/api/eki/scl/set-risk-assessment/cb-1"
        settings = _settings(callback_url_allowed_hosts=["epro-callback.test"])
        with patch("api.config.get_settings", return_value=settings):
            result = await deliver_report_activity(
                {"report_ref_key": "k", "report_id": "r"}, _cfg(url)
            )

        assert result["delivered"] is True and result["status_code"] == 201
        ((path, headers),) = server.requests
        assert path == "/api/eki/scl/set-risk-assessment/cb-1"
        assert headers["host"] == f"epro-callback.test:{server.port}"
        assert headers["authorization"] == "Bearer tok"
        assert server.sni == ["epro-callback.test"]

    async def test_certificate_for_another_host_fails_the_handshake(
        self, bookkeeping, tls_epro, pinned_to_loopback, monkeypatch
    ):
        _, record = bookkeeping
        server = tls_epro("someone-else.test")
        monkeypatch.setattr(activities, "_push_http_client", _trusting_client(server.ca_pem))
        url = f"https://epro-callback.test:{server.port}/cb"
        settings = _settings(callback_url_allowed_hosts=["epro-callback.test"])
        with patch("api.config.get_settings", return_value=settings):
            with pytest.raises(httpx.ConnectError, match="CERTIFICATE_VERIFY_FAILED"):
                await deliver_report_activity({"report_ref_key": "k", "report_id": "r"}, _cfg(url))
        assert server.requests == []  # nothing (no token, no report) was sent
        assert record.await_args.kwargs["status_code"] is None  # transient -> Temporal retry


# ===========================================================================
# SecurityCheckWorkflow -- callback_url through the workflow, priority on start
# ===========================================================================


def _stub_activities(calls: dict[str, list[Any]], deliver_error: bool) -> list[Any]:
    def _add(name: str, payload: Any) -> None:
        calls.setdefault(name, []).append(payload)

    @activity.defn(name="parse_fdx")
    async def parse_fdx(job_data: dict) -> dict:
        return {"parsed_ref_key": "parsed", "total_scenes": 1, "total_characters": 1}

    @activity.defn(name="analyze_scene_risk")
    async def analyze(job_data: dict) -> dict:
        return {"scene_index": 0, "scene_number": "1", "findings": []}

    @activity.defn(name="aggregate_report")
    async def aggregate(job_data: dict, job_metadata: dict) -> dict:
        return {"report_ref_key": "report-ref", "report_id": job_metadata["report_id"]}

    @activity.defn(name="deliver_report")
    async def deliver(report_data: dict, delivery_config: dict) -> dict:
        _add("deliver_report", delivery_config)
        if deliver_error:
            raise ApplicationError(
                "callback_url rejected", type=ERROR_TYPE_INVALID_CALLBACK_URL, non_retryable=True
            )
        return {"delivered": True, "delivery_mode": "push", "status_code": 201, "attempts_used": 1}

    @activity.defn(name="update_job_status")
    async def update_status(payload: dict) -> dict:
        _add("update_job_status", payload)
        return {"updated": True}

    @activity.defn(name="cleanup_buffer")
    async def cleanup(payload: dict) -> dict:
        _add("cleanup_buffer", payload)
        return {"deleted": 1}

    @activity.defn(name="send_delivery_failed_webhook")
    async def webhook(payload: dict) -> dict:
        _add("send_delivery_failed_webhook", payload)
        return {"sent": True, "status_code": 200, "attempts_used": 1}

    @activity.defn(name="record_dead_letter")
    async def dead_letter(payload: dict) -> dict:
        _add("record_dead_letter", payload)
        return {"recorded": True, "dead_letter_id": str(uuid.uuid4())}

    @activity.defn(name="check_report_retrieved")
    async def check_retrieved(payload: dict) -> dict:
        return {"found": True, "retrieved": True}

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


def _job_data(**extra: Any) -> dict[str, Any]:
    return {
        "ref_key": "script-ref",
        "script_format": "fdx",
        "project_id": "75",
        "job_id": str(uuid.uuid4()),
        "report_id": str(uuid.uuid4()),
        "user_id": "u1",
        "priority": 2,
        "delivery_mode": "push",
        "metadata": {},
        "script_id": 7,
        "buffer_ttl_seconds": 21600,
        **extra,
    }


async def _run_workflow(
    job_data: dict[str, Any], *, deliver_error: bool = False, priority: Priority | None = None
) -> tuple[dict[str, Any], dict[str, list[Any]], Any]:
    calls: dict[str, list[Any]] = {}
    async with await WorkflowEnvironment.start_time_skipping() as env:
        queue = f"cb-{uuid.uuid4().hex}"
        async with Worker(
            env.client,
            task_queue=queue,
            workflows=[SecurityCheckWorkflow],
            activities=_stub_activities(calls, deliver_error),
        ):
            handle = await env.client.start_workflow(
                SecurityCheckWorkflow.run,
                job_data,
                id=job_data["job_id"],
                task_queue=queue,
                priority=priority or Priority.default,
            )
            result = await handle.result()
            history = await handle.fetch_history()
    return result, calls, history


@pytest.mark.temporal
@pytest.mark.asyncio
class TestWorkflow:
    async def test_callback_url_reaches_the_delivery_activity(self):
        result, calls, _ = await _run_workflow(_job_data(callback_url=CALLBACK))
        assert result["status"] == "completed" and result["delivered"] is True
        (config,) = calls["deliver_report"]
        assert config["callback_url"] == CALLBACK
        assert config["delivery_mode"] == "push"

    async def test_pre_1_0_1_job_data_falls_back_to_the_default_target(self):
        result, calls, _ = await _run_workflow(_job_data())  # no callback_url key at all
        assert result["status"] == "completed"
        assert calls["deliver_report"][0]["callback_url"] is None

    async def test_delivery_time_rejection_runs_the_failure_branch(self):
        result, calls, _ = await _run_workflow(_job_data(callback_url=CALLBACK), deliver_error=True)
        assert result["status"] == "delivery_failed"
        assert result["failure_reason"] == "invalid_callback_url"
        assert len(calls["deliver_report"]) == 1  # non-retryable
        assert calls["cleanup_buffer"][0]["ref_keys"] == ["report-ref"]
        (dead_letter,) = calls["record_dead_letter"]
        assert dead_letter["reason"] == "invalid_callback_url"
        assert dead_letter["delivery_mode"] == "push"
        assert calls["send_delivery_failed_webhook"][0]["reason"] == "invalid_callback_url"
        failed = [p for p in calls["update_job_status"] if p["status"] == "failed"]
        assert failed[0]["error_message"] == "delivery_failed:invalid_callback_url"

    async def test_priority_is_recorded_on_the_workflow_and_its_activities(self):
        """The time-skipping server stores the start priority; activities are
        scheduled without an own priority, i.e. they inherit the workflow's
        (server-side merge). Real dispatch ordering is verified separately
        against a Temporal dev server (see the round-2 notes)."""
        _, _, history = await _run_workflow(
            _job_data(callback_url=CALLBACK), priority=Priority(priority_key=2)
        )
        started = history.events[0].workflow_execution_started_event_attributes
        assert started.priority.priority_key == 2
        scheduled = [
            e.activity_task_scheduled_event_attributes
            for e in history.events
            if e.HasField("activity_task_scheduled_event_attributes")
        ]
        assert scheduled
        # No per-activity override (key 0 = unset): the workflow code is unchanged
        # by priority, the server applies the workflow's priority to its tasks.
        assert {s.priority.priority_key for s in scheduled} == {0}
