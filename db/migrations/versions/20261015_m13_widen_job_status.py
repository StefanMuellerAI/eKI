"""Widen job_metadata.status to VARCHAR(20) so JobStatus.DELIVERING fits.

Revision ID: a7c9e1f3b5d2
Revises: f1a2b3c4d5e6
Create Date: 2026-10-15 12:00:00.000000

The initial migration created ``status`` as a non-native Enum, which
SQLAlchemy renders as ``VARCHAR(9)`` (length of ``CANCELLED``).  M10 added
``DELIVERING`` (10 chars) to ``JobStatus`` without widening the column, so on
PostgreSQL every ``_mark_delivering`` update failed with
``value too long for type character varying(9)``.  SQLite (unit suite) does
not enforce VARCHAR lengths, which hid the bug.
"""

from collections.abc import Sequence
from typing import Union

import sqlalchemy as sa
from alembic import op

# revision identifiers
revision: str = "a7c9e1f3b5d2"
down_revision: Union[str, None] = "f1a2b3c4d5e6"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.alter_column(
        "job_metadata",
        "status",
        existing_type=sa.String(length=9),
        type_=sa.String(length=20),
        existing_nullable=False,
    )


def downgrade() -> None:
    # Rows in DELIVERING would not fit back into VARCHAR(9); fold them into
    # RUNNING (the closest pre-M10 state) before narrowing.
    op.execute("UPDATE job_metadata SET status = 'RUNNING' WHERE status = 'DELIVERING'")
    op.alter_column(
        "job_metadata",
        "status",
        existing_type=sa.String(length=20),
        type_=sa.String(length=9),
        existing_nullable=False,
    )
