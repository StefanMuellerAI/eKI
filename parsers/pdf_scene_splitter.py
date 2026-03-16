"""Deterministic scene splitter for screenplay text.

Splits raw text at INT./EXT./INNEN/AUSSEN markers -- the single most
reliable structural signal in any screenplay, regardless of formatting.
"""

import re
from dataclasses import dataclass, field

# Matches lines that begin a new scene (DE + EN).
# Anchored to start-of-line via MULTILINE.
# Allows optional leading scene numbers (e.g. "42 INNEN. WOHNZIMMER - TAG").
SCENE_MARKER_RE = re.compile(
    r"^[ \t]*(?:\d+[A-Za-z]?\s+)?("
    r"INT\.\s*/\s*EXT\.|"
    r"INT/EXT\.|"
    r"EXT\.\s*/\s*INT\.|"
    r"EXT/INT\.|"
    r"INT\.|"
    r"EXT\.|"
    r"INNEN\s*/\s*AUSSEN|"
    r"INNEN/AUSSEN|"
    r"AUSSEN\s*/\s*INNEN|"
    r"AUSSEN/INNEN|"
    r"INNEN[\.\s]|"
    r"AUSSEN[\.\s]"
    r")",
    re.MULTILINE | re.IGNORECASE,
)


@dataclass(frozen=True, slots=True)
class RawSceneBlock:
    """A raw text block produced by the scene splitter."""

    index: int
    text: str
    heading_line: str  # First line of the block (the INT/EXT line), empty for preamble
    is_preamble: bool


def split_by_pages(page_texts: list[str]) -> list[RawSceneBlock]:
    """Split pages into blocks: page 1 = preamble, each subsequent page = scene.

    Used as a fallback when no INT/EXT scene markers are detected.
    Empty pages (after stripping) are skipped.
    """
    blocks: list[RawSceneBlock] = []
    idx = 0

    for page_num, text in enumerate(page_texts, start=1):
        stripped = text.strip()
        if not stripped:
            continue

        if page_num == 1:
            blocks.append(
                RawSceneBlock(
                    index=idx,
                    text=stripped,
                    heading_line="",
                    is_preamble=True,
                )
            )
        else:
            blocks.append(
                RawSceneBlock(
                    index=idx,
                    text=stripped,
                    heading_line=f"PAGE {page_num}",
                    is_preamble=False,
                )
            )
        idx += 1

    return blocks


def split_into_scenes(
    full_text: str,
    *,
    page_texts: list[str] | None = None,
) -> list[RawSceneBlock]:
    """Split *full_text* at scene-heading markers and return ordered blocks.

    - Everything before the first marker becomes a preamble block
      (``is_preamble=True``).  If there is no text before the first marker
      the preamble is omitted.
    - Each subsequent block starts with its heading line and includes all
      text up to the next marker (or end-of-string).
    - When no markers are found and *page_texts* is provided, falls back to
      page-by-page splitting (page 1 = preamble, subsequent pages = scenes).

    Returns an empty list only if *full_text* is empty/whitespace.
    """
    if not full_text or not full_text.strip():
        return []

    # Find all marker positions
    matches = list(SCENE_MARKER_RE.finditer(full_text))

    if not matches:
        # No scene markers -- try page-based fallback if page info is available
        if page_texts and len(page_texts) > 1:
            return split_by_pages(page_texts)

        # Single page or no page info: return entire text as preamble
        return [
            RawSceneBlock(
                index=0,
                text=full_text.strip(),
                heading_line="",
                is_preamble=True,
            )
        ]

    blocks: list[RawSceneBlock] = []
    idx = 0

    # Preamble: text before the first marker
    preamble_text = full_text[: matches[0].start()].strip()
    if preamble_text:
        blocks.append(
            RawSceneBlock(
                index=idx,
                text=preamble_text,
                heading_line="",
                is_preamble=True,
            )
        )
        idx += 1

    # Scene blocks
    for i, match in enumerate(matches):
        start = match.start()
        end = matches[i + 1].start() if i + 1 < len(matches) else len(full_text)
        block_text = full_text[start:end].strip()

        # First line = heading
        first_newline = block_text.find("\n")
        if first_newline == -1:
            heading_line = block_text
        else:
            heading_line = block_text[:first_newline].strip()

        blocks.append(
            RawSceneBlock(
                index=idx,
                text=block_text,
                heading_line=heading_line,
                is_preamble=False,
            )
        )
        idx += 1

    return blocks
