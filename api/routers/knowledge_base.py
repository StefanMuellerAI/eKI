"""Knowledge Base management endpoints (M06).

Provides upload/list/get/delete for safety documents that augment the
risk-analysis prompt.  Endpoints are namespaced under ``/v1/kb`` so they
cannot affect the existing ``/v1/security`` security-check flow.

The tenant scope is fixed to ``settings.kb_default_tenant_id`` until
multi-tenant routing is required (Pflichtenheft §4.3: Single-Tenant
Filmakademie).  API-key ownership is checked the same way as in the
security endpoints, so a leaked Filmakademie key cannot reach another
tenant's documents.
"""

from __future__ import annotations

import logging
from uuid import UUID

from fastapi import APIRouter, Depends, File, Form, HTTPException, Path, UploadFile, status
from pydantic import BaseModel, Field
from sqlalchemy.ext.asyncio import AsyncSession

from api.config import get_settings
from api.dependencies import get_db, verify_api_key
from api.rate_limiting import rate_limit_combined
from core.db_models import ApiKeyModel
from core.exceptions import (
    ConflictException,
    NotFoundException,
    ValidationException,
)
from llm.factory import get_llm_provider
from services.knowledge_base import KBDocumentSummary, KnowledgeBaseService

logger = logging.getLogger(__name__)

router = APIRouter()

_MAX_TAGS = 16
_MIN_TTL_HOURS = 1
_MAX_TTL_HOURS = 24 * 365  # 1 year hard cap


# ---------------------------------------------------------------------------
# Response models
# ---------------------------------------------------------------------------


class KBDocumentResponse(BaseModel):
    """Public representation of a KB document (no original text exposed)."""

    doc_id: UUID
    title: str
    source: str
    tags: list[str]
    uploaded_by: str
    created_at: str
    expires_at: str
    chunk_count: int

    @classmethod
    def from_summary(cls, summary: KBDocumentSummary) -> "KBDocumentResponse":
        return cls(
            doc_id=summary.doc_id,
            title=summary.title,
            source=summary.source,
            tags=summary.tags,
            uploaded_by=summary.uploaded_by,
            created_at=summary.created_at.isoformat(),
            expires_at=summary.expires_at.isoformat(),
            chunk_count=summary.chunk_count,
        )


class KBListResponse(BaseModel):
    """List response with simple pagination metadata."""

    total_returned: int = Field(..., description="Number of documents in this response")
    documents: list[KBDocumentResponse]


class KBDeleteResponse(BaseModel):
    """Returned by single-document delete."""

    deleted: bool = True
    doc_id: UUID


class KBDeleteByTagResponse(BaseModel):
    """Returned by tag-based bulk delete (e.g. wipe placeholders)."""

    deleted: bool = True
    tag: str
    count: int


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _service(db: AsyncSession) -> KnowledgeBaseService:
    settings = get_settings()
    return KnowledgeBaseService(
        db=db,
        llm=get_llm_provider(settings),
        secret_key=settings.api_secret_key,
    )


def _tenant_id() -> UUID:
    return UUID(get_settings().kb_default_tenant_id)


def _parse_tags(raw: str | None) -> list[str]:
    if not raw:
        return []
    parts = [t.strip() for t in raw.split(",") if t and t.strip()]
    if len(parts) > _MAX_TAGS:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail=f"Too many tags (max {_MAX_TAGS})",
        )
    return parts


# ---------------------------------------------------------------------------
# Endpoints
# ---------------------------------------------------------------------------


@router.post(
    "/documents",
    response_model=KBDocumentResponse,
    status_code=status.HTTP_201_CREATED,
    summary="Upload a KB document",
    description=(
        "Upload a safety document (.pdf, .md, .txt) into the project knowledge "
        "base. The document is chunked, embedded, and stored Fernet-encrypted. "
        "Idempotent via SHA-256 content hash: re-uploading the same content "
        "returns 409 Conflict."
    ),
    dependencies=[Depends(rate_limit_combined)],
)
async def upload_document(
    file: UploadFile = File(..., description="PDF, Markdown, or plain text file"),
    title: str = Form(..., min_length=1, max_length=255),
    source: str = Form("UPLOAD", description="UPLOAD | SHARE | URL | PLACEHOLDER"),
    tags: str | None = Form(None, description="Comma-separated tags"),
    ttl_hours: int = Form(720, ge=_MIN_TTL_HOURS, le=_MAX_TTL_HOURS),
    api_key: ApiKeyModel = Depends(verify_api_key),
    db: AsyncSession = Depends(get_db),
) -> KBDocumentResponse:
    raw = await file.read()
    tag_list = _parse_tags(tags)

    svc = _service(db)
    try:
        doc_id = await svc.ingest(
            file_bytes=raw,
            filename=file.filename or "upload",
            title=title,
            source=source,
            tags=tag_list,
            ttl_hours=ttl_hours,
            tenant_id=_tenant_id(),
            uploaded_by=api_key.user_id,
        )
    except ConflictException as exc:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=exc.message,
        ) from exc
    except ValidationException as exc:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail=exc.message,
        ) from exc

    summary = await svc.get_document(doc_id=doc_id, tenant_id=_tenant_id())
    return KBDocumentResponse.from_summary(summary)


@router.get(
    "/documents",
    response_model=KBListResponse,
    summary="List KB documents",
    description="List documents in the project knowledge base (no content).",
    dependencies=[Depends(rate_limit_combined)],
)
async def list_documents(
    tag: str | None = None,
    limit: int = 100,
    offset: int = 0,
    _api_key: ApiKeyModel = Depends(verify_api_key),
    db: AsyncSession = Depends(get_db),
) -> KBListResponse:
    if limit < 1 or limit > 500:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail="limit must be between 1 and 500",
        )
    if offset < 0:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail="offset must be >= 0",
        )

    svc = _service(db)
    summaries = await svc.list_documents(
        tenant_id=_tenant_id(),
        tag_filter=tag,
        limit=limit,
        offset=offset,
    )
    return KBListResponse(
        total_returned=len(summaries),
        documents=[KBDocumentResponse.from_summary(s) for s in summaries],
    )


@router.get(
    "/documents/{doc_id}",
    response_model=KBDocumentResponse,
    summary="Get KB document metadata",
    description="Return metadata for a single KB document. Original text is never returned.",
    dependencies=[Depends(rate_limit_combined)],
)
async def get_document(
    doc_id: UUID = Path(..., description="Document ID"),
    _api_key: ApiKeyModel = Depends(verify_api_key),
    db: AsyncSession = Depends(get_db),
) -> KBDocumentResponse:
    svc = _service(db)
    try:
        summary = await svc.get_document(doc_id=doc_id, tenant_id=_tenant_id())
    except NotFoundException as exc:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=exc.message,
        ) from exc
    return KBDocumentResponse.from_summary(summary)


@router.delete(
    "/documents/{doc_id}",
    response_model=KBDeleteResponse,
    summary="Delete a KB document",
    description="Delete a single KB document. Chunks cascade.",
    dependencies=[Depends(rate_limit_combined)],
)
async def delete_document(
    doc_id: UUID = Path(..., description="Document ID"),
    _api_key: ApiKeyModel = Depends(verify_api_key),
    db: AsyncSession = Depends(get_db),
) -> KBDeleteResponse:
    svc = _service(db)
    try:
        await svc.delete_document(doc_id=doc_id, tenant_id=_tenant_id())
    except NotFoundException as exc:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=exc.message,
        ) from exc
    return KBDeleteResponse(doc_id=doc_id)


@router.delete(
    "/documents",
    response_model=KBDeleteByTagResponse,
    summary="Delete KB documents by tag",
    description=(
        "Bulk delete all documents carrying the given tag. Typical use: "
        "`?tag=placeholder` to wipe seed data once real content from the "
        "safety officer is loaded."
    ),
    dependencies=[Depends(rate_limit_combined)],
)
async def delete_documents_by_tag(
    tag: str,
    _api_key: ApiKeyModel = Depends(verify_api_key),
    db: AsyncSession = Depends(get_db),
) -> KBDeleteByTagResponse:
    if not tag.strip():
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail="tag must not be empty",
        )
    svc = _service(db)
    count = await svc.delete_by_tag(tenant_id=_tenant_id(), tag=tag.strip())
    return KBDeleteByTagResponse(tag=tag.strip(), count=count)
