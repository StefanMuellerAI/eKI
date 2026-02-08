"""Report generator for JSON and PDF security reports.

Builds a complete SecurityReport from aggregated findings and generates
a human-readable PDF report with Executive Summary, scene findings,
and a To-Do list of safety measures.
"""

import base64
import io
import logging
from collections import defaultdict
from datetime import datetime
from typing import Any
from uuid import UUID

from reportlab.lib import colors
from reportlab.lib.pagesizes import A4
from reportlab.lib.styles import ParagraphStyle, getSampleStyleSheet
from reportlab.lib.units import cm, mm
from reportlab.platypus import (
    Paragraph,
    SimpleDocTemplate,
    Spacer,
    Table,
    TableStyle,
)

logger = logging.getLogger(__name__)

# Severity colors for the PDF
_SEVERITY_COLORS = {
    "critical": colors.Color(0.8, 0, 0),       # dark red
    "high": colors.Color(0.9, 0.3, 0),         # orange-red
    "medium": colors.Color(0.9, 0.6, 0),        # orange
    "low": colors.Color(0.2, 0.6, 0.2),         # green
    "info": colors.Color(0.4, 0.4, 0.4),        # grey
}


def build_report_dict(
    *,
    report_id: str,
    project_id: str,
    script_format: str,
    findings: list[dict[str, Any]],
    processing_time_seconds: float,
) -> dict[str, Any]:
    """Build a complete report dictionary from aggregated findings.

    Calculates risk_summary from findings and adds timestamps.
    """
    risk_summary = {"critical": 0, "high": 0, "medium": 0, "low": 0, "info": 0}
    for f in findings:
        level = f.get("risk_level", "info")
        risk_summary[level] = risk_summary.get(level, 0) + 1

    return {
        "report_id": report_id,
        "project_id": project_id,
        "script_format": script_format,
        "created_at": datetime.utcnow().isoformat(),
        "risk_summary": risk_summary,
        "total_findings": len(findings),
        "findings": findings,
        "processing_time_seconds": processing_time_seconds,
        "metadata": {
            "engine_version": "0.5.0",
            "taxonomy_version": "1.0",
        },
    }


def generate_pdf_report(report: dict[str, Any]) -> bytes:
    """Generate a PDF safety report from a report dictionary.

    Returns raw PDF bytes.
    """
    buf = io.BytesIO()
    doc = SimpleDocTemplate(
        buf,
        pagesize=A4,
        topMargin=2 * cm,
        bottomMargin=2 * cm,
        leftMargin=2 * cm,
        rightMargin=2 * cm,
    )

    styles = getSampleStyleSheet()
    title_style = ParagraphStyle(
        "ReportTitle", parent=styles["Title"], fontSize=18, spaceAfter=6 * mm,
    )
    h2_style = ParagraphStyle(
        "H2", parent=styles["Heading2"], fontSize=14, spaceBefore=8 * mm, spaceAfter=4 * mm,
    )
    body_style = styles["BodyText"]
    small_style = ParagraphStyle(
        "Small", parent=body_style, fontSize=8, textColor=colors.grey,
    )

    elements: list[Any] = []

    # --- Title ---
    elements.append(Paragraph("eKI Sicherheitsbericht", title_style))
    elements.append(Paragraph(
        f"Projekt: {report.get('project_id', 'N/A')} | "
        f"Format: {report.get('script_format', 'N/A').upper()} | "
        f"Erstellt: {report.get('created_at', '')[:19]}",
        body_style,
    ))
    elements.append(Spacer(1, 6 * mm))

    # --- Executive Summary ---
    elements.append(Paragraph("Executive Summary", h2_style))

    risk_summary = report.get("risk_summary", {})
    total = report.get("total_findings", 0)
    elements.append(Paragraph(
        f"<b>{total} Sicherheitsrisiken</b> identifiziert in der Analyse. "
        f"Davon <b>{risk_summary.get('critical', 0)} kritisch</b>, "
        f"<b>{risk_summary.get('high', 0)} hoch</b>, "
        f"{risk_summary.get('medium', 0)} mittel, "
        f"{risk_summary.get('low', 0)} niedrig, "
        f"{risk_summary.get('info', 0)} informativ.",
        body_style,
    ))
    elements.append(Spacer(1, 4 * mm))

    # Risk summary table
    summary_data = [["Severity", "Anzahl"]]
    for level in ("critical", "high", "medium", "low", "info"):
        summary_data.append([level.upper(), str(risk_summary.get(level, 0))])

    summary_table = Table(summary_data, colWidths=[4 * cm, 3 * cm])
    summary_table.setStyle(TableStyle([
        ("BACKGROUND", (0, 0), (-1, 0), colors.Color(0.2, 0.2, 0.3)),
        ("TEXTCOLOR", (0, 0), (-1, 0), colors.white),
        ("FONTSIZE", (0, 0), (-1, -1), 10),
        ("ALIGN", (1, 0), (1, -1), "CENTER"),
        ("GRID", (0, 0), (-1, -1), 0.5, colors.grey),
        ("ROWBACKGROUNDS", (0, 1), (-1, -1), [colors.Color(0.95, 0.95, 0.95), colors.white]),
    ]))
    elements.append(summary_table)
    elements.append(Spacer(1, 8 * mm))

    # --- Findings by Scene ---
    elements.append(Paragraph("Befunde nach Szenen", h2_style))

    findings = report.get("findings", [])
    by_scene: dict[str, list[dict]] = defaultdict(list)
    for f in findings:
        scene = f.get("scene_number") or "?"
        by_scene[scene].append(f)

    for scene_num in sorted(by_scene.keys(), key=lambda x: int(x) if x.isdigit() else 999):
        scene_findings = by_scene[scene_num]
        elements.append(Paragraph(f"<b>Szene {scene_num}</b> ({len(scene_findings)} Befunde)", body_style))

        for f in scene_findings:
            severity = f.get("risk_level", "info").upper()
            risk_class = f.get("risk_class", "")
            rule_id = f.get("rule_id", "")
            desc = _escape_html(f.get("description", ""))
            rec = _escape_html(f.get("recommendation", ""))
            likelihood = f.get("likelihood", 0)
            impact = f.get("impact", 0)

            finding_text = (
                f"<b>[{severity}]</b> {risk_class} ({rule_id}) | "
                f"L:{likelihood} x I:{impact}<br/>"
                f"{desc}<br/>"
                f"<i>Empfehlung: {rec}</i>"
            )
            elements.append(Paragraph(finding_text, body_style))

            # Measures
            measures = f.get("measures", [])
            if measures:
                for m in measures:
                    m_text = f"  &#8594; {m.get('code', '')}: {m.get('title', '')} ({m.get('responsible', '')}, {m.get('due', '')})"
                    elements.append(Paragraph(m_text, small_style))

            elements.append(Spacer(1, 2 * mm))

        elements.append(Spacer(1, 4 * mm))

    # --- To-Do List ---
    elements.append(Paragraph("Massnahmen-Checkliste", h2_style))

    all_measures: dict[str, dict] = {}
    for f in findings:
        for m in f.get("measures", []):
            code = m.get("code", "")
            if code and code not in all_measures:
                all_measures[code] = m

    if all_measures:
        todo_data = [["Code", "Massnahme", "Verantwortlich", "Frist"]]
        for code, m in sorted(all_measures.items()):
            todo_data.append([
                code,
                m.get("title", ""),
                m.get("responsible", ""),
                m.get("due", ""),
            ])

        todo_table = Table(todo_data, colWidths=[3.5 * cm, 7 * cm, 3.5 * cm, 2.5 * cm])
        todo_table.setStyle(TableStyle([
            ("BACKGROUND", (0, 0), (-1, 0), colors.Color(0.2, 0.2, 0.3)),
            ("TEXTCOLOR", (0, 0), (-1, 0), colors.white),
            ("FONTSIZE", (0, 0), (-1, -1), 9),
            ("GRID", (0, 0), (-1, -1), 0.5, colors.grey),
            ("ROWBACKGROUNDS", (0, 1), (-1, -1), [colors.Color(0.95, 0.95, 0.95), colors.white]),
            ("VALIGN", (0, 0), (-1, -1), "TOP"),
        ]))
        elements.append(todo_table)
    else:
        elements.append(Paragraph("Keine Massnahmen erforderlich.", body_style))

    # --- Footer ---
    elements.append(Spacer(1, 10 * mm))
    elements.append(Paragraph(
        f"Generiert von eKI API v{report.get('metadata', {}).get('engine_version', '0.5.0')} | "
        f"Taxonomie v{report.get('metadata', {}).get('taxonomy_version', '1.0')} | "
        f"Verarbeitungszeit: {report.get('processing_time_seconds', 0):.1f}s",
        small_style,
    ))

    doc.build(elements)
    return buf.getvalue()


def generate_pdf_base64(report: dict[str, Any]) -> str:
    """Generate a PDF report and return it as a base64-encoded string."""
    pdf_bytes = generate_pdf_report(report)
    return base64.b64encode(pdf_bytes).decode("ascii")


def _escape_html(text: str) -> str:
    """Escape HTML special characters for reportlab Paragraphs."""
    return (
        text.replace("&", "&amp;")
        .replace("<", "&lt;")
        .replace(">", "&gt;")
    )
