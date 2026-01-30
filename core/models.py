"""Pydantic models for API request/response schemas."""

import base64
import ipaddress
import re
from datetime import datetime
from enum import Enum
from typing import Any
from urllib.parse import urlparse
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, HttpUrl, field_validator


class JobStatus(str, Enum):
    """Status of an async security check job."""

    PENDING = "pending"
    RUNNING = "running"
    COMPLETED = "completed"
    FAILED = "failed"
    CANCELLED = "cancelled"


class RiskLevel(str, Enum):
    """Risk level classification."""

    CRITICAL = "critical"
    HIGH = "high"
    MEDIUM = "medium"
    LOW = "low"
    INFO = "info"


class ScriptFormat(str, Enum):
    """Supported script formats."""

    FDX = "fdx"  # Final Draft XML
    PDF = "pdf"  # PDF with OCR support


# Request Models


class SecurityCheckRequest(BaseModel):
    """Request model for security check endpoint."""

    script_content: str = Field(
        ...,
        description="Base64-encoded script content",
        min_length=1,
        max_length=10_485_760,  # 10MB limit
    )
    script_format: ScriptFormat = Field(..., description="Format of the script")
    project_id: str = Field(
        ...,
        description="eProjekt project ID",
        min_length=1,
        max_length=100,
        pattern=r"^[a-zA-Z0-9_-]+$",
    )
    callback_url: HttpUrl | None = Field(
        None, description="Optional callback URL for async results"
    )
    metadata: dict[str, Any] = Field(
        default_factory=dict, description="Additional metadata for audit trail"
    )

    @field_validator("script_content")
    @classmethod
    def validate_script_content(cls, v: str) -> str:
        """Validate base64 encoding and decoded size."""
        if not v.strip():
            raise ValueError("Script content cannot be empty")

        try:
            # Validate Base64 encoding
            decoded = base64.b64decode(v, validate=True)

            # Check decoded size (10MB limit)
            max_size = 10 * 1024 * 1024
            if len(decoded) > max_size:
                raise ValueError(f"Decoded script exceeds {max_size} byte limit")

            # Check for null bytes (potential binary exploit)
            if b"\x00" in decoded[:1000]:
                raise ValueError("Script contains invalid null bytes")

            # Validate it's text
            try:
                decoded[:1000].decode("utf-8")
            except UnicodeDecodeError:
                raise ValueError("Script does not appear to be valid text")

            return v

        except base64.binascii.Error:
            raise ValueError("Invalid base64 encoding")
        except Exception as e:
            raise ValueError(f"Script validation failed: {str(e)}")

    @field_validator("callback_url")
    @classmethod
    def validate_callback_url(cls, v: HttpUrl | None) -> HttpUrl | None:
        """Validate callback URL to prevent SSRF attacks."""
        if v is None:
            return v

        parsed = urlparse(str(v))

        # Only allow HTTPS in production (configurable)
        if parsed.scheme not in ["https", "http"]:
            raise ValueError("Callback URL must use HTTP or HTTPS")

        # Block private/internal IP ranges
        try:
            hostname = parsed.hostname
            if hostname:
                try:
                    ip = ipaddress.ip_address(hostname)
                    if (
                        ip.is_private
                        or ip.is_loopback
                        or ip.is_link_local
                        or ip.is_multicast
                        or ip.is_reserved
                    ):
                        raise ValueError(
                            "Callback URL cannot point to private/internal IP addresses"
                        )
                except ValueError:
                    pass  # Not an IP, it's a hostname
        except Exception:
            raise ValueError("Invalid callback URL hostname")

        # Whitelist of allowed domains
        allowed_domains = [
            "epro.filmakademie.de",
            "epro-stage.filmakademie.de",
        ]

        if parsed.hostname and parsed.hostname not in allowed_domains:
            raise ValueError(
                f"Callback URL domain not allowed. Allowed: {', '.join(allowed_domains)}"
            )

        return v

    @field_validator("project_id")
    @classmethod
    def validate_project_id(cls, v: str) -> str:
        """Validate project_id to prevent SQL injection."""
        if not v.strip():
            raise ValueError("project_id cannot be empty")

        if not re.match(r"^[a-zA-Z0-9_-]{1,100}$", v):
            raise ValueError(
                "project_id must contain only alphanumeric characters, "
                "hyphens, and underscores (max 100 chars)"
            )

        return v

    @field_validator("metadata")
    @classmethod
    def validate_metadata(cls, v: dict[str, Any]) -> dict[str, Any]:
        """Validate metadata dictionary to prevent injection attacks."""
        if len(v) > 50:
            raise ValueError("Too many metadata fields (max 50)")

        for key, value in v.items():
            # Validate key format
            if not re.match(r"^[a-zA-Z0-9_-]{1,50}$", key):
                raise ValueError(f"Invalid metadata key format: {key}")

            # Validate value type and size
            if value is not None:
                if isinstance(value, str):
                    if len(value) > 1000:
                        raise ValueError(
                            f"Metadata value too long for key '{key}' (max 1000 chars)"
                        )
                elif not isinstance(value, (int, float, bool)):
                    raise ValueError(
                        f"Invalid metadata value type for key '{key}'. "
                        "Allowed: string, number, boolean, null"
                    )

        return v


class AsyncSecurityCheckRequest(SecurityCheckRequest):
    """Request model for async security check endpoint."""

    priority: int = Field(
        default=5, ge=1, le=10, description="Job priority (1=highest, 10=lowest)"
    )


# Response Models


class HealthResponse(BaseModel):
    """Health check response."""

    model_config = ConfigDict(json_encoders={datetime: lambda v: v.isoformat()})

    status: str = Field(default="healthy", description="Service health status")
    timestamp: datetime = Field(
        default_factory=datetime.utcnow, description="Response timestamp"
    )
    version: str = Field(default="0.1.0", description="API version")


class ReadinessResponse(BaseModel):
    """Readiness check response with dependency status."""

    status: str = Field(..., description="Overall readiness status")
    timestamp: datetime = Field(
        default_factory=datetime.utcnow, description="Response timestamp"
    )
    services: dict[str, bool] = Field(..., description="Service availability status")


class RiskFinding(BaseModel):
    """Individual risk finding in a script."""

    id: str = Field(..., description="Unique finding ID")
    scene_number: str | None = Field(None, description="Scene number where risk was found")
    risk_level: RiskLevel = Field(..., description="Severity level")
    category: str = Field(..., description="Risk category")
    description: str = Field(..., description="Human-readable description")
    recommendation: str = Field(..., description="Mitigation recommendation")
    confidence: float = Field(
        ..., ge=0.0, le=1.0, description="Confidence score of the finding"
    )
    line_reference: str | None = Field(None, description="Specific line or dialogue reference")


class SecurityReport(BaseModel):
    """Security analysis report."""

    report_id: UUID = Field(..., description="Unique report ID")
    project_id: str = Field(..., description="eProjekt project ID")
    script_format: ScriptFormat = Field(..., description="Format of analyzed script")
    created_at: datetime = Field(
        default_factory=datetime.utcnow, description="Report creation timestamp"
    )
    risk_summary: dict[RiskLevel, int] = Field(
        ..., description="Count of findings per risk level"
    )
    total_findings: int = Field(..., description="Total number of findings")
    findings: list[RiskFinding] = Field(default_factory=list, description="List of findings")
    processing_time_seconds: float = Field(..., description="Time taken to process")
    metadata: dict[str, Any] = Field(
        default_factory=dict, description="Additional metadata"
    )


class SyncSecurityCheckResponse(BaseModel):
    """Response for synchronous security check."""

    report: SecurityReport = Field(..., description="Security analysis report")
    message: str = Field(
        default="Security check completed successfully",
        description="Human-readable message",
    )


class AsyncSecurityCheckResponse(BaseModel):
    """Response for asynchronous security check."""

    job_id: UUID = Field(..., description="Unique job ID for tracking")
    status: JobStatus = Field(default=JobStatus.PENDING, description="Initial job status")
    message: str = Field(
        default="Security check job created successfully",
        description="Human-readable message",
    )
    status_url: str = Field(..., description="URL to check job status")
    estimated_completion_seconds: int | None = Field(
        None, description="Estimated time to completion"
    )


class JobStatusResponse(BaseModel):
    """Response for job status query."""

    job_id: UUID = Field(..., description="Job ID")
    status: JobStatus = Field(..., description="Current job status")
    created_at: datetime = Field(..., description="Job creation timestamp")
    updated_at: datetime = Field(..., description="Last update timestamp")
    progress_percentage: int | None = Field(
        None, ge=0, le=100, description="Progress percentage if available"
    )
    report_id: UUID | None = Field(None, description="Report ID if completed")
    error_message: str | None = Field(None, description="Error message if failed")
    metadata: dict[str, Any] = Field(default_factory=dict, description="Job metadata")


class ReportResponse(BaseModel):
    """Response for report retrieval (one-shot pull mode)."""

    report: SecurityReport = Field(..., description="Security analysis report")
    message: str = Field(
        default="Report retrieved successfully. This URL is now invalidated.",
        description="Human-readable message",
    )


# Error Response Models


class ErrorDetail(BaseModel):
    """Detailed error information."""

    field: str | None = Field(None, description="Field that caused the error")
    message: str = Field(..., description="Error message")
    error_code: str | None = Field(None, description="Error code for programmatic handling")


class ErrorResponse(BaseModel):
    """Standard error response."""

    error: str = Field(..., description="Error type")
    message: str = Field(..., description="Human-readable error message")
    details: list[ErrorDetail] = Field(default_factory=list, description="Detailed error info")
    request_id: str | None = Field(None, description="Request tracking ID")
    timestamp: datetime = Field(
        default_factory=datetime.utcnow, description="Error timestamp"
    )
