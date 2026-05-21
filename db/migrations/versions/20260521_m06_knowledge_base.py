"""M06: Knowledge Base tables (kb_documents, kb_embeddings) with pgvector.

Adds the persistent storage for the safety knowledge base used by the
RAG-augmented risk analysis.  All tables are additive: dropping the
migration leaves the M05 schema fully functional.

Revision ID: e8f1c2d3a401
Revises: d4b7e9f23a01
Create Date: 2026-05-21 17:45:00.000000
"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op
from pgvector.sqlalchemy import Vector
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.dialects.postgresql import UUID as PGUUID

revision: str = "e8f1c2d3a401"
down_revision: Union[str, None] = "d4b7e9f23a01"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

# Embedding dimensionality matches mxbai-embed-large (Ollama default for M06).
# Must stay in sync with core.db_models.KnowledgeEmbedding.vector and
# api.config.Settings.ollama_embedding_model.
_VECTOR_DIM = 1024


def upgrade() -> None:
    # Enable pgvector extension (idempotent, safe if already present)
    op.execute("CREATE EXTENSION IF NOT EXISTS vector")

    # KB documents: encrypted originals + metadata + TTL
    op.create_table(
        "kb_documents",
        sa.Column("doc_id", PGUUID(as_uuid=True), primary_key=True),
        sa.Column("title", sa.String(255), nullable=False),
        sa.Column("source", sa.String(20), nullable=False),
        sa.Column("tenant_id", PGUUID(as_uuid=True), nullable=False),
        sa.Column("tags", JSONB, nullable=False, server_default=sa.text("'[]'::jsonb")),
        sa.Column("original_text_encrypted", sa.LargeBinary, nullable=False),
        sa.Column("content_hash", sa.String(64), nullable=False),
        sa.Column("uploaded_by", sa.String(255), nullable=False),
        sa.Column("ttl_hours", sa.Integer, nullable=False, server_default="720"),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
    )
    op.create_index("ix_kb_documents_tenant_id", "kb_documents", ["tenant_id"])
    op.create_index("ix_kb_documents_expires_at", "kb_documents", ["expires_at"])
    op.create_index(
        "ix_kb_documents_content_hash",
        "kb_documents",
        ["content_hash"],
        unique=True,
    )

    # KB embeddings: one row per chunk, vector(1024) for mxbai-embed-large
    op.create_table(
        "kb_embeddings",
        sa.Column("embedding_id", PGUUID(as_uuid=True), primary_key=True),
        sa.Column(
            "doc_id",
            PGUUID(as_uuid=True),
            sa.ForeignKey("kb_documents.doc_id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("chunk_id", sa.String(64), nullable=False),
        sa.Column("chunk_text", sa.Text, nullable=False),
        sa.Column("vector", Vector(_VECTOR_DIM), nullable=False),
        sa.Column("dim", sa.Integer, nullable=False, server_default=str(_VECTOR_DIM)),
        sa.Column("chunk_offset", sa.Integer, nullable=False),
        sa.Column("length", sa.Integer, nullable=False),
        sa.Column("hash", sa.String(64), nullable=False),
    )
    op.create_index("ix_kb_embeddings_doc_id", "kb_embeddings", ["doc_id"])
    # ivfflat ANN index using cosine distance.  lists=100 is a sane default
    # for up to ~10k rows; can be tuned later via REINDEX.
    op.execute(
        "CREATE INDEX ix_kb_embeddings_vector "
        "ON kb_embeddings USING ivfflat (vector vector_cosine_ops) "
        "WITH (lists = 100)"
    )


def downgrade() -> None:
    op.execute("DROP INDEX IF EXISTS ix_kb_embeddings_vector")
    op.drop_index("ix_kb_embeddings_doc_id", table_name="kb_embeddings")
    op.drop_table("kb_embeddings")
    op.drop_index("ix_kb_documents_content_hash", table_name="kb_documents")
    op.drop_index("ix_kb_documents_expires_at", table_name="kb_documents")
    op.drop_index("ix_kb_documents_tenant_id", table_name="kb_documents")
    op.drop_table("kb_documents")
    # Note: we intentionally do NOT drop the vector extension on downgrade
    # because other schemas may depend on it.
