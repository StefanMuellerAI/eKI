"""KB dedupe per tenant: unique (tenant_id, content_hash) instead of global content_hash.

Revision ID: b8d0f2a4c6e3
Revises: a7c9e1f3b5d2
Create Date: 2026-10-15 13:00:00.000000

M06 created ``ix_kb_documents_content_hash`` as a *global* unique index, while
``KnowledgeBaseService.ingest`` dedupes per tenant.  A second tenant uploading
the same document therefore hit an IntegrityError (HTTP 503) instead of
getting its own copy.
"""

from collections.abc import Sequence
from typing import Union

import sqlalchemy as sa
from alembic import op

# revision identifiers
revision: str = "b8d0f2a4c6e3"
down_revision: Union[str, None] = "a7c9e1f3b5d2"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.drop_index("ix_kb_documents_content_hash", table_name="kb_documents")
    op.create_index("ix_kb_documents_content_hash", "kb_documents", ["content_hash"], unique=False)
    op.create_unique_constraint(
        "uq_kb_documents_tenant_content_hash", "kb_documents", ["tenant_id", "content_hash"]
    )


def downgrade() -> None:
    # Refuse rather than silently delete another tenant's document.
    duplicates = (
        op.get_bind()
        .execute(
            sa.text(
                "SELECT count(*) FROM (SELECT content_hash FROM kb_documents "
                "GROUP BY content_hash HAVING count(*) > 1) d"
            )
        )
        .scalar()
    )
    if duplicates:
        raise RuntimeError(
            f"{duplicates} content hash(es) are shared by several tenants; the global "
            "unique index cannot be restored. Remove the duplicates first."
        )
    op.drop_constraint("uq_kb_documents_tenant_content_hash", "kb_documents", type_="unique")
    op.drop_index("ix_kb_documents_content_hash", table_name="kb_documents")
    op.create_index("ix_kb_documents_content_hash", "kb_documents", ["content_hash"], unique=True)
