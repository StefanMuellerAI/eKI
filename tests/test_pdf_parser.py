"""Tests for PDF parser, scene splitter, LLM structurer, and PromptManager."""

import io
import time
from pathlib import Path
from unittest.mock import AsyncMock

import pytest

from core.exceptions import ParsingException
from core.models import LocationType, ScriptFormat, TimeOfDay
from llm.prompt_manager import PromptManager
from parsers.pdf import PDFParser, extract_pdf_text
from parsers.pdf_llm_structurer import (
    llm_result_to_parsed_scene_fields,
    structure_scene_with_llm,
)
from parsers.pdf_scene_splitter import split_by_pages, split_into_scenes

PDF_FIXTURES = Path(__file__).parent / "fixtures" / "pdf"


def _read_pdf(name: str) -> bytes:
    return (PDF_FIXTURES / name).read_bytes()


# ===================================================================
# Scene Splitter
# ===================================================================


class TestSceneSplitter:
    """Tests for parsers.pdf_scene_splitter.split_into_scenes."""

    def test_simple_split(self):
        text = "Some preamble text\n\nINT. OFFICE - DAY\nAction here.\n\nEXT. PARK - NIGHT\nMore action."
        blocks = split_into_scenes(text)

        assert len(blocks) == 3  # preamble + 2 scenes
        assert blocks[0].is_preamble is True
        assert blocks[0].text == "Some preamble text"
        assert blocks[1].is_preamble is False
        assert blocks[1].heading_line == "INT. OFFICE - DAY"
        assert blocks[2].heading_line == "EXT. PARK - NIGHT"

    def test_no_preamble(self):
        text = "INT. ROOM - DAY\nSomeone enters.\n\nEXT. STREET - NIGHT\nRain falls."
        blocks = split_into_scenes(text)

        assert len(blocks) == 2
        assert blocks[0].is_preamble is False
        assert blocks[0].heading_line == "INT. ROOM - DAY"

    def test_german_markers(self):
        text = "INNEN. BUERO - TAG\nEin Mann.\n\nAUSSEN. WALD - NACHT\nEine Frau."
        blocks = split_into_scenes(text)

        assert len(blocks) == 2
        assert "BUERO" in blocks[0].text
        assert "WALD" in blocks[1].text

    def test_int_ext_combined(self):
        text = "INT./EXT. CAR - DAY\nDriving scene."
        blocks = split_into_scenes(text)

        assert len(blocks) == 1
        assert blocks[0].heading_line == "INT./EXT. CAR - DAY"

    def test_no_markers_returns_preamble(self):
        text = "This is just regular text with no scene markers at all."
        blocks = split_into_scenes(text)

        assert len(blocks) == 1
        assert blocks[0].is_preamble is True

    def test_empty_text(self):
        assert split_into_scenes("") == []
        assert split_into_scenes("   ") == []

    def test_case_insensitive(self):
        text = "int. room - day\nSome action.\n\next. garden - night\nMore action."
        blocks = split_into_scenes(text)

        assert len(blocks) == 2

    def test_block_indices_sequential(self):
        text = "Preamble\n\nINT. A - DAY\nA\n\nEXT. B - NIGHT\nB\n\nINT. C - DAY\nC"
        blocks = split_into_scenes(text)

        for i, block in enumerate(blocks):
            assert block.index == i

    def test_real_pdf_simple(self):
        """Test splitter on real extracted PDF text."""
        full_text, _pages, _ocr, _w = extract_pdf_text(_read_pdf("simple_screenplay.pdf"))
        blocks = split_into_scenes(full_text)

        scene_blocks = [b for b in blocks if not b.is_preamble]
        assert len(scene_blocks) == 2

    def test_real_pdf_multi(self):
        full_text, _pages, _ocr, _w = extract_pdf_text(_read_pdf("multi_scene.pdf"))
        blocks = split_into_scenes(full_text)

        scene_blocks = [b for b in blocks if not b.is_preamble]
        assert len(scene_blocks) == 8

    def test_real_pdf_german(self):
        full_text, _pages, _ocr, _w = extract_pdf_text(_read_pdf("german_screenplay.pdf"))
        blocks = split_into_scenes(full_text)

        scene_blocks = [b for b in blocks if not b.is_preamble]
        assert len(scene_blocks) == 3

    def test_real_pdf_no_structure(self):
        full_text, _pages, _ocr, _w = extract_pdf_text(_read_pdf("no_structure.pdf"))
        blocks = split_into_scenes(full_text)

        # Single-page, no markers -> single preamble block
        assert len(blocks) == 1
        assert blocks[0].is_preamble is True


# ===================================================================
# PDF Text Extraction
# ===================================================================


class TestPDFTextExtraction:
    """Tests for parsers.pdf.extract_pdf_text."""

    def test_simple_extraction(self):
        text, pages, ocr_pages, warnings = extract_pdf_text(_read_pdf("simple_screenplay.pdf"))
        assert len(text) > 50
        assert "LIVING ROOM" in text or "GARDEN" in text
        assert ocr_pages == []
        assert isinstance(pages, list)
        assert len(pages) >= 1

    def test_multi_page(self):
        text, pages, ocr_pages, warnings = extract_pdf_text(_read_pdf("multi_scene.pdf"))
        assert "POLICE STATION" in text
        assert "BEACH" in text
        assert len(pages) >= 1

    def test_returns_page_texts(self):
        """Page texts list preserves per-page boundaries."""
        text, pages, _ocr, _w = extract_pdf_text(_read_pdf("no_structure_multi_page.pdf"))
        assert len(pages) == 4
        assert "MYSTERIOUS TALE" in pages[0]

    def test_oversized_pdf_rejected(self):
        big = b"%" * (10 * 1024 * 1024 + 1)
        with pytest.raises(ParsingException, match="exceeds size limit"):
            extract_pdf_text(big)

    def test_invalid_pdf_raises(self):
        with pytest.raises(ParsingException, match="corrupted or malformed"):
            extract_pdf_text(b"not a pdf file at all")

    def test_corrupted_pdf_raises(self):
        """A file starting with %PDF but containing garbage should raise."""
        corrupt = b"%PDF-1.4 this is not a real pdf structure at all" + b"\x00" * 500
        with pytest.raises(ParsingException):
            extract_pdf_text(corrupt)

    def test_password_protected_pdf_raises(self):
        fixture = PDF_FIXTURES / "password_protected.pdf"
        if not fixture.exists():
            pytest.skip("password_protected.pdf fixture not generated")
        with pytest.raises(ParsingException, match="password-protected"):
            extract_pdf_text(fixture.read_bytes())

    def test_large_benchmark_performance(self):
        content = _read_pdf("large_120_pages.pdf")
        t0 = time.monotonic()
        text, _pages, _ocr, _w = extract_pdf_text(content)
        elapsed = time.monotonic() - t0

        assert len(text) > 1000
        assert elapsed < 10.0, f"PDF extraction took {elapsed:.2f}s, should be < 10s"

    def test_low_text_warning(self):
        """PDFs with very little text should produce a warning."""
        from reportlab.lib.pagesizes import LETTER
        from reportlab.pdfgen import canvas as cv

        buf = io.BytesIO()
        c = cv.Canvas(buf, pagesize=LETTER)
        c.setFont("Courier", 12)
        c.drawString(72, 700, "Hello world short text.")
        c.save()
        buf.seek(0)

        _text, _pages, _ocr, warnings = extract_pdf_text(buf.read())
        assert any("very little" in w for w in warnings)


# ===================================================================
# LLM Structurer
# ===================================================================


class TestLLMStructurer:
    """Tests for parsers.pdf_llm_structurer."""

    def test_llm_result_to_fields_basic(self):
        result = {
            "location": "OFFICE",
            "location_type": "INT",
            "time_of_day": "DAY",
            "characters": ["ANNA", "TOM"],
            "action_text": "They sit at a desk.",
            "dialogue": [
                {"character": "ANNA", "parenthetical": None, "text": "Hello."},
                {"character": "TOM", "parenthetical": "smiling", "text": "Hi there."},
            ],
        }
        fields = llm_result_to_parsed_scene_fields(result)

        assert fields["location"] == "OFFICE"
        assert fields["location_type"] == LocationType.INT
        assert fields["time_of_day"] == TimeOfDay.DAY
        assert fields["characters"] == ["ANNA", "TOM"]
        assert len(fields["dialogue"]) == 2
        assert fields["dialogue"][1].parenthetical == "smiling"

    def test_unknown_enum_values(self):
        result = {
            "location": "SOMEWHERE",
            "location_type": "INVALID",
            "time_of_day": "WEIRD",
            "characters": [],
            "action_text": "",
            "dialogue": [],
        }
        fields = llm_result_to_parsed_scene_fields(result)

        assert fields["location_type"] == LocationType.UNKNOWN
        assert fields["time_of_day"] == TimeOfDay.UNKNOWN

    def test_empty_dialogue_handled(self):
        result = {
            "location": "ROOM",
            "location_type": "INT",
            "time_of_day": "NIGHT",
            "characters": [],
            "action_text": "Dark room.",
            "dialogue": None,
        }
        fields = llm_result_to_parsed_scene_fields(result)
        assert fields["dialogue"] == []

    @pytest.mark.asyncio
    async def test_structure_scene_with_llm_success(self):
        mock_llm = AsyncMock()
        mock_llm.generate_structured = AsyncMock(
            return_value={
                "location": "PARK",
                "location_type": "EXT",
                "time_of_day": "DAY",
                "characters": ["SARAH"],
                "action_text": "Walking in the park.",
                "dialogue": [{"character": "SARAH", "text": "Nice day."}],
            }
        )

        result = await structure_scene_with_llm("EXT. PARK - DAY\nSarah walks.", mock_llm)
        assert result["location"] == "PARK"
        mock_llm.generate_structured.assert_called_once()

    @pytest.mark.asyncio
    async def test_structure_scene_with_llm_failure_returns_fallback(self):
        mock_llm = AsyncMock()
        mock_llm.generate_structured = AsyncMock(side_effect=Exception("LLM down"))

        result = await structure_scene_with_llm("EXT. PARK - DAY\nSarah walks.", mock_llm)
        assert result["location"] == "UNKNOWN"


# ===================================================================
# PromptManager
# ===================================================================


class TestPromptManager:
    """Tests for llm.prompt_manager.PromptManager."""

    def test_load_prompts(self):
        pm = PromptManager()
        assert pm.version == "1.0"
        assert "pdf_structuring" in pm.sections()
        assert "risk_analysis" in pm.sections()

    def test_get_scene_prompt(self):
        pm = PromptManager()
        system, user = pm.get("pdf_structuring", "scene", scene_text="INT. OFFICE - DAY")
        assert "screenplay" in system.lower()
        assert "INT. OFFICE - DAY" in user

    def test_get_risk_prompt(self):
        pm = PromptManager()
        system, user = pm.get(
            "risk_analysis",
            "scene",
            scene_number="1",
            location="HIGHWAY",
            location_type="EXT",
            time_of_day="DAY",
            scene_text="Cars race at high speed.",
            # M06: system prompt now references {taxonomy_context} and
            # {kb_context}; both must be supplied for substitution to succeed.
            taxonomy_context="taxonomy stub",
            kb_context="(none)",
        )
        assert "safety" in system.lower()
        assert "HIGHWAY" in user

    def test_missing_section_raises(self):
        pm = PromptManager()
        with pytest.raises(KeyError, match="Prompt not found"):
            pm.get("nonexistent", "scene", scene_text="test")

    def test_missing_variable_raises(self):
        pm = PromptManager()
        with pytest.raises(KeyError):
            pm.get("pdf_structuring", "scene")  # Missing scene_text

    def test_get_system_only(self):
        pm = PromptManager()
        system = pm.get_system("risk_analysis", "scene")
        assert "film safety" in system.lower()


# ===================================================================
# Parser Factory
# ===================================================================


class TestParserFactory:
    """Tests for parsers.base.get_parser with PDF support."""

    def test_pdf_parser(self):
        from parsers.base import get_parser

        parser = get_parser("pdf")
        assert isinstance(parser, PDFParser)
        assert parser.supported_format == ScriptFormat.PDF

    def test_fdx_parser_still_works(self):
        from parsers.base import get_parser
        from parsers.fdx import FDXParser

        parser = get_parser("fdx")
        assert isinstance(parser, FDXParser)


# ===================================================================
# Integration: Scene Splitter on real PDFs
# ===================================================================


class TestSceneSplitterIntegration:
    """Integration tests combining PDF extraction and scene splitting."""

    def test_large_pdf_scene_count(self):
        """The 120-page benchmark PDF should have ~60 scenes."""
        content = _read_pdf("large_120_pages.pdf")
        text, _pages, _ocr, _w = extract_pdf_text(content)
        blocks = split_into_scenes(text)

        scene_blocks = [b for b in blocks if not b.is_preamble]
        assert 55 <= len(scene_blocks) <= 65

    def test_large_pdf_split_performance(self):
        content = _read_pdf("large_120_pages.pdf")
        text, _pages, _ocr, _w = extract_pdf_text(content)

        t0 = time.monotonic()
        split_into_scenes(text)
        elapsed = time.monotonic() - t0

        assert elapsed < 1.0, f"Splitting took {elapsed:.2f}s, should be < 1s"


# ===================================================================
# Page-Based Fallback Splitting
# ===================================================================


class TestPageFallbackSplitting:
    """Tests for split_by_pages() and the fallback path in split_into_scenes()."""

    def test_split_by_pages_basic(self):
        pages = ["Title page content", "Chapter one text", "Chapter two text"]
        blocks = split_by_pages(pages)

        assert len(blocks) == 3
        assert blocks[0].is_preamble is True
        assert blocks[0].text == "Title page content"
        assert blocks[1].is_preamble is False
        assert blocks[1].heading_line == "PAGE 2"
        assert blocks[1].text == "Chapter one text"
        assert blocks[2].heading_line == "PAGE 3"

    def test_split_by_pages_skips_empty(self):
        pages = ["Title", "", "  ", "Content on page 4"]
        blocks = split_by_pages(pages)

        assert len(blocks) == 2
        assert blocks[0].is_preamble is True
        assert blocks[1].heading_line == "PAGE 4"

    def test_split_by_pages_single_page(self):
        pages = ["Only one page of content"]
        blocks = split_by_pages(pages)

        assert len(blocks) == 1
        assert blocks[0].is_preamble is True

    def test_split_by_pages_indices_sequential(self):
        pages = ["A", "B", "C", "D"]
        blocks = split_by_pages(pages)

        for i, block in enumerate(blocks):
            assert block.index == i

    def test_split_into_scenes_fallback_with_pages(self):
        """No markers + multiple pages -> page-based fallback."""
        full_text = "Page one.\nPage two.\nPage three."
        pages = ["Page one.", "Page two.", "Page three."]

        blocks = split_into_scenes(full_text, page_texts=pages)

        assert len(blocks) == 3
        assert blocks[0].is_preamble is True
        assert blocks[1].heading_line == "PAGE 2"
        assert blocks[2].heading_line == "PAGE 3"

    def test_split_into_scenes_no_fallback_single_page(self):
        """No markers + single page -> preamble only (no fallback)."""
        full_text = "Just some text without markers."
        pages = ["Just some text without markers."]

        blocks = split_into_scenes(full_text, page_texts=pages)

        assert len(blocks) == 1
        assert blocks[0].is_preamble is True

    def test_split_into_scenes_no_fallback_without_pages(self):
        """No markers + no page_texts -> old behavior (single preamble)."""
        full_text = "Some text on multiple conceptual pages."
        blocks = split_into_scenes(full_text)

        assert len(blocks) == 1
        assert blocks[0].is_preamble is True

    def test_markers_take_precedence_over_pages(self):
        """When markers exist, page_texts is ignored."""
        full_text = "Preamble\n\nINT. OFFICE - DAY\nAction.\n\nEXT. PARK - NIGHT\nMore."
        pages = ["Preamble\n\nINT. OFFICE - DAY\nAction.", "EXT. PARK - NIGHT\nMore."]

        blocks = split_into_scenes(full_text, page_texts=pages)

        scene_blocks = [b for b in blocks if not b.is_preamble]
        assert len(scene_blocks) == 2
        assert scene_blocks[0].heading_line == "INT. OFFICE - DAY"

    def test_real_multi_page_no_structure_pdf(self):
        """Integration: multi-page PDF without markers uses page fallback."""
        content = _read_pdf("no_structure_multi_page.pdf")
        full_text, pages, _ocr, _w = extract_pdf_text(content)

        blocks = split_into_scenes(full_text, page_texts=pages)

        preambles = [b for b in blocks if b.is_preamble]
        scenes = [b for b in blocks if not b.is_preamble]

        assert len(preambles) == 1
        assert len(scenes) == 3
        assert all(b.heading_line.startswith("PAGE ") for b in scenes)


# ===================================================================
# Real-world screenplay layouts (synthetic stand-ins for real scripts)
# ===================================================================

REALWORLD_HEADINGS = [
    "1 INT. KÜCHE - TAG",
    "2 EXT. HOF - NACHT",
    "3 INT./EXT. AUTO - FORTLAUFEND",
    "4 INT/EXT. TANKSTELLE - ABEND",
    "5A EXT. LANDSTRASSE - NACHT",
    "5B EXT. LANDSTRASSE - SPÄTER",
    "6. EXT. FELD - TAG",
    "7. INT. SCHEUNE - NACHT",
    "12.1 INT. FLUR - TAG",
    "12.1A. INT. BAD - TAG",
    "13 I/E. AUTO - NACHT",
    "14 E/I. TREPPENHAUS - NACHT",
    "15 INNEN. WOHNUNG - ABEND",
    "16 AUSSEN. BALKON - NACHT",
    "17 INNEN-AUSSEN. GARTEN - TAG",
    "18 INT. CUISINE - JOUR",
]


class _FixtureLLM:
    """Fake provider: answers the preamble prompt with a title, scenes with a location."""

    provider_name = "fake"

    def __init__(self) -> None:
        self.scene_calls = 0

    async def generate_structured(self, prompt, schema, system_prompt=None, **kwargs):
        if "title" in schema.get("properties", {}):
            return {"title": "DAS UNGESCHRIEBENE HAUS"}
        self.scene_calls += 1
        return {
            "location": f"ORT {self.scene_calls}",
            "location_type": "INT",
            "time_of_day": "DAY",
            "characters": [],
            "action_text": "Aktion.",
            "dialogue": [],
        }


class TestRealWorldLayoutFixtures:
    """``realworld_headings.pdf`` / ``suffix_headings.pdf`` rebuild the layouts of
    real German screenplays (numbered headings, page numbers, cue extensions,
    gender colon, French scene) with synthetic text; see tests/generate_pdf_fixtures.py.
    """

    def test_numbered_heading_variants_are_split_from_the_pdf(self):
        extraction = extract_pdf_text(_read_pdf("realworld_headings.pdf"))
        blocks = split_into_scenes(extraction.full_text, page_texts=extraction.page_texts)

        assert len(extraction.page_texts) == 5
        assert extraction.warnings == []
        assert blocks[0].is_preamble is True
        assert blocks[0].text.startswith("DAS UNGESCHRIEBENE HAUS")
        assert "5. Fassung" in blocks[0].text  # a numbered line without INT/EXT is no heading
        assert [b.heading_line for b in blocks[1:]] == REALWORLD_HEADINGS
        assert [b.index for b in blocks] == list(range(len(REALWORLD_HEADINGS) + 1))

    def test_page_numbers_stay_inside_the_running_scene(self):
        extraction = extract_pdf_text(_read_pdf("realworld_headings.pdf"))
        blocks = split_into_scenes(extraction.full_text, page_texts=extraction.page_texts)
        by_heading = {b.heading_line: b.text for b in blocks}

        # Each page starts with its page number ("2.", "3.", "4."); it ends the
        # previous page's last scene instead of opening a block of its own.
        assert by_heading["4 INT/EXT. TANKSTELLE - ABEND"].endswith("\n2.")
        assert by_heading["7. INT. SCHEUNE - NACHT"].endswith("\n3.")
        assert by_heading["14 E/I. TREPPENHAUS - NACHT"].endswith("\n4.")

    def test_cue_extensions_gender_colon_and_accents_survive_extraction(self):
        extraction = extract_pdf_text(_read_pdf("realworld_headings.pdf"))
        blocks = split_into_scenes(extraction.full_text, page_texts=extraction.page_texts)
        by_heading = {b.heading_line: b.text for b in blocks}

        assert "JONAS (O.S.)" in by_heading["1 INT. KÜCHE - TAG"]
        assert "JONAS (CONT'D)" in by_heading["5B EXT. LANDSTRASSE - SPÄTER"]
        assert "MARA (INTER)" in by_heading["13 I/E. AUTO - NACHT"]
        wohnung = by_heading["15 INNEN. WOHNUNG - ABEND"]
        assert "User:innen" in wohnung and "Nutzer:innen" in wohnung
        assert "COMPUTERSTIMME (V.O.)\nSystem: Zugriff verweigert." in wohnung
        assert "Élodie entre, un café à la main." in by_heading["18 INT. CUISINE - JOUR"]

    def test_suffix_style_headings_fall_back_to_pages_documented(self):
        # "1 WALDSTRASSE, DÄMMERUNG/AUSSEN": the designation at the END of the
        # heading is not a scene marker, so the document is split per page.
        extraction = extract_pdf_text(_read_pdf("suffix_headings.pdf"))
        blocks = split_into_scenes(extraction.full_text, page_texts=extraction.page_texts)

        assert [b.heading_line for b in blocks] == ["", "PAGE 2", "PAGE 3", "PAGE 4"]
        assert blocks[0].is_preamble is True
        assert blocks[1].text.startswith("1 WALDSTRASSE, DÄMMERUNG/AUSSEN")
        assert blocks[3].text.startswith("3 SEEUFER, NACHT/AUSSEN")

    async def test_pdf_parser_on_realworld_fixture(self):
        llm = _FixtureLLM()
        script = await PDFParser(llm_provider=llm).parse(_read_pdf("realworld_headings.pdf"))

        assert script.title == "DAS UNGESCHRIEBENE HAUS"
        assert [s.heading for s in script.scenes] == REALWORLD_HEADINGS
        assert [s.number for s in script.scenes] == [str(i) for i in range(1, 17)]
        assert {s.parse_method for s in script.scenes} == {"pdf_llm"}
        assert {s.parse_confidence for s in script.scenes} == {1.0}
        assert llm.scene_calls == len(REALWORLD_HEADINGS)
        assert not any("No scene markers" in w for w in script.warnings)

    async def test_pdf_parser_on_suffix_fixture_uses_page_fallback(self):
        llm = _FixtureLLM()
        script = await PDFParser(llm_provider=llm).parse(_read_pdf("suffix_headings.pdf"))

        assert script.total_scenes == 3
        assert [s.heading for s in script.scenes] == ["PAGE 2", "PAGE 3", "PAGE 4"]
        assert {s.parse_method for s in script.scenes} == {"pdf_page_fallback"}
        assert {s.parse_confidence for s in script.scenes} == {0.3}
        assert (
            "No scene markers (INT/EXT) found. Falling back to page-by-page splitting."
            in script.warnings
        )
