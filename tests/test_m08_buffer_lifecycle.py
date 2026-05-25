"""M08 -- Buffer-Lifecycle: Push und Pull loeschen Reportinhalte.

Pflichtenheft Abnahmetests 2 (Push) und 3 (Pull):
* Nach 2xx von ePro (Push) sind keine Inhalte mehr in der eKI.
* Nach erfolgreichem One-Shot-GET (Pull) wird der Report sofort
  geloescht; ein zweiter Abruf liefert 410.

Diese Tests benutzen FakeRedis-aehnliche Mocks. Sie pruefen die direkte
Wirkung der Activity bzw. des Endpoints auf die Buffer-Keys.
"""

import base64
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch
from uuid import uuid4

import pytest


def _push_report_package() -> dict[str, Any]:
    return {
        "report": {
            "report_id": str(uuid4()),
            "project_id": "75",
            "script_format": "fdx",
            "created_at": "2026-03-24T12:00:00",
            "risk_summary": {"critical": 0, "high": 0, "medium": 0, "low": 1, "info": 0},
            "total_findings": 1,
            "findings": [{
                "id": str(uuid4()), "scene_number": "1",
                "risk_level": "low", "category": "ENVIRONMENTAL",
                "risk_class": "NOISE", "rule_id": "SEC-E-004",
                "likelihood": 1, "impact": 1,
                "description": "Low-level finding",
                "recommendation": "Ear protection",
                "measures": [], "confidence": 0.7,
                "evidence": "loud machinery",
            }],
            "processing_time_seconds": 1.0,
            "metadata": {},
        },
        "pdf_base64": base64.b64encode(b"%PDF-1.4 test").decode(),
    }


def _settings(**overrides) -> MagicMock:
    s = MagicMock()
    s.epro_base_url = "https://staging.epro.filmakademie.de/api"
    s.epro_auth_token = ""
    s.epro_timeout = 30
    s.database_url = "postgresql+asyncpg://fake:fake@localhost/fake"
    s.api_secret_key = "test-secret-key-for-unit-tests-only"
    s.buffer_ttl_seconds = 3600
    for k, v in overrides.items():
        setattr(s, k, v)
    return s


@pytest.mark.asyncio
class TestPushDeletesBufferAfter2xx:
    """Pflichtenheft Abnahmetest 2."""

    async def test_2xx_calls_buffer_delete_with_report_ref_key(self):
        from workflows.activities import deliver_report_activity

        report_package = _push_report_package()

        buf = AsyncMock()
        buf.retrieve = AsyncMock(return_value=report_package)
        buf.delete = AsyncMock(return_value=1)
        buf.store = AsyncMock()

        mock_response = MagicMock()
        mock_response.status_code = 200
        mock_response.json.return_value = {"status": True, "message": "ok"}

        mock_client = AsyncMock()
        mock_client.__aenter__ = AsyncMock(return_value=mock_client)
        mock_client.__aexit__ = AsyncMock(return_value=False)
        mock_client.post = AsyncMock(return_value=mock_response)

        report_ref_key = "eki:buf:report-xyz"

        with patch("workflows.activities._get_buffer", return_value=buf), \
             patch("httpx.AsyncClient", return_value=mock_client), \
             patch("api.config.get_settings", return_value=_settings()):
            result = await deliver_report_activity(
                {"report_ref_key": report_ref_key, "report_id": str(uuid4()),
                 "total_findings": 1},
                {"delivery_mode": "push", "job_id": str(uuid4()),
                 "project_id": "75", "user_id": "u",
                 "script_format": "fdx", "script_id": 7},
            )

        assert result["delivered"] is True
        buf.delete.assert_awaited_once_with(report_ref_key)

    async def test_4xx_does_NOT_call_buffer_delete_directly(self):
        """Activity selber loescht nicht bei 4xx -- der Workflow-
        Failure-Branch ist dafuer zustaendig (cleanup_buffer_activity)."""
        from workflows.activities import deliver_report_activity

        report_package = _push_report_package()

        buf = AsyncMock()
        buf.retrieve = AsyncMock(return_value=report_package)
        buf.delete = AsyncMock(return_value=1)

        mock_response = MagicMock()
        mock_response.status_code = 422
        mock_response.json.return_value = {"detail": "invalid"}
        mock_response.raise_for_status = MagicMock()
        mock_response.request = MagicMock()

        mock_client = AsyncMock()
        mock_client.__aenter__ = AsyncMock(return_value=mock_client)
        mock_client.__aexit__ = AsyncMock(return_value=False)
        mock_client.post = AsyncMock(return_value=mock_response)

        with patch("workflows.activities._get_buffer", return_value=buf), \
             patch("httpx.AsyncClient", return_value=mock_client), \
             patch("api.config.get_settings", return_value=_settings()):
            result = await deliver_report_activity(
                {"report_ref_key": "eki:buf:abc", "report_id": str(uuid4()),
                 "total_findings": 1},
                {"delivery_mode": "push", "job_id": str(uuid4()),
                 "project_id": "75", "user_id": "u",
                 "script_format": "fdx"},
            )

        assert result["delivered"] is False
        assert result["hard_fail"] is True
        # Activity loescht NICHT; das macht cleanup_buffer_activity.
        buf.delete.assert_not_awaited()


@pytest.mark.asyncio
class TestPullEndpointDeletesBufferAfterRetrieval:
    """Pflichtenheft Abnahmetest 3: nach erstem 2xx-Abruf wird der Report
    sofort geloescht."""

    async def test_get_report_invokes_buffer_delete_after_2xx(
        self, client, db_session, auth_headers,
    ):
        """End-to-end ueber den FastAPI-TestClient."""
        from datetime import datetime

        from core.db_models import ReportMetadata

        report_id = uuid4()
        report_ref_key = "eki:buf:report-pull-1"

        report_meta = ReportMetadata(
            report_id=report_id,
            job_id=uuid4(),
            project_id="testproj",
            user_id="test-user-123",
            script_format="fdx",
            created_at=datetime.utcnow(),
            is_retrieved=False,
            total_findings=2,
            processing_time_seconds=1.0,
            report_ref_key=report_ref_key,
            delivery_mode="pull",
        )
        db_session.add(report_meta)
        await db_session.commit()

        # Patch SecureBuffer-Konstruktion in api.routers.security:_get_buffer
        fake_report_package = _push_report_package()
        fake_buffer = AsyncMock()
        fake_buffer.retrieve = AsyncMock(return_value=fake_report_package)
        fake_buffer.delete = AsyncMock(return_value=1)

        with patch("api.routers.security.SecureBuffer", return_value=fake_buffer):
            resp = client.get(
                f"/v1/security/reports/{report_id}", headers=auth_headers,
            )

        assert resp.status_code == 200
        fake_buffer.delete.assert_awaited_once_with(report_ref_key)

        # Zweite Anfrage muss 410 liefern (One-Shot).
        with patch("api.routers.security.SecureBuffer", return_value=fake_buffer):
            resp2 = client.get(
                f"/v1/security/reports/{report_id}", headers=auth_headers,
            )
        assert resp2.status_code == 410
