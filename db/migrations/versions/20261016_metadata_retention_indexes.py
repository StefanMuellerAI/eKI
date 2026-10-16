"""Metadata retention (1.0.1): indexes on the timestamps the nightly cleanup filters by.

Revision ID: c4e6a8b0d2f1
Revises: b8d0f2a4c6e3
Create Date: 2026-10-16 12:00:00.000000

The Temporal schedule ``eki-metadata-cleanup`` (``workflows/maintenance.py``)
deletes rows older than ``METADATA_RETENTION_DAYS`` in batches. Every batch
selects ``... WHERE created_at < :cutoff LIMIT n``; without an index that is a
full table scan per batch. ``api_keys`` stays unindexed (a handful of rows).

Additive only; plain ``CREATE INDEX`` (the tables are small, the lock is short).
"""

from collections.abc import Sequence
from typing import Union

from alembic import op

# revision identifiers
revision: str = "c4e6a8b0d2f1"
down_revision: Union[str, None] = "b8d0f2a4c6e3"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

# (index name, table, column) -- names match SQLAlchemy's ``index=True`` default.
_INDEXES = (
    ("ix_job_metadata_created_at", "job_metadata", "created_at"),
    ("ix_report_metadata_created_at", "report_metadata", "created_at"),
    ("ix_delivery_dead_letters_created_at", "delivery_dead_letters", "created_at"),
    ("ix_audit_logs_timestamp", "audit_logs", "timestamp"),
)


def upgrade() -> None:
    for name, table, column in _INDEXES:
        op.create_index(name, table, [column], unique=False)


def downgrade() -> None:
    for name, table, _column in reversed(_INDEXES):
        op.drop_index(name, table_name=table)
