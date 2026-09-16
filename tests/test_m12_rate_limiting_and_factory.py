"""M12 -- direct unit tests for rate limiting and the LLM factory smoke helper."""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest
from fastapi import HTTPException

from api.rate_limiting import (
    _client_ip_from_request,
    rate_limit_by_api_key,
    rate_limit_by_ip,
    rate_limit_combined,
)
from llm.factory import test_llm_provider as smoke_provider
from tests.conftest import MockRedis


def _request(peer: str = "10.0.0.5", headers: dict[str, str] | None = None) -> MagicMock:
    req = MagicMock()
    req.client = SimpleNamespace(host=peer)
    req.headers = headers or {}
    return req


def _settings(**overrides):
    base = {
        "rate_limit_enabled": True,
        "rate_limit_per_minute": 2,
        "rate_limit_per_hour": 3,
        "trust_proxy_headers": False,
        "trusted_proxy_ips": ["10.0.0.5"],
    }
    base.update(overrides)
    return SimpleNamespace(**base)


class TestClientIpResolution:
    def test_without_proxy_trust_uses_direct_peer(self):
        req = _request(headers={"X-Forwarded-For": "203.0.113.9"})
        assert _client_ip_from_request(req, _settings()) == "10.0.0.5"

    def test_untrusted_peer_is_not_honoured(self):
        req = _request(peer="192.0.2.1", headers={"X-Forwarded-For": "203.0.113.9"})
        assert _client_ip_from_request(req, _settings(trust_proxy_headers=True)) == "192.0.2.1"

    def test_trusted_proxy_forwarded_ip_is_used(self):
        req = _request(headers={"X-Forwarded-For": "203.0.113.9, 10.0.0.5"})
        assert _client_ip_from_request(req, _settings(trust_proxy_headers=True)) == "203.0.113.9"

    def test_invalid_forwarded_value_falls_back(self):
        req = _request(headers={"X-Forwarded-For": "not-an-ip"})
        assert _client_ip_from_request(req, _settings(trust_proxy_headers=True)) == "10.0.0.5"

    def test_empty_forwarded_header_falls_back(self):
        req = _request(headers={"X-Forwarded-For": ""})
        assert _client_ip_from_request(req, _settings(trust_proxy_headers=True)) == "10.0.0.5"

    def test_missing_client_is_unknown(self):
        req = _request()
        req.client = None
        assert _client_ip_from_request(req, _settings()) == "unknown"


@pytest.mark.asyncio
class TestRateLimits:
    async def test_ip_limit_exceeded_returns_429_with_retry_after(self):
        redis = MockRedis()
        settings = _settings(rate_limit_per_minute=2)
        req = _request()
        await rate_limit_by_ip(req, redis, settings)
        await rate_limit_by_ip(req, redis, settings)
        with pytest.raises(HTTPException) as exc:
            await rate_limit_by_ip(req, redis, settings)
        assert exc.value.status_code == 429
        assert exc.value.headers["Retry-After"] == "60"

    async def test_disabled_rate_limit_is_noop(self):
        redis = MockRedis()
        req = _request()
        for _ in range(10):
            await rate_limit_by_ip(req, redis, _settings(rate_limit_enabled=False))
        assert redis._store == {}

    async def test_redis_failure_fails_open(self):
        redis = MagicMock()
        redis.incr = AsyncMock(side_effect=ConnectionError("down"))
        await rate_limit_by_ip(_request(), redis, _settings())  # no exception

    async def test_api_key_limit_exceeded(self):
        redis = MockRedis()
        settings = _settings(rate_limit_per_hour=1)
        req = _request(headers={"Authorization": "Bearer eki_abc"})
        await rate_limit_by_api_key(req, redis, settings)
        with pytest.raises(HTTPException) as exc:
            await rate_limit_by_api_key(req, redis, settings)
        assert exc.value.status_code == 429
        assert "minutes" in exc.value.detail

    async def test_api_key_limit_ignores_non_bearer(self):
        redis = MockRedis()
        req = _request(headers={"Authorization": "Basic xyz"})
        await rate_limit_by_api_key(req, redis, _settings(rate_limit_per_hour=1))
        await rate_limit_by_api_key(req, redis, _settings(rate_limit_per_hour=1))
        assert redis._store == {}

    async def test_combined_applies_both(self):
        redis = MockRedis()
        settings = _settings(rate_limit_per_minute=5, rate_limit_per_hour=1)
        req = _request(headers={"Authorization": "Bearer eki_abc"})
        await rate_limit_combined(req, redis, settings)
        with pytest.raises(HTTPException):
            await rate_limit_combined(req, redis, settings)


@pytest.mark.asyncio
class TestFactorySmokeHelper:
    def _provider(self, *, healthy=True, response="Hello, eKI!", raise_exc=None):
        p = MagicMock()
        p.provider_name = "fake"
        p.health_check = AsyncMock(return_value=healthy)
        p.generate = AsyncMock(return_value=response, side_effect=raise_exc)
        return p

    async def test_success(self):
        assert await smoke_provider(self._provider()) is True

    async def test_health_failure(self):
        assert await smoke_provider(self._provider(healthy=False)) is False

    async def test_empty_response(self):
        assert await smoke_provider(self._provider(response="")) is False

    async def test_exception_is_swallowed(self):
        assert await smoke_provider(self._provider(raise_exc=RuntimeError("x"))) is False
