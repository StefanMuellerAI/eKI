"""API-layer gaps: settings parsing, dependency generators, app-level handlers,
readiness, multipart edge cases, KB router validation, request-model
validators and the Temporal context interceptor.

Runs unchanged in SQLite mode and with ``TEST_POSTGRES_URL`` (PostgreSQL).
"""

from __future__ import annotations

import base64
from datetime import UTC, datetime
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch
from uuid import UUID, uuid4

import pytest
from cryptography.fernet import Fernet
from fastapi import HTTPException, status
from fastapi.security import HTTPAuthorizationCredentials
from fastapi.testclient import TestClient
from pydantic import ValidationError
from redis.exceptions import ConnectionError as RedisConnectionError
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError, OperationalError
from temporalio import converter
from temporalio.api.common.v1 import Payload
from temporalio.exceptions import TemporalError

from api import dependencies
from api.config import Settings, get_settings
from api.dependencies import get_db, get_redis, get_temporal_client, verify_api_key
from api.main import app
from api.routers import knowledge_base as kb_router
from core import metrics, temporal_context
from core.db_models import JobMetadata
from core.exceptions import NotFoundException, ServiceUnavailableException
from core.models import JobStatus, SecurityCheckRequest
from core.temporal_context import (
    _WORKFLOW_REQUEST_ID,
    HEADER_REQUEST_ID,
    ContextPropagationInterceptor,
    _header_value,
    _WorkflowOutbound,
)
from core.version import __version__
from services.knowledge_base import KBDocumentSummary, _derive_kb_fernet_key
from tests.conftest import MINIMAL_PDF_BYTES

FDX_BYTES = b'<?xml version="1.0"?><FinalDraft DocumentType="Script"><Content/></FinalDraft>'


def _b64(raw: bytes) -> str:
    return base64.b64encode(raw).decode("ascii")


# ===========================================================================
# api/config.py
# ===========================================================================


class TestSecretFiles:
    @pytest.mark.parametrize(
        ("file_env", "field", "value"),
        [
            ("DATABASE_URL_FILE", "database_url", "postgresql+asyncpg://u:pw@db:5432/fromfile"),
            ("API_SECRET_KEY_FILE", "api_secret_key", "file-secret-0123456789abcdef-0123456"),
            ("EPRO_AUTH_TOKEN_FILE", "epro_auth_token", "epro-token-from-file"),
            ("EPRO_WEBHOOK_URL_FILE", "epro_webhook_url", "https://epro.example/hook"),
            ("MISTRAL_API_KEY_FILE", "mistral_api_key", "mistral-key-from-file"),
        ],
    )
    def test_file_value_wins_over_env_and_is_stripped(
        self, tmp_path, monkeypatch, file_env, field, value
    ):
        secret = tmp_path / "secret.txt"
        secret.write_text(f"\n  {value}  \n", encoding="utf-8")
        direct_env = file_env.removesuffix("_FILE")
        monkeypatch.setenv(direct_env, "postgresql+asyncpg://env:env@env:5432/env")
        monkeypatch.setenv(file_env, str(secret))

        settings = Settings(_env_file=None)

        assert str(getattr(settings, field)) == value

    def test_missing_file_is_rejected_with_variable_name(self, tmp_path, monkeypatch):
        missing = tmp_path / "does-not-exist"
        monkeypatch.setenv("API_SECRET_KEY_FILE", str(missing))
        with pytest.raises(ValidationError, match="API_SECRET_KEY_FILE points to unreadable file"):
            Settings(_env_file=None)

    def test_directory_is_rejected_as_unreadable(self, tmp_path, monkeypatch):
        monkeypatch.setenv("EPRO_AUTH_TOKEN_FILE", str(tmp_path))
        with pytest.raises(ValidationError, match="EPRO_AUTH_TOKEN_FILE points to unreadable file"):
            Settings(_env_file=None)

    def test_whitespace_only_file_is_rejected(self, tmp_path, monkeypatch):
        empty = tmp_path / "empty.txt"
        empty.write_text(" \n\t\n", encoding="utf-8")
        monkeypatch.setenv("MISTRAL_API_KEY_FILE", str(empty))
        with pytest.raises(ValidationError, match="MISTRAL_API_KEY_FILE points to empty file"):
            Settings(_env_file=None)

    def test_secret_loader_passes_non_dict_input_through(self):
        assert Settings.load_secrets_from_files("raw-input") == "raw-input"


class TestListSettingsParsing:
    @pytest.mark.parametrize("raw", ["", "   "])
    def test_blank_cors_origins_mean_no_origins(self, monkeypatch, raw):
        monkeypatch.setenv("CORS_ORIGINS", raw)
        assert Settings(_env_file=None).cors_origins == []

    def test_cors_origins_list_input_is_cleaned(self):
        settings = Settings(_env_file=None, cors_origins=[" https://a.example ", "", "  "])
        assert settings.cors_origins == ["https://a.example"]

    def test_cors_origins_of_wrong_type_are_rejected(self):
        with pytest.raises(ValidationError, match="cors_origins"):
            Settings(_env_file=None, cors_origins=None)

    def test_trusted_proxy_ips_from_json_list(self, monkeypatch):
        monkeypatch.setenv("TRUSTED_PROXY_IPS", '["10.0.0.1", " 10.0.0.2 ", ""]')
        assert Settings(_env_file=None).trusted_proxy_ips == ["10.0.0.1", "10.0.0.2"]

    def test_trusted_proxy_ips_invalid_json_is_rejected(self, monkeypatch):
        monkeypatch.setenv("TRUSTED_PROXY_IPS", "[10.0.0.1")
        with pytest.raises(ValueError):
            Settings(_env_file=None)

    def test_trusted_proxy_ips_list_input_is_cleaned(self):
        settings = Settings(_env_file=None, trusted_proxy_ips=[" 10.0.0.9", "", "10.0.0.8 "])
        assert settings.trusted_proxy_ips == ["10.0.0.9", "10.0.0.8"]

    def test_trusted_proxy_ips_of_wrong_type_are_rejected(self):
        with pytest.raises(ValidationError, match="trusted_proxy_ips"):
            Settings(_env_file=None, trusted_proxy_ips=None)

    def test_llm_provider_must_be_a_string(self):
        with pytest.raises(ValidationError, match="LLM_PROVIDER must be a string"):
            Settings(_env_file=None, llm_provider=123)

    @pytest.mark.parametrize(
        ("env", "development", "production"),
        [("development", True, False), ("stage", False, False), ("test", False, False)],
    )
    def test_environment_flags(self, env, development, production):
        settings = Settings(_env_file=None, env=env)
        assert settings.is_development is development
        assert settings.is_production is production


# ===========================================================================
# api/dependencies.py
# ===========================================================================


@pytest.mark.asyncio
class TestDependencyGenerators:
    async def test_settings_dependency_returns_the_cached_settings(self):
        assert await dependencies.get_settings_dependency() is get_settings()

    async def test_get_db_yields_the_session_and_finalises_it(self, monkeypatch):
        session = object()
        finalised: list[bool] = []

        async def fake_get_db_session():
            try:
                yield session
            finally:
                finalised.append(True)

        monkeypatch.setattr(dependencies, "get_db_session", fake_get_db_session)

        gen = get_db()
        assert await gen.__anext__() is session
        with pytest.raises(StopAsyncIteration):
            await gen.__anext__()
        assert finalised == [True]

    async def test_get_redis_builds_client_from_settings_and_closes_it(self, monkeypatch):
        redis_client = AsyncMock()
        from_url = MagicMock(return_value=redis_client)
        monkeypatch.setattr(dependencies.aioredis, "from_url", from_url)
        settings = get_settings()

        gen = get_redis()
        assert await gen.__anext__() is redis_client
        from_url.assert_called_once_with(
            str(settings.redis_url),
            encoding="utf-8",
            decode_responses=True,
            max_connections=settings.redis_max_connections,
        )
        redis_client.aclose.assert_not_awaited()

        # FastAPI throws the handler's exception into the generator: still closed.
        with pytest.raises(RuntimeError, match="handler failed"):
            await gen.athrow(RuntimeError("handler failed"))
        redis_client.aclose.assert_awaited_once()

    async def test_get_temporal_client_connects_with_interceptors(self):
        temporal_client = object()
        connect = AsyncMock(return_value=temporal_client)
        with patch.object(dependencies.TemporalClient, "connect", new=connect):
            gen = get_temporal_client()
            assert await gen.__anext__() is temporal_client

        args, kwargs = connect.call_args
        assert args == (get_settings().temporal_host,)
        assert any(isinstance(i, ContextPropagationInterceptor) for i in kwargs["interceptors"])

    async def test_unreachable_temporal_raises_service_unavailable(self):
        cause = RuntimeError("connection refused")
        with patch.object(dependencies.TemporalClient, "connect", new=AsyncMock(side_effect=cause)):
            with pytest.raises(ServiceUnavailableException) as exc_info:
                await get_temporal_client().__anext__()

        assert exc_info.value.status_code == status.HTTP_503_SERVICE_UNAVAILABLE
        assert exc_info.value.message == "Workflow engine is not reachable. Please try again later."
        assert exc_info.value.details == {"service": "temporal"}
        assert exc_info.value.__cause__ is cause

    async def test_empty_bearer_token_is_rejected_before_db_lookup(self):
        db = AsyncMock()
        with pytest.raises(HTTPException) as exc_info:
            await verify_api_key(
                credentials=HTTPAuthorizationCredentials(scheme="Bearer", credentials=""), db=db
            )
        assert exc_info.value.status_code == status.HTTP_401_UNAUTHORIZED
        assert exc_info.value.detail == "Invalid token"
        assert exc_info.value.headers == {"WWW-Authenticate": "Bearer"}
        db.execute.assert_not_awaited()


class TestTemporalUnavailableOverHttp:
    @pytest.mark.asyncio
    async def test_async_check_returns_503_without_side_effects(
        self, client, auth_headers, mock_redis
    ):
        app.dependency_overrides.pop(get_temporal_client)
        with patch.object(
            dependencies.TemporalClient,
            "connect",
            new=AsyncMock(side_effect=OSError("connection refused")),
        ):
            response = client.post(
                "/v1/security/check:async",
                json={
                    "script_content": _b64(MINIMAL_PDF_BYTES),
                    "script_format": "pdf",
                    "project_id": "no-temporal",
                },
                headers=auth_headers,
            )

        assert response.status_code == status.HTTP_503_SERVICE_UNAVAILABLE
        body = response.json()
        assert body["error"] == "ServiceUnavailableException"
        assert body["message"] == "Workflow engine is not reachable. Please try again later."
        assert body["details"] == [{"field": None, "message": "temporal", "error_code": None}]
        assert not [k for k in mock_redis._store if str(k).startswith("eki:buf")]


# ===========================================================================
# api/main.py
# ===========================================================================


class TestLifespan:
    def test_startup_publishes_build_info(self):
        from prometheus_client import REGISTRY

        metrics.set_build_info(version="0.0.0", llm_provider="none", role="worker")
        settings = get_settings()

        with patch("api.main.set_build_info", wraps=metrics.set_build_info) as spy:
            with TestClient(app) as test_client:
                assert test_client.get("/health").status_code == status.HTTP_200_OK

        spy.assert_called_once_with(
            version=__version__, llm_provider=settings.llm_provider, role="api"
        )
        labels = {"version": __version__, "llm_provider": settings.llm_provider, "role": "api"}
        assert REGISTRY.get_sample_value("eki_build_info", labels) == 1.0


_KB_LIST = "/v1/kb/documents"


class TestUnhandledExceptionHandler:
    @pytest.mark.parametrize(
        ("exc", "expected_status", "error", "message"),
        [
            (
                OperationalError("SELECT 1", {}, Exception("server closed the connection")),
                503,
                "ServiceUnavailable",
                "Database service is temporarily unavailable. Please try again later.",
            ),
            (
                RedisConnectionError("Error 111 connecting to redis:6379"),
                503,
                "ServiceUnavailable",
                "Cache service is temporarily unavailable. Please try again later.",
            ),
            (
                TemporalError("namespace not found"),
                503,
                "ServiceUnavailable",
                "Workflow service is temporarily unavailable. Please try again later.",
            ),
            (
                ConnectionRefusedError("[Errno 111] refused"),
                503,
                "ServiceUnavailable",
                "A backend service is temporarily unavailable. Please try again later.",
            ),
            (
                TimeoutError("read timed out"),
                503,
                "ServiceUnavailable",
                "A backend service is temporarily unavailable. Please try again later.",
            ),
            (
                RuntimeError("secret internal detail"),
                500,
                "InternalServerError",
                "An internal error occurred. Please try again later.",
            ),
        ],
        ids=["sqlalchemy", "redis", "temporal", "conn-refused", "timeout", "generic"],
    )
    @pytest.mark.asyncio
    async def test_unexpected_errors_are_classified_without_leaking_details(
        self, client, auth_headers, exc, expected_status, error, message
    ):
        with patch.object(kb_router, "_service", side_effect=exc):
            response = client.get(
                _KB_LIST, headers={**auth_headers, "X-Request-ID": "rid-unhandled"}
            )

        assert response.status_code == expected_status
        body = response.json()
        assert body["error"] == error
        assert body["message"] == message
        assert body["details"] == []
        assert body["request_id"] == "rid-unhandled"
        assert response.headers["X-Request-ID"] == "rid-unhandled"
        assert str(exc) not in response.text


class TestPydanticValidationErrorHandler:
    @pytest.mark.asyncio
    async def test_raw_validation_error_inside_handler_becomes_422(self, client, auth_headers):
        """A ``ValidationError`` raised by our own code (not request parsing) is normalised."""
        broken = KBDocumentSummary(
            doc_id="not-a-uuid",  # type: ignore[arg-type]
            title="t",
            source="UPLOAD",
            tags=[],
            uploaded_by="u",
            created_at=datetime.now(UTC),
            expires_at=datetime.now(UTC),
            chunk_count=1,
        )
        service = SimpleNamespace(list_documents=AsyncMock(return_value=[broken]))
        with patch.object(kb_router, "_service", return_value=service):
            response = client.get(_KB_LIST, headers=auth_headers)

        assert response.status_code == status.HTTP_422_UNPROCESSABLE_ENTITY
        body = response.json()
        assert body["error"] == "ValidationError"
        assert body["message"] == "Request validation failed"
        assert [(d["field"], d["error_code"]) for d in body["details"]] == [
            ("doc_id", "uuid_parsing")
        ]


class TestCustomOpenApi:
    @pytest.fixture(autouse=True)
    def _fresh_schema(self):
        saved = app.openapi_schema
        app.openapi_schema = None
        yield
        app.openapi_schema = saved

    def test_schema_is_built_once_and_documents_both_body_types(self):
        schema = app.openapi()
        assert app.openapi() is schema  # cached

        async_body = schema["paths"]["/v1/security/check:async"]["post"]["requestBody"]
        assert async_body["required"] is True
        assert async_body["content"]["application/json"]["schema"] == {
            "$ref": "#/components/schemas/AsyncSecurityCheckRequest"
        }
        assert "priority" in async_body["content"]["multipart/form-data"]["schema"]["properties"]

        sync_body = schema["paths"]["/v1/security/check"]["post"]["requestBody"]
        assert sync_body["content"]["application/json"]["schema"] == {
            "$ref": "#/components/schemas/SecurityCheckRequest"
        }
        assert "priority" not in sync_body["content"]["multipart/form-data"]["schema"]["properties"]
        components = schema["components"]["schemas"]
        assert {"SecurityCheckRequest", "AsyncSecurityCheckRequest", "ScriptFormat"} <= set(
            components
        )

    def test_missing_security_paths_are_skipped(self):
        with patch("api.main.get_openapi", return_value={"paths": {"/health": {"get": {}}}}):
            schema = app.openapi()
        assert schema["paths"] == {"/health": {"get": {}}}
        assert "SecurityCheckRequest" in schema["components"]["schemas"]


# ===========================================================================
# api/routers/health.py
# ===========================================================================


class _BrokenSession:
    async def execute(self, *args, **kwargs):
        raise OperationalError("SELECT 1", {}, ConnectionRefusedError("db down"))


class TestReadiness:
    @pytest.fixture
    def temporal_health(self):
        temporal_client = SimpleNamespace(
            service_client=SimpleNamespace(check_health=AsyncMock(return_value=True))
        )
        connect = AsyncMock(return_value=temporal_client)
        with patch("api.routers.health.TemporalClient.connect", new=connect):
            yield temporal_client.service_client.check_health

    @pytest.mark.asyncio
    async def test_all_dependencies_up_is_ready(self, client, auth_headers, temporal_health):
        response = client.get("/ready", headers=auth_headers)

        assert response.status_code == status.HTTP_200_OK
        body = response.json()
        assert body["status"] == "ready"
        assert body["services"] == {"database": True, "redis": True, "temporal": True}
        temporal_health.assert_awaited_once()

    @pytest.mark.parametrize("down", ["database", "redis", "temporal"])
    def test_any_dependency_down_is_503(self, client, mock_redis, temporal_health, down):
        app.dependency_overrides[verify_api_key] = lambda: SimpleNamespace(user_id="probe")
        if down == "database":

            async def broken_db():
                yield _BrokenSession()

            app.dependency_overrides[get_db] = broken_db
        elif down == "redis":
            mock_redis.ping = AsyncMock(side_effect=RedisConnectionError("redis down"))
        else:
            temporal_health.side_effect = RuntimeError("temporal down")

        response = client.get("/ready")

        assert response.status_code == status.HTTP_503_SERVICE_UNAVAILABLE
        body = response.json()
        assert body["status"] == "not_ready"
        expected = {"database": True, "redis": True, "temporal": True}
        expected[down] = False
        assert body["services"] == expected


# ===========================================================================
# api/routers/security.py -- multipart and failure branches
# ===========================================================================


class TestMultipartUploads:
    @pytest.mark.asyncio
    async def test_sync_check_accepts_fdx_upload(self, client, auth_headers):
        response = client.post(
            "/v1/security/check",
            files={"file": ("Drehbuch.FDX", FDX_BYTES, "application/xml")},
            data={"project_id": "proj-multipart"},
            headers=auth_headers,
        )

        assert response.status_code == status.HTTP_200_OK, response.text
        report = response.json()["report"]
        assert report["project_id"] == "proj-multipart"
        assert report["script_format"] == "fdx"
        assert report["metadata"]["user_id"] == "test-user-123"
        assert report["metadata"]["project_id"] == "test-project-456"

    @pytest.mark.asyncio
    async def test_async_check_passes_all_form_fields_through(
        self, client, db_session, auth_headers, mock_temporal, mock_redis
    ):
        key = f"mp-{uuid4().hex}"
        response = client.post(
            "/v1/security/check:async",
            files={"file": ("script.pdf", MINIMAL_PDF_BYTES, "application/pdf")},
            data={
                "project_id": "proj-mp-async",
                "script_format": "pdf",
                "script_id": "42",
                "priority": "2",
                "delivery": "push",
                "idempotency_key": key,
            },
            headers=auth_headers,
        )

        assert response.status_code == status.HTTP_202_ACCEPTED, response.text
        body = response.json()
        assert body["status"] == "pending"
        assert body["message"] == "Security check job started (delivery=push)"
        job_id = UUID(body["job_id"])

        job_data = mock_temporal.started[0]["args"][1]
        assert job_data["priority"] == 2
        assert job_data["script_id"] == 42
        assert job_data["delivery_mode"] == "push"
        assert job_data["script_format"] == "pdf"
        assert job_data["project_id"] == "proj-mp-async"
        assert "script_content" not in job_data
        assert job_data["ref_key"] in mock_redis._store

        job = (
            await db_session.execute(select(JobMetadata).where(JobMetadata.job_id == job_id))
        ).scalar_one()
        assert job.status == JobStatus.PENDING
        assert (job.priority, job.script_id, job.delivery_mode) == (2, 42, "push")
        assert job.idempotency_key == key
        assert job.user_id == "test-user-123"

    @pytest.mark.parametrize(
        ("path", "files", "data", "expected_status", "error", "message"),
        [
            (
                "/v1/security/check",
                {"attachment": ("a.fdx", FDX_BYTES, "application/xml")},
                {"project_id": "p"},
                400,
                "BadRequest",
                "Multipart request must include a 'file' field",
            ),
            (
                "/v1/security/check",
                {"file": ("empty.fdx", b"", "application/xml")},
                {"project_id": "p"},
                400,
                "BadRequest",
                "Uploaded file is empty",
            ),
            (
                "/v1/security/check",
                {"file": ("script.docx", b"PK\x03\x04", "application/octet-stream")},
                {"project_id": "p"},
                400,
                "BadRequest",
                "Unsupported file type. Allowed: .fdx, .pdf",
            ),
            (
                "/v1/security/check",
                {"file": ("fake.pdf", b"GIF89a not a pdf", "application/pdf")},
                {"project_id": "p"},
                400,
                "BadRequest",
                "Uploaded file does not appear to be a valid PDF",
            ),
            (
                "/v1/security/check:async",
                {"file": ("script.pdf", MINIMAL_PDF_BYTES, "application/pdf")},
                {"project_id": "p", "priority": "high"},
                422,
                "ValidationError",
                "Request validation failed: priority: must be an integer between 1 and 10",
            ),
        ],
        ids=["no-file-field", "empty-file", "bad-extension", "bad-pdf-magic", "priority-nan"],
    )
    @pytest.mark.asyncio
    async def test_invalid_uploads_are_rejected(
        self,
        client,
        auth_headers,
        mock_temporal,
        path,
        files,
        data,
        expected_status,
        error,
        message,
    ):
        response = client.post(path, files=files, data=data, headers=auth_headers)

        assert response.status_code == expected_status
        assert response.json()["error"] == error
        assert response.json()["message"] == message
        assert mock_temporal.started == []

    @pytest.mark.asyncio
    async def test_oversized_upload_is_413(self, client, auth_headers):
        with patch(
            "api.routers.security.get_settings",
            return_value=SimpleNamespace(max_upload_size_bytes=16),
        ):
            response = client.post(
                "/v1/security/check",
                files={"file": ("big.fdx", b"x" * 17, "application/xml")},
                data={"project_id": "p"},
                headers=auth_headers,
            )

        assert response.status_code == status.HTTP_413_REQUEST_ENTITY_TOO_LARGE
        assert response.json()["error"] == "PayloadTooLarge"
        assert response.json()["message"] == "File exceeds maximum size of 16 bytes"

    def test_upload_limit_falls_back_to_10_mb_without_settings(self):
        from api.routers.security import _max_upload_size

        with patch("api.routers.security.get_settings", side_effect=RuntimeError("no env")):
            assert _max_upload_size() == 10 * 1024 * 1024


class _JobInsertFails:
    """Session proxy whose commit fails like a lost unique-constraint race."""

    def __init__(self, session) -> None:
        self._session = session

    def __getattr__(self, name):
        return getattr(self._session, name)

    async def commit(self) -> None:
        if any(isinstance(obj, JobMetadata) for obj in self._session.new):
            raise IntegrityError("INSERT INTO job_metadata", {}, Exception("duplicate key"))
        await self._session.commit()


class TestAsyncFailureBranches:
    @pytest.mark.parametrize("idempotency_key", [None, "lost-race-without-winner"])
    @pytest.mark.asyncio
    async def test_integrity_error_without_existing_job_is_503(
        self, client, auth_headers, mock_temporal, mock_redis, idempotency_key
    ):
        base_override = app.dependency_overrides[get_db]

        async def failing_commit_db():
            async for session in base_override():
                yield _JobInsertFails(session)

        app.dependency_overrides[get_db] = failing_commit_db
        payload = {
            "script_content": _b64(MINIMAL_PDF_BYTES),
            "script_format": "pdf",
            "project_id": "integrity",
        }
        if idempotency_key:
            payload["idempotency_key"] = f"{idempotency_key}-{uuid4().hex}"

        response = client.post("/v1/security/check:async", json=payload, headers=auth_headers)

        assert response.status_code == status.HTTP_503_SERVICE_UNAVAILABLE
        assert response.json()["error"] == "ServiceUnavailable"
        assert response.json()["message"] == (
            "Database service is temporarily unavailable. Please try again later."
        )
        assert mock_temporal.started == []
        assert not [k for k in mock_redis._store if str(k).startswith("eki:buf")]

    @pytest.mark.asyncio
    async def test_service_unavailable_from_workflow_start_is_passed_through(
        self, client, auth_headers, mock_temporal
    ):
        mock_temporal.start_workflow = AsyncMock(
            side_effect=ServiceUnavailableException(
                "Temporal frontend overloaded", details={"service": "temporal"}
            )
        )
        response = client.post(
            "/v1/security/check:async",
            json={
                "script_content": _b64(MINIMAL_PDF_BYTES),
                "script_format": "pdf",
                "project_id": "overloaded",
            },
            headers=auth_headers,
        )

        assert response.status_code == status.HTTP_503_SERVICE_UNAVAILABLE
        assert response.json()["error"] == "ServiceUnavailableException"
        assert response.json()["message"] == "Temporal frontend overloaded"

    @pytest.mark.asyncio
    async def test_failed_buffer_cleanup_still_fails_job_cleanly(
        self, client, db_session, auth_headers, mock_temporal, mock_redis
    ):
        mock_temporal.start_workflow = AsyncMock(side_effect=RuntimeError("temporal down"))
        mock_redis.delete = AsyncMock(side_effect=RedisConnectionError("redis gone"))

        response = client.post(
            "/v1/security/check:async",
            json={
                "script_content": _b64(MINIMAL_PDF_BYTES),
                "script_format": "pdf",
                "project_id": "cleanup-fails",
            },
            headers=auth_headers,
        )

        assert response.status_code == status.HTTP_503_SERVICE_UNAVAILABLE
        body = response.json()
        assert body["error"] == "ServiceUnavailableException"
        assert body["message"] == (
            "Security check could not be started. The workflow engine is temporarily unavailable."
        )
        job_id = UUID(body["details"][0]["message"])
        mock_redis.delete.assert_awaited_once()

        db_session.expire_all()
        job = (
            await db_session.execute(select(JobMetadata).where(JobMetadata.job_id == job_id))
        ).scalar_one()
        assert job.status == JobStatus.FAILED
        assert job.error_message == "Workflow engine temporarily unavailable."
        # Cleanup failed: the encrypted buffer stays until its TTL reaps it.
        assert [k for k in mock_redis._store if str(k).startswith("eki:buf")]


# ===========================================================================
# api/routers/knowledge_base.py (service mocked; works without pgvector)
# ===========================================================================


def _summary(**overrides) -> KBDocumentSummary:
    values = {
        "doc_id": uuid4(),
        "title": "Stunt-SOP",
        "source": "UPLOAD",
        "tags": ["stunt"],
        "uploaded_by": "test-user-123",
        "created_at": datetime(2026, 10, 1, 8, 0, tzinfo=UTC),
        "expires_at": datetime(2026, 10, 31, 8, 0, tzinfo=UTC),
        "chunk_count": 3,
    }
    values.update(overrides)
    return KBDocumentSummary(**values)


class TestKnowledgeBaseRouter:
    @pytest.mark.parametrize(
        ("params", "message"),
        [
            ({"limit": 0}, "limit must be between 1 and 500"),
            ({"limit": 501}, "limit must be between 1 and 500"),
            ({"offset": -1}, "offset must be >= 0"),
        ],
    )
    @pytest.mark.asyncio
    async def test_list_rejects_out_of_range_paging(self, client, auth_headers, params, message):
        with patch.object(kb_router, "_service") as service_factory:
            response = client.get(_KB_LIST, headers=auth_headers, params=params)

        assert response.status_code == status.HTTP_422_UNPROCESSABLE_ENTITY
        assert response.json()["error"] == "ValidationError"
        assert response.json()["message"] == message
        service_factory.assert_not_called()

    @pytest.mark.asyncio
    async def test_get_returns_document_metadata(self, client, auth_headers):
        summary = _summary()
        service = SimpleNamespace(get_document=AsyncMock(return_value=summary))
        with patch.object(kb_router, "_service", return_value=service):
            response = client.get(f"{_KB_LIST}/{summary.doc_id}", headers=auth_headers)

        assert response.status_code == status.HTTP_200_OK
        assert response.json() == {
            "doc_id": str(summary.doc_id),
            "title": "Stunt-SOP",
            "source": "UPLOAD",
            "tags": ["stunt"],
            "uploaded_by": "test-user-123",
            "created_at": "2026-10-01T08:00:00+00:00",
            "expires_at": "2026-10-31T08:00:00+00:00",
            "chunk_count": 3,
        }
        service.get_document.assert_awaited_once_with(
            doc_id=summary.doc_id, tenant_id=UUID(get_settings().kb_default_tenant_id)
        )

    @pytest.mark.asyncio
    async def test_delete_unknown_document_is_404(self, client, auth_headers):
        doc_id = uuid4()
        service = SimpleNamespace(
            delete_document=AsyncMock(side_effect=NotFoundException("Document not found"))
        )
        with patch.object(kb_router, "_service", return_value=service):
            response = client.delete(f"{_KB_LIST}/{doc_id}", headers=auth_headers)

        assert response.status_code == status.HTTP_404_NOT_FOUND
        assert response.json()["error"] == "NotFound"
        assert response.json()["message"] == "Document not found"
        service.delete_document.assert_awaited_once()

    def test_service_is_wired_to_settings_secret_and_llm(self):
        settings = get_settings()
        provider = object()
        db = object()
        with patch.object(kb_router, "get_llm_provider", return_value=provider) as factory:
            service = kb_router._service(db)

        factory.assert_called_once_with(settings)
        token = Fernet(_derive_kb_fernet_key(settings.api_secret_key)).encrypt(b"geheim")
        doc = SimpleNamespace(doc_id=uuid4(), original_text_encrypted=token)
        assert service.decrypt_original(doc) == "geheim"


# ===========================================================================
# core/models.py -- request validators
# ===========================================================================


def _request(**overrides) -> SecurityCheckRequest:
    values = {"script_content": _b64(FDX_BYTES), "script_format": "fdx", "project_id": "p-1"}
    values.update(overrides)
    return SecurityCheckRequest(**values)


class TestSecurityCheckRequestValidation:
    @pytest.mark.parametrize(
        ("content", "fmt", "message"),
        [
            ("   ", "fdx", "Script content cannot be empty"),
            (
                _b64(b"<FinalDraft>\x00</FinalDraft>"),
                "fdx",
                "FDX script contains invalid null bytes",
            ),
            (_b64(b"\xff\xfe<FinalDraft/>"), "fdx", "FDX script does not appear to be valid text"),
            (_b64(b"GIF89a"), "pdf", "File does not appear to be a valid PDF"),
        ],
        ids=["blank", "fdx-null-bytes", "fdx-not-utf8", "pdf-bad-magic"],
    )
    def test_script_content_is_checked_per_format(self, content, fmt, message):
        with pytest.raises(ValidationError, match=message):
            _request(script_content=content, script_format=fmt)

    def test_valid_pdf_and_fdx_are_accepted(self):
        assert _request().script_format == "fdx"
        pdf = _request(script_content=_b64(MINIMAL_PDF_BYTES), script_format="pdf")
        assert pdf.script_format == "pdf"

    def test_payload_over_limit_is_rejected_before_decoding(self):
        too_big = _b64(b"a" * (10 * 1024 * 1024 + 1))
        with pytest.raises(ValidationError) as exc_info:
            _request(script_content=too_big)
        assert exc_info.value.errors()[0]["type"] == "string_too_long"

    def test_decoded_size_guard_is_defence_in_depth(self):
        """The field's ``max_length`` rejects first; the decoded-size check must still hold."""
        too_big = SecurityCheckRequest.model_construct(
            script_content=_b64(b"a" * (10 * 1024 * 1024 + 1)), script_format="fdx"
        )
        with pytest.raises(ValueError, match="Decoded script exceeds 10485760 byte limit"):
            too_big.validate_script_content()

    def test_callback_url_is_optional(self):
        assert _request(callback_url=None).callback_url is None

    @pytest.mark.parametrize(
        "url",
        [
            "https://epro.filmakademie.de/api/eki/callback",
            "https://STAGING.epro.filmakademie.de./cb",  # case + trailing dot normalised
        ],
    )
    def test_allowed_callback_urls(self, url):
        assert str(_request(callback_url=url).callback_url) == url.lower()

    def test_callback_url_without_host_is_rejected(self):
        with pytest.raises(ValueError, match="Invalid callback URL hostname"):
            SecurityCheckRequest.validate_callback_url("https:///no-host")

    @pytest.mark.parametrize(
        ("value", "message"),
        [("   ", "project_id cannot be empty"), ("a b", "must contain only alphanumeric")],
    )
    def test_project_id_validator_defence_in_depth(self, value, message):
        """The Field ``pattern`` rejects these first; the validator must agree."""
        with pytest.raises(ValueError, match=message):
            SecurityCheckRequest.validate_project_id(value)

    def test_metadata_accepts_scalar_values(self):
        metadata = {"scene": "12A", "count": 3, "ratio": 0.5, "flag": True, "none": None}
        assert _request(metadata=metadata).metadata == metadata

    @pytest.mark.parametrize(
        ("metadata", "message"),
        [
            ({"bad key!": "x"}, "Invalid metadata key format: bad key!"),
            ({"k" * 51: "x"}, "Invalid metadata key format"),
            ({"note": "x" * 1001}, "Metadata value too long for key 'note'"),
            ({"nested": {"a": 1}}, "Invalid metadata value type for key 'nested'"),
            ({"items": [1, 2]}, "Invalid metadata value type for key 'items'"),
        ],
        ids=["key-chars", "key-length", "value-length", "dict-value", "list-value"],
    )
    def test_metadata_rejections(self, metadata, message):
        with pytest.raises(ValidationError, match=message):
            _request(metadata=metadata)

    @pytest.mark.asyncio
    async def test_metadata_rejection_over_http_is_422(self, client, auth_headers):
        response = client.post(
            "/v1/security/check",
            json={
                "script_content": _b64(FDX_BYTES),
                "script_format": "fdx",
                "project_id": "p-1",
                "metadata": {"items": [1, 2]},
            },
            headers=auth_headers,
        )
        assert response.status_code == status.HTTP_422_UNPROCESSABLE_ENTITY
        assert "Invalid metadata value type for key 'items'" in response.json()["message"]


# ===========================================================================
# core/temporal_context.py
# ===========================================================================


class _RecordingOutbound:
    def __init__(self) -> None:
        self.local_input = None
        self.child_input = None

    def start_local_activity(self, input):
        self.local_input = input
        return "local-handle"

    async def start_child_workflow(self, input):
        self.child_input = input
        return "child-handle"


class TestTemporalContext:
    @pytest.mark.parametrize(
        "payload",
        [
            Payload(metadata={"encoding": b"binary/unknown"}, data=b"\x00"),
            converter.default().payload_converter.to_payload(12345),
        ],
        ids=["unknown-encoding", "not-a-string"],
    )
    def test_undecodable_header_yields_empty_request_id(self, payload):
        pc = converter.default().payload_converter
        assert _header_value({HEADER_REQUEST_ID: payload}, pc) == ""

    @pytest.fixture
    def workflow_converter(self):
        pc = converter.default().payload_converter
        with patch.object(temporal_context.workflow, "payload_converter", return_value=pc):
            yield pc

    @pytest.mark.asyncio
    async def test_local_activities_and_child_workflows_inherit_request_id(
        self, workflow_converter
    ):
        nxt = _RecordingOutbound()
        outbound = _WorkflowOutbound(nxt)
        existing = workflow_converter.to_payload("keep-me")
        token = _WORKFLOW_REQUEST_ID.set("rid-workflow")
        try:
            local = outbound.start_local_activity(SimpleNamespace(headers={"other": existing}))
            child = await outbound.start_child_workflow(SimpleNamespace(headers=None))
        finally:
            _WORKFLOW_REQUEST_ID.reset(token)

        assert (local, child) == ("local-handle", "child-handle")
        assert nxt.local_input.headers["other"] is existing
        assert _header_value(nxt.local_input.headers, workflow_converter) == "rid-workflow"
        assert _header_value(nxt.child_input.headers, workflow_converter) == "rid-workflow"

    @pytest.mark.asyncio
    async def test_without_request_id_headers_are_untouched(self, workflow_converter):
        nxt = _RecordingOutbound()
        outbound = _WorkflowOutbound(nxt)
        headers = {"other": workflow_converter.to_payload("x")}

        outbound.start_local_activity(SimpleNamespace(headers=headers))
        await outbound.start_child_workflow(SimpleNamespace(headers=headers))

        assert nxt.local_input.headers is headers
        assert nxt.child_input.headers is headers
