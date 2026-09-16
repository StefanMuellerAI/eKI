"""M10 -- deliver_report_activity hardening: bookkeeping, headers, retry classes."""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch
from uuid import uuid4

import httpx
import pytest

from core.logging_config import set_request_id
from workflows import activities
from workflows.activities import (
    _RETRYABLE_4XX,
    check_report_retrieved_activity,
    deliver_report_activity,
    record_dead_letter_activity,
)


def _package() -> dict[str, Any]:
    return {
        "report": {
            "report_id": str(uuid4()),
            "project_id": "75",
            "findings": [],
            "risk_summary": {"critical": 0, "high": 0, "medium": 0, "low": 0, "info": 0},
            "total_findings": 0,
        },
        "pdf_base64": "",
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
    }
    base.update(overrides)
    return SimpleNamespace(**base)


def _buffer(package: dict[str, Any]) -> MagicMock:
    buf = MagicMock()
    buf.retrieve = AsyncMock(return_value=package)
    buf.delete = AsyncMock(return_value=1)
    return buf


def _client(status_code: int | None = 200, *, exc: Exception | None = None) -> MagicMock:
    response = MagicMock()
    response.status_code = status_code
    response.json.return_value = {"message": "ok"}
    response.request = httpx.Request("POST", "https://epro.example/x")
    if status_code is not None and status_code >= 400:
        response.raise_for_status.side_effect = httpx.HTTPStatusError(
            "err", request=response.request, response=response
        )
    client = AsyncMock()
    client.__aenter__ = AsyncMock(return_value=client)
    client.__aexit__ = AsyncMock(return_value=False)
    client.post = AsyncMock(side_effect=exc) if exc else AsyncMock(return_value=response)
    return client


def _cfg(mode: str = "push") -> dict[str, Any]:
    return {
        "delivery_mode": mode,
        "job_id": str(uuid4()),
        "project_id": "75",
        "user_id": "u1",
        "script_format": "fdx",
        "script_id": 7,
    }


@pytest.fixture
def bookkeeping():
    with (
        patch.object(activities, "_mark_delivering", new=AsyncMock()) as mark,
        patch.object(activities, "_record_attempt", new=AsyncMock()) as record,
        patch.object(activities, "_current_attempt", return_value=3),
    ):
        yield mark, record


@pytest.mark.asyncio
class TestPushHardening:
    async def test_success_marks_delivering_then_delivered_with_headers(self, bookkeeping):
        mark, record = bookkeeping
        client = _client(200)
        set_request_id("req-push-1")
        report_id = str(uuid4())
        with (
            patch("workflows.activities._get_buffer", return_value=_buffer(_package())),
            patch("httpx.AsyncClient", return_value=client),
            patch("api.config.get_settings", return_value=_settings()),
        ):
            result = await deliver_report_activity(
                {"report_ref_key": "k", "report_id": report_id, "total_findings": 0}, _cfg()
            )

        assert result["delivered"] is True
        assert result["attempts_used"] == 3
        mark.assert_awaited_once()
        assert mark.await_args.kwargs["delivery_mode"] == "push"
        record.assert_awaited_once()
        assert record.await_args.kwargs == {
            "job_id": mark.await_args.kwargs["job_id"],
            "attempt": 3,
            "status_code": 200,
            "delivered": True,
            "delivery_mode": "push",
        }
        headers = client.post.await_args.kwargs["headers"]
        assert headers["Idempotency-Key"] == report_id
        assert headers["X-Request-ID"] == "req-push-1"
        assert headers["X-EKI-Attempt"] == "3"
        assert headers["Authorization"] == "Bearer tok"

    async def test_hard_4xx_returns_hard_fail_with_real_attempt(self, bookkeeping):
        _, record = bookkeeping
        with (
            patch("workflows.activities._get_buffer", return_value=_buffer(_package())),
            patch("httpx.AsyncClient", return_value=_client(422)),
            patch("api.config.get_settings", return_value=_settings()),
        ):
            result = await deliver_report_activity(
                {"report_ref_key": "k", "report_id": "r"}, _cfg()
            )
        assert result["delivered"] is False
        assert result["hard_fail"] is True
        assert result["attempts_used"] == 3
        assert record.await_args.kwargs["delivered"] is False
        assert record.await_args.kwargs["status_code"] == 422

    @pytest.mark.parametrize("code", sorted(_RETRYABLE_4XX))
    async def test_retryable_4xx_raises_for_temporal_retry(self, bookkeeping, code):
        with (
            patch("workflows.activities._get_buffer", return_value=_buffer(_package())),
            patch("httpx.AsyncClient", return_value=_client(code)),
            patch("api.config.get_settings", return_value=_settings()),
        ):
            with pytest.raises(httpx.HTTPStatusError):
                await deliver_report_activity({"report_ref_key": "k", "report_id": "r"}, _cfg())

    async def test_5xx_raises_and_records_attempt(self, bookkeeping):
        _, record = bookkeeping
        with (
            patch("workflows.activities._get_buffer", return_value=_buffer(_package())),
            patch("httpx.AsyncClient", return_value=_client(503)),
            patch("api.config.get_settings", return_value=_settings()),
        ):
            with pytest.raises(httpx.HTTPStatusError):
                await deliver_report_activity({"report_ref_key": "k", "report_id": "r"}, _cfg())
        assert record.await_args.kwargs["status_code"] == 503
        assert record.await_args.kwargs["delivered"] is False

    async def test_transport_error_raises_and_records_attempt(self, bookkeeping):
        _, record = bookkeeping
        with (
            patch("workflows.activities._get_buffer", return_value=_buffer(_package())),
            patch("httpx.AsyncClient", return_value=_client(None, exc=httpx.ConnectError("x"))),
            patch("api.config.get_settings", return_value=_settings()),
        ):
            with pytest.raises(httpx.ConnectError):
                await deliver_report_activity({"report_ref_key": "k", "report_id": "r"}, _cfg())
        assert record.await_args.kwargs["status_code"] is None

    async def test_pull_mode_marks_ready_without_transport(self, bookkeeping):
        mark, record = bookkeeping
        client = _client(200)
        with (
            patch("workflows.activities._get_buffer", return_value=_buffer(_package())),
            patch("httpx.AsyncClient", return_value=client),
            patch("api.config.get_settings", return_value=_settings()),
        ):
            result = await deliver_report_activity(
                {"report_ref_key": "k", "report_id": "r"}, _cfg("pull")
            )
        assert result["delivered"] is True
        client.post.assert_not_awaited()
        assert record.await_args.kwargs["delivery_mode"] == "pull"
        assert record.await_args.kwargs["delivered"] is True

    async def test_bookkeeping_failure_is_non_fatal(self):
        with (
            patch.object(activities, "_mark_delivering", new=AsyncMock(side_effect=RuntimeError)),
            patch.object(activities, "_record_attempt", new=AsyncMock(side_effect=RuntimeError)),
            patch("workflows.activities._get_buffer", return_value=_buffer(_package())),
            patch("httpx.AsyncClient", return_value=_client(200)),
            patch("api.config.get_settings", return_value=_settings()),
        ):
            result = await deliver_report_activity(
                {"report_ref_key": "k", "report_id": "r"}, _cfg()
            )
        assert result["delivered"] is True


class TestCurrentAttempt:
    def test_outside_activity_context_is_one(self):
        assert activities._current_attempt() == 1


@pytest.mark.asyncio
class TestDeadLetterAndPullCheck:
    async def test_record_dead_letter_without_job_id(self):
        assert (await record_dead_letter_activity({}))["recorded"] is False

    async def test_record_dead_letter_db_error_is_non_fatal(self):
        engine = MagicMock()
        engine.dispose = AsyncMock()
        factory = MagicMock(side_effect=RuntimeError("db down"))
        with patch.object(activities, "_session_factory", return_value=(engine, factory)):
            result = await record_dead_letter_activity(
                {"job_id": str(uuid4()), "reason": "hard_4xx"}
            )
        assert result == {"recorded": False, "reason": "RuntimeError"}
        engine.dispose.assert_awaited_once()

    async def test_check_report_retrieved_without_id(self):
        assert await check_report_retrieved_activity({}) == {"found": False, "retrieved": False}
