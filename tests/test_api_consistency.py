"""Consistency regressions found during the 1.0 handover review.

* error bodies carry the request id the middleware generated (was ``null``
  unless the client sent ``X-Request-ID``),
* the API connects to the same Temporal namespace as the worker,
* reports state the real engine version (was hard-coded ``0.6.0``).
"""

from __future__ import annotations

from unittest.mock import AsyncMock, patch
from uuid import uuid4

import pytest
from fastapi import status

from api import dependencies
from api.config import get_settings
from api.dependencies import get_db
from api.main import app
from core import metrics
from core.version import __version__
from services.report_generator import build_report_dict


class TestErrorBodyRequestId:
    def test_generated_request_id_is_in_header_and_body(self, client):
        response = client.get(f"/v1/security/jobs/{uuid4()}")  # no auth -> 401

        assert response.status_code == status.HTTP_401_UNAUTHORIZED
        header_id = response.headers["X-Request-ID"]
        assert header_id
        assert response.json()["request_id"] == header_id

    def test_client_request_id_is_echoed(self, client, auth_headers):
        response = client.get(
            f"/v1/security/jobs/{uuid4()}",
            headers={**auth_headers, "X-Request-ID": "epro-trace-4711"},
        )

        assert response.status_code == status.HTTP_404_NOT_FOUND
        assert response.headers["X-Request-ID"] == "epro-trace-4711"
        assert response.json()["request_id"] == "epro-trace-4711"

    @pytest.mark.parametrize(
        ("exc", "expected_status"),
        [
            (RuntimeError("boom"), status.HTTP_500_INTERNAL_SERVER_ERROR),
            (ConnectionRefusedError("db down"), status.HTTP_503_SERVICE_UNAVAILABLE),
        ],
        ids=["unexpected-500", "infrastructure-503"],
    )
    def test_unhandled_error_carries_request_id_and_metric_status(
        self, client, exc, expected_status
    ):
        """Unhandled errors used to be rendered outside the request-id middleware:
        no header, ``request_id: null`` and the metrics counted every 503 as 500."""

        async def broken_db():
            raise exc
            yield  # pragma: no cover - makes this an async generator

        app.dependency_overrides[get_db] = broken_db
        before = metrics.HTTP_REQUESTS_TOTAL.labels(
            method="GET", route="/v1/security/jobs/{job_id}", status=str(expected_status)
        )._value.get()

        response = client.get(
            f"/v1/security/jobs/{uuid4()}", headers={"Authorization": "Bearer eki_x"}
        )

        assert response.status_code == expected_status
        header_id = response.headers["X-Request-ID"]
        assert response.json()["request_id"] == header_id
        assert "boom" not in response.text and "db down" not in response.text
        after = metrics.HTTP_REQUESTS_TOTAL.labels(
            method="GET", route="/v1/security/jobs/{job_id}", status=str(expected_status)
        )._value.get()
        assert after == before + 1

    def test_validation_error_carries_request_id(self, client, auth_headers):
        response = client.get("/v1/security/jobs/not-a-uuid", headers=auth_headers)

        assert response.status_code == status.HTTP_422_UNPROCESSABLE_ENTITY
        assert response.json()["request_id"] == response.headers["X-Request-ID"]


class TestTemporalNamespace:
    @pytest.fixture
    def custom_namespace(self, monkeypatch):
        monkeypatch.setenv("TEMPORAL_NAMESPACE", "eki-prod")
        get_settings.cache_clear()
        yield "eki-prod"
        get_settings.cache_clear()

    @pytest.mark.asyncio
    async def test_api_client_uses_configured_namespace(self, custom_namespace):
        connect = AsyncMock(return_value=object())
        with patch.object(dependencies.TemporalClient, "connect", connect):
            gen = dependencies.get_temporal_client()
            await gen.__anext__()
            await gen.aclose()

        assert connect.await_args.kwargs["namespace"] == custom_namespace


class TestReportEngineVersion:
    def test_report_metadata_uses_package_version(self):
        report = build_report_dict(
            report_id=str(uuid4()),
            project_id="p",
            script_format="fdx",
            findings=[],
            processing_time_seconds=0.1,
        )

        assert report["metadata"]["engine_version"] == __version__
