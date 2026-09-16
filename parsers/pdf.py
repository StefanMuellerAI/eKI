"""PDF screenplay parser.

Extracts text from PDF files, splits at INT/EXT markers, and uses the
configured LLM (Ollama/Mistral) to structure each scene block into the
ParsedScene schema.  IDs, counters, and the character index are built
programmatically -- the LLM only handles unstructured-to-structured mapping.
"""

import io
import logging
import time
from collections import defaultdict
from typing import Any
from uuid import uuid4

from core.exceptions import ParsingException
from core.models import (
    CharacterInfo,
    ParsedScene,
    ParsedScript,
    ScriptFormat,
)
from parsers.base import ParserBase
from parsers.pdf_llm_structurer import (
    extract_title_from_preamble,
    llm_result_to_parsed_scene_fields,
    structure_scene_with_llm,
)
from parsers.pdf_ocr import MIN_TEXT_CHARS_PER_PAGE, OcrConfig, OcrRunner, PdfExtraction
from parsers.pdf_scene_splitter import split_into_scenes

logger = logging.getLogger(__name__)

# Fallback-Konstanten für den Fall, dass Settings beim Aufruf nicht ladbar
# sind (z.B. in stand-alone Tests ohne env-Konfig). Numerisch identisch
# zum Stand vor M07. Im Produktivpfad werden die Werte aus den Settings
# bezogen (max_pdf_pages, max_pdf_size_bytes).
_MAX_PDF_PAGES = 500
_MAX_PDF_SIZE = 10 * 1024 * 1024  # 10 MB


def _effective_pdf_limits(max_pages: int | None) -> tuple[int, int]:
    """Return (effective_max_pages, effective_max_size) using settings.

    max_pages aus dem Argument hat Vorrang (Test-Hook); fehlt es, wird der
    Wert aus settings.max_pdf_pages genommen. settings.max_pdf_size_bytes
    überschreibt _MAX_PDF_SIZE, falls verfügbar.
    """
    cfg_max_pages = _MAX_PDF_PAGES
    cfg_max_size = _MAX_PDF_SIZE
    try:
        from api.config import get_settings

        s = get_settings()
        cfg_max_pages = int(getattr(s, "max_pdf_pages", _MAX_PDF_PAGES))
        cfg_max_size = int(getattr(s, "max_pdf_size_bytes", _MAX_PDF_SIZE))
    except Exception:  # nosec B110
        # Settings unavailable (e.g. import-time in scripts) -> keep module defaults.
        pass

    effective_pages = max_pages if max_pages is not None else cfg_max_pages
    return effective_pages, cfg_max_size


def extract_pdf_text(
    content: bytes,
    max_pages: int | None = None,
    *,
    ocr_config: OcrConfig | None = None,
) -> PdfExtraction:
    """Extract text from a PDF, page by page, in-memory only.

    Returns a :class:`PdfExtraction`. It still unpacks as the legacy 4-tuple
    ``(full_text, page_texts, ocr_needed_pages, warnings)``.

    Pages without a text layer (< ``MIN_TEXT_CHARS_PER_PAGE`` chars) go through
    the OCR fallback (``parsers/pdf_ocr.py``). Their text is inserted **at the
    page's own position** in ``page_texts``; pages that still yield nothing stay
    as empty strings so page indices remain aligned with page numbers.
    """
    import pdfplumber
    from pdfminer.pdfdocument import PDFPasswordIncorrect
    from pdfminer.pdfparser import PDFSyntaxError
    from pdfplumber.utils.exceptions import PdfminerException

    effective_max_pages, effective_max_size = _effective_pdf_limits(max_pages)

    if len(content) > effective_max_size:
        raise ParsingException(
            f"PDF exceeds size limit ({len(content)} > {effective_max_size} bytes)",
            details={"size": len(content), "max_size": effective_max_size},
        )

    pages_text: list[str] = []
    warnings: list[str] = []
    ocr = OcrRunner(ocr_config)

    try:
        with pdfplumber.open(io.BytesIO(content)) as pdf:
            total_pages = len(pdf.pages)
            if total_pages > effective_max_pages:
                warnings.append(
                    f"PDF has {total_pages} pages. "
                    f"Only the first {effective_max_pages} pages were processed."
                )
            for i, page in enumerate(pdf.pages[:effective_max_pages]):
                text = page.extract_text() or ""
                if len(text.strip()) < MIN_TEXT_CHARS_PER_PAGE:
                    text = ocr.process(page, i + 1)
                pages_text.append(text)
    except PDFPasswordIncorrect:
        raise ParsingException(
            "PDF is password-protected. Please provide an unprotected file.",
            details={"reason": "password_protected"},
        )
    except PDFSyntaxError as exc:
        raise ParsingException(
            "PDF is corrupted or malformed and cannot be read.",
            details={"reason": str(exc)},
        )
    except PdfminerException as exc:
        inner = exc.__cause__ or exc.__context__
        if isinstance(inner, PDFPasswordIncorrect):
            raise ParsingException(
                "PDF is password-protected. Please provide an unprotected file.",
                details={"reason": "password_protected"},
            )
        raise ParsingException(
            "PDF is corrupted or malformed and cannot be read.",
            details={"reason": str(exc)},
        )
    except Exception as exc:
        raise ParsingException(
            f"PDF text extraction failed: {exc}",
            details={"reason": str(exc)},
        )

    full_text = "\n".join(t for t in pages_text if t)
    warnings.extend(ocr.warnings)

    if ocr.pages_done:
        logger.info(
            "OCR fallback applied to %d page(s): %s", len(ocr.pages_done), ocr.pages_done[:20]
        )
    if pages_text and len(full_text.strip()) < 50:
        warnings.append("PDF contains very little extractable text. Results may be incomplete.")

    return PdfExtraction(
        full_text=full_text,
        page_texts=pages_text,
        ocr_pages_done=sorted(ocr.pages_done),
        ocr_pages_skipped=sorted(ocr.pages_skipped),
        warnings=warnings,
    )


def _block_touches_pages(block: Any, page_texts: list[str], ocr_pages: set[int]) -> bool:
    """Heuristic: does *block* contain text that came from an OCR'd page?

    Scene blocks do not carry page numbers, so we check whether the block's
    first non-empty line appears in any OCR'd page's text.
    """
    probe = next((ln.strip() for ln in block.text.splitlines() if ln.strip()), "")
    if not probe:
        return False
    for page_no in ocr_pages:
        idx = page_no - 1
        if 0 <= idx < len(page_texts) and probe in page_texts[idx]:
            return True
    return False


class PDFParser(ParserBase):
    """LLM-assisted PDF screenplay parser.

    Pipeline:
    1. pdfplumber text extraction (no disk writes)
    2. Deterministic split at INT/EXT markers
    3. Per-scene LLM structuring (Ollama structured output)
    4. Programmatic aggregation (IDs, counters, character index)
    """

    def __init__(self, llm_provider: Any = None) -> None:
        self._llm = llm_provider

    @property
    def supported_format(self) -> ScriptFormat:
        return ScriptFormat.PDF

    async def parse(self, content: bytes) -> ParsedScript:  # type: ignore[override]
        """Parse raw PDF bytes into a ``ParsedScript``."""
        t0 = time.monotonic()
        warnings: list[str] = []

        # Lazy-init LLM provider if not injected
        if self._llm is None:
            from api.config import get_settings
            from llm.factory import get_llm_provider

            self._llm = get_llm_provider(get_settings())

        # 1. Extract text (with OCR fallback for image-only pages)
        extraction = extract_pdf_text(content)
        full_text, page_texts = extraction.full_text, extraction.page_texts
        warnings.extend(extraction.warnings)
        if extraction.ocr_pages_done:
            warnings.append(
                f"Pages {extraction.ocr_pages_done} had no text layer; text recovered via OCR."
            )
        if extraction.ocr_pages_skipped:
            warnings.append(
                f"Pages {extraction.ocr_pages_skipped} appear to be image-only and could not be "
                "OCR'd (OCR disabled, unavailable, capped or no text recognised)."
            )
        if not full_text.strip():
            raise ParsingException(
                "PDF contains no extractable text, even after OCR. "
                "The document may be image-only with unreadable scans.",
                details={
                    "ocr_pages_done": extraction.ocr_pages_done,
                    "ocr_pages_skipped": extraction.ocr_pages_skipped,
                },
            )
        ocr_page_set = set(extraction.ocr_pages_done)

        # 2. Deterministic split at INT/EXT markers (with page-based fallback)
        blocks = split_into_scenes(full_text, page_texts=page_texts)
        used_page_fallback = any(
            not b.is_preamble and b.heading_line.startswith("PAGE ") for b in blocks
        )
        if used_page_fallback:
            warnings.append(
                "No scene markers (INT/EXT) found. Falling back to page-by-page splitting."
            )

        # 3. LLM structuring per scene
        scenes: list[ParsedScene] = []
        title: str | None = None
        scene_counter = 0

        for block in blocks:
            if block.is_preamble:
                title = await extract_title_from_preamble(block.text, self._llm)
                continue

            scene_counter += 1
            is_page_fallback = block.heading_line.startswith("PAGE ")
            try:
                llm_result = await structure_scene_with_llm(block.text, self._llm)
                fields = llm_result_to_parsed_scene_fields(llm_result)
                if is_page_fallback:
                    confidence = 0.3 if fields["location"] != "UNKNOWN" else 0.1
                else:
                    confidence = 1.0 if fields["location"] != "UNKNOWN" else 0.5
                # OCR'd pages: recognised text is less reliable than a text layer.
                if ocr_page_set and _block_touches_pages(block, page_texts, ocr_page_set):
                    confidence = round(confidence * 0.7, 3)
            except Exception as exc:
                logger.warning("Scene %d LLM structuring failed: %s", scene_counter, exc)
                fields = {
                    "location": "UNKNOWN",
                    "location_type": "UNKNOWN",
                    "time_of_day": "UNKNOWN",
                    "characters": [],
                    "action_text": "",
                    "dialogue": [],
                }
                confidence = 0.0
                warnings.append(f"Scene {scene_counter}: LLM structuring failed")

            scene = ParsedScene(
                scene_id=uuid4(),
                number=str(scene_counter),
                heading=block.heading_line,
                text=block.text,
                parse_confidence=confidence,
                parse_method="pdf_page_fallback" if is_page_fallback else "pdf_llm",
                **fields,
            )
            scenes.append(scene)

        # 4. Programmatic aggregation
        characters = self._build_character_index(scenes)
        elapsed = time.monotonic() - t0

        avg_confidence = sum(s.parse_confidence for s in scenes) / len(scenes) if scenes else 0.0

        logger.info(
            "PDF parsed: %d scenes, %d characters, confidence=%.2f in %.1fs",
            len(scenes),
            len(characters),
            avg_confidence,
            elapsed,
        )

        return ParsedScript(
            script_id=uuid4(),
            title=title,
            format=ScriptFormat.PDF,
            total_scenes=len(scenes),
            scenes=scenes,
            characters=characters,
            parsing_time_seconds=round(elapsed, 3),
            overall_confidence=round(avg_confidence, 3),
            warnings=warnings,
            metadata={
                "parser": "pdf_llm",
                "ocr_pages_done": extraction.ocr_pages_done,
                "ocr_pages_skipped": extraction.ocr_pages_skipped,
            },
        )

    @staticmethod
    def _build_character_index(scenes: list[ParsedScene]) -> list[CharacterInfo]:
        """Aggregate character appearances across scenes."""
        appearances: dict[str, list[str]] = defaultdict(list)
        for scene in scenes:
            for name in scene.characters:
                appearances[name].append(str(scene.scene_id))
        return [
            CharacterInfo(name=name, scene_appearances=scene_ids)
            for name, scene_ids in appearances.items()
        ]
