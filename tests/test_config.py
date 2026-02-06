"""Tests for application settings parsing."""

import pytest

from api.config import Settings


def test_cors_origins_from_comma_separated_env(monkeypatch):
    """Parse CORS origins from comma-separated env var."""
    monkeypatch.setenv(
        "CORS_ORIGINS",
        "https://a.example, https://b.example",
    )
    settings = Settings()
    assert settings.cors_origins == ["https://a.example", "https://b.example"]


def test_cors_origins_from_json_env(monkeypatch):
    """Parse CORS origins from JSON array env var."""
    monkeypatch.setenv(
        "CORS_ORIGINS",
        '["https://a.example", "https://b.example"]',
    )
    settings = Settings()
    assert settings.cors_origins == ["https://a.example", "https://b.example"]


def test_trusted_proxy_ips_from_comma_separated_env(monkeypatch):
    """Parse trusted proxy IPs from comma-separated env var."""
    monkeypatch.setenv(
        "TRUSTED_PROXY_IPS",
        "10.0.0.1, 10.0.0.2",
    )
    settings = Settings()
    assert settings.trusted_proxy_ips == ["10.0.0.1", "10.0.0.2"]


def test_trusted_proxy_ips_empty_env(monkeypatch):
    """Treat empty trusted proxy IP env as an empty list."""
    monkeypatch.setenv("TRUSTED_PROXY_IPS", "")
    settings = Settings()
    assert settings.trusted_proxy_ips == []


def test_cors_origins_invalid_json_env_raises(monkeypatch):
    """Reject invalid JSON that starts with [ but is not valid."""
    monkeypatch.setenv("CORS_ORIGINS", "[not json]")
    with pytest.raises(ValueError):
        Settings()
