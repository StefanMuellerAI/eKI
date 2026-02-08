"""M05: Add delivery_mode, idempotency_key, report_ref_key columns.

Revision ID: c3a5f8d12e01
Revises: b7ed8ab1d224
Create Date: 2026-02-08 11:58:00.000000
"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

# revision identifiers
revision: str = "c3a5f8d12e01"
down_revision: Union[str, None] = "b7ed8ab1d224"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    # JobMetadata: add idempotency_key and delivery_mode
    op.add_column(
        "job_metadata",
        sa.Column("idempotency_key", sa.String(255), nullable=True),
    )
    op.add_column(
        "job_metadata",
        sa.Column("delivery_mode", sa.String(10), nullable=False, server_default="pull"),
    )
    op.create_index(
        "ix_job_metadata_idempotency_key",
        "job_metadata",
        ["idempotency_key"],
        unique=True,
    )

    # ReportMetadata: add report_ref_key and delivery_mode
    op.add_column(
        "report_metadata",
        sa.Column("report_ref_key", sa.String(255), nullable=True),
    )
    op.add_column(
        "report_metadata",
        sa.Column("delivery_mode", sa.String(10), nullable=False, server_default="pull"),
    )


def downgrade() -> None:
    op.drop_column("report_metadata", "delivery_mode")
    op.drop_column("report_metadata", "report_ref_key")
    op.drop_index("ix_job_metadata_idempotency_key", table_name="job_metadata")
    op.drop_column("job_metadata", "delivery_mode")
    op.drop_column("job_metadata", "idempotency_key")
