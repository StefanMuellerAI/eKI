"""M08 -- send_delivery_failed_webhook_activity Tests.

Verifiziert die Pflichtenheft-Forderung aus Anhang 1:
``security.delivery.failed`` Webhook mit Metadaten-Payload
``{job_id, report_id, reason, attempts}``.

Wichtige Verhaltensregeln (Pflichtenheft + M08-Plan):
* Default OFF -- ohne ``EPRO_WEBHOOK_URL`` darf KEIN HTTP-Call erfolgen.
* Bei Konfiguration: kleiner Retry-Loop (3 Versuche, exponentielles
  Backoff intern), bei finalem Fehler ``sent=False`` ohne Workflow-Abbruch.
* Payload enthaelt KEINE Reportinhalte -- nur die vier Metadaten-Felder.
"""

from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch
from uuid import uuid4

import pytest

from workflows.activities import send_delivery_failed_webhook_activity


def _settings(**overrides) -> SimpleNamespace:
    base = {
        "epro_webhook_url": "",
        "epro_auth_token": "",
        "epro_timeout": 5,
    }
    base.update(overrides)
    return SimpleNamespace(**base)


def _http_client(*, status_code: int = 200, raise_on_post: Exception | None = None):
    mock_response = MagicMock()
    mock_response.status_code = status_code

    mock_client = AsyncMock()
    mock_client.__aenter__ = AsyncMock(return_value=mock_client)
    mock_client.__aexit__ = AsyncMock(return_value=False)
    if raise_on_post is not None:
        mock_client.post = AsyncMock(side_effect=raise_on_post)
    else:
        mock_client.post = AsyncMock(return_value=mock_response)
    return mock_client, mock_response


@pytest.mark.asyncio
class TestWebhookOptIn:
    async def test_no_http_call_when_url_unset(self):
        """Default OFF -- Activity darf gar keine HTTP-Verbindung machen."""
        with patch("api.config.get_settings", return_value=_settings(epro_webhook_url="")):
            with patch("httpx.AsyncClient") as mock_client_class:
                result = await send_delivery_failed_webhook_activity({
                    "job_id": str(uuid4()),
                    "report_id": str(uuid4()),
                    "reason": "retry_window_exhausted",
                    "attempts": 5,
                })

        assert result["sent"] is False
        assert result["reason"] == "no_webhook_url"
        mock_client_class.assert_not_called()

    async def test_no_http_call_when_url_whitespace(self):
        with patch("api.config.get_settings", return_value=_settings(epro_webhook_url="   ")):
            with patch("httpx.AsyncClient") as mock_client_class:
                result = await send_delivery_failed_webhook_activity({
                    "job_id": str(uuid4()),
                    "report_id": str(uuid4()),
                    "reason": "hard_4xx",
                    "attempts": 1,
                })

        assert result["sent"] is False
        mock_client_class.assert_not_called()


@pytest.mark.asyncio
class TestWebhookSuccess:
    async def test_success_2xx_returns_sent_true(self):
        client, _ = _http_client(status_code=201)
        with patch("api.config.get_settings",
                   return_value=_settings(epro_webhook_url="https://epro/x/delivery-failed")):
            with patch("httpx.AsyncClient", return_value=client):
                result = await send_delivery_failed_webhook_activity({
                    "job_id": "job-1",
                    "report_id": "rep-1",
                    "reason": "retry_window_exhausted",
                    "attempts": 7,
                })

        assert result["sent"] is True
        assert result["status_code"] == 201
        assert result["attempts_used"] == 1

    async def test_payload_contains_exactly_four_keys(self):
        client, _ = _http_client(status_code=200)
        with patch("api.config.get_settings",
                   return_value=_settings(epro_webhook_url="https://epro/x/delivery-failed")):
            with patch("httpx.AsyncClient", return_value=client):
                await send_delivery_failed_webhook_activity({
                    "job_id": "job-2",
                    "report_id": "rep-2",
                    "reason": "hard_4xx",
                    "attempts": 1,
                    "extra_inside": "must_be_ignored",
                })

        call_kwargs = client.post.call_args.kwargs
        payload = call_kwargs.get("json")
        assert payload is not None
        assert set(payload.keys()) == {"job_id", "report_id", "reason", "attempts"}
        assert payload["job_id"] == "job-2"
        assert payload["report_id"] == "rep-2"
        assert payload["reason"] == "hard_4xx"
        assert payload["attempts"] == 1

    async def test_auth_header_sent_when_token_set(self):
        client, _ = _http_client(status_code=200)
        with patch("api.config.get_settings", return_value=_settings(
            epro_webhook_url="https://epro/x/delivery-failed",
            epro_auth_token="bearer-xyz",
        )):
            with patch("httpx.AsyncClient", return_value=client):
                await send_delivery_failed_webhook_activity({
                    "job_id": "j", "report_id": "r",
                    "reason": "hard_4xx", "attempts": 1,
                })

        headers = client.post.call_args.kwargs.get("headers", {})
        assert headers["Authorization"] == "Bearer bearer-xyz"
        assert headers["Content-Type"] == "application/json"


@pytest.mark.asyncio
class TestWebhookFailure:
    async def test_5xx_triggers_internal_retries_then_gives_up(self):
        client, _ = _http_client(status_code=503)
        with patch("api.config.get_settings",
                   return_value=_settings(epro_webhook_url="https://epro/x/delivery-failed")):
            with patch("httpx.AsyncClient", return_value=client):
                with patch("asyncio.sleep", new=AsyncMock(return_value=None)):
                    result = await send_delivery_failed_webhook_activity({
                        "job_id": "j", "report_id": "r",
                        "reason": "retry_window_exhausted", "attempts": 5,
                    })

        assert result["sent"] is False
        assert result["status_code"] == 503
        assert result["attempts_used"] == 3
        assert client.post.await_count == 3

    async def test_transport_error_returns_sent_false_no_raise(self):
        """Webhook-Activity darf NIE selbst raisen -- der Workflow-Failure-
        Branch darf nicht erneut faillen."""
        client, _ = _http_client(raise_on_post=ConnectionError("DNS down"))
        with patch("api.config.get_settings",
                   return_value=_settings(epro_webhook_url="https://epro/x/delivery-failed")):
            with patch("httpx.AsyncClient", return_value=client):
                with patch("asyncio.sleep", new=AsyncMock(return_value=None)):
                    result = await send_delivery_failed_webhook_activity({
                        "job_id": "j", "report_id": "r",
                        "reason": "retry_window_exhausted", "attempts": 5,
                    })

        assert result["sent"] is False
        assert result["attempts_used"] == 3
        assert result["error"] == "ConnectionError"
