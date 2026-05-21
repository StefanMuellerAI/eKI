"""Endpoint tests for /v1/kb/documents (M06).

The KB service requires pgvector which is not available in the SQLite
in-memory fixture used by the test suite, so we patch
``KnowledgeBaseService`` to verify the router contract: routes exist,
authentication is enforced, input validation triggers 422, conflicts
surface as 409, and the response shape matches ``KBDocumentResponse``.
"""

from __future__ import annotations

from datetime import datetime, timezone
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch
from uuid import uuid4

import pytest

from core.exceptions import ConflictException, NotFoundException, ValidationException
from services.knowledge_base import KBDocumentSummary


def _summary(title: str = "Stunt-SOP", source: str = "PLACEHOLDER") -> KBDocumentSummary:
    return KBDocumentSummary(
        doc_id=uuid4(),
        title=title,
        source=source,
        tags=["placeholder", "stunt"],
        uploaded_by="test-user-123",
        created_at=datetime.now(timezone.utc),
        expires_at=datetime.now(timezone.utc),
        chunk_count=4,
    )


@pytest.mark.asyncio
async def test_upload_requires_authentication(client) -> None:
    resp = client.post(
        "/v1/kb/documents",
        files={"file": ("x.md", b"# hi", "text/markdown")},
        data={"title": "X", "source": "UPLOAD"},
    )
    # FastAPI returns 403 for missing bearer credentials via HTTPBearer
    assert resp.status_code in (401, 403)


@pytest.mark.asyncio
async def test_upload_happy_path(client, auth_headers) -> None:
    summary = _summary()

    service_mock = SimpleNamespace(
        ingest=AsyncMock(return_value=summary.doc_id),
        get_document=AsyncMock(return_value=summary),
    )
    with patch("api.routers.knowledge_base._service", return_value=service_mock):
        resp = client.post(
            "/v1/kb/documents",
            headers=auth_headers,
            files={"file": ("01_stunt_sop.md", b"---\ntitle: Stunt\n---\nBody", "text/markdown")},
            data={
                "title": "Stunt-SOP",
                "source": "PLACEHOLDER",
                "tags": "placeholder,stunt",
                "ttl_hours": "720",
            },
        )

    assert resp.status_code == 201, resp.text
    body = resp.json()
    assert body["title"] == "Stunt-SOP"
    assert body["source"] == "PLACEHOLDER"
    assert body["chunk_count"] == 4
    assert "placeholder" in body["tags"]


@pytest.mark.asyncio
async def test_upload_duplicate_returns_409(client, auth_headers) -> None:
    service_mock = SimpleNamespace(
        ingest=AsyncMock(side_effect=ConflictException("dup", details={"content_hash": "abc"})),
        get_document=AsyncMock(),
    )
    with patch("api.routers.knowledge_base._service", return_value=service_mock):
        resp = client.post(
            "/v1/kb/documents",
            headers=auth_headers,
            files={"file": ("x.md", b"same", "text/markdown")},
            data={"title": "X", "source": "UPLOAD", "tags": "", "ttl_hours": "720"},
        )

    assert resp.status_code == 409


@pytest.mark.asyncio
async def test_upload_validation_error_returns_422(client, auth_headers) -> None:
    service_mock = SimpleNamespace(
        ingest=AsyncMock(side_effect=ValidationException("File is empty")),
        get_document=AsyncMock(),
    )
    with patch("api.routers.knowledge_base._service", return_value=service_mock):
        resp = client.post(
            "/v1/kb/documents",
            headers=auth_headers,
            files={"file": ("x.md", b"", "text/markdown")},
            data={"title": "X", "source": "UPLOAD", "tags": "", "ttl_hours": "720"},
        )

    assert resp.status_code == 422


@pytest.mark.asyncio
async def test_upload_rejects_too_many_tags(client, auth_headers) -> None:
    tags = ",".join(f"t{i}" for i in range(20))
    resp = client.post(
        "/v1/kb/documents",
        headers=auth_headers,
        files={"file": ("x.md", b"hi", "text/markdown")},
        data={"title": "X", "source": "UPLOAD", "tags": tags, "ttl_hours": "720"},
    )
    assert resp.status_code == 422


@pytest.mark.asyncio
async def test_list_returns_summaries(client, auth_headers) -> None:
    summaries = [_summary("Stunt-SOP"), _summary("Fire SFX")]
    service_mock = SimpleNamespace(
        list_documents=AsyncMock(return_value=summaries),
    )
    with patch("api.routers.knowledge_base._service", return_value=service_mock):
        resp = client.get("/v1/kb/documents", headers=auth_headers)

    assert resp.status_code == 200
    body = resp.json()
    assert body["total_returned"] == 2
    assert {d["title"] for d in body["documents"]} == {"Stunt-SOP", "Fire SFX"}


@pytest.mark.asyncio
async def test_get_missing_returns_404(client, auth_headers) -> None:
    service_mock = SimpleNamespace(
        get_document=AsyncMock(side_effect=NotFoundException("not found")),
    )
    with patch("api.routers.knowledge_base._service", return_value=service_mock):
        resp = client.get(f"/v1/kb/documents/{uuid4()}", headers=auth_headers)

    assert resp.status_code == 404


@pytest.mark.asyncio
async def test_delete_single_document(client, auth_headers) -> None:
    doc_id = uuid4()
    service_mock = SimpleNamespace(delete_document=AsyncMock())
    with patch("api.routers.knowledge_base._service", return_value=service_mock):
        resp = client.delete(f"/v1/kb/documents/{doc_id}", headers=auth_headers)

    assert resp.status_code == 200
    body = resp.json()
    assert body["deleted"] is True
    assert body["doc_id"] == str(doc_id)


@pytest.mark.asyncio
async def test_delete_by_tag_for_placeholder_wipe(client, auth_headers) -> None:
    service_mock = SimpleNamespace(delete_by_tag=AsyncMock(return_value=6))
    with patch("api.routers.knowledge_base._service", return_value=service_mock):
        resp = client.delete(
            "/v1/kb/documents",
            headers=auth_headers,
            params={"tag": "placeholder"},
        )

    assert resp.status_code == 200
    body = resp.json()
    assert body["deleted"] is True
    assert body["tag"] == "placeholder"
    assert body["count"] == 6


@pytest.mark.asyncio
async def test_delete_by_tag_rejects_empty_tag(client, auth_headers) -> None:
    resp = client.delete(
        "/v1/kb/documents",
        headers=auth_headers,
        params={"tag": "   "},
    )
    assert resp.status_code == 422
