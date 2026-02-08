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
from parsers.pdf_scene_splitter import split_into_scenes

logger = logging.getLogger(__name__)

_MAX_PDF_PAGES = 500
_MAX_PDF_SIZE = 10 * 1024 * 1024  # 10 MB


def extract_pdf_text(content: bytes, max_pages: int = _MAX_PDF_PAGES) -> tuple[str, list[int]]:
    """Extract text from a PDF, page by page, in-memory only.

    Returns ``(full_text, ocr_needed_pages)`` where *ocr_needed_pages* lists
    1-based page numbers that appear to be image-only (< 10 text characters).
    """
    import pdfplumber

    if len(content) > _MAX_PDF_SIZE:
        raise ParsingException(
            f"PDF exceeds size limit ({len(content)} > {_MAX_PDF_SIZE} bytes)",
            details={"size": len(content), "max_size": _MAX_PDF_SIZE},
        )

    pages_text: list[str] = []
    ocr_needed: list[int] = []

    try:
        with pdfplumber.open(io.BytesIO(content)) as pdf:
            for i, page in enumerate(pdf.pages[:max_pages]):
                text = page.extract_text() or ""
                if len(text.strip()) < 10:
                    ocr_needed.append(i + 1)
                    continue
                pages_text.append(text)
    except Exception as exc:
        raise ParsingException(
            f"PDF text extraction failed: {exc}",
            details={"reason": str(exc)},
        )

    return "\n".join(pages_text), ocr_needed


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

        # 1. Extract text
        full_text, ocr_pages = extract_pdf_text(content)
        if ocr_pages:
            warnings.append(
                f"Pages {ocr_pages} appear to be scanned/image-only. OCR not yet implemented."
            )
        if not full_text.strip():
            raise ParsingException(
                "PDF contains no extractable text",
                details={"ocr_pages": ocr_pages},
            )

        # 2. Deterministic split at INT/EXT markers
        blocks = split_into_scenes(full_text)

        # 3. LLM structuring per scene
        scenes: list[ParsedScene] = []
        title: str | None = None
        scene_counter = 0

        for block in blocks:
            if block.is_preamble:
                title = await extract_title_from_preamble(block.text, self._llm)
                continue

            scene_counter += 1
            try:
                llm_result = await structure_scene_with_llm(block.text, self._llm)
                fields = llm_result_to_parsed_scene_fields(llm_result)
                confidence = 1.0 if fields["location"] != "UNKNOWN" else 0.5
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
                parse_method="pdf_llm",
                **fields,
            )
            scenes.append(scene)

        # 4. Programmatic aggregation
        characters = self._build_character_index(scenes)
        elapsed = time.monotonic() - t0

        avg_confidence = (
            sum(s.parse_confidence for s in scenes) / len(scenes) if scenes else 0.0
        )

        logger.info(
            "PDF parsed: %d scenes, %d characters, confidence=%.2f in %.1fs",
            len(scenes), len(characters), avg_confidence, elapsed,
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
                "ocr_pages_skipped": ocr_pages,
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
