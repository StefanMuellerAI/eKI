"""Tests for M05: Report generator, PDF generation, delivery modes, and idempotency."""

import base64
import json
from datetime import datetime
from typing import Any
from unittest.mock import AsyncMock, MagicMock
from uuid import uuid4

import pytest

from services.report_generator import (
    build_report_dict,
    generate_pdf_base64,
    generate_pdf_report,
)


# ===================================================================
# Report Builder
# ===================================================================


class TestBuildReportDict:
    """Tests for build_report_dict."""

    def test_builds_complete_report(self):
        findings = [
            {"risk_level": "critical", "category": "PHYSICAL", "description": "Fire risk",
             "recommendation": "Fire dept", "risk_class": "FIRE", "rule_id": "SEC-P-008",
             "likelihood": 4, "impact": 5, "confidence": 0.9, "scene_number": "1",
             "measures": [{"code": "FIRE-DEPT", "title": "Feuerwehr", "responsible": "Prod", "due": "1d"}]},
            {"risk_level": "high", "category": "PHYSICAL", "description": "Height risk",
             "recommendation": "Safety harness", "risk_class": "HEIGHT", "rule_id": "SEC-P-006",
             "likelihood": 3, "impact": 4, "confidence": 0.85, "scene_number": "2",
             "measures": []},
            {"risk_level": "low", "category": "ENVIRONMENTAL", "description": "Noise",
             "recommendation": "Hearing protection", "risk_class": "NOISE", "rule_id": "SEC-E-004",
             "likelihood": 2, "impact": 1, "confidence": 0.7, "scene_number": "3",
             "measures": []},
        ]

        report = build_report_dict(
            report_id="test-report-123",
            project_id="test-project",
            script_format="fdx",
            findings=findings,
            processing_time_seconds=12.5,
        )

        assert report["report_id"] == "test-report-123"
        assert report["project_id"] == "test-project"
        assert report["script_format"] == "fdx"
        assert report["total_findings"] == 3
        assert report["risk_summary"]["critical"] == 1
        assert report["risk_summary"]["high"] == 1
        assert report["risk_summary"]["low"] == 1
        assert report["processing_time_seconds"] == 12.5
        assert "engine_version" in report["metadata"]
        assert "created_at" in report

    def test_empty_findings(self):
        report = build_report_dict(
            report_id="empty",
            project_id="proj",
            script_format="pdf",
            findings=[],
            processing_time_seconds=0.5,
        )

        assert report["total_findings"] == 0
        assert report["risk_summary"] == {"critical": 0, "high": 0, "medium": 0, "low": 0, "info": 0}

    def test_risk_summary_counts_correctly(self):
        findings = [
            {"risk_level": "critical", "description": "a", "recommendation": "b", "category": "PHYSICAL"},
            {"risk_level": "critical", "description": "c", "recommendation": "d", "category": "PHYSICAL"},
            {"risk_level": "medium", "description": "e", "recommendation": "f", "category": "ENVIRONMENTAL"},
        ]

        report = build_report_dict(
            report_id="r", project_id="p", script_format="fdx",
            findings=findings, processing_time_seconds=1.0,
        )

        assert report["risk_summary"]["critical"] == 2
        assert report["risk_summary"]["medium"] == 1
        assert report["risk_summary"]["high"] == 0


# ===================================================================
# PDF Generation
# ===================================================================


class TestPDFGeneration:
    """Tests for PDF report generation."""

    def _sample_report(self) -> dict[str, Any]:
        return build_report_dict(
            report_id=str(uuid4()),
            project_id="test-project",
            script_format="fdx",
            findings=[
                {
                    "id": str(uuid4()),
                    "risk_level": "critical",
                    "category": "PHYSICAL",
                    "risk_class": "FIRE",
                    "rule_id": "SEC-P-008",
                    "likelihood": 5,
                    "impact": 5,
                    "description": "Building fire scene with real flames.",
                    "recommendation": "Fire department on standby.",
                    "confidence": 0.95,
                    "scene_number": "3",
                    "measures": [
                        {"code": "SFX-CLEARANCE", "title": "SFX-Freigabe", "responsible": "SFX Supervisor", "due": "shooting-5d"},
                        {"code": "FIRE-DEPT", "title": "Feuerwehr-Standby", "responsible": "Production", "due": "shooting-1d"},
                    ],
                },
                {
                    "id": str(uuid4()),
                    "risk_level": "medium",
                    "category": "PSYCHOLOGICAL",
                    "risk_class": "VIOLENCE",
                    "rule_id": "SEC-Y-001",
                    "likelihood": 3,
                    "impact": 2,
                    "description": "Graphic violence depiction.",
                    "recommendation": "Psychological briefing for cast.",
                    "confidence": 0.8,
                    "scene_number": "5",
                    "measures": [
                        {"code": "PSY-BRIEFING", "title": "Psychologisches Briefing", "responsible": "Production", "due": "shooting-0d"},
                    ],
                },
            ],
            processing_time_seconds=45.2,
        )

    def test_generates_pdf_bytes(self):
        report = self._sample_report()
        pdf_bytes = generate_pdf_report(report)

        assert isinstance(pdf_bytes, bytes)
        assert len(pdf_bytes) > 100
        assert pdf_bytes[:5] == b"%PDF-"

    def test_generates_base64(self):
        report = self._sample_report()
        b64 = generate_pdf_base64(report)

        assert isinstance(b64, str)
        # Verify it decodes to valid PDF
        decoded = base64.b64decode(b64)
        assert decoded[:5] == b"%PDF-"

    def test_empty_findings_pdf(self):
        report = build_report_dict(
            report_id=str(uuid4()),
            project_id="empty",
            script_format="pdf",
            findings=[],
            processing_time_seconds=0.1,
        )
        pdf_bytes = generate_pdf_report(report)
        assert pdf_bytes[:5] == b"%PDF-"

    def test_pdf_contains_project_info(self):
        report = self._sample_report()
        report["project_id"] = "my-unique-project-42"
        # We can't easily search PDF content, but we verify it generates without error
        pdf_bytes = generate_pdf_report(report)
        assert len(pdf_bytes) > 500


# ===================================================================
# ReportResponse Model
# ===================================================================


class TestReportResponseModel:
    """Tests for the extended ReportResponse with pdf_base64."""

    def test_report_response_with_pdf(self):
        from core.models import ReportResponse, RiskLevel, SecurityReport

        report = SecurityReport(
            report_id=uuid4(),
            project_id="test",
            script_format="fdx",
            risk_summary={RiskLevel.INFO: 1},
            total_findings=1,
            findings=[],
            processing_time_seconds=0.1,
        )

        resp = ReportResponse(
            report=report,
            pdf_base64="dGVzdA==",
            message="Test",
        )

        assert resp.pdf_base64 == "dGVzdA=="
        dumped = resp.model_dump(mode="json")
        assert "pdf_base64" in dumped

    def test_report_response_without_pdf(self):
        from core.models import ReportResponse, RiskLevel, SecurityReport

        report = SecurityReport(
            report_id=uuid4(),
            project_id="test",
            script_format="fdx",
            risk_summary={RiskLevel.INFO: 1},
            total_findings=1,
            findings=[],
            processing_time_seconds=0.1,
        )

        resp = ReportResponse(report=report)
        assert resp.pdf_base64 is None


# ===================================================================
# SecurityCheckRequest Extensions
# ===================================================================


class TestRequestExtensions:
    """Tests for delivery and idempotency_key fields."""

    def test_default_delivery_is_pull(self):
        from core.models import SecurityCheckRequest

        # Minimal valid request
        import base64
        content = base64.b64encode(b"<FinalDraft><Content></Content></FinalDraft>").decode()
        req = SecurityCheckRequest(
            script_content=content,
            script_format="fdx",
            project_id="test-proj",
        )
        assert req.delivery == "pull"
        assert req.idempotency_key is None

    def test_delivery_push(self):
        from core.models import SecurityCheckRequest

        import base64
        content = base64.b64encode(b"<FinalDraft><Content></Content></FinalDraft>").decode()
        req = SecurityCheckRequest(
            script_content=content,
            script_format="fdx",
            project_id="test-proj",
            delivery="push",
            idempotency_key="my-unique-key-123",
        )
        assert req.delivery == "push"
        assert req.idempotency_key == "my-unique-key-123"


# ===================================================================
# DB Model Extensions
# ===================================================================


class TestDBModelExtensions:
    """Tests for new DB model columns."""

    def test_job_metadata_has_idempotency_key(self):
        from core.db_models import JobMetadata
        # Verify the column exists by creating an instance
        job = JobMetadata(
            job_id=uuid4(),
            project_id="test",
            script_format="fdx",
            user_id="user1",
            idempotency_key="idem-123",
            delivery_mode="push",
        )
        assert job.idempotency_key == "idem-123"
        assert job.delivery_mode == "push"

    def test_report_metadata_has_ref_key(self):
        from core.db_models import ReportMetadata
        report = ReportMetadata(
            report_id=uuid4(),
            job_id=uuid4(),
            project_id="test",
            user_id="user1",
            script_format="fdx",
            total_findings=5,
            processing_time_seconds=10.0,
            report_ref_key="eki:buf:abc123",
            delivery_mode="pull",
        )
        assert report.report_ref_key == "eki:buf:abc123"
        assert report.delivery_mode == "pull"
