"""M10: Outbound-Adapter Hardening -- delivery bookkeeping, dead letters, scoped idempotency.

Revision ID: f1a2b3c4d5e6
Revises: e8f1c2d3a401
Create Date: 2026-09-16 12:00:00.000000

Additive only:
* job_metadata.delivery_status / delivery_attempts / delivery_last_status_code /
  delivery_last_attempt_at / delivered_at
* delivery_dead_letters table (content-free)
* idempotency_key uniqueness scoped to (user_id, idempotency_key)
* api_keys.is_admin for /v1/ops/*
"""

from collections.abc import Sequence
from typing import Union

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

# revision identifiers
revision: str = "f1a2b3c4d5e6"
down_revision: Union[str, None] = "e8f1c2d3a401"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    # --- job_metadata: delivery bookkeeping -------------------------------
    op.add_column(
        "job_metadata",
        sa.Column("delivery_status", sa.String(20), nullable=False, server_default="pending"),
    )
    op.add_column(
        "job_metadata",
        sa.Column("delivery_attempts", sa.Integer(), nullable=False, server_default="0"),
    )
    op.add_column(
        "job_metadata", sa.Column("delivery_last_status_code", sa.Integer(), nullable=True)
    )
    op.add_column(
        "job_metadata",
        sa.Column("delivery_last_attempt_at", sa.DateTime(timezone=True), nullable=True),
    )
    op.add_column(
        "job_metadata", sa.Column("delivered_at", sa.DateTime(timezone=True), nullable=True)
    )

    # Backfill: jobs that already completed before M10 count as delivered.
    op.execute("UPDATE job_metadata SET delivery_status = 'delivered' WHERE status = 'COMPLETED'")
    op.execute(
        "UPDATE job_metadata SET delivery_status = 'failed' "
        "WHERE status = 'FAILED' AND error_message LIKE 'delivery_failed:%'"
    )

    # --- idempotency: global unique -> (user_id, idempotency_key) --------
    op.drop_index("ix_job_metadata_idempotency_key", table_name="job_metadata")
    op.create_index(
        "ix_job_metadata_idempotency_key", "job_metadata", ["idempotency_key"], unique=False
    )
    op.create_unique_constraint(
        "uq_job_metadata_user_idempotency", "job_metadata", ["user_id", "idempotency_key"]
    )

    # --- api_keys.is_admin --------------------------------------------------
    op.add_column(
        "api_keys",
        sa.Column("is_admin", sa.Boolean(), nullable=False, server_default="false"),
    )

    # --- delivery_dead_letters ---------------------------------------------
    op.create_table(
        "delivery_dead_letters",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column("job_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("report_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("project_id", sa.String(255), nullable=False),
        sa.Column("user_id", sa.String(255), nullable=False),
        sa.Column("delivery_mode", sa.String(10), nullable=False, server_default="push"),
        sa.Column("reason", sa.String(50), nullable=False),
        sa.Column("attempts", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("last_status_code", sa.Integer(), nullable=True),
        sa.Column("last_error_type", sa.String(100), nullable=True),
        sa.Column("webhook_sent", sa.Boolean(), nullable=False, server_default="false"),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column("acknowledged_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("acknowledged_by", sa.String(255), nullable=True),
        sa.Column("note", sa.String(1000), nullable=True),
    )
    op.create_index("ix_delivery_dead_letters_job_id", "delivery_dead_letters", ["job_id"])
    op.create_index("ix_delivery_dead_letters_project_id", "delivery_dead_letters", ["project_id"])
    op.create_index("ix_delivery_dead_letters_user_id", "delivery_dead_letters", ["user_id"])
    op.create_index(
        "ix_delivery_dead_letters_acknowledged_at",
        "delivery_dead_letters",
        ["acknowledged_at"],
    )


def downgrade() -> None:
    op.drop_index("ix_delivery_dead_letters_acknowledged_at", table_name="delivery_dead_letters")
    op.drop_index("ix_delivery_dead_letters_user_id", table_name="delivery_dead_letters")
    op.drop_index("ix_delivery_dead_letters_project_id", table_name="delivery_dead_letters")
    op.drop_index("ix_delivery_dead_letters_job_id", table_name="delivery_dead_letters")
    op.drop_table("delivery_dead_letters")

    op.drop_column("api_keys", "is_admin")

    op.drop_constraint("uq_job_metadata_user_idempotency", "job_metadata", type_="unique")
    op.drop_index("ix_job_metadata_idempotency_key", table_name="job_metadata")
    op.create_index(
        "ix_job_metadata_idempotency_key", "job_metadata", ["idempotency_key"], unique=True
    )

    op.drop_column("job_metadata", "delivered_at")
    op.drop_column("job_metadata", "delivery_last_attempt_at")
    op.drop_column("job_metadata", "delivery_last_status_code")
    op.drop_column("job_metadata", "delivery_attempts")
    op.drop_column("job_metadata", "delivery_status")
