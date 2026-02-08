"""Tests for PDF parser, scene splitter, LLM structurer, and PromptManager."""

import io
import time
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from core.exceptions import ParsingException
from core.models import LocationType, ScriptFormat, TimeOfDay
from llm.prompt_manager import PromptManager
from parsers.pdf import PDFParser, extract_pdf_text
from parsers.pdf_llm_structurer import (
    SCENE_SCHEMA,
    llm_result_to_parsed_scene_fields,
    structure_scene_with_llm,
)
from parsers.pdf_scene_splitter import RawSceneBlock, split_into_scenes

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
        full_text, _ = extract_pdf_text(_read_pdf("simple_screenplay.pdf"))
        blocks = split_into_scenes(full_text)

        scene_blocks = [b for b in blocks if not b.is_preamble]
        assert len(scene_blocks) == 2

    def test_real_pdf_multi(self):
        full_text, _ = extract_pdf_text(_read_pdf("multi_scene.pdf"))
        blocks = split_into_scenes(full_text)

        scene_blocks = [b for b in blocks if not b.is_preamble]
        assert len(scene_blocks) == 8

    def test_real_pdf_german(self):
        full_text, _ = extract_pdf_text(_read_pdf("german_screenplay.pdf"))
        blocks = split_into_scenes(full_text)

        scene_blocks = [b for b in blocks if not b.is_preamble]
        assert len(scene_blocks) == 3

    def test_real_pdf_no_structure(self):
        full_text, _ = extract_pdf_text(_read_pdf("no_structure.pdf"))
        blocks = split_into_scenes(full_text)

        # Should be a single preamble block
        assert len(blocks) == 1
        assert blocks[0].is_preamble is True


# ===================================================================
# PDF Text Extraction
# ===================================================================


class TestPDFTextExtraction:
    """Tests for parsers.pdf.extract_pdf_text."""

    def test_simple_extraction(self):
        text, ocr_pages = extract_pdf_text(_read_pdf("simple_screenplay.pdf"))
        assert len(text) > 50
        assert "LIVING ROOM" in text or "GARDEN" in text
        assert ocr_pages == []

    def test_multi_page(self):
        text, ocr_pages = extract_pdf_text(_read_pdf("multi_scene.pdf"))
        assert "POLICE STATION" in text
        assert "BEACH" in text

    def test_oversized_pdf_rejected(self):
        big = b"%" * (10 * 1024 * 1024 + 1)
        with pytest.raises(ParsingException, match="exceeds size limit"):
            extract_pdf_text(big)

    def test_invalid_pdf_raises(self):
        with pytest.raises(ParsingException, match="text extraction failed"):
            extract_pdf_text(b"not a pdf file at all")

    def test_large_benchmark_performance(self):
        content = _read_pdf("large_120_pages.pdf")
        t0 = time.monotonic()
        text, _ = extract_pdf_text(content)
        elapsed = time.monotonic() - t0

        assert len(text) > 1000
        assert elapsed < 10.0, f"PDF extraction took {elapsed:.2f}s, should be < 10s"


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
        mock_llm.generate_structured = AsyncMock(return_value={
            "location": "PARK",
            "location_type": "EXT",
            "time_of_day": "DAY",
            "characters": ["SARAH"],
            "action_text": "Walking in the park.",
            "dialogue": [{"character": "SARAH", "text": "Nice day."}],
        })

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
            "risk_analysis", "scene",
            scene_number="1",
            location="HIGHWAY",
            location_type="EXT",
            time_of_day="DAY",
            scene_text="Cars race at high speed.",
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
        text, _ = extract_pdf_text(content)
        blocks = split_into_scenes(text)

        scene_blocks = [b for b in blocks if not b.is_preamble]
        # We generated 60 scenes
        assert 55 <= len(scene_blocks) <= 65

    def test_large_pdf_split_performance(self):
        content = _read_pdf("large_120_pages.pdf")
        text, _ = extract_pdf_text(content)

        t0 = time.monotonic()
        blocks = split_into_scenes(text)
        elapsed = time.monotonic() - t0

        assert elapsed < 1.0, f"Splitting took {elapsed:.2f}s, should be < 1s"
