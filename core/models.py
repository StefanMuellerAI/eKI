"""Pydantic models for API request/response schemas."""

import base64
import ipaddress
import re
from datetime import datetime
from enum import Enum
from typing import Any
from urllib.parse import urlparse
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, HttpUrl, field_validator, model_validator


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


# ---------------------------------------------------------------------------
# Transient Scene Models (M02) -- never persisted to DB
# ---------------------------------------------------------------------------


class TimeOfDay(str, Enum):
    """Time-of-day designation extracted from scene headings."""

    DAY = "DAY"
    NIGHT = "NIGHT"
    DAWN = "DAWN"
    DUSK = "DUSK"
    MORNING = "MORNING"
    EVENING = "EVENING"
    CONTINUOUS = "CONTINUOUS"
    UNKNOWN = "UNKNOWN"


class LocationType(str, Enum):
    """Interior/exterior designation from scene headings."""

    INT = "INT"
    EXT = "EXT"
    INT_EXT = "INT/EXT"
    UNKNOWN = "UNKNOWN"


class DialogueLine(BaseModel):
    """A single dialogue line spoken by a character."""

    character: str = Field(..., description="Speaking character name")
    parenthetical: str | None = Field(None, description="Parenthetical direction")
    text: str = Field(..., description="Dialogue text")


class ParsedScene(BaseModel):
    """A single parsed scene from a screenplay."""

    scene_id: UUID = Field(..., description="Unique scene identifier")
    number: str | None = Field(None, description="Scene number from FDX")
    heading: str = Field(..., description="Original scene heading text")
    location: str = Field(..., description="Extracted location name")
    location_type: LocationType = Field(..., description="INT/EXT designation")
    time_of_day: TimeOfDay = Field(..., description="Time of day")
    characters: list[str] = Field(default_factory=list, description="Speaking characters")
    action_text: str = Field(default="", description="Combined action/description text")
    dialogue: list[DialogueLine] = Field(default_factory=list, description="Dialogue lines")
    text: str = Field(default="", description="Full scene text (heading + action + dialogue)")
    parse_confidence: float = Field(
        default=1.0, ge=0.0, le=1.0,
        description="Confidence of structural classification (1.0 = FDX/certain, 0.0 = failed)",
    )
    parse_method: str = Field(
        default="fdx", description="Parser method: fdx, pdf_llm, pdf_fallback"
    )


class CharacterInfo(BaseModel):
    """Aggregated character information across the script."""

    name: str = Field(..., description="Character name")
    scene_appearances: list[str] = Field(
        default_factory=list, description="Scene IDs where character appears"
    )


class ParsedScript(BaseModel):
    """Complete parsed screenplay -- transient, never persisted."""

    script_id: UUID = Field(..., description="Unique script identifier")
    title: str | None = Field(None, description="Script title if available")
    format: ScriptFormat = Field(..., description="Source format (fdx/pdf)")
    total_scenes: int = Field(..., description="Total number of scenes")
    scenes: list[ParsedScene] = Field(default_factory=list, description="Parsed scenes")
    characters: list[CharacterInfo] = Field(
        default_factory=list, description="Character index"
    )
    parsing_time_seconds: float = Field(..., description="Time taken to parse")
    metadata: dict[str, Any] = Field(default_factory=dict, description="Parser metadata")
    overall_confidence: float = Field(
        default=1.0, ge=0.0, le=1.0,
        description="Average parse confidence across all scenes",
    )
    warnings: list[str] = Field(
        default_factory=list, description="Parser warnings (OCR needed, low confidence, etc.)"
    )


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
    delivery: str = Field(
        default="pull", description="Delivery mode: 'pull' (One-Shot GET) or 'push' (POST to ePro)"
    )
    idempotency_key: str | None = Field(
        None, description="Optional idempotency key to prevent duplicate jobs",
        max_length=255,
    )
    metadata: dict[str, Any] = Field(
        default_factory=dict, description="Additional metadata for audit trail"
    )

    @model_validator(mode="after")
    def validate_script_content(self) -> "SecurityCheckRequest":
        """Validate base64 encoding, size, and content type based on format."""
        v = self.script_content
        fmt = self.script_format

        if not v.strip():
            raise ValueError("Script content cannot be empty")

        try:
            decoded = base64.b64decode(v, validate=True)
        except base64.binascii.Error:
            raise ValueError("Invalid base64 encoding")

        # Check decoded size (10MB limit)
        max_size = 10 * 1024 * 1024
        if len(decoded) > max_size:
            raise ValueError(f"Decoded script exceeds {max_size} byte limit")

        # Format-specific validation
        if fmt == ScriptFormat.FDX:
            # FDX is XML text -- validate no null bytes and valid UTF-8
            if b"\x00" in decoded[:1000]:
                raise ValueError("FDX script contains invalid null bytes")
            try:
                decoded[:1000].decode("utf-8")
            except UnicodeDecodeError:
                raise ValueError("FDX script does not appear to be valid text")
        elif fmt == ScriptFormat.PDF:
            # PDF is binary -- just check it starts with %PDF
            if not decoded[:5].startswith(b"%PDF"):
                raise ValueError("File does not appear to be a valid PDF")

        return self

    @field_validator("callback_url")
    @classmethod
    def validate_callback_url(cls, v: HttpUrl | None) -> HttpUrl | None:
        """Validate callback URL to prevent SSRF attacks."""
        if v is None:
            return v

        parsed = urlparse(str(v))

        # Enforce TLS for callback delivery.
        if parsed.scheme != "https":
            raise ValueError("Callback URL must use HTTPS")

        # Block private/internal IP ranges
        hostname = parsed.hostname
        if not hostname:
            raise ValueError("Invalid callback URL hostname")

        try:
            ip = ipaddress.ip_address(hostname)
        except ValueError:
            ip = None

        if ip and (
            ip.is_private or ip.is_loopback or ip.is_link_local or ip.is_multicast or ip.is_reserved
        ):
            raise ValueError("Callback URL cannot point to private/internal IP addresses")

        # Whitelist of allowed domains
        allowed_domains = {
            "epro.filmakademie.de",
            "epro-stage.filmakademie.de",
        }

        if hostname.lower().rstrip(".") not in allowed_domains:
            raise ValueError(
                "Callback URL domain not allowed. "
                f"Allowed: {', '.join(sorted(allowed_domains))}"
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
                elif not isinstance(value, int | float | bool):
                    raise ValueError(
                        f"Invalid metadata value type for key '{key}'. "
                        "Allowed: string, number, boolean, null"
                    )

        return v


class AsyncSecurityCheckRequest(SecurityCheckRequest):
    """Request model for async security check endpoint."""

    priority: int = Field(default=5, ge=1, le=10, description="Job priority (1=highest, 10=lowest)")


# Response Models


class HealthResponse(BaseModel):
    """Health check response."""

    model_config = ConfigDict(json_encoders={datetime: lambda v: v.isoformat()})

    status: str = Field(default="healthy", description="Service health status")
    timestamp: datetime = Field(default_factory=datetime.utcnow, description="Response timestamp")
    version: str = Field(default="0.1.0", description="API version")


class ReadinessResponse(BaseModel):
    """Readiness check response with dependency status."""

    status: str = Field(..., description="Overall readiness status")
    timestamp: datetime = Field(default_factory=datetime.utcnow, description="Response timestamp")
    services: dict[str, bool] = Field(..., description="Service availability status")


class MeasureItem(BaseModel):
    """A codified safety measure from the measures catalog."""

    code: str = Field(..., description="Measure code (e.g. RIG-SAFETY, PSY-BRIEFING)")
    title: str = Field(..., description="Human-readable measure title")
    responsible: str = Field(default="", description="Responsible role (e.g. Stunt Coordination)")
    due: str = Field(default="", description="Due date template (e.g. shooting-3d)")


class RiskFinding(BaseModel):
    """Individual risk finding in a script (M04: extended with taxonomy and scoring)."""

    id: str = Field(..., description="Unique finding ID")
    scene_number: str | None = Field(None, description="Scene number where risk was found")
    risk_level: RiskLevel = Field(..., description="Severity level (calculated from likelihood x impact)")
    category: str = Field(..., description="Risk category: PHYSICAL, ENVIRONMENTAL, PSYCHOLOGICAL")
    risk_class: str = Field(
        default="", description="Specific risk class (e.g. FIRE, HEIGHT, INTIMACY)"
    )
    rule_id: str = Field(default="", description="Taxonomy rule ID (e.g. SEC-P-008)")
    likelihood: int = Field(
        default=0, ge=0, le=5, description="Likelihood of risk occurring (1-5, 0=unscored)"
    )
    impact: int = Field(
        default=0, ge=0, le=5, description="Severity of impact if risk occurs (1-5, 0=unscored)"
    )
    description: str = Field(..., description="Human-readable description")
    recommendation: str = Field(..., description="Mitigation recommendation")
    measures: list[MeasureItem] = Field(
        default_factory=list, description="Applicable safety measures from the catalog"
    )
    confidence: float = Field(..., ge=0.0, le=1.0, description="Confidence score of the finding")
    line_reference: str | None = Field(None, description="Specific line or dialogue reference")


class SecurityReport(BaseModel):
    """Security analysis report."""

    report_id: UUID = Field(..., description="Unique report ID")
    project_id: str = Field(..., description="eProjekt project ID")
    script_format: ScriptFormat = Field(..., description="Format of analyzed script")
    created_at: datetime = Field(
        default_factory=datetime.utcnow, description="Report creation timestamp"
    )
    risk_summary: dict[RiskLevel, int] = Field(..., description="Count of findings per risk level")
    total_findings: int = Field(..., description="Total number of findings")
    findings: list[RiskFinding] = Field(default_factory=list, description="List of findings")
    processing_time_seconds: float = Field(..., description="Time taken to process")
    metadata: dict[str, Any] = Field(default_factory=dict, description="Additional metadata")


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
    pdf_base64: str | None = Field(
        None, description="Base64-encoded PDF report (human-readable)"
    )
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
    timestamp: datetime = Field(default_factory=datetime.utcnow, description="Error timestamp")
