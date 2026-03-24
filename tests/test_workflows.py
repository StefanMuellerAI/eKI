"""Tests for Temporal workflows and activities.

Activities depend on Redis (SecureBuffer), Postgres, and LLM providers.
These tests mock external dependencies to verify activity logic and workflow
orchestration in isolation.
"""

import base64
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch
from uuid import uuid4

import pytest

from workflows.activities import (
    deliver_report_activity,
    update_job_status_activity,
)


# ===================================================================
# Helpers
# ===================================================================


def _make_report_package(
    *,
    project_id: str = "75",
    n_critical: int = 1,
    n_high: int = 0,
    n_low: int = 1,
) -> dict[str, Any]:
    """Build a realistic report_package (as stored in Redis by aggregate_report)."""
    findings: list[dict[str, Any]] = []
    for i in range(n_critical):
        findings.append({
            "id": str(uuid4()), "scene_number": str(i + 1),
            "risk_level": "critical", "category": "PHYSICAL",
            "risk_class": "FIRE", "rule_id": "SEC-P-008",
            "likelihood": 5, "impact": 5,
            "description": f"Critical finding {i}",
            "recommendation": "Immediate action required",
            "measures": [{"code": "FIRE-DEPT", "title": "Feuerwehr", "responsible": "Prod", "due": "1d"}],
            "confidence": 0.95, "evidence": "flames visible",
        })
    for i in range(n_high):
        findings.append({
            "id": str(uuid4()), "scene_number": str(n_critical + i + 1),
            "risk_level": "high", "category": "PHYSICAL",
            "risk_class": "HEIGHT", "rule_id": "SEC-P-006",
            "likelihood": 4, "impact": 4,
            "description": f"High finding {i}",
            "recommendation": "Safety harness", "measures": [],
            "confidence": 0.85, "evidence": "cliff scene",
        })
    for i in range(n_low):
        findings.append({
            "id": str(uuid4()), "scene_number": str(n_critical + n_high + i + 1),
            "risk_level": "low", "category": "ENVIRONMENTAL",
            "risk_class": "NOISE", "rule_id": "SEC-E-004",
            "likelihood": 1, "impact": 1,
            "description": f"Low finding {i}",
            "recommendation": "Ear protection", "measures": [],
            "confidence": 0.7, "evidence": "loud machinery",
        })

    risk_summary = {"critical": n_critical, "high": n_high, "medium": 0, "low": n_low, "info": 0}
    report = {
        "report_id": str(uuid4()),
        "project_id": project_id,
        "script_format": "fdx",
        "created_at": "2026-03-24T12:00:00",
        "risk_summary": risk_summary,
        "total_findings": len(findings),
        "findings": findings,
        "processing_time_seconds": 10.0,
        "metadata": {"engine_version": "0.5.0", "taxonomy_version": "1.0"},
    }

    pdf_bytes = b"%PDF-1.4 fake test pdf content"
    pdf_b64 = base64.b64encode(pdf_bytes).decode()

    return {"report": report, "pdf_base64": pdf_b64}


def _mock_buffer(report_package: dict[str, Any] | None = None):
    """Return a mocked SecureBuffer that returns the given report_package."""
    buf = AsyncMock()
    buf.retrieve = AsyncMock(return_value=report_package or {})
    buf.store = AsyncMock(return_value="eki:buf:mock-ref-key")
    buf.delete = AsyncMock()
    return buf


def _mock_settings(**overrides):
    """Return a mocked Settings object with sensible defaults."""
    s = MagicMock()
    s.epro_base_url = "https://staging.epro.filmakademie.de/api"
    s.epro_auth_token = ""
    s.epro_timeout = 30
    s.database_url = "postgresql+asyncpg://fake:fake@localhost/fake"
    s.redis_url = "redis://localhost:6379/0"
    s.api_secret_key = "test-secret-key-for-unit-tests-only"
    s.buffer_ttl_seconds = 3600
    for k, v in overrides.items():
        setattr(s, k, v)
    return s


# ===================================================================
# update_job_status_activity
# ===================================================================


@pytest.mark.asyncio
class TestUpdateJobStatusActivity:
    """Tests for update_job_status_activity."""

    async def test_returns_false_for_missing_job_id(self):
        result = await update_job_status_activity({"status": "running"})
        assert result["updated"] is False
        assert "missing" in result["reason"]

    async def test_returns_false_for_missing_status(self):
        result = await update_job_status_activity({"job_id": str(uuid4())})
        assert result["updated"] is False
        assert "missing" in result["reason"]


# ===================================================================
# deliver_report_activity — Pull mode
# ===================================================================


@pytest.mark.asyncio
class TestDeliverReportPullMode:
    """Tests for deliver_report_activity in pull mode (report stays in Redis)."""

    @patch("workflows.activities._get_buffer")
    async def test_pull_mode_returns_report_url(self, mock_get_buffer):
        buf = _mock_buffer()
        mock_get_buffer.return_value = buf

        report_id = str(uuid4())
        report_data = {
            "report_ref_key": "eki:buf:test",
            "report_id": report_id,
            "total_findings": 3,
        }
        delivery_config = {
            "delivery_mode": "pull",
            "job_id": str(uuid4()),
            "project_id": "75",
            "user_id": "testuser",
            "script_format": "fdx",
        }

        result = await deliver_report_activity(report_data, delivery_config)

        assert result["delivered"] is True
        assert result["delivery_mode"] == "pull"
        assert report_id in result["report_url"]


# ===================================================================
# deliver_report_activity — Push mode
# ===================================================================


def _setup_push_mocks(report_package, *, response_json=None, post_side_effect=None, settings_overrides=None):
    """Create all mocks needed for push-mode tests. Returns (patches_dict, mock_http_client)."""
    buf = _mock_buffer(report_package)

    mock_response = MagicMock()
    mock_response.status_code = 200
    mock_response.json.return_value = response_json or {"status": True, "message": "ok"}
    mock_response.raise_for_status = MagicMock()

    mock_http_client = AsyncMock()
    mock_http_client.__aenter__ = AsyncMock(return_value=mock_http_client)
    mock_http_client.__aexit__ = AsyncMock(return_value=False)
    if post_side_effect:
        mock_http_client.post = AsyncMock(side_effect=post_side_effect)
    else:
        mock_http_client.post = AsyncMock(return_value=mock_response)

    settings = _mock_settings(**(settings_overrides or {}))

    patches = {
        "buf": patch("workflows.activities._get_buffer", return_value=buf),
        "httpx": patch("httpx.AsyncClient", return_value=mock_http_client),
        "settings": patch("api.config.get_settings", return_value=settings),
    }
    return patches, mock_http_client, buf


@pytest.mark.asyncio
class TestDeliverReportPushMode:
    """Tests for deliver_report_activity in push mode (POST to ePro)."""

    async def test_push_sends_multipart_to_epro(self):
        """Verify the push block sends multipart form-data to the correct URL."""
        report_package = _make_report_package(project_id="75", n_critical=1)
        patches, mock_http, buf = _setup_push_mocks(
            report_package,
            response_json={"status": True, "message": "Risk assessment persisted (42)"},
        )

        with patches["buf"], patches["httpx"], patches["settings"]:
            result = await deliver_report_activity(
                {"report_ref_key": "eki:buf:test", "report_id": str(uuid4()), "total_findings": 2},
                {"delivery_mode": "push", "job_id": str(uuid4()), "project_id": "75",
                 "user_id": "testuser", "script_format": "fdx", "script_id": 123},
            )

        assert result["delivered"] is True
        assert result["delivery_mode"] == "push"
        assert result["epro_status"] == 1

        call_args = mock_http.post.call_args
        url = call_args.args[0] if call_args.args else call_args.kwargs.get("url", "")
        assert "/eki/scl/set-risk-assessment/75" in url

        form_data = call_args.kwargs.get("data", {})
        assert form_data["script_id"] == "123"
        assert form_data["status"] == "1"
        assert len(form_data["assessment"]) > 0

        files = call_args.kwargs.get("files", {})
        assert "file" in files
        filename, pdf_bytes, content_type = files["file"]
        assert filename.endswith(".pdf")
        assert content_type == "application/pdf"
        assert pdf_bytes[:5] == b"%PDF-"

        buf.delete.assert_awaited_once()

    async def test_push_script_id_defaults_to_negative_one(self):
        """When script_id is None, it should default to -1 for ePro."""
        report_package = _make_report_package(n_critical=0, n_high=0, n_low=1)
        patches, mock_http, _ = _setup_push_mocks(report_package)

        with patches["buf"], patches["httpx"], patches["settings"]:
            result = await deliver_report_activity(
                {"report_ref_key": "eki:buf:x", "report_id": str(uuid4()), "total_findings": 1},
                {"delivery_mode": "push", "job_id": str(uuid4()), "project_id": "99",
                 "user_id": "u", "script_format": "fdx"},
            )

        assert result["delivered"] is True
        assert result["epro_status"] == 0

        form_data = mock_http.post.call_args.kwargs.get("data", {})
        assert form_data["script_id"] == "-1"
        assert form_data["status"] == "0"

    async def test_push_failure_returns_error(self):
        """When ePro returns an error, delivery should fail gracefully."""
        report_package = _make_report_package()
        patches, _, buf = _setup_push_mocks(
            report_package, post_side_effect=ConnectionError("ePro unreachable"),
        )

        with patches["buf"], patches["httpx"], patches["settings"]:
            result = await deliver_report_activity(
                {"report_ref_key": "eki:buf:x", "report_id": str(uuid4()), "total_findings": 1},
                {"delivery_mode": "push", "job_id": str(uuid4()), "project_id": "75",
                 "user_id": "u", "script_format": "fdx", "script_id": 10},
            )

        assert result["delivered"] is False
        assert result["delivery_mode"] == "push"
        assert "error" in result
        buf.delete.assert_not_awaited()

    async def test_push_no_auth_header_when_token_empty(self):
        """With empty epro_auth_token, no Authorization header should be sent."""
        report_package = _make_report_package()
        patches, mock_http, _ = _setup_push_mocks(
            report_package, settings_overrides={"epro_auth_token": ""},
        )

        with patches["buf"], patches["httpx"], patches["settings"]:
            await deliver_report_activity(
                {"report_ref_key": "eki:buf:x", "report_id": str(uuid4()), "total_findings": 1},
                {"delivery_mode": "push", "job_id": str(uuid4()), "project_id": "75",
                 "user_id": "u", "script_format": "fdx"},
            )

        headers = mock_http.post.call_args.kwargs.get("headers")
        assert headers is None or "Authorization" not in (headers or {})

    async def test_push_sends_auth_header_when_token_set(self):
        """When epro_auth_token is configured, Authorization header should be present."""
        report_package = _make_report_package()
        patches, mock_http, _ = _setup_push_mocks(
            report_package, settings_overrides={"epro_auth_token": "secret-token-123"},
        )

        with patches["buf"], patches["httpx"], patches["settings"]:
            await deliver_report_activity(
                {"report_ref_key": "eki:buf:x", "report_id": str(uuid4()), "total_findings": 1},
                {"delivery_mode": "push", "job_id": str(uuid4()), "project_id": "75",
                 "user_id": "u", "script_format": "fdx"},
            )

        headers = mock_http.post.call_args.kwargs.get("headers")
        assert headers is not None
        assert headers["Authorization"] == "Bearer secret-token-123"
