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


# ---------------------------------------------------------------------------
# LLM_PROVIDER Validierung (Doku-Bug-Hardening, post-M07)
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "value,expected",
    [
        ("mistral_cloud", "mistral_cloud"),
        ("local_mistral", "local_mistral"),
        ("ollama", "ollama"),
        ("OLLAMA", "ollama"),
        ("  mistral_cloud  ", "mistral_cloud"),
    ],
)
def test_llm_provider_accepts_valid_values(monkeypatch, value, expected):
    """Gültige Provider werden akzeptiert und auf Kleinschreibung normiert."""
    monkeypatch.setenv("LLM_PROVIDER", value)
    settings = Settings()
    assert settings.llm_provider == expected


@pytest.mark.parametrize(
    "value,expected_hint",
    [
        ("mistral-cloud", "mistral_cloud"),
        ("local-mistral", "local_mistral"),
    ],
)
def test_llm_provider_hyphen_typo_is_rejected_with_hint(
    monkeypatch, value, expected_hint
):
    """Bindestrich-Schreibweise wird abgewiesen, Fehlermeldung nennt den
    korrekten Unterstrich-Namen."""
    monkeypatch.setenv("LLM_PROVIDER", value)
    with pytest.raises(ValueError) as exc_info:
        Settings()
    err = str(exc_info.value)
    assert "hyphen" in err.lower() or expected_hint in err


def test_llm_provider_unknown_value_is_rejected(monkeypatch):
    """Unbekannte Provider-Namen werden mit Liste der gültigen Optionen
    abgewiesen."""
    monkeypatch.setenv("LLM_PROVIDER", "openai")
    with pytest.raises(ValueError) as exc_info:
        Settings()
    err = str(exc_info.value)
    assert "Invalid LLM_PROVIDER" in err
    assert "mistral_cloud" in err
    assert "ollama" in err
