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

from core.version import __version__

logger = logging.getLogger(__name__)

# Severity colors for the PDF
_SEVERITY_COLORS = {
    "critical": colors.Color(0.8, 0, 0),  # dark red
    "high": colors.Color(0.9, 0.3, 0),  # orange-red
    "medium": colors.Color(0.9, 0.6, 0),  # orange
    "low": colors.Color(0.2, 0.6, 0.2),  # green
    "info": colors.Color(0.4, 0.4, 0.4),  # grey
}


# Text before the first scene heading is never risk-checked (it only yields the
# title). Up to this size it is taken for a title page and not mentioned.
PREAMBLE_NOTE_MIN_CHARS = 1500


def _page_list(pages: list[int]) -> str:
    """Compact page list: [3, 7, 8, 9, 12] -> "3, 7–9, 12"."""
    ordered = sorted({int(p) for p in pages})
    runs: list[list[int]] = []
    for page in ordered:
        if runs and page == runs[-1][1] + 1:
            runs[-1][1] = page
        else:
            runs.append([page, page])
    return ", ".join(str(a) if a == b else f"{a}–{b}" for a, b in runs)


def build_coverage_notes(parsed_metadata: dict[str, Any]) -> tuple[list[str], bool]:
    """German, content-free notes on what the analysis did not cover.

    Returns ``(notes, analysis_complete)``. Only page numbers and counts are
    used, never script text. ``analysis_complete`` is False when part of the
    script was not risk-checked; a purely informative note keeps it True.
    """
    notes: list[str] = []
    complete = True

    total = parsed_metadata.get("pages_total")
    processed = parsed_metadata.get("pages_processed")
    if isinstance(total, int) and isinstance(processed, int) and total > processed:
        notes.append(
            f"Nur die ersten {processed} von {total} Seiten wurden verarbeitet "
            f"(Grenze MAX_PDF_PAGES); die Seiten {processed + 1}–{total} sind nicht geprüft."
        )
        complete = False

    skipped = [p for p in parsed_metadata.get("ocr_pages_skipped") or [] if isinstance(p, int)]
    if skipped:
        notes.append(
            f"{len(skipped)} Seite(n) ohne lesbaren Text wurden nicht geprüft (leer, Scan ohne "
            f"Texterkennung oder Texterkennung ohne Ergebnis): Seite {_page_list(skipped)}."
        )
        complete = False

    if parsed_metadata.get("used_page_fallback"):
        notes.append(
            "Keine Szenenüberschriften (INT./EXT., INNEN/AUSSEN) erkannt: Die eKI hat "
            "seitenweise geprüft; Seite 1 galt als Titelseite und wurde nicht geprüft."
        )
        complete = False
    else:
        preamble = parsed_metadata.get("preamble_chars")
        if isinstance(preamble, int) and preamble >= PREAMBLE_NOTE_MIN_CHARS:
            notes.append(
                f"Vor der ersten Szenenüberschrift stehen rund {preamble} Zeichen Text; die eKI "
                "wertet sie als Titelseite und hat sie nicht auf Risiken geprüft."
            )
            complete = False

    numbered = parsed_metadata.get("scenes_numbered_by_position")
    if isinstance(numbered, int) and numbered > 0:
        notes.append(
            f"Die FDX-Datei enthält für {numbered} Szene(n) keine Szenennummer; die eKI hat "
            "sie nach ihrer Reihenfolge nummeriert."
        )

    return notes, complete


def build_report_dict(
    *,
    report_id: str,
    project_id: str,
    script_format: str,
    findings: list[dict[str, Any]],
    processing_time_seconds: float,
    coverage_notes: list[str] | None = None,
    analysis_complete: bool = True,
) -> dict[str, Any]:
    """Build a complete report dictionary from aggregated findings.

    Calculates risk_summary from findings and adds timestamps. Coverage notes
    (``build_coverage_notes``) go into ``metadata`` together with
    ``analysis_complete``.
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
            "engine_version": __version__,
            "taxonomy_version": "1.0",
            "analysis_complete": analysis_complete,
            "coverage_notes": list(coverage_notes or []),
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
        "ReportTitle",
        parent=styles["Title"],
        fontSize=18,
        spaceAfter=6 * mm,
    )
    h2_style = ParagraphStyle(
        "H2",
        parent=styles["Heading2"],
        fontSize=14,
        spaceBefore=8 * mm,
        spaceAfter=4 * mm,
    )
    body_style = styles["BodyText"]
    small_style = ParagraphStyle(
        "Small",
        parent=body_style,
        fontSize=8,
        textColor=colors.grey,
    )

    elements: list[Any] = []

    # --- Title ---
    elements.append(Paragraph("eKI Sicherheitsbericht", title_style))
    elements.append(
        Paragraph(
            f"Projekt: {_escape_html(report.get('project_id', 'N/A'))} | "
            f"Format: {_escape_html(str(report.get('script_format', 'N/A')).upper())} | "
            f"Erstellt: {_escape_html(str(report.get('created_at', ''))[:19])}",
            body_style,
        )
    )
    elements.append(Spacer(1, 6 * mm))

    # --- Executive Summary ---
    elements.append(Paragraph("Executive Summary", h2_style))

    risk_summary = report.get("risk_summary", {})
    total = report.get("total_findings", 0)
    elements.append(
        Paragraph(
            f"<b>{_escape_html(total)} Sicherheitsrisiken</b> identifiziert in der Analyse. "
            f"Davon <b>{_escape_html(risk_summary.get('critical', 0))} kritisch</b>, "
            f"<b>{_escape_html(risk_summary.get('high', 0))} hoch</b>, "
            f"{_escape_html(risk_summary.get('medium', 0))} mittel, "
            f"{_escape_html(risk_summary.get('low', 0))} niedrig, "
            f"{_escape_html(risk_summary.get('info', 0))} informativ.",
            body_style,
        )
    )
    elements.append(Spacer(1, 4 * mm))

    # Risk summary table
    summary_data = [["Severity", "Anzahl"]]
    for level in ("critical", "high", "medium", "low", "info"):
        summary_data.append([level.upper(), str(risk_summary.get(level, 0))])

    summary_table = Table(summary_data, colWidths=[4 * cm, 3 * cm])
    summary_table.setStyle(
        TableStyle(
            [
                ("BACKGROUND", (0, 0), (-1, 0), colors.Color(0.2, 0.2, 0.3)),
                ("TEXTCOLOR", (0, 0), (-1, 0), colors.white),
                ("FONTSIZE", (0, 0), (-1, -1), 10),
                ("ALIGN", (1, 0), (1, -1), "CENTER"),
                ("GRID", (0, 0), (-1, -1), 0.5, colors.grey),
                (
                    "ROWBACKGROUNDS",
                    (0, 1),
                    (-1, -1),
                    [colors.Color(0.95, 0.95, 0.95), colors.white],
                ),
            ]
        )
    )
    elements.append(summary_table)
    elements.append(Spacer(1, 8 * mm))

    # --- Coverage notes (what was not analysed) ---
    meta = report.get("metadata", {}) or {}
    coverage_notes = meta.get("coverage_notes") or []
    if coverage_notes:
        elements.append(Paragraph("Hinweise zur Vollständigkeit", h2_style))
        if meta.get("analysis_complete") is False:
            elements.append(
                Paragraph(
                    "<b>Die Analyse ist unvollständig.</b> Die folgenden Teile des Drehbuchs "
                    "wurden nicht auf Risiken geprüft und müssen manuell bewertet werden.",
                    body_style,
                )
            )
        for note in coverage_notes:
            elements.append(Paragraph(f"• {_escape_html(note)}", body_style))
        elements.append(Spacer(1, 6 * mm))

    # --- Findings by Scene ---
    elements.append(Paragraph("Befunde nach Szenen", h2_style))

    findings = report.get("findings", [])
    by_scene: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for f in findings:
        scene = f.get("scene_number") or "?"
        by_scene[scene].append(f)

    for scene_num in sorted(by_scene.keys(), key=_scene_sort_key):
        scene_findings = by_scene[scene_num]
        elements.append(
            Paragraph(
                f"<b>Szene {_escape_html(scene_num)}</b> ({len(scene_findings)} Befunde)",
                body_style,
            )
        )

        # Every interpolated value is escaped: scene numbers come verbatim from
        # the uploaded FDX, class/rule ids from the LLM, project ids from the
        # client -- unescaped '<' made reportlab raise and the PDF was dropped.
        for f in scene_findings:
            severity = _escape_html(str(f.get("risk_level") or "info").upper())
            risk_class = _escape_html(f.get("risk_class", ""))
            rule_id = _escape_html(f.get("rule_id", ""))
            desc = _escape_html(f.get("description", ""))
            rec = _escape_html(f.get("recommendation", ""))
            evidence = _escape_html(f.get("evidence", ""))
            likelihood = _escape_html(f.get("likelihood", 0))
            impact = _escape_html(f.get("impact", 0))

            finding_text = (
                f"<b>[{severity}]</b> {risk_class} ({rule_id}) | "
                f"L:{likelihood} x I:{impact}<br/>"
                f"{desc}<br/>"
            )
            if evidence:
                finding_text += f'<i>Evidenz: "{evidence}"</i><br/>'
            finding_text += f"<i>Empfehlung: {rec}</i>"
            elements.append(Paragraph(finding_text, body_style))

            # Measures
            measures = f.get("measures", [])
            if measures:
                for m in measures:
                    m_text = (
                        f"  &#8594; {_escape_html(m.get('code', ''))}: "
                        f"{_escape_html(m.get('title', ''))} "
                        f"({_escape_html(m.get('responsible', ''))}, {_escape_html(m.get('due', ''))})"
                    )
                    elements.append(Paragraph(m_text, small_style))

            elements.append(Spacer(1, 2 * mm))

        elements.append(Spacer(1, 4 * mm))

    # --- To-Do List ---
    elements.append(Paragraph("Massnahmen-Checkliste", h2_style))

    all_measures: dict[str, dict[str, Any]] = {}
    for f in findings:
        for m in f.get("measures", []):
            code = m.get("code", "")
            if code and code not in all_measures:
                all_measures[code] = m

    if all_measures:
        todo_data = [["Code", "Massnahme", "Verantwortlich", "Frist"]]
        for code, m in sorted(all_measures.items()):
            todo_data.append(
                [
                    code,
                    m.get("title", ""),
                    m.get("responsible", ""),
                    m.get("due", ""),
                ]
            )

        todo_table = Table(todo_data, colWidths=[3.5 * cm, 7 * cm, 3.5 * cm, 2.5 * cm])
        todo_table.setStyle(
            TableStyle(
                [
                    ("BACKGROUND", (0, 0), (-1, 0), colors.Color(0.2, 0.2, 0.3)),
                    ("TEXTCOLOR", (0, 0), (-1, 0), colors.white),
                    ("FONTSIZE", (0, 0), (-1, -1), 9),
                    ("GRID", (0, 0), (-1, -1), 0.5, colors.grey),
                    (
                        "ROWBACKGROUNDS",
                        (0, 1),
                        (-1, -1),
                        [colors.Color(0.95, 0.95, 0.95), colors.white],
                    ),
                    ("VALIGN", (0, 0), (-1, -1), "TOP"),
                ]
            )
        )
        elements.append(todo_table)
    else:
        elements.append(Paragraph("Keine Massnahmen erforderlich.", body_style))

    # --- Footer ---
    elements.append(Spacer(1, 10 * mm))
    elements.append(
        Paragraph(
            f"Generiert von eKI API v"
            f"{_escape_html(report.get('metadata', {}).get('engine_version', __version__))} | "
            f"Taxonomie v{_escape_html(report.get('metadata', {}).get('taxonomy_version', '1.0'))} | "
            f"Verarbeitungszeit: {report.get('processing_time_seconds', 0):.1f}s",
            small_style,
        )
    )

    doc.build(elements)
    return buf.getvalue()


def generate_pdf_base64(report: dict[str, Any]) -> str:
    """Generate a PDF report and return it as a base64-encoded string."""
    pdf_bytes = generate_pdf_report(report)
    return base64.b64encode(pdf_bytes).decode("ascii")


def _escape_html(text: Any) -> str:
    """Escape markup characters for reportlab Paragraphs (non-str values via ``str()``)."""
    if text is None:
        return ""
    return str(text).replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


def _scene_sort_key(scene: Any) -> tuple[int, int]:
    """Numeric scene numbers first (ascending), then all others in first-seen order.

    ``str.isdigit()`` also accepts '²' or '①', which ``int()`` rejects; scene
    numbers come verbatim from the uploaded FDX, so use ``isdecimal()``.
    """
    text = str(scene)
    if text.isdecimal():
        return (0, int(text))
    return (1, 0)


# ---------------------------------------------------------------------------
# ePro push helpers
# ---------------------------------------------------------------------------

_LEVEL_LABELS = {
    "critical": "CRITICAL",
    "high": "HIGH",
    "medium": "MEDIUM",
    "low": "LOW",
    "info": "INFO",
}


def compute_epro_status(report: dict[str, Any]) -> int:
    """Derive ePro risk-assessment status code from report findings.

    Returns 0 (no significant risk) or 1 (high/critical risks found).
    The mapping may be extended once ePro supports additional codes.
    """
    for finding in report.get("findings", []):
        if finding.get("risk_level") in ("critical", "high"):
            return 1
    return 0


def generate_assessment_text(report: dict[str, Any]) -> str:
    """Build a human-readable plain-text risk assessment for the ePro API.

    Returned text is sent as the ``assessment`` multipart field to
    ``POST /api/eki/scl/set-risk-assessment/{project_id}``.
    """
    lines: list[str] = []
    risk_summary = report.get("risk_summary", {})
    total = report.get("total_findings", 0)

    lines.append(f"eKI Safety Risk Assessment — Project {report.get('project_id', 'N/A')}")
    lines.append("=" * 60)
    lines.append("")

    summary_parts = []
    for level in ("critical", "high", "medium", "low", "info"):
        count = risk_summary.get(level, 0)
        if count:
            summary_parts.append(f"{count} {_LEVEL_LABELS.get(level, level)}")
    lines.append(
        f"Total findings: {total} ({', '.join(summary_parts) if summary_parts else 'none'})"
    )
    lines.append("")

    meta = report.get("metadata", {}) or {}
    coverage_notes = meta.get("coverage_notes") or []
    if meta.get("analysis_complete") is False:
        lines.append(
            "WARNING: Analysis incomplete - parts of the script were not analysed "
            "(see 'Hinweise zur Vollständigkeit' in the PDF report):"
        )
    elif coverage_notes:
        lines.append("Notes:")
    for note in coverage_notes:
        lines.append(f"  - {note}")
    if coverage_notes:
        lines.append("")

    if total == 0:
        lines.append("No safety risks identified in the analyzed script.")
        return "\n".join(lines)

    findings = report.get("findings", [])
    by_scene: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for f in findings:
        scene = f.get("scene_number") or "?"
        by_scene[scene].append(f)

    for scene_num in sorted(by_scene, key=_scene_sort_key):
        scene_findings = by_scene[scene_num]
        lines.append(f"--- Scene {scene_num} ({len(scene_findings)} finding(s)) ---")
        for f in scene_findings:
            level = _LEVEL_LABELS.get(f.get("risk_level", "info"), "INFO")
            risk_class = f.get("risk_class", "")
            rule_id = f.get("rule_id", "")
            desc = f.get("description", "")
            rec = f.get("recommendation", "")

            header = f"[{level}] {risk_class}"
            if rule_id:
                header += f" ({rule_id})"
            lines.append(header)
            if desc:
                lines.append(f"  {desc}")
            if rec:
                lines.append(f"  -> Recommendation: {rec}")

            measures = f.get("measures", [])
            for m in measures:
                lines.append(
                    f"  * {m.get('code', '')}: {m.get('title', '')} "
                    f"[{m.get('responsible', '')}, {m.get('due', '')}]"
                )
            lines.append("")
        lines.append("")

    meta = report.get("metadata", {})
    lines.append(
        f"Generated by eKI v{meta.get('engine_version', __version__)} | "
        f"Taxonomy v{meta.get('taxonomy_version', '1.0')} | "
        f"Processing time: {report.get('processing_time_seconds', 0):.1f}s"
    )
    return "\n".join(lines)
