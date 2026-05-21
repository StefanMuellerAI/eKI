"""Knowledge Base service (M06).

Provides:

* ingest of safety documents (PDF / Markdown / Plain text) into pgvector
* top-k retrieval by cosine similarity for the RAG-augmented risk analysis
* listing, deletion, and TTL-based cleanup

The original document text is Fernet-encrypted at rest (same key derivation
as :class:`services.secure_buffer.SecureBuffer`). The vector and chunk text
itself live alongside in plaintext because pgvector cannot search ciphertext;
both are inside the encrypted Postgres volume, so the encryption guard is
the application key controlling decryption of the *original* document.
"""

from __future__ import annotations

import base64
import hashlib
import io
import logging
import re
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any
from uuid import UUID, uuid4

from cryptography.fernet import Fernet, InvalidToken
from sqlalchemy import delete, func, select
from sqlalchemy.ext.asyncio import AsyncSession

from core.db_models import KnowledgeDocument, KnowledgeEmbedding
from core.exceptions import (
    ConflictException,
    EKIException,
    NotFoundException,
    ValidationException,
)
from llm.base import BaseLLMProvider

logger = logging.getLogger(__name__)

# Match SecureBuffer's key derivation so operators only need one master secret.
_FERNET_KEY_SALT = b"eki-kb-doc-v1"

# Allowed extensions for ingest.  PDF + plain-text formats only -- no image
# documents.  OCR is out of scope for M06 (matches §4.3 KB scope).
_ALLOWED_EXTENSIONS = {".pdf", ".md", ".markdown", ".txt"}

# Chunking parameters (Pflichtenheft §4.3: 800-1500 tokens).  We approximate
# tokens via characters (1 token ~ 4 chars for German/English text).
_CHUNK_CHARS_TARGET = 4800   # ~1200 tokens
_CHUNK_CHARS_MAX = 6000      # ~1500 tokens
_CHUNK_CHARS_MIN = 3200      # ~800 tokens
_CHUNK_CHAR_OVERLAP = 400    # ~100 tokens of overlap between chunks

_MAX_UPLOAD_BYTES = 10 * 1024 * 1024  # 10 MB, mirrors security check upload


@dataclass
class KBSearchHit:
    """One retrieval result for prompt assembly."""

    doc_id: UUID
    title: str
    tags: list[str]
    chunk_id: str
    chunk_text: str
    distance: float  # cosine distance, lower is more similar


@dataclass
class KBDocumentSummary:
    """Lightweight document listing (no original text exposed)."""

    doc_id: UUID
    title: str
    source: str
    tags: list[str]
    uploaded_by: str
    created_at: datetime
    expires_at: datetime
    chunk_count: int


def _derive_kb_fernet_key(secret: str) -> bytes:
    """Derive a Fernet key from the master secret, namespaced for the KB."""
    digest = hashlib.sha256(secret.encode("utf-8") + _FERNET_KEY_SALT).digest()
    return base64.urlsafe_b64encode(digest)


class KnowledgeBaseService:
    """Async service for KB ingest/search/list/delete/cleanup."""

    def __init__(
        self,
        db: AsyncSession,
        llm: BaseLLMProvider,
        secret_key: str,
    ) -> None:
        self._db = db
        self._llm = llm
        self._fernet = Fernet(_derive_kb_fernet_key(secret_key))

    # ------------------------------------------------------------------
    # Ingest
    # ------------------------------------------------------------------

    async def ingest(
        self,
        *,
        file_bytes: bytes,
        filename: str,
        title: str,
        source: str,
        tags: list[str],
        ttl_hours: int,
        tenant_id: UUID,
        uploaded_by: str,
    ) -> UUID:
        """Ingest a single document into the KB.

        Returns the created ``doc_id``.  Raises:

        * :class:`ValidationException` on bad input (size, extension, empty text)
        * :class:`ConflictException` when a document with the same content
          hash already exists for *tenant_id*
        """
        if len(file_bytes) == 0:
            raise ValidationException("Uploaded file is empty")
        if len(file_bytes) > _MAX_UPLOAD_BYTES:
            raise ValidationException(
                f"File exceeds maximum size of {_MAX_UPLOAD_BYTES} bytes",
                details={"size": len(file_bytes)},
            )

        ext = self._extension(filename)
        if ext not in _ALLOWED_EXTENSIONS:
            raise ValidationException(
                f"Unsupported file type: {ext}. Allowed: {sorted(_ALLOWED_EXTENSIONS)}",
            )

        text = self._extract_text(file_bytes, ext)
        text = self._strip_frontmatter(text)
        if not text.strip():
            raise ValidationException(
                "Document contains no extractable text. "
                "OCR for scanned documents is not supported in M06."
            )

        content_hash = hashlib.sha256(text.encode("utf-8")).hexdigest()

        existing = await self._db.execute(
            select(KnowledgeDocument.doc_id)
            .where(
                KnowledgeDocument.content_hash == content_hash,
                KnowledgeDocument.tenant_id == tenant_id,
            )
        )
        if existing.scalar_one_or_none() is not None:
            raise ConflictException(
                "Document with identical content already exists",
                details={"content_hash": content_hash},
            )

        chunks = self._chunk_text(text)
        if not chunks:
            raise ValidationException("Document produced no chunks after splitting")

        now = datetime.now(timezone.utc)
        doc = KnowledgeDocument(
            doc_id=uuid4(),
            title=title.strip()[:255],
            source=source.upper().strip()[:20],
            tenant_id=tenant_id,
            tags=list(dict.fromkeys(t.strip() for t in tags if t and t.strip())),
            original_text_encrypted=self._fernet.encrypt(text.encode("utf-8")),
            content_hash=content_hash,
            uploaded_by=uploaded_by,
            ttl_hours=int(ttl_hours),
            expires_at=now + timedelta(hours=int(ttl_hours)),
        )
        self._db.add(doc)
        await self._db.flush()

        for idx, (offset, chunk_text) in enumerate(chunks):
            vector = await self._llm.embed(chunk_text)
            self._db.add(
                KnowledgeEmbedding(
                    embedding_id=uuid4(),
                    doc_id=doc.doc_id,
                    chunk_id=f"{doc.doc_id}:{idx:04d}",
                    chunk_text=chunk_text,
                    vector=vector,
                    dim=len(vector),
                    chunk_offset=offset,
                    length=len(chunk_text),
                    hash=hashlib.sha256(chunk_text.encode("utf-8")).hexdigest(),
                )
            )

        await self._db.commit()
        logger.info(
            "KB ingest: doc_id=%s title=%r chunks=%d source=%s",
            doc.doc_id, doc.title, len(chunks), doc.source,
        )
        return doc.doc_id

    # ------------------------------------------------------------------
    # Search
    # ------------------------------------------------------------------

    async def search(
        self,
        *,
        query_text: str,
        tenant_id: UUID,
        top_k: int = 3,
    ) -> list[KBSearchHit]:
        """Return the top-k most similar chunks for *query_text*.

        Filters out expired documents and limits to *tenant_id*.  Distance
        uses pgvector cosine distance (``<=>``), where 0 is identical.
        """
        if not query_text or not query_text.strip():
            return []
        if top_k <= 0:
            return []

        query_vec = await self._llm.embed(query_text)

        now = datetime.now(timezone.utc)
        distance_col = KnowledgeEmbedding.vector.cosine_distance(query_vec).label(
            "distance"
        )
        stmt = (
            select(
                KnowledgeDocument.doc_id,
                KnowledgeDocument.title,
                KnowledgeDocument.tags,
                KnowledgeEmbedding.chunk_id,
                KnowledgeEmbedding.chunk_text,
                distance_col,
            )
            .join(
                KnowledgeEmbedding,
                KnowledgeEmbedding.doc_id == KnowledgeDocument.doc_id,
            )
            .where(
                KnowledgeDocument.tenant_id == tenant_id,
                KnowledgeDocument.expires_at > now,
            )
            .order_by(distance_col.asc())
            .limit(top_k)
        )
        result = await self._db.execute(stmt)
        rows = result.all()
        return [
            KBSearchHit(
                doc_id=row[0],
                title=row[1],
                tags=list(row[2] or []),
                chunk_id=row[3],
                chunk_text=row[4],
                distance=float(row[5]),
            )
            for row in rows
        ]

    # ------------------------------------------------------------------
    # Listing / deletion / cleanup
    # ------------------------------------------------------------------

    async def list_documents(
        self,
        *,
        tenant_id: UUID,
        tag_filter: str | None = None,
        limit: int = 100,
        offset: int = 0,
    ) -> list[KBDocumentSummary]:
        """Return metadata summaries for KB documents of *tenant_id*."""
        chunk_count_subq = (
            select(
                KnowledgeEmbedding.doc_id,
                func.count(KnowledgeEmbedding.embedding_id).label("chunk_count"),
            )
            .group_by(KnowledgeEmbedding.doc_id)
            .subquery()
        )

        stmt = (
            select(
                KnowledgeDocument,
                func.coalesce(chunk_count_subq.c.chunk_count, 0).label("chunk_count"),
            )
            .outerjoin(
                chunk_count_subq,
                chunk_count_subq.c.doc_id == KnowledgeDocument.doc_id,
            )
            .where(KnowledgeDocument.tenant_id == tenant_id)
            .order_by(KnowledgeDocument.created_at.desc())
            .offset(offset)
            .limit(limit)
        )

        if tag_filter:
            stmt = stmt.where(KnowledgeDocument.tags.contains([tag_filter]))

        rows = (await self._db.execute(stmt)).all()
        return [
            KBDocumentSummary(
                doc_id=doc.doc_id,
                title=doc.title,
                source=doc.source,
                tags=list(doc.tags or []),
                uploaded_by=doc.uploaded_by,
                created_at=doc.created_at,
                expires_at=doc.expires_at,
                chunk_count=int(chunk_count or 0),
            )
            for doc, chunk_count in rows
        ]

    async def get_document(
        self, *, doc_id: UUID, tenant_id: UUID
    ) -> KBDocumentSummary:
        """Return metadata for a single document, raising 404 if absent."""
        stmt = select(KnowledgeDocument).where(
            KnowledgeDocument.doc_id == doc_id,
            KnowledgeDocument.tenant_id == tenant_id,
        )
        doc = (await self._db.execute(stmt)).scalar_one_or_none()
        if doc is None:
            raise NotFoundException(
                "Document not found", details={"doc_id": str(doc_id)}
            )
        chunk_count = (
            await self._db.execute(
                select(func.count(KnowledgeEmbedding.embedding_id)).where(
                    KnowledgeEmbedding.doc_id == doc.doc_id
                )
            )
        ).scalar_one()
        return KBDocumentSummary(
            doc_id=doc.doc_id,
            title=doc.title,
            source=doc.source,
            tags=list(doc.tags or []),
            uploaded_by=doc.uploaded_by,
            created_at=doc.created_at,
            expires_at=doc.expires_at,
            chunk_count=int(chunk_count or 0),
        )

    async def delete_document(self, *, doc_id: UUID, tenant_id: UUID) -> None:
        """Delete a single document (chunks cascade via FK)."""
        stmt = (
            delete(KnowledgeDocument)
            .where(
                KnowledgeDocument.doc_id == doc_id,
                KnowledgeDocument.tenant_id == tenant_id,
            )
            .returning(KnowledgeDocument.doc_id)
        )
        result = await self._db.execute(stmt)
        deleted = result.first()
        await self._db.commit()
        if deleted is None:
            raise NotFoundException(
                "Document not found", details={"doc_id": str(doc_id)}
            )
        logger.info("KB delete: doc_id=%s tenant=%s", doc_id, tenant_id)

    async def delete_by_tag(self, *, tenant_id: UUID, tag: str) -> int:
        """Delete all documents that carry *tag* (e.g. 'placeholder').

        Returns the number of documents removed.
        """
        stmt = (
            delete(KnowledgeDocument)
            .where(
                KnowledgeDocument.tenant_id == tenant_id,
                KnowledgeDocument.tags.contains([tag]),
            )
            .returning(KnowledgeDocument.doc_id)
        )
        result = await self._db.execute(stmt)
        rows = result.all()
        await self._db.commit()
        logger.info(
            "KB delete_by_tag: tenant=%s tag=%s count=%d", tenant_id, tag, len(rows)
        )
        return len(rows)

    async def cleanup_expired(self) -> int:
        """Delete documents whose ``expires_at`` is in the past.  Returns count."""
        now = datetime.now(timezone.utc)
        stmt = (
            delete(KnowledgeDocument)
            .where(KnowledgeDocument.expires_at <= now)
            .returning(KnowledgeDocument.doc_id)
        )
        result = await self._db.execute(stmt)
        rows = result.all()
        await self._db.commit()
        if rows:
            logger.info("KB cleanup_expired: removed %d documents", len(rows))
        return len(rows)

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------

    def decrypt_original(self, doc: KnowledgeDocument) -> str:
        """Decrypt the original document text (admin / debug helper)."""
        try:
            return self._fernet.decrypt(doc.original_text_encrypted).decode("utf-8")
        except InvalidToken as exc:
            raise EKIException(
                "Failed to decrypt KB document (key rotated or data corrupted)",
                details={"doc_id": str(doc.doc_id)},
            ) from exc

    @staticmethod
    def _extension(filename: str) -> str:
        idx = filename.lower().rfind(".")
        return filename[idx:].lower() if idx != -1 else ""

    @staticmethod
    def _extract_text(file_bytes: bytes, ext: str) -> str:
        if ext == ".pdf":
            # Local import keeps non-PDF paths free of the heavy dependency.
            import pdfplumber
            try:
                with pdfplumber.open(io.BytesIO(file_bytes)) as pdf:
                    parts = [page.extract_text() or "" for page in pdf.pages]
            except Exception as exc:
                raise ValidationException(
                    f"PDF extraction failed: {exc}",
                    details={"reason": str(exc)},
                )
            return "\n".join(parts)
        # .md / .markdown / .txt
        try:
            return file_bytes.decode("utf-8")
        except UnicodeDecodeError:
            raise ValidationException(
                "Text file is not valid UTF-8. Convert and try again."
            )

    @staticmethod
    def _strip_frontmatter(text: str) -> str:
        """Drop a leading YAML front-matter block delimited by '---' lines."""
        if not text.lstrip().startswith("---"):
            return text
        stripped = text.lstrip()
        end = stripped.find("\n---", 3)
        if end == -1:
            return text
        # Skip past the closing '---' line + newline
        remainder_start = stripped.find("\n", end + 4)
        if remainder_start == -1:
            return ""
        return stripped[remainder_start + 1:]

    @staticmethod
    def _chunk_text(text: str) -> list[tuple[int, str]]:
        """Split *text* into overlapping chunks at paragraph boundaries.

        Returns ``[(offset, chunk_text), ...]``.  Targets ~1200 tokens per
        chunk with ~100 tokens overlap, sized via characters.  Falls back
        to hard character splits inside paragraphs that exceed the max.
        """
        if not text.strip():
            return []

        normalized = re.sub(r"[ \t]+", " ", text).strip()
        # Split at blank-line boundaries first to keep semantics intact.
        paragraphs = [p.strip() for p in re.split(r"\n\s*\n", normalized) if p.strip()]
        if not paragraphs:
            return []

        chunks: list[tuple[int, str]] = []
        buffer = ""
        buffer_offset = 0
        offset_cursor = 0

        for para in paragraphs:
            # Track absolute char offset of this paragraph in the original
            # normalized text.  We rebuild offsets approximately because the
            # split itself lost exact positions; for retrieval relevance the
            # offset is mainly diagnostic.
            para_offset = normalized.find(para, offset_cursor)
            if para_offset == -1:
                para_offset = offset_cursor
            offset_cursor = para_offset + len(para)

            if not buffer:
                buffer = para
                buffer_offset = para_offset
                continue

            candidate = f"{buffer}\n\n{para}"
            if len(candidate) <= _CHUNK_CHARS_MAX:
                buffer = candidate
                if len(buffer) >= _CHUNK_CHARS_TARGET:
                    chunks.append((buffer_offset, buffer))
                    # Carry overlap forward to keep continuity at boundaries
                    overlap = buffer[-_CHUNK_CHAR_OVERLAP:]
                    buffer = overlap
                    buffer_offset = buffer_offset + len(candidate) - len(overlap)
                continue

            # Adding this paragraph would exceed the hard max -> flush
            if len(buffer) >= _CHUNK_CHARS_MIN:
                chunks.append((buffer_offset, buffer))
                overlap = buffer[-_CHUNK_CHAR_OVERLAP:]
                buffer = f"{overlap}\n\n{para}" if overlap.strip() else para
                buffer_offset = para_offset - len(overlap)
            else:
                # Buffer too small to ship; force-split the oversized paragraph.
                for sub in KnowledgeBaseService._split_long(para):
                    if buffer and len(buffer) + len(sub) + 2 <= _CHUNK_CHARS_MAX:
                        buffer = f"{buffer}\n\n{sub}"
                    else:
                        if buffer:
                            chunks.append((buffer_offset, buffer))
                        buffer = sub
                        buffer_offset = para_offset

        if buffer.strip():
            chunks.append((buffer_offset, buffer.strip()))

        return chunks

    @staticmethod
    def _split_long(paragraph: str) -> list[str]:
        """Cut a paragraph that exceeds the max into target-sized slices."""
        return [
            paragraph[i : i + _CHUNK_CHARS_TARGET]
            for i in range(0, len(paragraph), _CHUNK_CHARS_TARGET)
        ]
