"""Application configuration using Pydantic Settings."""

from functools import lru_cache
from typing import Any

from pydantic import Field, PostgresDsn, RedisDsn, field_validator, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    """Application settings loaded from environment variables."""

    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        case_sensitive=False,
        extra="ignore",
    )

    # Environment
    env: str = Field(default="development", description="Environment: development, stage, prod")
    debug: bool = Field(default=False, description="Debug mode")

    # API Configuration
    api_host: str = Field(default="0.0.0.0", description="API host")
    api_port: int = Field(default=8000, description="API port")
    api_workers: int = Field(default=4, description="Number of worker processes")
    api_reload: bool = Field(default=False, description="Auto-reload on code changes")

    # Database
    database_url: PostgresDsn = Field(
        default="postgresql+asyncpg://eki_user:replace-me@localhost:5432/eki_db",
        description="PostgreSQL connection URL",
    )
    database_pool_size: int = Field(default=20, description="Database connection pool size")
    database_max_overflow: int = Field(default=10, description="Max overflow for connection pool")

    # Redis
    redis_url: RedisDsn = Field(
        default="redis://localhost:6379/0", description="Redis connection URL"
    )
    redis_max_connections: int = Field(default=50, description="Max Redis connections")

    # Temporal
    temporal_host: str = Field(default="localhost:7233", description="Temporal server host")
    temporal_namespace: str = Field(default="default", description="Temporal namespace")
    temporal_task_queue: str = Field(
        default="security-check", description="Temporal task queue name"
    )
    temporal_workflow_execution_timeout: int = Field(
        default=2400, description="Workflow execution timeout in seconds (40 min)"
    )

    # Security
    api_secret_key: str = Field(
        default="replace-this-secret-key-with-at-least-32-characters",
        description="Secret key for token generation",
    )
    api_token_expire_minutes: int = Field(default=60, description="Access token expiration time")
    cors_origins: list[str] = Field(
        default=["http://localhost:3000", "http://localhost:8000"],
        description="Allowed CORS origins",
    )

    # Rate Limiting
    rate_limit_enabled: bool = Field(default=True, description="Enable rate limiting")
    rate_limit_per_minute: int = Field(default=60, description="API calls per minute per client")
    rate_limit_per_hour: int = Field(default=1000, description="API calls per hour per client")
    trust_proxy_headers: bool = Field(
        default=False,
        description="Trust X-Forwarded-For headers from trusted proxy IPs only",
    )
    trusted_proxy_ips: list[str] = Field(
        default_factory=list,
        description="Comma-separated list of trusted reverse proxy IP addresses",
    )

    # Operational endpoint controls
    metrics_enabled: bool = Field(default=True, description="Enable Prometheus metrics endpoint")

    # eProjekt Integration (for future milestones)
    epro_base_url: str = Field(
        default="https://epro-stage.filmakademie.de/api", description="eProjekt base URL"
    )
    epro_auth_token: str = Field(default="", description="eProjekt authentication token")
    epro_timeout: int = Field(default=30, description="eProjekt API timeout in seconds")

    # LLM Provider (for future milestones)
    llm_provider: str = Field(
        default="mistral_cloud",
        description="LLM provider: mistral_cloud, local_mistral, or ollama",
    )
    mistral_api_key: str = Field(default="", description="Mistral API key")
    mistral_model: str = Field(default="mistral-large-latest", description="Mistral model name")
    mistral_timeout: int = Field(default=120, description="Mistral request timeout in seconds")

    # Ollama Configuration
    ollama_base_url: str = Field(default="http://ollama:11434", description="Ollama base URL")
    ollama_model: str = Field(
        default="mistral", description="Ollama model name (e.g., mistral, llama2, codellama)"
    )
    ollama_timeout: int = Field(default=120, description="Ollama request timeout in seconds")

    # Observability
    otel_enabled: bool = Field(default=True, description="Enable OpenTelemetry")
    otel_service_name: str = Field(default="eki-api", description="Service name for traces")
    otel_exporter_otlp_endpoint: str = Field(
        default="http://localhost:4318", description="OTLP exporter endpoint"
    )
    prometheus_port: int = Field(default=9090, description="Prometheus metrics port")

    # Logging
    log_level: str = Field(default="INFO", description="Logging level")
    log_format: str = Field(default="json", description="Log format: json or console")

    @field_validator("cors_origins", mode="before")
    @classmethod
    def parse_cors_origins(cls, v: Any) -> list[str]:
        """Parse CORS origins from string or list."""
        if isinstance(v, str):
            return [origin.strip() for origin in v.split(",")]
        return v

    @field_validator("trusted_proxy_ips", mode="before")
    @classmethod
    def parse_trusted_proxy_ips(cls, v: Any) -> list[str]:
        """Parse trusted proxy IPs from comma-separated string or list."""
        if isinstance(v, str):
            return [ip.strip() for ip in v.split(",") if ip.strip()]
        return v

    @property
    def is_production(self) -> bool:
        """Check if running in production."""
        return self.env in {"prod", "production"}

    @property
    def is_development(self) -> bool:
        """Check if running in development."""
        return self.env == "development"

    @model_validator(mode="after")
    def validate_production_security(self) -> "Settings":
        """Enforce secure settings when running in production."""
        if not self.is_production:
            return self

        insecure_defaults = {
            "replace-this-secret-key-with-at-least-32-characters",
            "change-me-in-production-min-32-chars",
        }
        if len(self.api_secret_key) < 32 or self.api_secret_key in insecure_defaults:
            raise ValueError("API_SECRET_KEY must be a strong non-default secret in production")

        database_url_str = str(self.database_url)
        if "eki_password" in database_url_str or "replace-me" in database_url_str:
            raise ValueError("DATABASE_URL contains insecure placeholder credentials")

        if self.debug:
            raise ValueError("DEBUG must be false in production")

        return self


@lru_cache
def get_settings() -> Settings:
    """Get cached settings instance."""
    return Settings()
