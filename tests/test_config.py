"""Tests for application settings parsing."""

import pytest
from pydantic import ValidationError

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
def test_llm_provider_hyphen_typo_is_rejected_with_hint(monkeypatch, value, expected_hint):
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


class TestProductionSecurityValidator:
    """``ENV=production`` must refuse to start with placeholder secrets."""

    _STRONG_KEY = "k8Jf2nQ7xLp4Vr9Tz1Wb6Yc3Hd5Ms0Ge"  # 32 chars, no marker
    _STRONG_DB = "postgresql+asyncpg://eki_user:Xy7pQ2rT9vB4@postgres:5432/eki_db"

    def _settings(self, monkeypatch, **env: str) -> Settings:
        base = {
            "ENV": "production",
            "DEBUG": "false",
            "LLM_PROVIDER": "local_mistral",
            "API_SECRET_KEY": self._STRONG_KEY,
            "DATABASE_URL": self._STRONG_DB,
        }
        base.update(env)
        for key, value in base.items():
            monkeypatch.setenv(key, value)
        return Settings(_env_file=None)

    def test_strong_settings_are_accepted(self, monkeypatch):
        settings = self._settings(monkeypatch)
        assert settings.is_production

    @pytest.mark.parametrize(
        "secret",
        [
            "CHANGE_ME_256_BIT_SECRET_KEY_HERE",  # verbatim from .env.example (33 chars)
            "change-me-in-production-min-32-chars",
            "replace-this-secret-key-with-at-least-32-characters",
            "short",
        ],
    )
    def test_placeholder_or_weak_secret_key_is_rejected(self, monkeypatch, secret):
        with pytest.raises(ValueError, match="API_SECRET_KEY"):
            self._settings(monkeypatch, API_SECRET_KEY=secret)

    @pytest.mark.parametrize(
        "url",
        [
            # verbatim from .env.example
            "postgresql+asyncpg://eki_user:CHANGE_ME_STRONG_PASSWORD_HERE@postgres:5432/eki_db",
            "postgresql+asyncpg://eki_user:replace-me@localhost:5432/eki_db",
            "postgresql+asyncpg://eki_user:eki_password@postgres:5432/eki_db",
        ],
    )
    def test_placeholder_database_password_is_rejected(self, monkeypatch, url):
        with pytest.raises(ValueError, match="DATABASE_URL"):
            self._settings(monkeypatch, DATABASE_URL=url)

    def test_debug_is_rejected(self, monkeypatch):
        with pytest.raises(ValueError, match="DEBUG"):
            self._settings(monkeypatch, DEBUG="true")

    def test_external_llm_requires_explicit_opt_in(self, monkeypatch):
        with pytest.raises(ValueError, match="mistral_cloud"):
            self._settings(monkeypatch, LLM_PROVIDER="mistral_cloud")
        settings = self._settings(
            monkeypatch,
            LLM_PROVIDER="mistral_cloud",
            LLM_ALLOW_EXTERNAL_PROVIDERS="true",
            MISTRAL_API_KEY="test-key",
        )
        assert settings.llm_provider == "mistral_cloud"

    def test_placeholders_are_fine_outside_production(self, monkeypatch):
        settings = self._settings(
            monkeypatch,
            ENV="development",
            API_SECRET_KEY="CHANGE_ME_256_BIT_SECRET_KEY_HERE",
        )
        assert not settings.is_production


class TestDocumentLimitSettings:
    """MAX_CONCURRENT_DOCUMENTS / DOCUMENT_QUEUE_TIMEOUT_SECONDS."""

    def _settings(self, monkeypatch, **env: str) -> Settings:
        for name in (
            "MAX_CONCURRENT_DOCUMENTS",
            "DOCUMENT_QUEUE_TIMEOUT_SECONDS",
            "BUFFER_TTL_SECONDS",
            "TEMPORAL_WORKFLOW_EXECUTION_TIMEOUT",
        ):
            monkeypatch.delenv(name, raising=False)
        for name, value in env.items():
            monkeypatch.setenv(name, value)
        return Settings(_env_file=None)

    def test_defaults_queue_one_document_for_two_hours(self, monkeypatch):
        settings = self._settings(monkeypatch)
        assert settings.max_concurrent_documents == 1
        assert settings.document_queue_timeout_seconds == 7200

    def test_zero_disables_the_limit(self, monkeypatch):
        assert (
            self._settings(monkeypatch, MAX_CONCURRENT_DOCUMENTS="0").max_concurrent_documents == 0
        )

    @pytest.mark.parametrize("value", ["-1", "51"])
    def test_limit_out_of_range_is_rejected(self, monkeypatch, value):
        with pytest.raises(ValidationError):
            self._settings(monkeypatch, MAX_CONCURRENT_DOCUMENTS=value)

    def test_queue_timeout_has_a_floor(self, monkeypatch):
        with pytest.raises(ValidationError):
            self._settings(monkeypatch, DOCUMENT_QUEUE_TIMEOUT_SECONDS="30")

    def test_queue_timeout_must_be_shorter_than_the_buffer_ttl(self, monkeypatch):
        with pytest.raises(ValidationError, match="must be shorter than BUFFER_TTL_SECONDS"):
            self._settings(monkeypatch, DOCUMENT_QUEUE_TIMEOUT_SECONDS="21600")

    def test_queue_and_buffer_ttl_must_fit_into_the_workflow_timeout(self, monkeypatch):
        with pytest.raises(ValidationError, match="must stay below TEMPORAL_WORKFLOW_EXECUTION"):
            self._settings(
                monkeypatch,
                DOCUMENT_QUEUE_TIMEOUT_SECONDS="14400",
                TEMPORAL_WORKFLOW_EXECUTION_TIMEOUT="36000",
            )

    def test_timing_rules_do_not_apply_without_a_limit(self, monkeypatch):
        settings = self._settings(
            monkeypatch, MAX_CONCURRENT_DOCUMENTS="0", DOCUMENT_QUEUE_TIMEOUT_SECONDS="99999"
        )
        assert settings.document_queue_timeout_seconds == 99999
