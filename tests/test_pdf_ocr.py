"""OCR fallback for image-only PDF pages (Pflichtenheft §4.1)."""

from __future__ import annotations

from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from parsers import pdf_ocr
from parsers.pdf import PDFParser, extract_pdf_text
from parsers.pdf_ocr import OcrConfig, OcrRunner, PdfExtraction, tesseract_available

FIXTURES = Path(__file__).parent / "fixtures" / "pdf"
SCANNED = FIXTURES / "scanned_screenplay.pdf"
BLANK_MIDDLE = FIXTURES / "blank_middle_page.pdf"

needs_tesseract = pytest.mark.skipif(
    not tesseract_available(), reason="tesseract binary / pytesseract not installed"
)


class TestPdfExtraction:
    def test_legacy_tuple_unpacking_still_works(self):
        ex = PdfExtraction(
            full_text="a\nb",
            page_texts=["a", "b"],
            ocr_pages_done=[2],
            ocr_pages_skipped=[3],
            warnings=["w"],
        )
        full_text, pages, ocr_pages, warnings = ex
        assert full_text == "a\nb"
        assert pages == ["a", "b"]
        assert ocr_pages == [2, 3]
        assert warnings == ["w"]


class TestOcrRunner:
    def test_disabled_skips_pages_without_calling_tesseract(self):
        runner = OcrRunner(OcrConfig(enabled=False))
        with patch.object(pdf_ocr, "ocr_page") as ocr_page:
            assert runner.process(MagicMock(), 3) == ""
        ocr_page.assert_not_called()
        assert runner.pages_skipped == [3]
        assert runner.warnings == []

    def test_unavailable_binary_adds_single_warning(self):
        with patch.object(pdf_ocr, "tesseract_available", return_value=False):
            runner = OcrRunner(OcrConfig(enabled=True))
        assert runner.available is False
        assert any("not available" in w for w in runner.warnings)
        assert runner.process(MagicMock(), 1) == ""
        assert runner.pages_skipped == [1]

    def test_page_cap_is_enforced(self):
        with (
            patch.object(pdf_ocr, "tesseract_available", return_value=True),
            patch.object(pdf_ocr, "ocr_page", return_value="INT. SOMEWHERE - DAY text"),
        ):
            runner = OcrRunner(OcrConfig(enabled=True, max_pages=2))
            assert runner.process(MagicMock(), 1)
            assert runner.process(MagicMock(), 2)
            assert runner.process(MagicMock(), 3) == ""
        assert runner.pages_done == [1, 2]
        assert runner.pages_skipped == [3]
        assert any("OCR_MAX_PAGES" in w for w in runner.warnings)

    def test_ocr_exception_marks_page_skipped(self):
        with (
            patch.object(pdf_ocr, "tesseract_available", return_value=True),
            patch.object(pdf_ocr, "ocr_page", side_effect=RuntimeError("tesseract timeout")),
        ):
            runner = OcrRunner(OcrConfig(enabled=True))
            assert runner.process(MagicMock(), 5) == ""
        assert runner.pages_skipped == [5]
        assert runner.pages_done == []

    def test_too_little_recognised_text_counts_as_skipped(self):
        with (
            patch.object(pdf_ocr, "tesseract_available", return_value=True),
            patch.object(pdf_ocr, "ocr_page", return_value="..."),
        ):
            runner = OcrRunner(OcrConfig(enabled=True))
            assert runner.process(MagicMock(), 1) == ""
        assert runner.pages_skipped == [1]

    def test_config_from_settings_falls_back_to_defaults(self):
        cfg = OcrConfig.from_settings()
        assert cfg.languages and cfg.dpi > 0 and cfg.max_pages > 0


class TestPageAlignment:
    def test_blank_page_keeps_index_alignment_without_ocr(self):
        ex = extract_pdf_text(BLANK_MIDDLE.read_bytes(), ocr_config=OcrConfig(enabled=False))
        assert len(ex.page_texts) == 3
        assert ex.page_texts[1] == ""
        assert "BUERO" in ex.page_texts[0]
        assert "STRASSE" in ex.page_texts[2]
        assert ex.ocr_pages_skipped == [2]
        assert ex.ocr_pages_done == []

    def test_disabled_ocr_on_scanned_page_reports_skipped(self):
        ex = extract_pdf_text(SCANNED.read_bytes(), ocr_config=OcrConfig(enabled=False))
        assert ex.ocr_pages_skipped == [1]
        assert ex.page_texts[0] == ""
        assert "WOHNZIMMER" in ex.page_texts[1]


@needs_tesseract
@pytest.mark.ocr
class TestOcrIntegration:
    def test_scanned_page_text_is_recovered_in_place(self):
        ex = extract_pdf_text(SCANNED.read_bytes())
        assert ex.ocr_pages_done == [1]
        assert ex.ocr_pages_skipped == []
        assert len(ex.page_texts) == 2
        assert "INT. KUECHE" in ex.page_texts[0].upper()
        assert "EXT. HOF" in ex.page_texts[0].upper()
        assert "WOHNZIMMER" in ex.page_texts[1]
        assert "KUECHE" in ex.full_text.upper() and "WOHNZIMMER" in ex.full_text

    @pytest.mark.asyncio
    async def test_parser_marks_ocr_scenes_with_reduced_confidence(self):
        from unittest.mock import AsyncMock

        llm = MagicMock()
        llm.generate_structured = AsyncMock(
            return_value={
                "location": "KUECHE",
                "location_type": "INT",
                "time_of_day": "DAY",
                "characters": ["ANNA"],
                "action_text": "x",
                "dialogue": [],
            }
        )
        llm.generate = AsyncMock(return_value="Titel")

        parser = PDFParser(llm_provider=llm)
        script = await parser.parse(SCANNED.read_bytes())

        assert script.metadata["ocr_pages_done"] == [1]
        assert any("recovered via OCR" in w for w in script.warnings)
        assert script.total_scenes >= 3
        # Scenes originating from the OCR'd page are discounted; the text-layer scene is not.
        confidences = sorted(s.parse_confidence for s in script.scenes)
        assert confidences[0] == pytest.approx(0.7)
        assert confidences[-1] == 1.0
        assert sum(1 for c in confidences if c == pytest.approx(0.7)) == 2
