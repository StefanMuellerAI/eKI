"""OCR fallback for image-only PDF pages (Pflichtenheft §4.1 "PDF mit OCR-Fallback").

Pages whose embedded text layer is empty (scanned screenplays) are rendered
via pdfplumber/pypdfium2 to a PIL image and passed to Tesseract through
``pytesseract``. No temp files: rendering and OCR happen in memory.

Guard rails (all via settings, see ``api/config.py``):

* ``OCR_ENABLED``              master switch (default ``true``); silently degrades
                               to "skip page + warning" when the tesseract
                               binary or ``pytesseract`` is missing.
* ``OCR_LANGUAGES``            tesseract language string (default ``deu+eng``).
* ``OCR_MAX_PAGES``            cap on OCR'd pages per document (runtime bound).
* ``OCR_DPI``                  render resolution (300 is the tesseract sweet spot).
* ``OCR_PAGE_TIMEOUT_SECONDS`` per-page tesseract timeout.

OCR'd text is marked with ``PdfExtraction.ocr_pages_done`` so downstream
confidence scoring can discount it (``parsers/pdf.py``).
"""

from __future__ import annotations

import logging
import shutil
from dataclasses import dataclass, field
from typing import Any

logger = logging.getLogger(__name__)

# Threshold below which a page counts as "no text layer" (kept from M03).
MIN_TEXT_CHARS_PER_PAGE = 10


@dataclass
class OcrConfig:
    enabled: bool = True
    languages: str = "deu+eng"
    max_pages: int = 50
    dpi: int = 300
    page_timeout_seconds: int = 60

    @classmethod
    def from_settings(cls) -> OcrConfig:
        try:
            from api.config import get_settings

            s = get_settings()
            return cls(
                enabled=bool(getattr(s, "ocr_enabled", True)),
                languages=str(getattr(s, "ocr_languages", "deu+eng")),
                max_pages=int(getattr(s, "ocr_max_pages", 50)),
                dpi=int(getattr(s, "ocr_dpi", 300)),
                page_timeout_seconds=int(getattr(s, "ocr_page_timeout_seconds", 60)),
            )
        except Exception:
            return cls()


@dataclass
class PdfExtraction:
    """Result of ``extract_pdf_text``.

    ``page_texts`` has exactly one entry per processed page (index == page - 1),
    so page-based fallback splitting stays aligned even when pages were OCR'd or
    left empty. Before the OCR milestone, image-only pages were dropped from the
    list, silently shifting every following page number.
    """

    full_text: str
    page_texts: list[str]
    ocr_pages_done: list[int] = field(default_factory=list)
    ocr_pages_skipped: list[int] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)

    @property
    def ocr_needed_pages(self) -> list[int]:
        """All pages that had no usable text layer (OCR'd or skipped)."""
        return sorted(set(self.ocr_pages_done) | set(self.ocr_pages_skipped))

    # Backwards-compatible tuple unpacking for legacy callers/tests.
    def __iter__(self) -> Any:
        return iter((self.full_text, self.page_texts, self.ocr_needed_pages, self.warnings))


def tesseract_available() -> bool:
    """True when both the pytesseract package and the tesseract binary exist."""
    try:
        import pytesseract  # noqa: F401
    except Exception:
        return False
    return shutil.which("tesseract") is not None


def ocr_page(page: Any, config: OcrConfig) -> str:
    """OCR one pdfplumber ``Page`` object; returns extracted text ('' on failure)."""
    import pytesseract

    image = page.to_image(resolution=config.dpi).original
    if image.mode not in ("L", "RGB"):
        image = image.convert("RGB")
    text = pytesseract.image_to_string(
        image,
        lang=config.languages,
        timeout=config.page_timeout_seconds,
    )
    return (text or "").strip()


class OcrRunner:
    """Stateful helper that applies the per-document page cap and availability check."""

    def __init__(self, config: OcrConfig | None = None) -> None:
        self.config = config or OcrConfig.from_settings()
        self.available = self.config.enabled and tesseract_available()
        self.pages_done: list[int] = []
        self.pages_skipped: list[int] = []
        self.warnings: list[str] = []
        if self.config.enabled and not self.available:
            self.warnings.append(
                "OCR requested but tesseract/pytesseract not available; image-only pages skipped."
            )

    def process(self, page: Any, page_number: int) -> str:
        """Return OCR text for *page* or '' (page recorded as skipped)."""
        if not self.available:
            self.pages_skipped.append(page_number)
            return ""
        if len(self.pages_done) >= self.config.max_pages:
            if page_number not in self.pages_skipped:
                self.pages_skipped.append(page_number)
            if not any("OCR_MAX_PAGES" in w for w in self.warnings):
                self.warnings.append(
                    f"OCR page cap reached (OCR_MAX_PAGES={self.config.max_pages}); "
                    "remaining image-only pages skipped."
                )
            return ""
        try:
            text = ocr_page(page, self.config)
        except Exception as exc:
            logger.warning("OCR failed for page %d: %s", page_number, type(exc).__name__)
            self.pages_skipped.append(page_number)
            return ""
        if len(text) < MIN_TEXT_CHARS_PER_PAGE:
            self.pages_skipped.append(page_number)
            return ""
        self.pages_done.append(page_number)
        return text


__all__ = [
    "MIN_TEXT_CHARS_PER_PAGE",
    "OcrConfig",
    "OcrRunner",
    "PdfExtraction",
    "ocr_page",
    "tesseract_available",
]
