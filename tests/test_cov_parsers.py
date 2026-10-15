"""Edge-case and error-path tests for the screenplay parsers.

Complements ``test_pdf_parser.py``, ``test_pdf_ocr.py`` and
``test_fdx_parser.py`` with the branches those suites do not reach:

* ``parsers/pdf.py``: settings-driven limits, page-cap truncation of large
  documents, every ``extract_pdf_text`` error mapping, the OCR fallback
  wiring (without needing a tesseract binary), and the full
  ``PDFParser.parse`` pipeline (preamble title, page fallback, OCR
  confidence discount, LLM outage, malformed LLM output, lazy provider
  init, character index).
* ``parsers/pdf_llm_structurer.py``: preamble title extraction and enum /
  dialogue normalisation.
* ``parsers/pdf_ocr.py``: settings loading, tesseract detection and the
  in-memory ``ocr_page`` contract (mode conversion, lang/dpi/timeout).
* ``parsers/secure_xml.py``: every exception mapping incl. the defusedxml
  branches that are unreachable with the default ``fromstring`` flags.
* ``parsers/fdx.py``: title page, Shot paragraphs, empty paragraphs, text
  runs and character index details.

No real LLM, network or tesseract binary is needed; all of these are
replaced by fakes so the tests are deterministic.
"""

from __future__ import annotations

import importlib.util
import io
import logging
import os
import sys
from types import ModuleType, SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest
from defusedxml.common import DTDForbidden, ExternalReferenceForbidden
from pdfminer.pdfdocument import PDFPasswordIncorrect
from pdfminer.pdfparser import PDFSyntaxError
from PIL import Image

from core.exceptions import LLMException, ParsingException
from core.models import LocationType, ScriptFormat, TimeOfDay
from parsers import pdf as pdf_module
from parsers import pdf_ocr, secure_xml
from parsers.fdx import FDXParser
from parsers.pdf import PDFParser, _block_touches_pages, _effective_pdf_limits, extract_pdf_text
from parsers.pdf_llm_structurer import (
    PREAMBLE_SCHEMA,
    SCENE_SCHEMA,
    extract_title_from_preamble,
    llm_result_to_parsed_scene_fields,
    structure_scene_with_llm,
)
from parsers.pdf_ocr import OcrConfig, OcrRunner, PdfExtraction, ocr_page, tesseract_available
from parsers.pdf_scene_splitter import RawSceneBlock, split_into_scenes
from parsers.scene_heading import parse_scene_heading
from parsers.secure_xml import parse_xml_safe

# ===================================================================
# Helpers
# ===================================================================


def _make_pdf(page_lines: list[list[str]]) -> bytes:
    """Render a text-layer PDF with one page per entry of *page_lines*."""
    from reportlab.lib.pagesizes import LETTER
    from reportlab.pdfgen import canvas as rl_canvas

    buf = io.BytesIO()
    c = rl_canvas.Canvas(buf, pagesize=LETTER)
    c.setFont("Courier", 12)
    for lines in page_lines:
        y = 720
        for line in lines:
            c.drawString(72, y, line)
            y -= 16
        c.showPage()
    c.save()
    return buf.getvalue()


def _scene_result(location: str = "OFFICE", **overrides: Any) -> dict[str, Any]:
    """A schema-conformant LLM structuring result."""
    result: dict[str, Any] = {
        "location": location,
        "location_type": "INT",
        "time_of_day": "DAY",
        "characters": [],
        "action_text": "Action.",
        "dialogue": [],
    }
    result.update(overrides)
    return result


class _RoutingLLM:
    """Fake LLM provider that answers preamble and scene prompts separately.

    ``scene_results`` are returned in order for scene-structuring calls;
    ``title`` (or ``title_exc``) answers the preamble call.  Every call is
    recorded so tests can inspect schema / prompt / temperature.
    """

    provider_name = "fake"

    def __init__(
        self,
        scene_results: list[Any] | None = None,
        *,
        title: str | None = "THE TITLE",
        scene_exc: Exception | None = None,
        title_exc: Exception | None = None,
    ) -> None:
        self.scene_results = list(scene_results or [])
        self.title = title
        self.scene_exc = scene_exc
        self.title_exc = title_exc
        self.calls: list[dict[str, Any]] = []

    async def generate_structured(
        self,
        prompt: str,
        schema: dict[str, Any],
        system_prompt: str | None = None,
        temperature: float = 0.2,
        **kwargs: Any,
    ) -> Any:
        self.calls.append(
            {
                "prompt": prompt,
                "schema": schema,
                "system_prompt": system_prompt,
                "temperature": temperature,
            }
        )
        if schema is PREAMBLE_SCHEMA:
            if self.title_exc is not None:
                raise self.title_exc
            return {"title": self.title}
        if self.scene_exc is not None:
            raise self.scene_exc
        if self.scene_results:
            return self.scene_results.pop(0)
        return _scene_result()

    @property
    def scene_calls(self) -> list[dict[str, Any]]:
        return [c for c in self.calls if c["schema"] is SCENE_SCHEMA]


def _patch_extraction(monkeypatch: pytest.MonkeyPatch, extraction: PdfExtraction) -> list[bytes]:
    """Replace ``parsers.pdf.extract_pdf_text`` with a fake; returns seen inputs."""
    seen: list[bytes] = []

    def _fake(content: bytes, max_pages: int | None = None, *, ocr_config: Any = None):
        seen.append(content)
        return extraction

    monkeypatch.setattr(pdf_module, "extract_pdf_text", _fake)
    return seen


class _FakePage:
    def __init__(self, text: str | None = "", exc: Exception | None = None) -> None:
        self._text = text
        self._exc = exc

    def extract_text(self) -> str | None:
        if self._exc is not None:
            raise self._exc
        return self._text


class _FakePdf:
    def __init__(self, pages: list[_FakePage]) -> None:
        self.pages = pages

    def __enter__(self) -> _FakePdf:
        return self

    def __exit__(self, *exc: object) -> bool:
        return False


def _patch_pdfplumber_open(monkeypatch: pytest.MonkeyPatch, result: Any) -> None:
    """Make ``pdfplumber.open`` return *result* or raise it if it is an exception."""
    import pdfplumber

    def _open(*args: Any, **kwargs: Any) -> Any:
        if isinstance(result, BaseException):
            raise result
        return result

    monkeypatch.setattr(pdfplumber, "open", _open)


def _raise_settings() -> Any:
    raise RuntimeError("settings unavailable")


# ===================================================================
# parsers/pdf.py -- limits
# ===================================================================


class TestEffectivePdfLimits:
    """``_effective_pdf_limits`` resolves page/size caps from settings."""

    def test_values_come_from_settings(self, monkeypatch):
        settings = SimpleNamespace(max_pdf_pages=7, max_pdf_size_bytes=1234)
        monkeypatch.setattr("api.config.get_settings", lambda: settings)

        assert _effective_pdf_limits(None) == (7, 1234)

    def test_explicit_max_pages_overrides_settings(self, monkeypatch):
        settings = SimpleNamespace(max_pdf_pages=7, max_pdf_size_bytes=1234)
        monkeypatch.setattr("api.config.get_settings", lambda: settings)

        assert _effective_pdf_limits(3) == (3, 1234)

    def test_settings_failure_falls_back_to_module_defaults(self, monkeypatch):
        monkeypatch.setattr("api.config.get_settings", _raise_settings)

        assert _effective_pdf_limits(None) == (
            pdf_module._MAX_PDF_PAGES,
            pdf_module._MAX_PDF_SIZE,
        )
        assert _effective_pdf_limits(12) == (12, pdf_module._MAX_PDF_SIZE)

    def test_settings_without_attributes_use_defaults(self, monkeypatch):
        monkeypatch.setattr("api.config.get_settings", lambda: SimpleNamespace())

        assert _effective_pdf_limits(None) == (500, 10 * 1024 * 1024)

    def test_size_limit_from_settings_is_enforced(self, monkeypatch):
        """A small configured size cap rejects the PDF before pdfplumber runs."""
        settings = SimpleNamespace(max_pdf_pages=500, max_pdf_size_bytes=100)
        monkeypatch.setattr("api.config.get_settings", lambda: settings)
        _patch_pdfplumber_open(monkeypatch, AssertionError("must not open"))

        with pytest.raises(ParsingException, match="exceeds size limit") as exc:
            extract_pdf_text(b"%PDF" + b"x" * 200)

        assert exc.value.details == {"size": 204, "max_size": 100}

    def test_content_exactly_at_size_limit_is_accepted(self, monkeypatch):
        settings = SimpleNamespace(max_pdf_pages=500, max_pdf_size_bytes=10)
        monkeypatch.setattr("api.config.get_settings", lambda: settings)
        _patch_pdfplumber_open(monkeypatch, _FakePdf([_FakePage("INT. ROOM - DAY text")]))

        ex = extract_pdf_text(b"0123456789", ocr_config=OcrConfig(enabled=False))

        assert ex.page_texts == ["INT. ROOM - DAY text"]


# ===================================================================
# parsers/pdf.py -- extract_pdf_text
# ===================================================================


class TestExtractPdfTextLargeDocuments:
    """Large documents are truncated at the page cap with a warning."""

    def test_page_cap_truncates_and_warns(self):
        content = _make_pdf(
            [[f"INT. ROOM {n} - DAY", f"Page {n} action text goes here."] for n in range(1, 4)]
        )

        ex = extract_pdf_text(content, max_pages=2, ocr_config=OcrConfig(enabled=False))

        assert len(ex.page_texts) == 2
        assert "ROOM 1" in ex.page_texts[0]
        assert "ROOM 2" in ex.page_texts[1]
        assert "ROOM 3" not in ex.full_text
        assert "PDF has 3 pages. Only the first 2 pages were processed." in ex.warnings

    def test_no_warning_when_page_count_equals_cap(self):
        content = _make_pdf([["INT. ROOM - DAY", "Some action text on the page."]] * 2)

        ex = extract_pdf_text(content, max_pages=2, ocr_config=OcrConfig(enabled=False))

        assert len(ex.page_texts) == 2
        assert not any("Only the first" in w for w in ex.warnings)

    def test_page_cap_from_settings(self, monkeypatch):
        settings = SimpleNamespace(max_pdf_pages=1, max_pdf_size_bytes=10 * 1024 * 1024)
        monkeypatch.setattr("api.config.get_settings", lambda: settings)
        content = _make_pdf([["INT. A - DAY", "First page body."], ["EXT. B - NIGHT", "Second."]])

        ex = extract_pdf_text(content, ocr_config=OcrConfig(enabled=False))

        assert len(ex.page_texts) == 1
        assert "PDF has 2 pages. Only the first 1 pages were processed." in ex.warnings


class TestExtractPdfTextErrors:
    """Every low-level PDF failure is mapped to a ``ParsingException``."""

    def test_password_incorrect_raised_directly(self, monkeypatch):
        _patch_pdfplumber_open(monkeypatch, PDFPasswordIncorrect())

        with pytest.raises(ParsingException, match="password-protected") as exc:
            extract_pdf_text(b"%PDF-1.4 fake")

        assert exc.value.details == {"reason": "password_protected"}

    def test_syntax_error_raised_directly(self, monkeypatch):
        _patch_pdfplumber_open(monkeypatch, PDFSyntaxError("No /Root object!"))

        with pytest.raises(ParsingException, match="corrupted or malformed") as exc:
            extract_pdf_text(b"%PDF-1.4 fake")

        assert exc.value.details == {"reason": "No /Root object!"}

    def test_pdfminer_exception_without_password_cause_is_malformed(self, monkeypatch):
        from pdfplumber.utils.exceptions import PdfminerException

        _patch_pdfplumber_open(monkeypatch, PdfminerException("broken xref"))

        with pytest.raises(ParsingException, match="corrupted or malformed") as exc:
            extract_pdf_text(b"%PDF-1.4 fake")

        assert "broken xref" in exc.value.details["reason"]

    def test_pdfminer_exception_wrapping_password_error(self, monkeypatch):
        from pdfplumber.utils.exceptions import PdfminerException

        wrapped = PdfminerException("wrapped")
        wrapped.__cause__ = PDFPasswordIncorrect()
        _patch_pdfplumber_open(monkeypatch, wrapped)

        with pytest.raises(ParsingException, match="password-protected"):
            extract_pdf_text(b"%PDF-1.4 fake")

    def test_unexpected_page_error_is_wrapped(self, monkeypatch):
        """A crash while reading a page surfaces as ParsingException, not raw."""
        pdf = _FakePdf([_FakePage("ok text page one"), _FakePage(exc=RuntimeError("bad font"))])
        _patch_pdfplumber_open(monkeypatch, pdf)

        with pytest.raises(ParsingException) as exc:
            extract_pdf_text(b"%PDF-1.4 fake", ocr_config=OcrConfig(enabled=False))

        assert exc.value.message == "PDF text extraction failed: bad font"
        assert exc.value.details == {"reason": "bad font"}

    def test_zero_page_pdf_returns_empty_without_low_text_warning(self, monkeypatch):
        _patch_pdfplumber_open(monkeypatch, _FakePdf([]))

        ex = extract_pdf_text(b"%PDF-1.4 fake", ocr_config=OcrConfig(enabled=False))

        assert ex.full_text == ""
        assert ex.page_texts == []
        assert ex.warnings == []


class TestExtractPdfTextOcrWiring:
    """OCR fallback wiring inside ``extract_pdf_text`` (tesseract is faked)."""

    @pytest.fixture
    def fake_ocr(self, monkeypatch):
        """Pretend tesseract is installed; OCR result is chosen per page number."""
        ocr_texts: dict[int, str] = {}
        seen_pages: list[Any] = []

        def _ocr_page(page: Any, config: OcrConfig) -> str:
            seen_pages.append(page)
            return ocr_texts.get(len(seen_pages), "")

        monkeypatch.setattr(pdf_ocr, "tesseract_available", lambda: True)
        monkeypatch.setattr(pdf_ocr, "ocr_page", _ocr_page)
        return ocr_texts, seen_pages

    def test_ocr_text_is_inserted_at_the_page_position(self, monkeypatch, fake_ocr):
        ocr_texts, seen_pages = fake_ocr
        ocr_texts[1] = "INT. KELLER - NACHT\nRecovered by OCR."
        blank = _FakePage("")
        pdf = _FakePdf(
            [
                _FakePage("INT. BUERO - TAG\nText layer page one."),
                blank,
                _FakePage("EXT. HOF - TAG\nText layer page three."),
            ]
        )
        _patch_pdfplumber_open(monkeypatch, pdf)

        ex = extract_pdf_text(b"%PDF-1.4 fake", ocr_config=OcrConfig(enabled=True))

        assert seen_pages == [blank]  # only the text-less page is OCR'd
        assert ex.ocr_pages_done == [2]
        assert ex.ocr_pages_skipped == []
        assert ex.page_texts[1] == "INT. KELLER - NACHT\nRecovered by OCR."
        assert (
            ex.full_text.index("BUERO") < ex.full_text.index("KELLER") < ex.full_text.index("HOF")
        )
        assert ex.ocr_needed_pages == [2]

    def test_page_with_whitespace_only_counts_as_textless(self, monkeypatch, fake_ocr):
        ocr_texts, _seen = fake_ocr
        ocr_texts[1] = "AUSSEN. WALD - NACHT recovered"
        _patch_pdfplumber_open(monkeypatch, _FakePdf([_FakePage("   \n  short ")]))

        ex = extract_pdf_text(b"%PDF-1.4 fake", ocr_config=OcrConfig(enabled=True))

        assert ex.ocr_pages_done == [1]
        assert ex.page_texts == ["AUSSEN. WALD - NACHT recovered"]

    def test_unrecognisable_page_stays_empty_and_is_skipped(self, monkeypatch, fake_ocr):
        _patch_pdfplumber_open(
            monkeypatch,
            _FakePdf([_FakePage("INT. A - DAY with enough text"), _FakePage(None)]),
        )

        ex = extract_pdf_text(b"%PDF-1.4 fake", ocr_config=OcrConfig(enabled=True))

        assert ex.page_texts == ["INT. A - DAY with enough text", ""]
        assert ex.ocr_pages_done == []
        assert ex.ocr_pages_skipped == [2]

    def test_ocr_runner_warnings_are_propagated(self, monkeypatch):
        monkeypatch.setattr(pdf_ocr, "tesseract_available", lambda: False)
        _patch_pdfplumber_open(monkeypatch, _FakePdf([_FakePage("")]))

        ex = extract_pdf_text(b"%PDF-1.4 fake", ocr_config=OcrConfig(enabled=True))

        assert ex.ocr_pages_skipped == [1]
        assert any("tesseract/pytesseract not available" in w for w in ex.warnings)


# ===================================================================
# parsers/pdf.py -- _block_touches_pages
# ===================================================================


def _block(text: str) -> RawSceneBlock:
    return RawSceneBlock(index=0, text=text, heading_line=text.split("\n")[0], is_preamble=False)


class TestBlockTouchesPages:
    """Heuristic that links a scene block to OCR'd pages."""

    def test_empty_block_never_matches(self):
        assert _block_touches_pages(_block("  \n\n "), ["  "], {1}) is False

    def test_first_non_empty_line_found_on_ocr_page(self):
        pages = ["INT. A - DAY\nx", "INT. KELLER - NACHT\nDark."]
        block = _block("\n  INT. KELLER - NACHT  \nDark.")

        assert _block_touches_pages(block, pages, {2}) is True

    def test_line_only_on_text_layer_page_is_not_matched(self):
        pages = ["INT. A - DAY\nx", "INT. KELLER - NACHT\nDark."]

        assert _block_touches_pages(_block("INT. A - DAY\nx"), pages, {2}) is False

    def test_out_of_range_page_numbers_are_ignored(self):
        pages = ["INT. A - DAY"]

        assert _block_touches_pages(_block("INT. A - DAY"), pages, {0, 5}) is False


class TestSplitterNumberedAndAbbreviatedMarkers:
    """Dotted scene numbers and I/E abbreviations start new scenes."""

    @pytest.mark.parametrize(
        ("a", "b"),
        [
            ("1. INT.", "2. EXT."),
            ("12. INNEN.", "13. AUSSEN."),
            ("12.1 INT.", "12.2 EXT."),
            ("12.1A. INT.", "12.2 EXT."),
            ("I/E.", "E/I."),
            ("I./E.", "I / E."),
            ("INNEN-AUSSEN.", "AUSSEN - INNEN"),
            ("42 INT.", "42A EXT."),
        ],
    )
    def test_marker_variants_are_split(self, a, b):
        text = f"TITEL\n\n{a} HAUS - TAG\nAktion eins.\n\n{b} WALD - NACHT\nAktion zwei.\n"

        blocks = split_into_scenes(text)

        assert [(blk.is_preamble, blk.heading_line) for blk in blocks] == [
            (True, ""),
            (False, f"{a} HAUS - TAG"),
            (False, f"{b} WALD - NACHT"),
        ]

    @pytest.mark.parametrize(
        "line",
        ["1.5 Liter Wasser stehen bereit.", "Ich/Er. lacht.", "INTERIEUR: alles neu.", "EXTRA."],
    )
    def test_prose_lines_are_not_split(self, line):
        blocks = split_into_scenes(f"INT. A - TAG\n{line}\nNoch mehr.")

        assert [b.heading_line for b in blocks] == ["INT. A - TAG"]


class TestHyphenatedInnenAussen:
    @pytest.mark.parametrize(
        ("text", "location", "tod"),
        [
            ("INNEN-AUSSEN. GARTEN - TAG", "GARTEN", TimeOfDay.DAY),
            ("INNEN - AUSSEN. GARTEN - TAG", "GARTEN", TimeOfDay.DAY),
            ("AUSSEN–INNEN AUTO - NACHT", "AUTO", TimeOfDay.NIGHT),
            ("innen / aussen. auto - abend", "auto", TimeOfDay.EVENING),
        ],
    )
    def test_combined_designation_is_int_ext(self, text, location, tod):
        hc = parse_scene_heading(text)

        assert (hc.location_type, hc.location, hc.time_of_day) == (
            LocationType.INT_EXT,
            location,
            tod,
        )

    def test_word_starting_with_aussen_is_not_the_combined_designation(self):
        hc = parse_scene_heading("INNEN - AUSSENBEREICH - TAG")

        assert hc.location_type == LocationType.INT

    @pytest.mark.parametrize(
        ("text", "loc_type"),
        [
            ("INNEN/AUSSEN. GARTEN - TAG", LocationType.INT_EXT),
            ("INNEN. KÜCHE - TAG", LocationType.INT),
            ("AUSSEN. HOF - NACHT", LocationType.EXT),
        ],
    )
    def test_existing_designations_unchanged(self, text, loc_type):
        assert parse_scene_heading(text).location_type == loc_type


class TestSplitterHeadingOnlyScene:
    def test_trailing_heading_without_body_is_its_own_heading(self):
        """A last scene consisting only of its heading line keeps that line."""
        blocks = split_into_scenes("INT. A - DAY\nAction.\n\n12 EXT. HOF - NACHT")

        assert [b.heading_line for b in blocks] == ["INT. A - DAY", "12 EXT. HOF - NACHT"]
        assert blocks[1].text == "12 EXT. HOF - NACHT"


# ===================================================================
# parsers/pdf.py -- PDFParser.parse
# ===================================================================


_MARKER_TEXT = (
    "MEIN DREHBUCH\nvon Jemand\n\n"
    "INT. OFFICE - DAY\nAnna works.\n\n"
    "EXT. PARK - NIGHT\nTom runs.\n\n"
    "INT. KITCHEN - MORNING\nAnna cooks."
)


@pytest.mark.asyncio
class TestPDFParserPipeline:
    """End-to-end behaviour of ``PDFParser.parse`` with a fake LLM."""

    async def test_marker_scenes_title_confidence_and_character_index(self, monkeypatch):
        _patch_extraction(monkeypatch, PdfExtraction(_MARKER_TEXT, [_MARKER_TEXT]))
        llm = _RoutingLLM(
            [
                _scene_result("OFFICE", characters=["ANNA"]),
                _scene_result("UNKNOWN", characters=["TOM"]),
                _scene_result("KITCHEN", characters=["ANNA", "TOM"]),
            ],
            title="Mein Drehbuch",
        )

        script = await PDFParser(llm_provider=llm).parse(b"%PDF-fake")

        assert script.title == "Mein Drehbuch"
        assert script.format == ScriptFormat.PDF
        assert script.total_scenes == 3
        assert [s.number for s in script.scenes] == ["1", "2", "3"]
        assert [s.heading for s in script.scenes] == [
            "INT. OFFICE - DAY",
            "EXT. PARK - NIGHT",
            "INT. KITCHEN - MORNING",
        ]
        assert all(s.parse_method == "pdf_llm" for s in script.scenes)
        # Known location -> 1.0, UNKNOWN location -> 0.5
        assert [s.parse_confidence for s in script.scenes] == [1.0, 0.5, 1.0]
        assert script.overall_confidence == pytest.approx(0.833)
        assert script.warnings == []
        assert script.metadata == {
            "parser": "pdf_llm",
            "ocr_pages_done": [],
            "ocr_pages_skipped": [],
        }

        index = {c.name: c.scene_appearances for c in script.characters}
        ids = [str(s.scene_id) for s in script.scenes]
        assert index == {"ANNA": [ids[0], ids[2]], "TOM": [ids[1], ids[2]]}
        # Scene IDs are unique per scene.
        assert len(set(ids)) == 3

    async def test_scene_text_and_temperature_are_sent_to_llm(self, monkeypatch):
        _patch_extraction(monkeypatch, PdfExtraction(_MARKER_TEXT, [_MARKER_TEXT]))
        llm = _RoutingLLM()

        await PDFParser(llm_provider=llm).parse(b"%PDF-fake")

        assert len(llm.calls) == 4  # preamble + 3 scenes
        assert "MEIN DREHBUCH" in llm.calls[0]["prompt"]
        assert "Anna works." in llm.scene_calls[0]["prompt"]
        assert all(c["temperature"] == 0.1 for c in llm.calls)

    async def test_page_fallback_marks_scenes_and_lowers_confidence(self, monkeypatch):
        pages = ["Title page", "Chapter one prose.", "Chapter two prose."]
        _patch_extraction(monkeypatch, PdfExtraction("\n".join(pages), pages))
        llm = _RoutingLLM([_scene_result("GARDEN"), _scene_result("UNKNOWN")], title=None)

        script = await PDFParser(llm_provider=llm).parse(b"%PDF-fake")

        assert script.title is None
        assert [s.heading for s in script.scenes] == ["PAGE 2", "PAGE 3"]
        assert all(s.parse_method == "pdf_page_fallback" for s in script.scenes)
        # Page fallback: 0.3 with a recognised location, 0.1 without.
        assert [s.parse_confidence for s in script.scenes] == [0.3, 0.1]
        assert script.overall_confidence == pytest.approx(0.2)
        assert (
            "No scene markers (INT/EXT) found. Falling back to page-by-page splitting."
            in script.warnings
        )

    async def test_ocr_scenes_are_discounted_and_warnings_reported(self, monkeypatch):
        page1 = "INT. KELLER - NACHT\nScanned page content."
        page2 = "EXT. HOF - TAG\nText layer content."
        extraction = PdfExtraction(
            full_text=f"{page1}\n{page2}",
            page_texts=[page1, page2, ""],
            ocr_pages_done=[1],
            ocr_pages_skipped=[3],
            warnings=["upstream warning"],
        )
        _patch_extraction(monkeypatch, extraction)
        llm = _RoutingLLM([_scene_result("KELLER"), _scene_result("UNKNOWN")])

        script = await PDFParser(llm_provider=llm).parse(b"%PDF-fake")

        # OCR'd scene: 1.0 * 0.7; text-layer scene with UNKNOWN location: 0.5
        assert [s.parse_confidence for s in script.scenes] == [0.7, 0.5]
        assert script.warnings[0] == "upstream warning"
        assert "Pages [1] had no text layer; text recovered via OCR." in script.warnings
        assert any(w.startswith("Pages [3] appear to be image-only") for w in script.warnings)
        assert script.metadata["ocr_pages_done"] == [1]
        assert script.metadata["ocr_pages_skipped"] == [3]

    async def test_no_extractable_text_raises_with_ocr_details(self, monkeypatch):
        extraction = PdfExtraction(
            full_text="  \n ", page_texts=["", ""], ocr_pages_done=[], ocr_pages_skipped=[1, 2]
        )
        _patch_extraction(monkeypatch, extraction)
        llm = _RoutingLLM()

        with pytest.raises(ParsingException, match="no extractable text") as exc:
            await PDFParser(llm_provider=llm).parse(b"%PDF-fake")

        assert exc.value.details == {"ocr_pages_done": [], "ocr_pages_skipped": [1, 2]}
        assert llm.calls == []

    async def test_llm_outage_degrades_to_unknown_scenes(self, monkeypatch):
        """structure_scene_with_llm swallows provider errors -> UNKNOWN fields."""
        _patch_extraction(monkeypatch, PdfExtraction(_MARKER_TEXT, [_MARKER_TEXT]))
        llm = _RoutingLLM(
            scene_exc=LLMException("Ollama down"), title_exc=LLMException("Ollama down")
        )

        script = await PDFParser(llm_provider=llm).parse(b"%PDF-fake")

        assert script.title is None
        assert script.total_scenes == 3
        for scene in script.scenes:
            assert scene.location == "UNKNOWN"
            assert scene.location_type == LocationType.UNKNOWN
            assert scene.time_of_day == TimeOfDay.UNKNOWN
            assert scene.characters == []
            assert scene.dialogue == []
            assert scene.parse_confidence == 0.5
            # The raw scene text is preserved for downstream risk analysis.
            assert scene.text.startswith(scene.heading)
        assert script.characters == []

    async def test_type_invalid_llm_fields_degrade_scene_instead_of_crashing(self, monkeypatch):
        """Schema-violating LLM output is coerced per field; the parse never aborts."""
        _patch_extraction(monkeypatch, PdfExtraction(_MARKER_TEXT, [_MARKER_TEXT]))
        llm = _RoutingLLM(
            [
                _scene_result("OFFICE", characters=["ANNA"], location_type=5),
                ["not", "a", "dict"],  # wrong top-level type -> fallback answer
                _scene_result(
                    7,  # non-string location
                    characters="TOM",  # str instead of list[str]
                    action_text=["x"],
                    dialogue=[{"character": 3, "text": "Hi"}, {"character": "TOM", "text": 4}],
                ),
            ],
            title=123,  # non-string title must not break ParsedScript validation
        )

        script = await PDFParser(llm_provider=llm).parse(b"%PDF-fake")

        assert script.total_scenes == 3
        assert script.title is None
        first, second, third = script.scenes
        assert first.location == "OFFICE"
        assert first.location_type == LocationType.UNKNOWN
        assert first.characters == ["ANNA"]
        assert first.parse_confidence == 1.0
        for scene in (second, third):
            assert scene.location == "UNKNOWN"
            assert scene.characters == []
            assert scene.action_text == ""
            assert scene.dialogue == []
            assert scene.parse_confidence == 0.5
            assert scene.parse_method == "pdf_llm"
        assert script.warnings == []
        assert [c.name for c in script.characters] == ["ANNA"]

    async def test_residual_scene_error_degrades_only_that_scene(self, monkeypatch, caplog):
        """Any error while building one scene yields confidence 0.0 + a warning."""
        _patch_extraction(monkeypatch, PdfExtraction(_MARKER_TEXT, [_MARKER_TEXT]))
        original = pdf_module.llm_result_to_parsed_scene_fields
        calls = {"n": 0}

        def _leaky_conversion(result: Any) -> dict[str, Any]:
            calls["n"] += 1
            fields = original(result)
            if calls["n"] == 2:
                fields["characters"] = "SECRET-SCRIPT-NAME"  # bypasses coercion
            return fields

        monkeypatch.setattr(pdf_module, "llm_result_to_parsed_scene_fields", _leaky_conversion)
        llm = _RoutingLLM(
            [
                _scene_result("OFFICE", characters=["ANNA"]),
                _scene_result("PARK"),
                _scene_result("KITCHEN", characters=["ANNA"]),
            ]
        )

        with caplog.at_level(logging.WARNING, logger="parsers.pdf"):
            script = await PDFParser(llm_provider=llm).parse(b"%PDF-fake")

        assert [s.parse_confidence for s in script.scenes] == [1.0, 0.0, 1.0]
        failed = script.scenes[1]
        assert failed.number == "2"
        assert failed.heading == "EXT. PARK - NIGHT"
        assert failed.text.startswith("EXT. PARK - NIGHT")
        assert failed.location == "UNKNOWN"
        assert failed.location_type == LocationType.UNKNOWN
        assert failed.time_of_day == TimeOfDay.UNKNOWN
        assert failed.characters == []
        assert failed.parse_method == "pdf_llm"
        assert script.warnings == ["Scene 2: LLM structuring failed"]
        assert script.overall_confidence == pytest.approx(0.667)
        index = {c.name: len(c.scene_appearances) for c in script.characters}
        assert index == {"ANNA": 2}
        # Log hygiene: only the exception type is logged, never the offending value.
        messages = [r.getMessage() for r in caplog.records if r.name == "parsers.pdf"]
        assert "Scene 2 LLM structuring failed: ValidationError" in messages
        assert not any("SECRET-SCRIPT-NAME" in m for m in messages)

    async def test_page_fallback_scene_error_keeps_fallback_method(self, monkeypatch):
        pages = ["Title page", "Chapter one prose."]
        _patch_extraction(monkeypatch, PdfExtraction("\n".join(pages), pages))

        def _boom(result: Any) -> dict[str, Any]:
            raise RuntimeError("unexpected")

        monkeypatch.setattr(pdf_module, "llm_result_to_parsed_scene_fields", _boom)

        script = await PDFParser(llm_provider=_RoutingLLM()).parse(b"%PDF-fake")

        assert script.scenes[0].parse_method == "pdf_page_fallback"
        assert script.scenes[0].parse_confidence == 0.0
        assert "Scene 1: LLM structuring failed" in script.warnings

    async def test_preamble_only_document_has_title_but_no_scenes(self, monkeypatch):
        text = "A short treatment without any scene headings."
        _patch_extraction(monkeypatch, PdfExtraction(text, [text]))
        llm = _RoutingLLM(title="Treatment")

        script = await PDFParser(llm_provider=llm).parse(b"%PDF-fake")

        assert script.title == "Treatment"
        assert script.total_scenes == 0
        assert script.scenes == []
        assert script.characters == []
        assert script.overall_confidence == 0.0

    async def test_large_document_keeps_sequential_numbering(self, monkeypatch):
        n = 250
        text = "\n\n".join(f"INT. ROOM {i} - DAY\nAction {i}." for i in range(1, n + 1))
        _patch_extraction(monkeypatch, PdfExtraction(text, [text]))
        results = [
            _scene_result(f"ROOM {i}", characters=["ANNA"] if i % 2 else ["TOM"])
            for i in range(1, n + 1)
        ]
        llm = _RoutingLLM(results)

        script = await PDFParser(llm_provider=llm).parse(b"%PDF-fake")

        assert script.total_scenes == n
        assert script.scenes[-1].number == str(n)
        assert script.scenes[-1].location == f"ROOM {n}"
        assert len(llm.calls) == n  # no preamble call
        index = {c.name: len(c.scene_appearances) for c in script.characters}
        assert index == {"ANNA": 125, "TOM": 125}
        assert script.overall_confidence == 1.0

    async def test_large_real_pdf_is_truncated_at_configured_page_cap(self, monkeypatch):
        settings = SimpleNamespace(
            max_pdf_pages=3, max_pdf_size_bytes=10 * 1024 * 1024, ocr_enabled=False
        )
        monkeypatch.setattr("api.config.get_settings", lambda: settings)
        content = _make_pdf(
            [[f"INT. ROOM {n} - DAY", f"Action on page {n} of the script."] for n in range(1, 6)]
        )
        llm = _RoutingLLM()

        script = await PDFParser(llm_provider=llm).parse(content)

        assert script.total_scenes == 3
        assert [s.heading for s in script.scenes] == [
            "INT. ROOM 1 - DAY",
            "INT. ROOM 2 - DAY",
            "INT. ROOM 3 - DAY",
        ]
        assert "PDF has 5 pages. Only the first 3 pages were processed." in script.warnings

    async def test_provider_is_lazily_created_from_settings_once(self, monkeypatch):
        sentinel_settings = SimpleNamespace(llm_provider="ollama")
        created: list[Any] = []
        llm = _RoutingLLM()

        def _factory(settings: Any) -> Any:
            created.append(settings)
            return llm

        monkeypatch.setattr("api.config.get_settings", lambda: sentinel_settings)
        monkeypatch.setattr("llm.factory.get_llm_provider", _factory)
        _patch_extraction(monkeypatch, PdfExtraction(_MARKER_TEXT, [_MARKER_TEXT]))

        parser = PDFParser()
        await parser.parse(b"%PDF-fake")
        await parser.parse(b"%PDF-fake")

        assert created == [sentinel_settings]
        assert parser._llm is llm
        assert len(llm.scene_calls) == 6


def test_pdf_parser_supported_format_and_injected_provider():
    llm = MagicMock()
    parser = PDFParser(llm_provider=llm)

    assert parser.supported_format == ScriptFormat.PDF
    assert parser._llm is llm


# ===================================================================
# parsers/pdf_llm_structurer.py
# ===================================================================


@pytest.mark.asyncio
class TestPreambleTitleExtraction:
    """``extract_title_from_preamble`` uses the preamble prompt and schema."""

    async def test_returns_title_and_uses_preamble_prompt(self):
        llm = MagicMock()
        llm.generate_structured = AsyncMock(return_value={"title": "BABYSTAR"})

        title = await extract_title_from_preamble("BABYSTAR\nDrehbuch von X", llm)

        assert title == "BABYSTAR"
        kwargs = llm.generate_structured.await_args.kwargs
        assert kwargs["schema"] is PREAMBLE_SCHEMA
        assert kwargs["temperature"] == 0.1
        assert "metadata extractor" in kwargs["system_prompt"]
        assert "BABYSTAR\nDrehbuch von X" in kwargs["prompt"]
        assert "[TEXT START]" in kwargs["prompt"]

    async def test_null_title_returns_none(self):
        llm = MagicMock()
        llm.generate_structured = AsyncMock(return_value={"title": None})

        assert await extract_title_from_preamble("no title here", llm) is None

    async def test_missing_title_key_returns_none(self):
        llm = MagicMock()
        llm.generate_structured = AsyncMock(return_value={})

        assert await extract_title_from_preamble("no title here", llm) is None

    async def test_llm_exception_returns_none(self):
        llm = MagicMock()
        llm.generate_structured = AsyncMock(side_effect=LLMException("timeout"))

        assert await extract_title_from_preamble("TITLE", llm) is None

    async def test_non_dict_result_returns_none(self):
        llm = MagicMock()
        llm.generate_structured = AsyncMock(return_value=["TITLE"])

        assert await extract_title_from_preamble("TITLE", llm) is None

    @pytest.mark.parametrize(
        ("raw", "expected"),
        [(123, None), (["A"], None), ("   ", None), ("  Babystar \n", "Babystar")],
    )
    async def test_title_is_coerced_to_clean_string_or_none(self, raw, expected):
        llm = MagicMock()
        llm.generate_structured = AsyncMock(return_value={"title": raw})

        assert await extract_title_from_preamble("x", llm) == expected

    async def test_custom_temperature_is_forwarded(self):
        llm = MagicMock()
        llm.generate_structured = AsyncMock(return_value={"title": "X"})

        await extract_title_from_preamble("X", llm, temperature=0.0)

        assert llm.generate_structured.await_args.kwargs["temperature"] == 0.0


@pytest.mark.asyncio
class TestSceneStructuring:
    async def test_uses_scene_schema_and_scene_prompt(self):
        llm = MagicMock()
        llm.generate_structured = AsyncMock(return_value=_scene_result())

        result = await structure_scene_with_llm("INT. A - DAY\nText.", llm, temperature=0.3)

        assert result == _scene_result()
        kwargs = llm.generate_structured.await_args.kwargs
        assert kwargs["schema"] is SCENE_SCHEMA
        assert kwargs["temperature"] == 0.3
        assert "[SCENE START]\nINT. A - DAY\nText.\n[SCENE END]" in kwargs["prompt"]
        assert "screenplay structure parser" in kwargs["system_prompt"]

    @pytest.mark.parametrize("bad", [None, ["a"], "INT. OFFICE", 42])
    async def test_non_object_result_returns_fallback(self, bad):
        llm = MagicMock()
        llm.generate_structured = AsyncMock(return_value=bad)

        result = await structure_scene_with_llm("INT. A - DAY", llm)

        assert result["location"] == "UNKNOWN"
        assert result["characters"] == []

    async def test_failure_returns_fresh_fallback_each_time(self):
        """The fallback dict must not be shared (callers may mutate it)."""
        llm = MagicMock()
        llm.generate_structured = AsyncMock(side_effect=RuntimeError("boom"))

        first = await structure_scene_with_llm("x", llm)
        first["characters"].append("MUTATED")
        second = await structure_scene_with_llm("x", llm)

        assert second["characters"] == []
        assert set(second) == set(SCENE_SCHEMA["required"])


class TestResultNormalisation:
    """``llm_result_to_parsed_scene_fields`` normalises sloppy LLM output."""

    def test_enum_values_are_case_and_whitespace_insensitive(self):
        fields = llm_result_to_parsed_scene_fields(
            _scene_result(location_type=" int/ext ", time_of_day="night ")
        )

        assert fields["location_type"] == LocationType.INT_EXT
        assert fields["time_of_day"] == TimeOfDay.NIGHT

    def test_missing_and_null_fields_get_defaults(self):
        fields = llm_result_to_parsed_scene_fields(
            {"location": None, "location_type": None, "time_of_day": "", "characters": None}
        )

        assert fields == {
            "location": "UNKNOWN",
            "location_type": LocationType.UNKNOWN,
            "time_of_day": TimeOfDay.UNKNOWN,
            "characters": [],
            "action_text": "",
            "dialogue": [],
        }

    @pytest.mark.parametrize("bad", [None, [], "text", 3])
    def test_non_dict_result_maps_to_unknown_fields(self, bad):
        fields = llm_result_to_parsed_scene_fields(bad)

        assert fields == {
            "location": "UNKNOWN",
            "location_type": LocationType.UNKNOWN,
            "time_of_day": TimeOfDay.UNKNOWN,
            "characters": [],
            "action_text": "",
            "dialogue": [],
        }

    def test_type_invalid_fields_are_coerced(self):
        fields = llm_result_to_parsed_scene_fields(
            {
                "location": {"name": "OFFICE"},
                "location_type": ["INT"],
                "time_of_day": 12,
                "characters": "ANNA",
                "action_text": 99,
                "dialogue": {"character": "ANNA", "text": "x"},
            }
        )

        assert fields["location"] == "UNKNOWN"
        assert fields["location_type"] == LocationType.UNKNOWN
        assert fields["time_of_day"] == TimeOfDay.UNKNOWN
        assert fields["characters"] == []
        assert fields["action_text"] == ""
        assert fields["dialogue"] == []

    def test_characters_are_stripped_deduplicated_and_filtered(self):
        fields = llm_result_to_parsed_scene_fields(
            _scene_result(
                location="  BUERO ",
                characters=[" ANNA", "TOM", "ANNA ", "", "  ", None, 5, ["X"], "TOM"],
            )
        )

        assert fields["location"] == "BUERO"
        assert fields["characters"] == ["ANNA", "TOM"]

    def test_dialogue_fields_are_type_checked(self):
        fields = llm_result_to_parsed_scene_fields(
            _scene_result(
                dialogue=[
                    {"character": 3, "text": "numeric speaker"},
                    {"character": "ANNA", "text": ["not", "text"]},
                    {"character": "ANNA", "text": "   "},
                    {"character": " ANNA ", "parenthetical": 7, "text": "Ja."},
                    {"character": "TOM", "parenthetical": "  ", "text": "Nein."},
                    {"character": "TOM", "parenthetical": " (leise) ", "text": "Doch."},
                ]
            )
        )

        assert [(d.character, d.parenthetical, d.text) for d in fields["dialogue"]] == [
            ("ANNA", None, "Ja."),
            ("TOM", None, "Nein."),
            ("TOM", "(leise)", "Doch."),
        ]

    def test_incomplete_dialogue_entries_are_dropped(self):
        fields = llm_result_to_parsed_scene_fields(
            _scene_result(
                dialogue=[
                    {"character": "ANNA", "text": "Hallo."},
                    {"character": "", "text": "no speaker"},
                    {"character": "TOM", "text": ""},
                    {"text": "missing character key"},
                    "not a dict",
                    None,
                    {"character": "TOM", "parenthetical": "leise", "text": "Psst."},
                ]
            )
        )

        lines = fields["dialogue"]
        assert [(d.character, d.parenthetical, d.text) for d in lines] == [
            ("ANNA", None, "Hallo."),
            ("TOM", "leise", "Psst."),
        ]


# ===================================================================
# parsers/pdf_ocr.py
# ===================================================================


def _exec_pdf_ocr_copy(monkeypatch: pytest.MonkeyPatch) -> None:
    """Execute parsers/pdf_ocr.py as a throw-away module.

    The real ``parsers.pdf_ocr`` stays untouched; the probe copy is only
    registered (dataclasses need that) for the duration of the test.
    """
    name = "_pdf_ocr_import_probe"
    spec = importlib.util.spec_from_file_location(name, pdf_ocr.__file__)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    monkeypatch.setitem(sys.modules, name, module)
    spec.loader.exec_module(module)


class TestOmpThreadLimit:
    """Importing the OCR module caps tesseract's OpenMP threads by default."""

    def test_default_is_set_on_import(self, monkeypatch):
        monkeypatch.setenv("OMP_THREAD_LIMIT", "placeholder")  # restored on teardown
        monkeypatch.delenv("OMP_THREAD_LIMIT")

        _exec_pdf_ocr_copy(monkeypatch)

        assert os.environ["OMP_THREAD_LIMIT"] == "1"

    def test_explicit_value_is_not_clobbered(self, monkeypatch):
        monkeypatch.setenv("OMP_THREAD_LIMIT", "4")

        _exec_pdf_ocr_copy(monkeypatch)

        assert os.environ["OMP_THREAD_LIMIT"] == "4"


class TestOcrConfigFromSettings:
    def test_reads_all_values_from_settings(self, monkeypatch):
        settings = SimpleNamespace(
            ocr_enabled=False,
            ocr_languages="eng",
            ocr_max_pages=3,
            ocr_dpi=150,
            ocr_page_timeout_seconds=7,
        )
        monkeypatch.setattr("api.config.get_settings", lambda: settings)

        cfg = OcrConfig.from_settings()

        assert cfg == OcrConfig(
            enabled=False, languages="eng", max_pages=3, dpi=150, page_timeout_seconds=7
        )

    def test_settings_failure_returns_defaults(self, monkeypatch):
        monkeypatch.setattr("api.config.get_settings", _raise_settings)

        assert OcrConfig.from_settings() == OcrConfig()

    def test_runner_without_config_uses_settings(self, monkeypatch):
        settings = SimpleNamespace(ocr_enabled=False, ocr_max_pages=9)
        monkeypatch.setattr("api.config.get_settings", lambda: settings)

        runner = OcrRunner()

        assert runner.config.enabled is False
        assert runner.config.max_pages == 9
        assert runner.available is False
        assert runner.warnings == []  # disabled on purpose -> no "unavailable" warning


class TestTesseractAvailable:
    def test_false_when_pytesseract_cannot_be_imported(self, monkeypatch):
        monkeypatch.setitem(sys.modules, "pytesseract", None)
        monkeypatch.setattr(pdf_ocr.shutil, "which", lambda name: "/usr/bin/tesseract")

        assert tesseract_available() is False

    def test_false_when_binary_missing(self, monkeypatch):
        monkeypatch.setitem(sys.modules, "pytesseract", ModuleType("pytesseract"))
        monkeypatch.setattr(pdf_ocr.shutil, "which", lambda name: None)

        assert tesseract_available() is False

    def test_true_when_package_and_binary_present(self, monkeypatch):
        looked_up: list[str] = []

        def _which(name: str) -> str:
            looked_up.append(name)
            return "/usr/bin/tesseract"

        monkeypatch.setitem(sys.modules, "pytesseract", ModuleType("pytesseract"))
        monkeypatch.setattr(pdf_ocr.shutil, "which", _which)

        assert tesseract_available() is True
        assert looked_up == ["tesseract"]


class _ImagePage:
    """Minimal pdfplumber ``Page`` stand-in for ``ocr_page``."""

    def __init__(self, image: Image.Image) -> None:
        self.image = image
        self.resolutions: list[int] = []

    def to_image(self, resolution: int) -> Any:
        self.resolutions.append(resolution)
        return SimpleNamespace(original=self.image)


class TestOcrPage:
    @pytest.fixture
    def fake_tesseract(self, monkeypatch):
        calls: list[dict[str, Any]] = []
        module = ModuleType("pytesseract")
        module.result = "  INT. KUECHE - TAG\nAnna kocht.  \n"

        def image_to_string(image: Image.Image, lang: str, timeout: int) -> Any:
            calls.append({"mode": image.mode, "lang": lang, "timeout": timeout})
            return module.result

        module.image_to_string = image_to_string
        monkeypatch.setitem(sys.modules, "pytesseract", module)
        return module, calls

    def test_rgba_image_is_converted_and_config_forwarded(self, fake_tesseract):
        _module, calls = fake_tesseract
        page = _ImagePage(Image.new("RGBA", (4, 4)))
        cfg = OcrConfig(languages="deu", dpi=200, page_timeout_seconds=9)

        text = ocr_page(page, cfg)

        assert text == "INT. KUECHE - TAG\nAnna kocht."
        assert page.resolutions == [200]
        assert calls == [{"mode": "RGB", "lang": "deu", "timeout": 9}]

    def test_grayscale_image_is_passed_unchanged(self, fake_tesseract):
        _module, calls = fake_tesseract
        page = _ImagePage(Image.new("L", (4, 4)))

        ocr_page(page, OcrConfig())

        assert calls[0]["mode"] == "L"
        assert calls[0]["lang"] == "deu+eng"
        assert calls[0]["timeout"] == 60

    def test_none_result_becomes_empty_string(self, fake_tesseract):
        module, _calls = fake_tesseract
        module.result = None

        assert ocr_page(_ImagePage(Image.new("RGB", (4, 4))), OcrConfig()) == ""


class TestOcrRunnerCap:
    def test_cap_warning_is_emitted_once_and_pages_not_duplicated(self, monkeypatch):
        monkeypatch.setattr(pdf_ocr, "tesseract_available", lambda: True)
        monkeypatch.setattr(pdf_ocr, "ocr_page", lambda page, cfg: "INT. RAUM - TAG text")
        runner = OcrRunner(OcrConfig(enabled=True, max_pages=1))

        assert runner.process(object(), 1)
        assert runner.process(object(), 2) == ""
        assert runner.process(object(), 2) == ""
        assert runner.process(object(), 3) == ""

        assert runner.pages_done == [1]
        assert runner.pages_skipped == [2, 3]
        assert sum("OCR_MAX_PAGES=1" in w for w in runner.warnings) == 1

    def test_ocr_needed_pages_is_sorted_union(self):
        ex = PdfExtraction("", [], ocr_pages_done=[5, 2], ocr_pages_skipped=[3, 2])

        assert ex.ocr_needed_pages == [2, 3, 5]


# ===================================================================
# parsers/secure_xml.py
# ===================================================================


def _stub_safe_et(monkeypatch: pytest.MonkeyPatch, exc: Exception) -> None:
    """Replace the defusedxml module used by secure_xml with one that raises *exc*."""

    def _fromstring(content: bytes) -> Any:
        raise exc

    stub = SimpleNamespace(fromstring=_fromstring, ParseError=secure_xml.SafeET.ParseError)
    monkeypatch.setattr(secure_xml, "SafeET", stub)


class TestSecureXmlErrorMapping:
    def test_dtd_forbidden_is_mapped(self, monkeypatch):
        _stub_safe_et(monkeypatch, DTDForbidden("r", None, None))

        with pytest.raises(ParsingException, match="forbidden DTD") as exc:
            parse_xml_safe(b"<r/>")

        assert exc.value.details == {"reason": "dtd_forbidden"}

    def test_external_reference_forbidden_is_mapped(self, monkeypatch):
        _stub_safe_et(
            monkeypatch, ExternalReferenceForbidden(None, None, "http://evil/x.dtd", None)
        )

        with pytest.raises(ParsingException, match="external references") as exc:
            parse_xml_safe(b"<r/>")

        assert exc.value.details == {"reason": "external_reference_forbidden"}

    def test_unexpected_error_is_wrapped(self, monkeypatch):
        _stub_safe_et(monkeypatch, MemoryError("too deep"))

        with pytest.raises(ParsingException, match="XML parsing failed: too deep") as exc:
            parse_xml_safe(b"<r/>")

        assert exc.value.details == {"reason": "unknown"}

    def test_unknown_encoding_declaration_is_wrapped(self):
        content = b'<?xml version="1.0" encoding="bogus-enc"?><r/>'

        with pytest.raises(ParsingException, match="XML parsing failed") as exc:
            parse_xml_safe(content)

        assert exc.value.details == {"reason": "unknown"}

    def test_parameter_entity_is_rejected_as_entity_attack(self):
        content = b'<!DOCTYPE r [<!ENTITY % p SYSTEM "http://evil/x">%p;]><r/>'

        with pytest.raises(ParsingException) as exc:
            parse_xml_safe(content)

        assert exc.value.details == {"reason": "entities_forbidden"}

    def test_empty_document_is_malformed(self):
        with pytest.raises(ParsingException, match="Malformed XML") as exc:
            parse_xml_safe(b"")

        assert exc.value.details == {"reason": "parse_error"}

    def test_custom_size_limit_boundary(self):
        assert parse_xml_safe(b"<r>12</r>", max_size=9).tag == "r"

        with pytest.raises(ParsingException, match=r"exceeds size limit \(10 > 9 bytes\)") as exc:
            parse_xml_safe(b"<r>123</r>", max_size=9)

        assert exc.value.details == {"size": 10, "max_size": 9}


# ===================================================================
# parsers/fdx.py
# ===================================================================


def _fdx(content_paragraphs: str, title_page: str = "") -> bytes:
    return (
        '<?xml version="1.0" encoding="UTF-8"?>\n'
        '<FinalDraft DocumentType="Script" Version="4">\n'
        f"<Content>{content_paragraphs}</Content>\n"
        f"{title_page}"
        "</FinalDraft>"
    ).encode()


def _para(ptype: str, *texts: str, number: str | None = None) -> str:
    num = f' Number="{number}"' if number else ""
    runs = "".join(f"<Text>{t}</Text>" for t in texts)
    return f'<Paragraph Type="{ptype}"{num}>{runs}</Paragraph>'


@pytest.mark.asyncio
class TestFdxEdgeCases:
    async def test_title_is_first_non_empty_title_page_paragraph(self):
        title_page = (
            "<TitlePage><Content>"
            "<Paragraph><Text></Text></Paragraph>"
            "<Paragraph><Text>DER LETZTE </Text><Text>STUNT</Text></Paragraph>"
            "<Paragraph><Text>by Somebody</Text></Paragraph>"
            "</Content></TitlePage>"
        )
        content = _fdx(_para("Scene Heading", "INT. HALLE - TAG", number="1"), title_page)

        script = await FDXParser().parse(content)

        # Style runs are joined verbatim (whitespace lives inside the runs).
        assert script.title == "DER LETZTE STUNT"

    async def test_title_is_none_when_title_page_is_empty(self):
        title_page = "<TitlePage><Content><Paragraph/></Content></TitlePage>"
        script = await FDXParser().parse(_fdx(_para("Scene Heading", "INT. A - DAY"), title_page))

        assert script.title is None

    async def test_shot_paragraph_is_action_and_resets_speaker(self):
        body = "".join(
            [
                _para("Scene Heading", "EXT. DACH - NACHT", number="7"),
                _para("Character", "MAX"),
                _para("Dialogue", "Ich springe."),
                _para("Shot", "CLOSE ON: MAX'S FEET"),
                _para("Dialogue", "Orphaned line after shot."),
            ]
        )

        script = await FDXParser().parse(_fdx(body))
        scene = script.scenes[0]

        assert scene.number == "7"
        assert scene.action_text == "CLOSE ON: MAX'S FEET"
        assert [(d.character, d.text) for d in scene.dialogue] == [("MAX", "Ich springe.")]
        assert "Orphaned line after shot." in scene.text

    async def test_empty_paragraphs_are_skipped_entirely(self):
        body = "".join(
            [
                _para("Scene Heading", "INT. KUECHE - TAG"),
                '<Paragraph Type="Action"><Text></Text></Paragraph>',
                '<Paragraph Type="Action"/>',
                _para("Action", "Anna kocht."),
                '<Paragraph Type="Character"><Text></Text></Paragraph>',
            ]
        )

        scene = (await FDXParser().parse(_fdx(body))).scenes[0]

        assert scene.action_text == "Anna kocht."
        assert scene.text == "INT. KUECHE - TAG\nAnna kocht."
        assert scene.characters == []

    async def test_parenthetical_applies_to_next_dialogue_only(self):
        body = "".join(
            [
                _para("Scene Heading", "INT. FLUR - NACHT"),
                _para("Character", "LENA"),
                _para("Parenthetical", "(flüsternd)"),
                _para("Dialogue", "Bist du da?"),
                _para("Dialogue", "Hallo?"),
                _para("Character", "LENA"),
                _para("Dialogue", "Antworte!"),
            ]
        )

        scene = (await FDXParser().parse(_fdx(body))).scenes[0]

        assert [(d.character, d.parenthetical, d.text) for d in scene.dialogue] == [
            ("LENA", "flüsternd", "Bist du da?"),
            ("LENA", None, "Hallo?"),
            ("LENA", None, "Antworte!"),
        ]
        assert scene.characters == ["LENA"]

    async def test_content_before_first_heading_is_kept_as_scene_zero(self):
        """A cold open before the first heading must not silently disappear."""
        body = "".join(
            [
                _para("Action", "COLD OPEN: Ein Auto explodiert."),
                _para("Character", "NARRATOR"),
                _para("Dialogue", "Es begann mit einem Knall."),
                _para("Scene Heading", "INT. A - DAY", number="1"),
                _para("Action", "Inside."),
            ]
        )

        script = await FDXParser().parse(_fdx(body))

        assert script.total_scenes == 2
        lead, first = script.scenes
        assert lead.number == "0"
        assert lead.heading == "(before first scene heading)"
        assert (lead.location, lead.location_type, lead.time_of_day) == (
            "UNKNOWN",
            LocationType.UNKNOWN,
            TimeOfDay.UNKNOWN,
        )
        assert lead.text == "COLD OPEN: Ein Auto explodiert.\nNARRATOR\nEs begann mit einem Knall."
        assert lead.action_text == "COLD OPEN: Ein Auto explodiert."
        assert [(d.character, d.text) for d in lead.dialogue] == [
            ("NARRATOR", "Es begann mit einem Knall.")
        ]
        assert lead.parse_confidence == 0.3
        assert lead.parse_method == "fdx_fallback"
        # The headed scene is unaffected.
        assert (first.number, first.text, first.parse_confidence) == (
            "1",
            "INT. A - DAY\nInside.",
            1.0,
        )
        assert first.parse_method == "fdx"
        assert script.warnings == [
            "FDX content before the first scene heading was kept as scene 0 (location unknown)."
        ]
        assert script.overall_confidence == pytest.approx(0.65)
        assert [c.name for c in script.characters] == ["NARRATOR"]
        assert script.metadata == {"parser": "fdx", "paragraph_count": 5}

    async def test_transition_only_preamble_does_not_create_a_scene(self):
        body = "".join(
            [
                _para("Transition", "FADE IN:"),
                '<Paragraph Type="Action"><Text>   </Text></Paragraph>',
                _para("Scene Heading", "INT. A - DAY"),
                _para("Action", "Inside."),
            ]
        )

        script = await FDXParser().parse(_fdx(body))

        assert script.total_scenes == 1
        assert script.scenes[0].heading == "INT. A - DAY"
        assert script.warnings == []
        assert script.overall_confidence == 1.0

    async def test_unknown_paragraph_type_only_lands_in_full_text(self):
        body = "".join(
            [
                _para("Scene Heading", "INT. A - DAY"),
                _para("Cast List", "ANNA, TOM"),
                "<Paragraph><Text>No type attribute</Text></Paragraph>",
            ]
        )

        scene = (await FDXParser().parse(_fdx(body))).scenes[0]

        assert scene.action_text == ""
        assert "ANNA, TOM" in scene.text
        assert "No type attribute" in scene.text

    async def test_document_without_scene_headings_becomes_one_fallback_scene(self):
        """No headings must not yield an empty (i.e. "risk-free") script."""
        body = "".join(
            [
                _para("Action", "Explosion! Das Lager brennt."),
                _para("Character", "ANNA"),
                _para("Dialogue", "Hilfe!"),
                _para("Transition", "CUT TO:"),
                _para("Action", "Rauch."),
            ]
        )

        script = await FDXParser().parse(_fdx(body))

        assert script.total_scenes == 1
        scene = script.scenes[0]
        assert scene.number == "1"
        assert scene.heading == "(no scene heading)"
        assert scene.location == "UNKNOWN"
        assert scene.location_type == LocationType.UNKNOWN
        assert scene.text == "Explosion! Das Lager brennt.\nANNA\nHilfe!\nCUT TO:\nRauch."
        assert scene.characters == ["ANNA"]
        assert scene.parse_confidence == 0.3
        assert scene.parse_method == "fdx_fallback"
        assert script.overall_confidence == 0.3
        assert script.warnings == [
            "FDX contains no scene headings; the whole script was analysed as a single "
            "scene (location unknown)."
        ]
        assert [c.name for c in script.characters] == ["ANNA"]

    @pytest.mark.parametrize(
        "body",
        ["", '<Paragraph Type="Action"/>', _para("Transition", "FADE OUT.")],
        ids=["empty", "blank-paragraph", "transition-only"],
    )
    async def test_document_without_script_content_warns(self, body):
        script = await FDXParser().parse(_fdx(body))

        assert script.total_scenes == 0
        assert script.scenes == []
        assert script.overall_confidence == 0.0
        assert script.warnings == ["FDX contains no script content."]

    async def test_regular_script_has_full_confidence_and_no_warnings(self):
        body = _para("Scene Heading", "INT. A - DAY") + _para("Action", "x")

        script = await FDXParser().parse(_fdx(body))

        assert script.overall_confidence == 1.0
        assert script.warnings == []
        assert script.scenes[0].parse_method == "fdx"

    async def test_style_runs_are_concatenated_verbatim(self):
        body = (
            _para("Scene Heading", "INT. A - ", "TAG")
            + '<Paragraph Type="Action"><Text>Explo</Text><Text Style="Bold">sion</Text>'
            "<Text> jetzt!</Text></Paragraph>"
        )

        scene = (await FDXParser().parse(_fdx(body))).scenes[0]

        assert scene.heading == "INT. A - TAG"
        assert scene.action_text == "Explosion jetzt!"

    async def test_dual_dialogue_paragraphs_are_parsed_in_order(self):
        body = "".join(
            [
                _para("Scene Heading", "INT. HAUS - TAG"),
                _para("Action", "Beide schreien gleichzeitig."),
                "<Paragraph><DualDialogue>",
                _para("Character", "ANNA"),
                _para("Parenthetical", "(wütend)"),
                _para("Dialogue", "Ich zünde das Haus an."),
                _para("Character", "BEN"),
                _para("Dialogue", "Nein!"),
                "</DualDialogue></Paragraph>",
                _para("Action", "Stille."),
            ]
        )

        script = await FDXParser().parse(_fdx(body))
        scene = script.scenes[0]

        assert [(d.character, d.parenthetical, d.text) for d in scene.dialogue] == [
            ("ANNA", "wütend", "Ich zünde das Haus an."),
            ("BEN", None, "Nein!"),
        ]
        assert scene.characters == ["ANNA", "BEN"]
        assert scene.action_text == "Beide schreien gleichzeitig.\nStille."
        assert "Ich zünde das Haus an." in scene.text
        # Container replaced by its 5 inner paragraphs: 1 + 1 + 5 + 1.
        assert script.metadata["paragraph_count"] == 8

    async def test_scene_summary_paragraphs_are_not_script_text(self):
        body = (
            '<Paragraph Type="Scene Heading"><Text>INT. A - DAY</Text>'
            "<SceneProperties><Summary><Paragraph><Text>Notiz: hier Stunt</Text></Paragraph>"
            "</Summary></SceneProperties></Paragraph>" + _para("Action", "Inside.")
        )

        scene = (await FDXParser().parse(_fdx(body))).scenes[0]

        assert scene.text == "INT. A - DAY\nInside."

    async def test_character_index_spans_scenes(self):
        body = "".join(
            [
                _para("Scene Heading", "INT. A - DAY"),
                _para("Character", "ANNA"),
                _para("Dialogue", "Eins."),
                _para("Character", "ANNA"),
                _para("Dialogue", "Zwei."),
                _para("Scene Heading", "EXT. B - NIGHT"),
                _para("Character", "TOM"),
                _para("Dialogue", "Drei."),
                _para("Character", "ANNA"),
                _para("Dialogue", "Vier."),
            ]
        )

        script = await FDXParser().parse(_fdx(body))
        ids = [str(s.scene_id) for s in script.scenes]
        index = {c.name: c.scene_appearances for c in script.characters}

        assert script.scenes[0].characters == ["ANNA"]
        assert index == {"ANNA": [ids[0], ids[1]], "TOM": [ids[1]]}

    async def test_extract_paragraphs_without_content_returns_empty(self):
        root = parse_xml_safe(b"<FinalDraft><Other/></FinalDraft>")

        assert FDXParser._extract_paragraphs(root) == []
