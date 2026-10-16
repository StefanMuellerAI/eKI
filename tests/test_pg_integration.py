"""PostgreSQL (+pgvector) integration tests.

Everything in this module needs a real PostgreSQL 16 with the ``vector``
extension and only runs when ``TEST_POSTGRES_URL`` is set (CI does; see
``tests/conftest.py``).  Without it the whole module is skipped.

Covered here because SQLite cannot exercise it:

* Alembic: ``upgrade head`` on an empty database, schema drift against
  ``core.db_models`` (what ``alembic check`` does), full downgrade/re-upgrade,
  and the M13 ``job_metadata.status`` widening (``JobStatus.DELIVERING`` did
  not fit into the original ``VARCHAR(9)``).
* ``services.knowledge_base`` against real pgvector: ingest, dedupe, chunking,
  cosine ranking, tenant isolation, tags, TTL cleanup, encryption at rest.
* The ``/v1/kb`` HTTP endpoints end-to-end (no service mocks).
* Delivery bookkeeping (``_mark_delivering`` / ``_record_attempt``).
"""

from __future__ import annotations

import hashlib
import io
import logging.config
import re
from collections.abc import Iterator, Sequence
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from unittest.mock import patch
from uuid import UUID, uuid4

import pytest
from alembic import command
from alembic.autogenerate import compare_metadata
from alembic.config import Config
from alembic.migration import MigrationContext
from alembic.script import ScriptDirectory
from fastapi import status
from sqlalchemy import create_engine, func, insert, inspect, select, text, update
from sqlalchemy.engine import URL, make_url
from sqlalchemy.exc import DataError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker
from sqlalchemy.pool import NullPool

from api.config import get_settings
from core.db_models import (
    Base,
    JobMetadata,
    KnowledgeDocument,
    KnowledgeEmbedding,
    ReportMetadata,
)
from core.exceptions import (
    ConflictException,
    EKIException,
    NotFoundException,
    ValidationException,
)
from core.models import JobStatus, ScriptFormat
from llm.base import BaseLLMProvider
from services.knowledge_base import KnowledgeBaseService
from tests.conftest import TEST_POSTGRES_URL, requires_postgres
from workflows import activities
from workflows.activities import _build_kb_context, _mark_delivering, _record_attempt

pytestmark = requires_postgres

REPO_ROOT = Path(__file__).resolve().parent.parent

_HEAD_REVISION = "c4e6a8b0d2f1"  # 1.0.1: metadata retention indexes
_PRE_M13_REVISION = "f1a2b3c4d5e6"  # M10: delivery hardening
_APP_TABLES = {
    "api_keys",
    "audit_logs",
    "job_metadata",
    "delivery_dead_letters",
    "report_metadata",
    "kb_documents",
    "kb_embeddings",
}


# ===========================================================================
# Alembic migrations (dedicated throw-away database per test)
# ===========================================================================


def _sync_url(database: str) -> URL:
    """TEST_POSTGRES_URL credentials/host, psycopg2 driver, other database."""
    return make_url(TEST_POSTGRES_URL).set(drivername="postgresql+psycopg2", database=database)


def _status_column_length(engine) -> int:
    with engine.connect() as conn:
        return conn.execute(
            text(
                "SELECT character_maximum_length FROM information_schema.columns "
                "WHERE table_name = 'job_metadata' AND column_name = 'status'"
            )
        ).scalar_one()


def _insert_job(conn, job_status: JobStatus, **extra: Any) -> UUID:
    """Insert through the ORM table so the value is rendered exactly like the app does."""
    job_id = uuid4()
    conn.execute(
        insert(JobMetadata.__table__).values(
            job_id=job_id,
            project_id="mig-project",
            script_format=ScriptFormat.FDX,
            status=job_status,
            user_id="mig-user",
            **extra,
        )
    )
    return job_id


def _raw_status(conn, job_id: UUID) -> str:
    return conn.execute(
        text("SELECT status FROM job_metadata WHERE job_id = :id"), {"id": job_id}
    ).scalar_one()


class _MigrationDB(SimpleNamespace):
    """``cfg`` (alembic Config), ``engine`` (sync, NullPool), ``name``, ``logging_configs``."""


@pytest.fixture
def migration_db() -> Iterator[_MigrationDB]:
    """Create an empty database, point ``api.config.get_settings`` at it, drop it afterwards.

    ``db/migrations/env.py`` takes the URL from ``get_settings().database_url``
    (an ``+asyncpg`` URL, exactly as in production) and swaps in psycopg2, so
    the fixture sets ``DATABASE_URL`` and clears the ``lru_cache`` on both ends.
    ``logging.config.fileConfig`` is recorded but not applied: alembic.ini
    would otherwise disable every existing logger for the rest of the session.
    """
    base_db = make_url(TEST_POSTGRES_URL).database or "eki"
    name = f"{base_db}_mig_{uuid4().hex[:12]}"
    admin = create_engine(_sync_url("postgres"), isolation_level="AUTOCOMMIT", poolclass=NullPool)
    engine = create_engine(_sync_url(name), poolclass=NullPool)
    logging_configs: list[str] = []

    with admin.connect() as conn:
        conn.execute(text(f'CREATE DATABASE "{name}"'))
    try:
        with pytest.MonkeyPatch.context() as mp:
            mp.chdir(REPO_ROOT)
            mp.setenv(
                "DATABASE_URL",
                make_url(TEST_POSTGRES_URL)
                .set(database=name)
                .render_as_string(hide_password=False),
            )
            mp.setattr(
                logging.config, "fileConfig", lambda path, *a, **kw: logging_configs.append(path)
            )
            get_settings.cache_clear()
            try:
                yield _MigrationDB(
                    cfg=Config("alembic.ini"),
                    engine=engine,
                    name=name,
                    logging_configs=logging_configs,
                )
            finally:
                get_settings.cache_clear()
    finally:
        engine.dispose()
        with admin.connect() as conn:
            conn.execute(text(f'DROP DATABASE IF EXISTS "{name}" WITH (FORCE)'))
        admin.dispose()


def _schema_diff(engine) -> list:
    with engine.connect() as conn:
        ctx = MigrationContext.configure(conn, opts={"compare_type": True})
        return compare_metadata(ctx, Base.metadata)


class TestAlembicMigrations:
    def test_head_is_the_m13_revision(self, migration_db):
        """Guards against a second head (two migrations with the same down_revision)."""
        heads = ScriptDirectory.from_config(migration_db.cfg).get_heads()
        assert heads == [_HEAD_REVISION]

    def test_upgrade_head_yields_schema_identical_to_models(self, migration_db):
        command.upgrade(migration_db.cfg, "head")

        with migration_db.engine.connect() as conn:
            assert MigrationContext.configure(conn).get_current_revision() == _HEAD_REVISION
        tables = set(inspect(migration_db.engine).get_table_names())
        assert _APP_TABLES <= tables

        # Same comparison ``alembic check`` performs: no drift in either direction.
        assert _schema_diff(migration_db.engine) == []
        command.check(migration_db.cfg)  # raises AutogenerateDiffsDetected on drift

        # env.py wires alembic.ini logging and connects through the settings URL.
        assert migration_db.logging_configs
        assert all(path.endswith("alembic.ini") for path in migration_db.logging_configs)
        assert get_settings().database_url.path == f"/{migration_db.name}"

        # The M06 ANN index exists with the cosine opclass the KB search relies on.
        with migration_db.engine.connect() as conn:
            indexdef = conn.execute(
                text("SELECT indexdef FROM pg_indexes WHERE indexname = 'ix_kb_embeddings_vector'")
            ).scalar_one()
        assert "ivfflat" in indexdef
        assert "vector_cosine_ops" in indexdef

    def test_downgrade_base_and_upgrade_again(self, migration_db):
        command.upgrade(migration_db.cfg, "head")
        command.downgrade(migration_db.cfg, "base")

        tables = set(inspect(migration_db.engine).get_table_names())
        assert tables & _APP_TABLES == set()
        with migration_db.engine.connect() as conn:
            assert MigrationContext.configure(conn).get_current_revision() is None

        command.upgrade(migration_db.cfg, "head")
        assert _schema_diff(migration_db.engine) == []
        assert _status_column_length(migration_db.engine) == 20

    def test_every_job_status_fits_the_migrated_column(self, migration_db):
        """Regression: ``DELIVERING`` (10 chars) overflowed the original ``VARCHAR(9)``."""
        command.upgrade(migration_db.cfg, "head")
        assert _status_column_length(migration_db.engine) >= max(len(m.name) for m in JobStatus)

        with migration_db.engine.begin() as conn:
            ids = {member: _insert_job(conn, member) for member in JobStatus}

        table = JobMetadata.__table__
        with migration_db.engine.connect() as conn:
            for member, job_id in ids.items():
                assert _raw_status(conn, job_id) == member.name  # non-native Enum stores names
                loaded = conn.execute(
                    select(table.c.status).where(table.c.job_id == job_id)
                ).scalar_one()
                assert loaded is member

    def test_m13_downgrade_folds_delivering_into_running(self, migration_db):
        command.upgrade(migration_db.cfg, "head")
        with migration_db.engine.begin() as conn:
            delivering = _insert_job(conn, JobStatus.DELIVERING)
            running = _insert_job(conn, JobStatus.RUNNING)
            completed = _insert_job(conn, JobStatus.COMPLETED)

        command.downgrade(migration_db.cfg, _PRE_M13_REVISION)

        assert _status_column_length(migration_db.engine) == 9
        with migration_db.engine.connect() as conn:
            assert _raw_status(conn, delivering) == "RUNNING"
            assert _raw_status(conn, running) == "RUNNING"
            assert _raw_status(conn, completed) == "COMPLETED"
            assert MigrationContext.configure(conn).get_current_revision() == _PRE_M13_REVISION

        # Proves the regression test above discriminates: the pre-M13 column rejects it.
        with pytest.raises(DataError, match="value too long"):
            with migration_db.engine.begin() as conn:
                _insert_job(conn, JobStatus.DELIVERING)

        command.upgrade(migration_db.cfg, "head")
        assert _status_column_length(migration_db.engine) == 20
        with migration_db.engine.begin() as conn:
            job_id = _insert_job(conn, JobStatus.DELIVERING)
            assert _raw_status(conn, job_id) == "DELIVERING"


# ===========================================================================
# Knowledge base service against pgvector
# ===========================================================================

_DIM = 1024
_UNKNOWN_AXIS = _DIM - 1


class KeywordEmbedder(BaseLLMProvider):
    """Deterministic 1024-dim embeddings: one orthogonal axis per vocabulary word.

    A text maps to the sum of the axes of the words it contains, so cosine
    distances are known in closed form (``feuer`` vs ``feuer wasser`` =
    ``1 - 1/sqrt(2)``).  Texts without a known word land on a dedicated axis
    so no vector is ever all-zero (cosine distance would be NaN).  Vocabulary
    words deliberately contain non-hex letters so the uuid suffixes used to
    keep content hashes unique can never match them.
    """

    VOCABULARY = ("feuer", "wasser", "hoehe", "tier", "waffe", "stunt")

    def __init__(self) -> None:
        super().__init__({})
        self._axes = {word: axis for axis, word in enumerate(self.VOCABULARY)}
        self.calls: list[str] = []

    async def embed(self, text: str) -> list[float]:
        self.calls.append(text)
        vector = [0.0] * _DIM
        lowered = text.lower()
        for word, axis in self._axes.items():
            if re.search(rf"\b{word}\b", lowered):
                vector[axis] = 1.0
        if not any(vector):
            vector[_UNKNOWN_AXIS] = 1.0
        return vector

    async def generate(self, prompt: str, system_prompt: str | None = None, **kwargs: Any) -> str:
        raise NotImplementedError

    async def generate_structured(
        self, prompt: str, schema: dict[str, Any], system_prompt: str | None = None, **kwargs: Any
    ) -> dict[str, Any]:
        raise NotImplementedError

    async def health_check(self) -> bool:
        return True

    @property
    def provider_name(self) -> str:
        return "keyword-embedder"


_SECRET = "pg-integration-secret-key-with-32-chars!"


@pytest.fixture
def embedder() -> KeywordEmbedder:
    return KeywordEmbedder()


@pytest.fixture
def kb(db_session, embedder) -> KnowledgeBaseService:
    return KnowledgeBaseService(db=db_session, llm=embedder, secret_key=_SECRET)


@pytest.fixture
def tenant() -> UUID:
    """Fresh tenant per test: the database is shared by the whole session."""
    return uuid4()


async def _ingest(
    kb: KnowledgeBaseService,
    tenant: UUID,
    body: str,
    *,
    title: str = "Doc",
    tags: Sequence[str] = (),
    filename: str = "doc.md",
    ttl_hours: int = 24,
    unique: bool = True,
) -> UUID:
    if unique:
        body = f"{body}\n\nRef {uuid4().hex}"
    return await kb.ingest(
        file_bytes=body.encode("utf-8"),
        filename=filename,
        title=title,
        source="upload",
        tags=list(tags),
        ttl_hours=ttl_hours,
        tenant_id=tenant,
        uploaded_by="kb-tester",
    )


async def _doc_row(session: AsyncSession, doc_id: UUID) -> KnowledgeDocument | None:
    return (
        await session.execute(select(KnowledgeDocument).where(KnowledgeDocument.doc_id == doc_id))
    ).scalar_one_or_none()


async def _chunks(session: AsyncSession, doc_id: UUID) -> list[KnowledgeEmbedding]:
    return list(
        (
            await session.execute(
                select(KnowledgeEmbedding)
                .where(KnowledgeEmbedding.doc_id == doc_id)
                .order_by(KnowledgeEmbedding.chunk_id)
            )
        )
        .scalars()
        .all()
    )


async def _expire(session: AsyncSession, doc_id: UUID) -> None:
    await session.execute(
        update(KnowledgeDocument)
        .where(KnowledgeDocument.doc_id == doc_id)
        .values(expires_at=datetime.now(UTC) - timedelta(minutes=1))
    )
    await session.commit()


@pytest.mark.asyncio
class TestKnowledgeBaseIngest:
    async def test_ingest_persists_metadata_chunk_and_vector(
        self, kb, db_session, embedder, tenant
    ):
        body = "# Feuer am Set\n\nPyrotechnik nur mit Feuerwehr-Abnahme. Feuer ist gefaehrlich."
        markdown = f'---\ntitle: "ignored"\ntags: ["x"]\n---\n{body}\n'
        doc_id = await kb.ingest(
            file_bytes=markdown.encode("utf-8"),
            filename="01_FEUER.MD",
            title="  " + "T" * 300 + "  ",
            source=" placeholder ",
            tags=["stunt", " stunt ", "", "  ", "feuer"],
            ttl_hours=48,
            tenant_id=tenant,
            uploaded_by="kb-tester",
        )

        doc = await _doc_row(db_session, doc_id)
        assert doc is not None
        assert doc.tenant_id == tenant
        assert doc.title == "T" * 255
        assert doc.source == "PLACEHOLDER"
        assert doc.tags == ["stunt", "feuer"]  # stripped, de-duplicated, order kept
        assert doc.uploaded_by == "kb-tester"
        assert doc.ttl_hours == 48
        assert abs((doc.expires_at - datetime.now(UTC)) - timedelta(hours=48)) < timedelta(
            minutes=1
        )
        # Front matter is stripped before hashing and chunking.
        stored_text = kb.decrypt_original(doc)
        assert stored_text.startswith("# Feuer am Set")
        assert "ignored" not in stored_text
        assert doc.content_hash == hashlib.sha256(stored_text.encode("utf-8")).hexdigest()

        chunks = await _chunks(db_session, doc_id)
        assert len(chunks) == 1
        chunk = chunks[0]
        assert chunk.chunk_id == f"{doc_id}:0000"
        assert chunk.chunk_offset == 0
        assert chunk.chunk_text == stored_text.strip()
        assert chunk.length == len(chunk.chunk_text)
        assert chunk.hash == hashlib.sha256(chunk.chunk_text.encode("utf-8")).hexdigest()
        assert chunk.dim == _DIM
        assert embedder.calls == [chunk.chunk_text]
        assert [float(x) for x in chunk.vector] == await embedder.embed(chunk.chunk_text)

    async def test_original_text_is_encrypted_at_rest(self, kb, db_session, embedder, tenant):
        doc_id = await _ingest(kb, tenant, "Geheimer Drehplan: Waffe im Requisitenwagen.")

        raw = (
            await db_session.execute(
                text("SELECT original_text_encrypted FROM kb_documents WHERE doc_id = :id"),
                {"id": doc_id},
            )
        ).scalar_one()
        raw = bytes(raw)
        assert b"Geheimer Drehplan" not in raw
        assert b"Requisitenwagen" not in raw
        assert raw.startswith(b"gAAAAA")  # Fernet token, version byte 0x80

        doc = await _doc_row(db_session, doc_id)
        assert kb.decrypt_original(doc).startswith("Geheimer Drehplan: Waffe im Requisitenwagen.")

        other_key = KnowledgeBaseService(db=db_session, llm=embedder, secret_key="another-secret")
        with pytest.raises(EKIException, match="Failed to decrypt KB document") as exc_info:
            other_key.decrypt_original(doc)
        assert exc_info.value.details == {"doc_id": str(doc_id)}

    async def test_duplicate_content_in_same_tenant_is_rejected(self, kb, db_session, tenant):
        body = f"Stunt-Briefing vor jedem Take. {uuid4().hex}"
        first = await _ingest(kb, tenant, body, unique=False)

        # Different file name, title, tags and front matter -- same body text.
        with pytest.raises(ConflictException) as exc_info:
            await kb.ingest(
                file_bytes=f"---\ntitle: other\n---\n{body}".encode(),
                filename="copy.txt",
                title="Kopie",
                source="UPLOAD",
                tags=["copy"],
                ttl_hours=1,
                tenant_id=tenant,
                uploaded_by="someone-else",
            )
        expected_hash = hashlib.sha256(body.encode("utf-8")).hexdigest()
        assert exc_info.value.details == {"content_hash": expected_hash}

        count = (
            await db_session.execute(
                select(func.count())
                .select_from(KnowledgeDocument)
                .where(KnowledgeDocument.tenant_id == tenant)
            )
        ).scalar_one()
        assert count == 1
        assert (await _doc_row(db_session, first)) is not None

    @pytest.mark.parametrize(
        ("file_bytes", "filename", "message"),
        [
            (b"", "empty.md", "Uploaded file is empty"),
            (b"x" * (10 * 1024 * 1024 + 1), "huge.txt", "exceeds maximum size"),
            (b"Feuer", "notes.docx", "Unsupported file type: .docx"),
            (b"Feuer", "no_extension", "Unsupported file type: ."),
            (b"\xff\xfe\x00 not utf-8", "latin.txt", "not valid UTF-8"),
            (b"%PDF-1.4 truncated garbage", "broken.pdf", "PDF extraction failed"),
            (b"---\ntitle: only front matter\n---", "fm.md", "no extractable text"),
            (b"   \n\n\t  ", "blank.txt", "no extractable text"),
        ],
        ids=[
            "empty",
            "too-large",
            "docx",
            "no-extension",
            "invalid-utf8",
            "broken-pdf",
            "front-matter-only",
            "whitespace-only",
        ],
    )
    async def test_invalid_uploads_are_rejected_without_writes(
        self, kb, db_session, embedder, tenant, file_bytes, filename, message
    ):
        with pytest.raises(ValidationException, match=re.escape(message)):
            await kb.ingest(
                file_bytes=file_bytes,
                filename=filename,
                title="bad",
                source="UPLOAD",
                tags=[],
                ttl_hours=1,
                tenant_id=tenant,
                uploaded_by="kb-tester",
            )
        assert embedder.calls == []
        count = (
            await db_session.execute(
                select(func.count())
                .select_from(KnowledgeDocument)
                .where(KnowledgeDocument.tenant_id == tenant)
            )
        ).scalar_one()
        assert count == 0

    async def test_pdf_text_is_extracted(self, kb, db_session, tenant):
        from reportlab.pdfgen import canvas

        buffer = io.BytesIO()
        pdf = canvas.Canvas(buffer)
        pdf.drawString(72, 720, "Hoehe: Absturzsicherung ab zwei Metern Pflicht.")
        pdf.drawString(72, 700, f"Dokument {uuid4().hex}")
        pdf.showPage()
        pdf.save()

        doc_id = await kb.ingest(
            file_bytes=buffer.getvalue(),
            filename="Hoehe.PDF",
            title="Hoehenarbeit",
            source="UPLOAD",
            tags=["hoehe"],
            ttl_hours=1,
            tenant_id=tenant,
            uploaded_by="kb-tester",
        )
        chunks = await _chunks(db_session, doc_id)
        assert len(chunks) == 1
        assert "Absturzsicherung ab zwei Metern" in chunks[0].chunk_text
        assert chunks[0].vector[KeywordEmbedder.VOCABULARY.index("hoehe")] == 1.0

    async def test_long_document_is_split_into_ordered_chunks(
        self, kb, db_session, embedder, tenant
    ):
        """1000-char paragraph + 9000-char paragraph: the oversized one is force-split.

        Expected (see ``_chunk_text``): chunk 0 = first paragraph + first 4800
        chars of the second (1000 + 2 + 4800 = 5802), chunk 1 = the remaining
        4200 chars -- each embedded separately and stored in order.
        """
        marker = uuid4().hex
        first = ("Feuer " * 200)[:999] + "A"
        second = (f"Wasser {marker} " * 400)[:8999] + "Z"
        doc_id = await kb.ingest(
            file_bytes=f"{first}\n\n{second}".encode(),
            filename="long.txt",
            title="Lang",
            source="UPLOAD",
            tags=[],
            ttl_hours=1,
            tenant_id=tenant,
            uploaded_by="kb-tester",
        )

        chunks = await _chunks(db_session, doc_id)
        assert [c.chunk_id for c in chunks] == [f"{doc_id}:0000", f"{doc_id}:0001"]
        assert [c.length for c in chunks] == [5802, 4200]
        assert all(c.length <= 6000 for c in chunks)
        assert chunks[0].chunk_text == f"{first}\n\n{second[:4800]}"
        assert chunks[1].chunk_text == second[4800:].strip()
        assert 0 == chunks[0].chunk_offset < chunks[1].chunk_offset < len(first) + len(second)
        assert embedder.calls == [c.chunk_text for c in chunks]

        summary = await kb.get_document(doc_id=doc_id, tenant_id=tenant)
        assert summary.chunk_count == 2

    async def test_paragraph_overflow_flushes_chunk_and_carries_overlap(
        self, kb, db_session, tenant
    ):
        """3500 + 3000 chars exceed the 6000 hard max: flush the first paragraph,
        start the next chunk with its last 400 chars as overlap."""
        marker = uuid4().hex
        first = ("Feuer " * 600)[:3499] + "A"
        second = (f"Wasser {marker} " * 100)[:2999] + "B"
        doc_id = await kb.ingest(
            file_bytes=f"{first}\n\n{second}".encode(),
            filename="overflow.md",
            title="Overflow",
            source="UPLOAD",
            tags=[],
            ttl_hours=1,
            tenant_id=tenant,
            uploaded_by="kb-tester",
        )

        chunks = await _chunks(db_session, doc_id)
        assert [c.chunk_text for c in chunks] == [first, f"{first[-400:]}\n\n{second}"]
        assert [c.length for c in chunks] == [3500, 3402]
        assert 0 == chunks[0].chunk_offset < chunks[1].chunk_offset < len(first)


@pytest.mark.asyncio
class TestKnowledgeBaseSearch:
    async def test_results_are_ranked_by_cosine_distance(self, kb, embedder, tenant):
        exact = await _ingest(kb, tenant, "Feuer", title="exact", tags=["feuer"])
        mixed = await _ingest(kb, tenant, "Feuer und Wasser", title="mixed")
        await _ingest(kb, tenant, "Wasser", title="water")
        await _ingest(kb, tenant, "Tier", title="animal")
        embedder.calls.clear()

        hits = await kb.search(query_text="Feuer!", tenant_id=tenant, top_k=2)

        assert embedder.calls == ["Feuer!"]
        assert [h.doc_id for h in hits] == [exact, mixed]
        assert hits[0].distance == pytest.approx(0.0, abs=1e-6)
        assert hits[1].distance == pytest.approx(1 - 2**-0.5, abs=1e-6)
        assert hits[0].title == "exact"
        assert hits[0].tags == ["feuer"]
        assert hits[0].chunk_id == f"{exact}:0000"
        assert hits[0].chunk_text.startswith("Feuer")

        everything = await kb.search(query_text="feuer", tenant_id=tenant, top_k=10)
        assert [h.title for h in everything][:2] == ["exact", "mixed"]
        assert {h.title for h in everything[2:]} == {"water", "animal"}
        assert all(h.distance == pytest.approx(1.0, abs=1e-6) for h in everything[2:])

    @pytest.mark.parametrize(("query", "top_k"), [("", 3), ("   \n", 3), ("Feuer", 0)])
    async def test_trivial_queries_short_circuit(self, kb, embedder, tenant, query, top_k):
        await _ingest(kb, tenant, "Feuer")
        embedder.calls.clear()
        assert await kb.search(query_text=query, tenant_id=tenant, top_k=top_k) == []
        assert embedder.calls == []

    async def test_search_is_tenant_scoped_and_ignores_expired_documents(
        self, kb, db_session, tenant
    ):
        mine = await _ingest(kb, tenant, "Waffe am Set", title="mine")
        expired = await _ingest(kb, tenant, "Waffe im Lager", title="expired")
        other_tenant = uuid4()
        await _ingest(kb, other_tenant, "Waffe beim Nachbarn", title="foreign")
        await _expire(db_session, expired)

        hits = await kb.search(query_text="Waffe", tenant_id=tenant, top_k=10)
        assert [h.doc_id for h in hits] == [mine]

        foreign = await kb.search(query_text="Waffe", tenant_id=other_tenant, top_k=10)
        assert [h.title for h in foreign] == ["foreign"]

        assert await kb.search(query_text="Waffe", tenant_id=uuid4(), top_k=10) == []

    async def test_build_kb_context_reads_real_pgvector(self, kb, embedder, tenant):
        """The opt-in RAG path of the risk activity end-to-end against PostgreSQL."""
        await _ingest(kb, tenant, "Stunt: Fallmatten und Sicherung pruefen.", title="Stunt-SOP")
        await _ingest(kb, tenant, "Tier am Set: Tiertrainer anwesend.", title="Tier-SOP")
        settings = SimpleNamespace(
            kb_retrieval_enabled=True,
            kb_default_tenant_id=str(tenant),
            kb_top_k=1,
            kb_max_chunk_chars_in_prompt=12,
            database_url=TEST_POSTGRES_URL,
            api_secret_key=_SECRET,
            llm_provider="ollama",
        )
        with patch("llm.factory.get_llm_provider", return_value=embedder):
            context = await _build_kb_context(scene_text="Der Stunt vom Dach", settings=settings)
        assert context == "[Stunt-SOP] Stunt: Fallm..."


@pytest.mark.asyncio
class TestKnowledgeBaseListingAndDeletion:
    async def test_list_filters_by_tag_orders_newest_first_and_paginates(
        self, kb, db_session, tenant
    ):
        old = await _ingest(kb, tenant, "Feuer alt", title="old", tags=["sfx", "feuer"])
        mid = await _ingest(kb, tenant, "Wasser", title="mid", tags=["sfx"])
        new = await _ingest(kb, tenant, "Tier", title="new", tags=["tier"])
        base = datetime(2026, 1, 1, tzinfo=UTC)
        for offset, doc_id in enumerate((old, mid, new)):
            await db_session.execute(
                update(KnowledgeDocument)
                .where(KnowledgeDocument.doc_id == doc_id)
                .values(created_at=base + timedelta(hours=offset))
            )
        await db_session.commit()
        await _ingest(kb, uuid4(), "Feuer fremd", tags=["sfx"])  # other tenant

        everything = await kb.list_documents(tenant_id=tenant)
        assert [d.title for d in everything] == ["new", "mid", "old"]
        assert all(d.chunk_count == 1 for d in everything)
        assert everything[2].tags == ["sfx", "feuer"]
        assert everything[2].uploaded_by == "kb-tester"

        sfx = await kb.list_documents(tenant_id=tenant, tag_filter="sfx")
        assert [d.doc_id for d in sfx] == [mid, old]
        assert await kb.list_documents(tenant_id=tenant, tag_filter="sf") == []

        page1 = await kb.list_documents(tenant_id=tenant, limit=2, offset=0)
        page2 = await kb.list_documents(tenant_id=tenant, limit=2, offset=2)
        assert [d.doc_id for d in page1] == [new, mid]
        assert [d.doc_id for d in page2] == [old]

    async def test_get_document_is_tenant_scoped(self, kb, tenant):
        doc_id = await _ingest(kb, tenant, "Hoehe", title="height", tags=["hoehe"])

        summary = await kb.get_document(doc_id=doc_id, tenant_id=tenant)
        assert (summary.doc_id, summary.title, summary.source) == (doc_id, "height", "UPLOAD")
        assert summary.tags == ["hoehe"]
        assert summary.chunk_count == 1
        assert summary.expires_at > summary.created_at

        with pytest.raises(NotFoundException) as exc_info:
            await kb.get_document(doc_id=doc_id, tenant_id=uuid4())
        assert exc_info.value.details == {"doc_id": str(doc_id)}
        with pytest.raises(NotFoundException):
            await kb.get_document(doc_id=uuid4(), tenant_id=tenant)

    async def test_delete_document_cascades_and_is_tenant_scoped(self, kb, db_session, tenant):
        doc_id = await _ingest(kb, tenant, "Waffe")
        keep = await _ingest(kb, tenant, "Wasser")

        with pytest.raises(NotFoundException):
            await kb.delete_document(doc_id=doc_id, tenant_id=uuid4())
        assert await _doc_row(db_session, doc_id) is not None

        await kb.delete_document(doc_id=doc_id, tenant_id=tenant)
        assert await _doc_row(db_session, doc_id) is None
        assert await _chunks(db_session, doc_id) == []  # ON DELETE CASCADE
        assert len(await _chunks(db_session, keep)) == 1

        with pytest.raises(NotFoundException):
            await kb.delete_document(doc_id=doc_id, tenant_id=tenant)

    async def test_delete_by_tag_matches_whole_tags_within_tenant(self, kb, db_session, tenant):
        p1 = await _ingest(kb, tenant, "Feuer", tags=["placeholder", "feuer"])
        p2 = await _ingest(kb, tenant, "Wasser", tags=["placeholder"])
        similar = await _ingest(kb, tenant, "Tier", tags=["placeholder-old"])
        untagged = await _ingest(kb, tenant, "Hoehe")
        other_tenant = uuid4()
        foreign = await _ingest(kb, other_tenant, "Waffe", tags=["placeholder"])

        assert await kb.delete_by_tag(tenant_id=tenant, tag="placeholder") == 2

        for doc_id in (p1, p2):
            assert await _doc_row(db_session, doc_id) is None
            assert await _chunks(db_session, doc_id) == []
        for doc_id in (similar, untagged, foreign):
            assert await _doc_row(db_session, doc_id) is not None
        assert await kb.delete_by_tag(tenant_id=tenant, tag="placeholder") == 0

    async def test_cleanup_expired_removes_only_expired_documents(self, kb, db_session, tenant):
        expired = await _ingest(kb, tenant, "Feuer alt")
        foreign_expired = await _ingest(kb, uuid4(), "Feuer fremd alt")
        fresh = await _ingest(kb, tenant, "Feuer neu")
        await _expire(db_session, expired)
        await _expire(db_session, foreign_expired)

        removed = await kb.cleanup_expired()

        # TTL cleanup is global (all tenants); other tests may leave expired rows too.
        assert removed >= 2
        assert await _doc_row(db_session, expired) is None
        assert await _doc_row(db_session, foreign_expired) is None
        assert await _chunks(db_session, expired) == []
        assert await _doc_row(db_session, fresh) is not None
        assert await kb.cleanup_expired() == 0


@pytest.mark.asyncio
class TestKnowledgeBaseDedupe:
    async def test_same_content_can_be_ingested_by_two_tenants(self, kb, tenant):
        body = f"Gemeinsame Branchen-SOP: Feuer. {uuid4().hex}"
        first = await _ingest(kb, tenant, body, unique=False)
        second = await _ingest(kb, uuid4(), body, unique=False)
        assert first != second

    async def test_lost_dedupe_race_is_reported_as_conflict(self, kb, db_session, tenant):
        body = f"Doppelt hochgeladen: Stunt. {uuid4().hex}"
        await _ingest(kb, tenant, body, unique=False)

        real_execute = db_session.execute
        calls = {"n": 0}

        class _NoRow:
            def scalar_one_or_none(self) -> None:
                return None

        async def lookup_misses_once(statement, *args, **kwargs):
            calls["n"] += 1
            if calls["n"] == 1:  # the content-hash lookup in ingest()
                return _NoRow()
            return await real_execute(statement, *args, **kwargs)

        with patch.object(db_session, "execute", new=lookup_misses_once):
            with pytest.raises(ConflictException):
                await _ingest(kb, tenant, body, unique=False)


# ===========================================================================
# /v1/kb HTTP endpoints end-to-end (real service, real pgvector)
# ===========================================================================


@pytest.fixture
def kb_llm(embedder) -> Iterator[KeywordEmbedder]:
    with patch("api.routers.knowledge_base.get_llm_provider", return_value=embedder):
        yield embedder


def _upload(client, headers, body: str, *, tag: str, title: str = "SOP", ttl_hours: int = 24):
    return client.post(
        "/v1/kb/documents",
        headers=headers,
        files={"file": ("sop.md", body.encode("utf-8"), "text/markdown")},
        data={
            "title": title,
            "source": "placeholder",
            "tags": f"{tag}, sop ,",
            "ttl_hours": str(ttl_hours),
        },
    )


class TestKnowledgeBaseEndpoints:
    @pytest.mark.asyncio
    async def test_upload_get_list_delete_roundtrip(self, client, admin_headers, kb_llm):
        tag = f"t-{uuid4().hex[:8]}"
        response = _upload(client, admin_headers, f"Feuer {uuid4().hex}", tag=tag, ttl_hours=48)

        assert response.status_code == status.HTTP_201_CREATED, response.text
        created = response.json()
        doc_id = created["doc_id"]
        assert created["title"] == "SOP"
        assert created["source"] == "PLACEHOLDER"
        assert created["tags"] == [tag, "sop"]
        assert created["uploaded_by"] == "ops-admin"  # KB is admin-only since 1.0.1
        assert created["chunk_count"] == 1
        lifetime = datetime.fromisoformat(created["expires_at"]) - datetime.fromisoformat(
            created["created_at"]
        )
        assert abs(lifetime - timedelta(hours=48)) < timedelta(minutes=1)
        assert "original_text" not in str(created)

        fetched = client.get(f"/v1/kb/documents/{doc_id}", headers=admin_headers)
        assert fetched.status_code == status.HTTP_200_OK
        assert fetched.json() == created

        listed = client.get("/v1/kb/documents", headers=admin_headers, params={"tag": tag})
        assert listed.status_code == status.HTTP_200_OK
        assert listed.json() == {"total_returned": 1, "documents": [created]}

        deleted = client.delete(f"/v1/kb/documents/{doc_id}", headers=admin_headers)
        assert deleted.status_code == status.HTTP_200_OK
        assert deleted.json() == {"deleted": True, "doc_id": doc_id}

        gone = client.get(f"/v1/kb/documents/{doc_id}", headers=admin_headers)
        assert gone.status_code == status.HTTP_404_NOT_FOUND
        assert gone.json()["error"] == "NotFound"
        assert gone.json()["message"] == "Document not found"

        again = client.delete(f"/v1/kb/documents/{doc_id}", headers=admin_headers)
        assert again.status_code == status.HTTP_404_NOT_FOUND
        assert again.json()["message"] == "Document not found"

    @pytest.mark.asyncio
    async def test_duplicate_upload_is_409_and_bad_file_is_422(self, client, admin_headers, kb_llm):
        tag = f"t-{uuid4().hex[:8]}"
        body = f"Wasser {uuid4().hex}"
        assert _upload(client, admin_headers, body, tag=tag).status_code == 201

        duplicate = _upload(client, admin_headers, body, tag=tag, title="Copy")
        assert duplicate.status_code == status.HTTP_409_CONFLICT
        assert duplicate.json()["error"] == "Conflict"
        assert duplicate.json()["message"] == "Document with identical content already exists"

        bad = client.post(
            "/v1/kb/documents",
            headers=admin_headers,
            files={"file": ("plan.docx", b"PK\x03\x04", "application/octet-stream")},
            data={"title": "Docx"},
        )
        assert bad.status_code == status.HTTP_422_UNPROCESSABLE_ENTITY
        assert bad.json()["error"] == "ValidationError"
        assert "Unsupported file type: .docx" in bad.json()["message"]

        listed = client.get("/v1/kb/documents", headers=admin_headers, params={"tag": tag})
        assert listed.json()["total_returned"] == 1
        assert (
            client.delete("/v1/kb/documents", headers=admin_headers, params={"tag": tag}).json()[
                "count"
            ]
            == 1
        )

    @pytest.mark.asyncio
    async def test_pagination_and_delete_by_tag(self, client, admin_headers, kb_llm):
        tag = f"t-{uuid4().hex[:8]}"
        for word in ("Feuer", "Wasser", "Tier"):
            assert (
                _upload(client, admin_headers, f"{word} {uuid4().hex}", tag=tag).status_code == 201
            )

        page1 = client.get(
            "/v1/kb/documents", headers=admin_headers, params={"tag": tag, "limit": 2}
        ).json()
        page2 = client.get(
            "/v1/kb/documents", headers=admin_headers, params={"tag": tag, "limit": 2, "offset": 2}
        ).json()
        assert page1["total_returned"] == 2
        assert page2["total_returned"] == 1
        ids = [d["doc_id"] for d in page1["documents"] + page2["documents"]]
        assert len(set(ids)) == 3

        wiped = client.delete("/v1/kb/documents", headers=admin_headers, params={"tag": f" {tag} "})
        assert wiped.status_code == status.HTTP_200_OK
        assert wiped.json() == {"deleted": True, "tag": tag, "count": 3}
        after = client.get("/v1/kb/documents", headers=admin_headers, params={"tag": tag}).json()
        assert after == {"total_returned": 0, "documents": []}


# ===========================================================================
# Delivery bookkeeping on PostgreSQL
# ===========================================================================


class _NoDisposeEngine:
    """Keeps the activities' ``engine.dispose()`` away from the shared test engine."""

    def __init__(self, engine) -> None:
        self._engine = engine

    async def dispose(self) -> None:
        return None


@pytest.fixture
def activity_db(test_engine):
    factory = async_sessionmaker(test_engine, class_=AsyncSession, expire_on_commit=False)
    with patch.object(
        activities, "_session_factory", return_value=(_NoDisposeEngine(test_engine), factory)
    ):
        yield factory


async def _new_running_job(factory, delivery_mode: str = "push") -> JobMetadata:
    async with factory() as session:
        job = JobMetadata(
            job_id=uuid4(),
            project_id="pg-delivery",
            script_format=ScriptFormat.PDF,
            status=JobStatus.RUNNING,
            user_id="pg-user",
            priority=5,
            delivery_mode=delivery_mode,
        )
        session.add(job)
        await session.commit()
        return job


@pytest.mark.asyncio
class TestDeliveryBookkeepingOnPostgres:
    async def test_mark_delivering_stores_delivering_status(self, activity_db):
        """Regression: on PostgreSQL this UPDATE failed with ``value too long for VARCHAR(9)``."""
        job = await _new_running_job(activity_db)
        report_id = uuid4()
        kwargs = {
            "report_id": str(report_id),
            "job_id": str(job.job_id),
            "project_id": "pg-delivery",
            "user_id": "pg-user",
            "script_format": "pdf",
            "total_findings": 7,
            "report_ref_key": "eki:buf:report",
            "delivery_mode": "push",
        }

        await _mark_delivering(**kwargs)
        await _mark_delivering(**kwargs)  # Temporal retry: idempotent

        async with activity_db() as session:
            raw_status = (
                await session.execute(
                    text("SELECT status FROM job_metadata WHERE job_id = :id"), {"id": job.job_id}
                )
            ).scalar_one()
            assert raw_status == "DELIVERING"
            fresh = (
                await session.execute(select(JobMetadata).where(JobMetadata.job_id == job.job_id))
            ).scalar_one()
            assert fresh.status is JobStatus.DELIVERING
            assert fresh.delivery_status == "delivering"
            assert fresh.report_id == report_id
            reports = (
                (
                    await session.execute(
                        select(ReportMetadata).where(ReportMetadata.report_id == report_id)
                    )
                )
                .scalars()
                .all()
            )
            assert len(reports) == 1
            assert reports[0].job_id == job.job_id
            assert reports[0].script_format is ScriptFormat.PDF
            assert reports[0].total_findings == 7
            assert reports[0].report_ref_key == "eki:buf:report"
            assert 0 <= reports[0].processing_time_seconds < 600

    async def test_record_attempts_then_completion(self, activity_db):
        job = await _new_running_job(activity_db)
        await _mark_delivering(
            report_id=str(uuid4()),
            job_id=str(job.job_id),
            project_id="pg-delivery",
            user_id="pg-user",
            script_format="pdf",
            total_findings=0,
            report_ref_key="ref",
            delivery_mode="push",
        )

        await _record_attempt(
            job_id=str(job.job_id),
            attempt=1,
            status_code=503,
            delivered=False,
            delivery_mode="push",
        )
        async with activity_db() as session:
            fresh = (
                await session.execute(select(JobMetadata).where(JobMetadata.job_id == job.job_id))
            ).scalar_one()
            assert fresh.status is JobStatus.DELIVERING
            assert fresh.delivery_attempts == 1
            assert fresh.delivery_last_status_code == 503
            assert fresh.delivery_last_attempt_at.tzinfo is not None
            assert fresh.delivered_at is None

        await _record_attempt(
            job_id=str(job.job_id), attempt=2, status_code=201, delivered=True, delivery_mode="push"
        )
        async with activity_db() as session:
            fresh = (
                await session.execute(select(JobMetadata).where(JobMetadata.job_id == job.job_id))
            ).scalar_one()
            assert fresh.status is JobStatus.COMPLETED
            assert fresh.delivery_status == "delivered"
            assert fresh.delivery_attempts == 2
            assert fresh.delivery_last_status_code == 201
            assert fresh.progress_percentage == 100
            assert abs(fresh.delivered_at - datetime.now(UTC)) < timedelta(minutes=1)
