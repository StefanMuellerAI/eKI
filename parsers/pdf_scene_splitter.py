"""Deterministic scene splitter for screenplay text.

Splits raw text at INT./EXT./INNEN/AUSSEN markers -- the single most
reliable structural signal in any screenplay, regardless of formatting.
"""

import re
from dataclasses import dataclass, field

# Matches lines that begin a new scene (DE + EN).
# Anchored to start-of-line via MULTILINE.
SCENE_MARKER_RE = re.compile(
    r"^[ \t]*("
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


def split_into_scenes(full_text: str) -> list[RawSceneBlock]:
    """Split *full_text* at scene-heading markers and return ordered blocks.

    - Everything before the first marker becomes a preamble block
      (``is_preamble=True``).  If there is no text before the first marker
      the preamble is omitted.
    - Each subsequent block starts with its heading line and includes all
      text up to the next marker (or end-of-string).

    Returns an empty list only if *full_text* is empty/whitespace.
    """
    if not full_text or not full_text.strip():
        return []

    # Find all marker positions
    matches = list(SCENE_MARKER_RE.finditer(full_text))

    if not matches:
        # No scene markers found at all -- return entire text as a single
        # preamble block so downstream can still process it.
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
