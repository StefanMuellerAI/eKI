"""Coverage notes in the report and German heading spellings (ß)."""

from __future__ import annotations

import pytest

from core.models import LocationType
from parsers.pdf_scene_splitter import split_into_scenes
from parsers.scene_heading import parse_scene_heading
from services.report_generator import (
    PREAMBLE_NOTE_MIN_CHARS,
    _page_list,
    build_coverage_notes,
    build_report_dict,
)


class TestBuildCoverageNotes:
    def test_nothing_missing(self):
        assert build_coverage_notes({}) == ([], True)
        assert build_coverage_notes({"pages_total": 3, "pages_processed": 3}) == ([], True)

    def test_truncated_pdf(self):
        notes, complete = build_coverage_notes({"pages_total": 501, "pages_processed": 500})
        assert complete is False
        assert notes == [
            "Nur die ersten 500 von 501 Seiten wurden verarbeitet (Grenze MAX_PDF_PAGES); "
            "die Seiten 501–501 sind nicht geprüft."
        ]

    def test_page_fallback_mentions_the_unchecked_title_page(self):
        notes, complete = build_coverage_notes({"used_page_fallback": True, "preamble_chars": 9999})
        assert complete is False
        assert len(notes) == 1
        assert "Seite 1 galt als Titelseite" in notes[0]

    @pytest.mark.parametrize(
        ("chars", "noted"),
        [(0, False), (PREAMBLE_NOTE_MIN_CHARS - 1, False), (PREAMBLE_NOTE_MIN_CHARS, True)],
    )
    def test_long_text_before_the_first_heading(self, chars, noted):
        notes, complete = build_coverage_notes({"preamble_chars": chars})
        assert complete is not noted
        assert bool(notes) is noted
        if noted:
            assert f"rund {chars} Zeichen" in notes[0]

    def test_numbering_by_position_is_informative_only(self):
        notes, complete = build_coverage_notes({"scenes_numbered_by_position": 4})
        assert complete is True
        assert notes == [
            "Die FDX-Datei enthält für 4 Szene(n) keine Szenennummer; die eKI hat sie nach "
            "ihrer Reihenfolge nummeriert."
        ]

    def test_ignores_malformed_values(self):
        assert build_coverage_notes(
            {"pages_total": "620", "ocr_pages_skipped": ["x"], "preamble_chars": None}
        ) == ([], True)

    @pytest.mark.parametrize(
        ("pages", "expected"),
        [([], ""), ([5], "5"), ([3, 7, 8, 9, 12], "3, 7–9, 12"), ([2, 1, 2], "1–2")],
    )
    def test_page_list(self, pages, expected):
        assert _page_list(pages) == expected

    def test_report_metadata_carries_the_notes(self):
        report = build_report_dict(
            report_id="r",
            project_id="p",
            script_format="pdf",
            findings=[],
            processing_time_seconds=1.0,
            coverage_notes=["x"],
            analysis_complete=False,
        )
        assert report["metadata"]["coverage_notes"] == ["x"]
        assert report["metadata"]["analysis_complete"] is False


class TestEszettHeadings:
    @pytest.mark.parametrize(
        "heading",
        ["AUßEN. WALD - TAG", "Außen. Wald - Tag", "AUẞEN. WALD - TAG", "12 AUßEN. WALD - TAG"],
    )
    def test_pdf_splitter_recognises_aussen_with_eszett(self, heading):
        blocks = split_into_scenes(f"Titel\n\n{heading}\nText.\n\nINNEN. HAUS - NACHT\nMehr.")
        assert [b.heading_line for b in blocks if not b.is_preamble] == [
            heading,
            "INNEN. HAUS - NACHT",
        ]

    @pytest.mark.parametrize(
        ("heading", "loc_type", "location"),
        [
            ("AUßEN. WALD - TAG", LocationType.EXT, "WALD"),
            ("Außen. Straße - Nacht", LocationType.EXT, "Straße"),
            ("AUẞEN. HOF - TAG", LocationType.EXT, "HOF"),
            ("INNEN/AUßEN. AUTO - TAG", LocationType.INT_EXT, "AUTO"),
            ("INNEN - AUßEN GARTEN - TAG", LocationType.INT_EXT, "GARTEN"),
            ("AUßEN/INNEN. AUTO - TAG", LocationType.INT_EXT, "AUTO"),
            ("AUSSEN. WALD - TAG", LocationType.EXT, "WALD"),
        ],
    )
    def test_heading_parser_keeps_the_location_intact(self, heading, loc_type, location):
        parsed = parse_scene_heading(heading)
        assert parsed.location_type == loc_type
        assert parsed.location == location
