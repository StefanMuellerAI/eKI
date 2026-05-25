"""Application configuration using Pydantic Settings."""

import json
from functools import lru_cache
from pathlib import Path
from typing import Annotated, Any, ClassVar

from pydantic import Field, PostgresDsn, RedisDsn, field_validator, model_validator
from pydantic_settings import BaseSettings, NoDecode, SettingsConfigDict


def _read_secret_file(path: str, env_name: str) -> str:
    """Read a secret value from file and return stripped content."""
    try:
        value = Path(path).read_text(encoding="utf-8").strip()
    except OSError as exc:
        raise ValueError(f"{env_name} points to unreadable file: {path}") from exc

    if not value:
        raise ValueError(f"{env_name} points to empty file: {path}")

    return value


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
    database_url_file: str | None = Field(
        default=None,
        description="Optional file path containing full DATABASE_URL (Docker secret pattern)",
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
        default=14400, description="Workflow execution timeout in seconds (4h)"
    )

    # Security
    api_secret_key: str = Field(
        default="replace-this-secret-key-with-at-least-32-characters",
        description="Secret key for token generation",
    )
    api_secret_key_file: str | None = Field(
        default=None,
        description="Optional file path containing API_SECRET_KEY (Docker secret pattern)",
    )
    api_token_expire_minutes: int = Field(default=60, description="Access token expiration time")
    cors_origins: Annotated[list[str], NoDecode] = Field(
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
    trusted_proxy_ips: Annotated[list[str], NoDecode] = Field(
        default_factory=list,
        description="Comma-separated list of trusted reverse proxy IP addresses",
    )

    # Operational endpoint controls
    metrics_enabled: bool = Field(default=True, description="Enable Prometheus metrics endpoint")

    # eProjekt Integration (for future milestones)
    epro_base_url: str = Field(
        default="https://staging.epro.filmakademie.de/api", description="eProjekt base URL"
    )
    epro_auth_token: str = Field(default="", description="eProjekt authentication token")
    epro_auth_token_file: str | None = Field(
        default=None,
        description="Optional file path containing eProjekt auth token",
    )
    epro_timeout: int = Field(default=30, description="eProjekt API timeout in seconds")

    # M08 - security.delivery.failed Webhook (opt-in)
    # Default leer = Webhook deaktiviert; bestehende Funktion bleibt
    # unveraendert. Wird gesendet, wenn der 6h-Push-Retry erschoepft ist
    # oder ein 4xx-Hard-Fail eintritt (Pflichtenheft Anhang 1 + Abnahmetest 4).
    epro_webhook_url: str = Field(
        default="",
        description=(
            "URL fuer security.delivery.failed Webhook-POST. "
            "Leer = Webhook deaktiviert. Erwartet vollstaendige URL "
            "(inkl. Schema), z.B. https://staging.epro.filmakademie.de/"
            "api/eki/scl/delivery-failed."
        ),
    )
    epro_webhook_url_file: str | None = Field(
        default=None,
        description=(
            "Optionaler Pfad zu einer Datei mit der Webhook-URL "
            "(Docker-Secrets-Pattern, analog zu EPRO_AUTH_TOKEN_FILE)."
        ),
    )

    # LLM Provider (for future milestones)
    llm_provider: str = Field(
        default="mistral_cloud",
        description="LLM provider: mistral_cloud, local_mistral, or ollama",
    )
    mistral_api_key: str = Field(default="", description="Mistral API key")
    mistral_api_key_file: str | None = Field(
        default=None,
        description="Optional file path containing Mistral API key",
    )
    mistral_model: str = Field(default="mistral-large-latest", description="Mistral model name")
    mistral_timeout: int = Field(default=120, description="Mistral request timeout in seconds")

    # Ollama Configuration
    ollama_base_url: str = Field(default="http://ollama:11434", description="Ollama base URL")
    ollama_model: str = Field(
        default="mistral",
        description="Ollama model name (e.g., mistral, gemma4:31b, qwen3.6:35b)",
    )
    ollama_timeout: int = Field(default=300, description="Ollama request timeout in seconds")
    ollama_think: bool = Field(
        default=False,
        description=(
            "Enable reasoning/thinking mode for thinking-capable models "
            "(gemma4, qwen3.x). Disabled by default for faster, cleaner "
            "structured output. Ignored by non-thinking models like mistral."
        ),
    )
    ollama_num_ctx: int = Field(
        default=32768,
        description=(
            "Context window in tokens for Ollama requests. Must cover "
            "system prompt + taxonomy context + schema + scene text."
        ),
    )
    ollama_embedding_model: str = Field(
        default="mxbai-embed-large",
        description=(
            "Ollama model used for KB embeddings (M06). Must produce a "
            "1024-dim vector to match the kb_embeddings.vector column. "
            "Tested with mxbai-embed-large (512 token / ~1800 char hard "
            "limit) and bge-m3 (8192 token, 1024 dim, ideal match). "
            "If you switch to bge-m3, bump ollama_embedding_max_chars."
        ),
    )
    ollama_embedding_max_chars: int = Field(
        default=1000,
        description=(
            "Pre-truncate embed() input to this many characters. "
            "mxbai-embed-large has a hard 512-token context and refuses "
            "inputs > ~1100 chars of dense German/Markdown with HTTP 500. "
            "1000 chars is the verified safe ceiling. The full chunk_text "
            "is still stored in kb_embeddings; only the vector is computed "
            "over the truncated prefix.  For full Pflichtenheft chunks "
            "(800-1500 tokens), switch to bge-m3 and raise this to ~30000."
        ),
    )

    # Knowledge Base (M06)
    kb_retrieval_enabled: bool = Field(
        default=False,
        description=(
            "Master switch for KB retrieval in the risk analysis pipeline. "
            "Default OFF: the risk flow is byte-identical to the M05 path. "
            "Turn ON only after KB content has been validated and seeded."
        ),
    )
    kb_default_tenant_id: str = Field(
        default="00000000-0000-0000-0000-000000000001",
        description=(
            "Tenant UUID used by all KB operations until multi-tenant "
            "routing is required. Pinned to Filmakademie's single tenant."
        ),
    )
    kb_top_k: int = Field(
        default=3,
        description="Number of KB chunks retrieved per scene during risk analysis.",
    )
    kb_max_chunk_chars_in_prompt: int = Field(
        default=600,
        description=(
            "Max characters per retrieved chunk inserted into the LLM prompt "
            "to keep the context window under control on large screenplays."
        ),
    )

    # Observability
    otel_enabled: bool = Field(default=True, description="Enable OpenTelemetry")
    otel_service_name: str = Field(default="eki-api", description="Service name for traces")
    otel_exporter_otlp_endpoint: str = Field(
        default="http://localhost:4318", description="OTLP exporter endpoint"
    )
    prometheus_port: int = Field(default=9090, description="Prometheus metrics port")

    # Delivery
    delivery_mode: str = Field(
        default="pull", description="Default delivery mode: 'pull' (One-Shot GET) or 'push' (POST to ePro)"
    )

    # Transient Buffer
    buffer_ttl_seconds: int = Field(
        default=21600, description="Transient secure buffer TTL in seconds (default 6h)"
    )

    # Logging
    log_level: str = Field(default="INFO", description="Logging level")
    log_format: str = Field(default="json", description="Log format: json or console")

    # =====================================================================
    # M07 – Großdokument-Optimierung
    # Alle Defaults sind so gewählt, dass das Verhalten ohne Konfig-Wechsel
    # bytewise identisch zum M06-Stand bleibt. Parallel-Pfad greift NUR, wenn
    # `llm_parallel_enabled=true` UND mindestens eine Concurrency > 1 gesetzt
    # wird.
    # =====================================================================

    llm_parallel_enabled: bool = Field(
        default=False,
        description=(
            "Master-Flag M07. Default OFF = bytewise identisch zu M06. "
            "Erst in Kombination mit pdf_structure_concurrency oder "
            "risk_analysis_concurrency > 1 wird Parallelität im Workflow "
            "aktiv."
        ),
    )
    ollama_max_concurrent_requests: int = Field(
        default=1,
        ge=1,
        le=8,
        description=(
            "Prozessweiter Hard-Cap für gleichzeitige Ollama-Requests. "
            "Wird im OllamaProvider als modul-globaler Semaphore umgesetzt "
            "und gilt damit über alle Workflows und Activities hinweg. "
            "Default 1 = serialisiert wie heute. Werte > 1 nur auf "
            "dediziertem System mit ausreichend VRAM."
        ),
    )
    ollama_min_interval_ms: int = Field(
        default=0,
        ge=0,
        description=(
            "Optionaler Mindestabstand zwischen zwei Ollama-Calls in "
            "Millisekunden. Schützt knapp dimensionierte Shared-GPU-"
            "Systeme zusätzlich zum Semaphore. Default 0 = aus."
        ),
    )
    pdf_structure_concurrency: int = Field(
        default=1,
        ge=1,
        le=8,
        description=(
            "Maximale Anzahl parallel ausgeführter "
            "structure_scene_llm-Activities pro Workflow. Default 1 = "
            "strikt sequenziell wie heute."
        ),
    )
    risk_analysis_concurrency: int = Field(
        default=1,
        ge=1,
        le=8,
        description=(
            "Maximale Anzahl parallel ausgeführter "
            "analyze_scene_risk-Activities pro Workflow. Default 1 = "
            "strikt sequenziell wie heute."
        ),
    )
    llm_activity_timeout_seconds: int = Field(
        default=600,
        ge=60,
        description=(
            "start_to_close_timeout für die LLM-Activities (Strukturierung "
            "und Risikoanalyse). Vor M07 hardcoded 300s; auf 600s erhöht, "
            "um Backoff bei voller Ollama-Queue abzufedern."
        ),
    )
    max_pdf_pages: int = Field(
        default=500,
        ge=1,
        description=(
            "Obergrenze für PDF-Seiten beim Parsing. Vor M07 Modul-Konstante "
            "in parsers/pdf.py."
        ),
    )
    max_pdf_size_bytes: int = Field(
        default=10 * 1024 * 1024,
        ge=1,
        description=(
            "Obergrenze für PDF-Größe in Bytes beim Parsing. Vor M07 "
            "Modul-Konstante in parsers/pdf.py (10 MB)."
        ),
    )
    max_upload_size_bytes: int = Field(
        default=10 * 1024 * 1024,
        ge=1,
        description=(
            "Obergrenze für Multipart-Upload-Größe. Vor M07 Modul-Konstante "
            "in api/routers/security.py (10 MB)."
        ),
    )
    worker_max_concurrent_activities: int = Field(
        default=20,
        ge=1,
        description=(
            "Temporal Worker Cap für gleichzeitige Activities. Vor M07 "
            "in worker/main.py hardcoded."
        ),
    )
    worker_max_concurrent_workflow_tasks: int = Field(
        default=10,
        ge=1,
        description=(
            "Temporal Worker Cap für gleichzeitige Workflow Tasks. Vor "
            "M07 in worker/main.py hardcoded."
        ),
    )

    @field_validator("cors_origins", mode="before")
    @classmethod
    def parse_cors_origins(cls, v: Any) -> list[str]:
        """Parse CORS origins from string or list."""
        if isinstance(v, str):
            value = v.strip()
            if not value:
                return []
            if value.startswith("["):
                parsed = json.loads(value)
                if not isinstance(parsed, list):
                    raise ValueError("CORS_ORIGINS JSON value must be a list")
                return [str(origin).strip() for origin in parsed if str(origin).strip()]
            return [origin.strip() for origin in value.split(",") if origin.strip()]
        if isinstance(v, list):
            return [str(origin).strip() for origin in v if str(origin).strip()]
        return v

    @field_validator("trusted_proxy_ips", mode="before")
    @classmethod
    def parse_trusted_proxy_ips(cls, v: Any) -> list[str]:
        """Parse trusted proxy IPs from comma-separated string or list."""
        if isinstance(v, str):
            value = v.strip()
            if not value:
                return []
            if value.startswith("["):
                parsed = json.loads(value)
                if not isinstance(parsed, list):
                    raise ValueError("TRUSTED_PROXY_IPS JSON value must be a list")
                return [str(ip).strip() for ip in parsed if str(ip).strip()]
            return [ip.strip() for ip in value.split(",") if ip.strip()]
        if isinstance(v, list):
            return [str(ip).strip() for ip in v if str(ip).strip()]
        return v

    # Hier wird LLM_PROVIDER schon beim App-Start gegen das erlaubte Set
    # geprueft, statt erst beim ersten LLM-Call in llm/factory.py zu
    # scheitern. Kleinschreibung + Strip ist tolerant; Bindestrich/
    # Unterstrich-Verwechslungen (haeufiger Schreibfehler) werden mit
    # einer klaren Fehlermeldung abgewiesen.
    _LLM_PROVIDER_ALIASES: ClassVar[dict[str, str]] = {
        "mistral-cloud": "mistral_cloud",
        "local-mistral": "local_mistral",
    }

    @field_validator("llm_provider", mode="before")
    @classmethod
    def validate_llm_provider(cls, v: Any) -> str:
        """Normalize and validate LLM_PROVIDER against the allowed set."""
        if not isinstance(v, str):
            raise ValueError("LLM_PROVIDER must be a string")
        value = v.strip().lower()
        if value in cls._LLM_PROVIDER_ALIASES:
            raise ValueError(
                f"LLM_PROVIDER='{v}' uses a hyphen, but the code expects "
                f"underscores. Use '{cls._LLM_PROVIDER_ALIASES[value]}' "
                f"instead."
            )
        allowed = {"mistral_cloud", "local_mistral", "ollama"}
        if value not in allowed:
            raise ValueError(
                f"Invalid LLM_PROVIDER='{v}'. "
                f"Valid options: {sorted(allowed)}."
            )
        return value

    @model_validator(mode="before")
    @classmethod
    def load_secrets_from_files(cls, data: Any) -> Any:
        """Allow *_FILE settings to populate sensitive values from mounted secrets."""
        if not isinstance(data, dict):
            return data

        settings = dict(data)
        file_mapping = {
            "database_url_file": "database_url",
            "api_secret_key_file": "api_secret_key",
            "epro_auth_token_file": "epro_auth_token",
            "epro_webhook_url_file": "epro_webhook_url",
            "mistral_api_key_file": "mistral_api_key",
        }

        for file_field, target_field in file_mapping.items():
            file_path = settings.get(file_field)
            if file_path:
                settings[target_field] = _read_secret_file(file_path, file_field.upper())

        return settings

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
