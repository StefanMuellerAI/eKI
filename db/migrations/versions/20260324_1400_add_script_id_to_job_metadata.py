"""Add script_id column to job_metadata for ePro document correlation.

Revision ID: d4b7e9f23a01
Revises: c3a5f8d12e01
Create Date: 2026-03-24 14:00:00.000000
"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "d4b7e9f23a01"
down_revision: Union[str, None] = "c3a5f8d12e01"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column(
        "job_metadata",
        sa.Column("script_id", sa.Integer(), nullable=True),
    )


def downgrade() -> None:
    op.drop_column("job_metadata", "script_id")
