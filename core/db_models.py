"""SQLAlchemy database models for audit metadata."""

from datetime import datetime
from uuid import UUID, uuid4

from sqlalchemy import JSON, Boolean, DateTime, Enum, Integer, String, Text, func
from sqlalchemy.dialects.postgresql import UUID as PGUUID
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column

from core.models import JobStatus, ScriptFormat


class Base(DeclarativeBase):
    """Base class for all database models."""

    pass


class ApiKeyModel(Base):
    """
    API key model for authentication.

    Stores hashed API keys (never store plaintext!).
    """

    __tablename__ = "api_keys"

    id: Mapped[UUID] = mapped_column(PGUUID(as_uuid=True), primary_key=True, default=uuid4)

    # User/Organization identification
    user_id: Mapped[str] = mapped_column(String(255), nullable=False, index=True)
    organization_id: Mapped[str | None] = mapped_column(String(255), nullable=True, index=True)

    # API Key (hashed with SHA-256)
    key_hash: Mapped[str] = mapped_column(String(64), nullable=False, unique=True, index=True)

    # Metadata
    name: Mapped[str] = mapped_column(String(255), nullable=False)
    description: Mapped[str | None] = mapped_column(String(1000), nullable=True)

    # Status
    is_active: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)

    # Timestamps
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    last_used_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)

    # Usage tracking
    usage_count: Mapped[int] = mapped_column(Integer, default=0, nullable=False)

    def __repr__(self) -> str:
        return f"<ApiKey(id={self.id}, user_id={self.user_id}, name={self.name})>"


class AuditLog(Base):
    """Audit log for API requests."""

    __tablename__ = "audit_logs"

    id: Mapped[UUID] = mapped_column(PGUUID(as_uuid=True), primary_key=True, default=uuid4)
    timestamp: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    endpoint: Mapped[str] = mapped_column(String(255), nullable=False)
    method: Mapped[str] = mapped_column(String(10), nullable=False)
    user_id: Mapped[str | None] = mapped_column(String(255), nullable=True)
    project_id: Mapped[str | None] = mapped_column(String(255), nullable=True)
    request_id: Mapped[str | None] = mapped_column(String(255), nullable=True)
    status_code: Mapped[int] = mapped_column(Integer, nullable=False)
    processing_time_ms: Mapped[int | None] = mapped_column(Integer, nullable=True)
    extra_metadata: Mapped[dict] = mapped_column(JSON, nullable=False, default=dict)


class JobMetadata(Base):
    """Metadata for asynchronous security check jobs."""

    __tablename__ = "job_metadata"

    job_id: Mapped[UUID] = mapped_column(PGUUID(as_uuid=True), primary_key=True, default=uuid4)
    project_id: Mapped[str] = mapped_column(String(255), nullable=False, index=True)
    script_format: Mapped[ScriptFormat] = mapped_column(
        Enum(ScriptFormat, native_enum=False), nullable=False
    )
    status: Mapped[JobStatus] = mapped_column(
        Enum(JobStatus, native_enum=False), nullable=False, default=JobStatus.PENDING
    )
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )
    user_id: Mapped[str] = mapped_column(String(255), nullable=False, index=True)
    priority: Mapped[int] = mapped_column(Integer, nullable=False, default=5)
    progress_percentage: Mapped[int | None] = mapped_column(Integer, nullable=True)
    report_id: Mapped[UUID | None] = mapped_column(PGUUID(as_uuid=True), nullable=True)
    error_message: Mapped[str | None] = mapped_column(Text, nullable=True)
    workflow_id: Mapped[str | None] = mapped_column(String(255), nullable=True)
    idempotency_key: Mapped[str | None] = mapped_column(
        String(255), nullable=True, unique=True, index=True
    )
    delivery_mode: Mapped[str] = mapped_column(String(10), nullable=False, default="pull")
    extra_metadata: Mapped[dict] = mapped_column(JSON, nullable=False, default=dict)


class ReportMetadata(Base):
    """Metadata for security reports (not the full report content)."""

    __tablename__ = "report_metadata"

    report_id: Mapped[UUID] = mapped_column(PGUUID(as_uuid=True), primary_key=True, default=uuid4)
    job_id: Mapped[UUID] = mapped_column(PGUUID(as_uuid=True), nullable=False, index=True)
    project_id: Mapped[str] = mapped_column(String(255), nullable=False, index=True)
    user_id: Mapped[str] = mapped_column(String(255), nullable=False, index=True)
    script_format: Mapped[ScriptFormat] = mapped_column(
        Enum(ScriptFormat, native_enum=False), nullable=False
    )
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    retrieved_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    is_retrieved: Mapped[bool] = mapped_column(default=False, nullable=False)
    total_findings: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    processing_time_seconds: Mapped[float] = mapped_column(nullable=False)
    report_ref_key: Mapped[str | None] = mapped_column(String(255), nullable=True)
    delivery_mode: Mapped[str] = mapped_column(String(10), nullable=False, default="pull")
    extra_metadata: Mapped[dict] = mapped_column(JSON, nullable=False, default=dict)
