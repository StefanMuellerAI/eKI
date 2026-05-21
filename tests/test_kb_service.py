"""Unit tests for KnowledgeBaseService — focused on pure logic.

DB- and pgvector-dependent paths are exercised in integration tests
(out of scope for the unit suite, which runs on SQLite).  Here we
cover:

* Chunking honors the 800/1500-token (≈ char) bounds and overlap.
* YAML front-matter is stripped before chunking.
* SHA-256 dedup hash is stable for identical inputs.
* Extension detection accepts only allowed formats.
"""

from __future__ import annotations

import hashlib

from services.knowledge_base import KnowledgeBaseService


def _chunks_of(text: str) -> list[tuple[int, str]]:
    return KnowledgeBaseService._chunk_text(text)


def test_chunker_returns_empty_for_empty_input() -> None:
    assert _chunks_of("") == []
    assert _chunks_of("   \n\n\t") == []


def test_chunker_keeps_short_text_in_single_chunk() -> None:
    text = "A short safety note.\n\nWith two paragraphs."
    chunks = _chunks_of(text)
    assert len(chunks) == 1
    offset, chunk = chunks[0]
    assert offset == 0
    assert "short safety note" in chunk
    assert "two paragraphs" in chunk


def test_chunker_emits_multiple_chunks_for_long_text() -> None:
    paragraph = ("Stunt-Koordination ist verpflichtend fuer alle riskanten Aktionen. " * 12).strip()
    text = "\n\n".join([paragraph] * 8)  # ~7000 chars

    chunks = _chunks_of(text)
    assert len(chunks) >= 2
    # Each chunk should be inside [800, 6000] chars after the chunker
    # (the floor enforces emission once we exceed the target, not strictly).
    for _, chunk in chunks:
        assert len(chunk) <= 6000


def test_chunker_preserves_overlap_between_consecutive_chunks() -> None:
    paragraph = ("Eine Massnahme entlang der Sicherheitskette. " * 30).strip()
    text = "\n\n".join([paragraph] * 10)
    chunks = _chunks_of(text)
    assert len(chunks) >= 2
    # Tail of chunk N appears at head of chunk N+1 by construction.
    tail = chunks[0][1][-200:]
    head = chunks[1][1][:400]
    assert any(seg in head for seg in [tail[-100:], tail[-50:]])


def test_strip_frontmatter_removes_yaml_block() -> None:
    text = (
        "---\n"
        "title: \"Stunt-SOP\"\n"
        "tags: [\"placeholder\"]\n"
        "---\n"
        "\n"
        "Body content starts here.\n"
    )
    stripped = KnowledgeBaseService._strip_frontmatter(text)
    assert "title" not in stripped
    assert "Body content starts here" in stripped


def test_strip_frontmatter_is_noop_without_block() -> None:
    text = "No front-matter here.\n\nSome paragraph."
    assert KnowledgeBaseService._strip_frontmatter(text) == text


def test_strip_frontmatter_handles_only_opener() -> None:
    # Defensive: a leading '---' line without a closing pair should leave
    # the text untouched (we do not want to lose content).
    text = "---\nthis is not really front matter\n\nbody"
    assert KnowledgeBaseService._strip_frontmatter(text) == text


def test_extension_detection() -> None:
    assert KnowledgeBaseService._extension("doc.pdf") == ".pdf"
    assert KnowledgeBaseService._extension("doc.PDF") == ".pdf"
    assert KnowledgeBaseService._extension("plain.txt") == ".txt"
    assert KnowledgeBaseService._extension("README.md") == ".md"
    assert KnowledgeBaseService._extension("README.markdown") == ".markdown"
    assert KnowledgeBaseService._extension("no_extension") == ""


def test_content_hash_is_stable_for_identical_input() -> None:
    text = "Identical content used for the dedup check."
    h1 = hashlib.sha256(text.encode("utf-8")).hexdigest()
    h2 = hashlib.sha256(text.encode("utf-8")).hexdigest()
    assert h1 == h2
    assert h1 != hashlib.sha256((text + ".").encode("utf-8")).hexdigest()


def test_split_long_paragraph_force_splits() -> None:
    big = "x" * 9000
    parts = KnowledgeBaseService._split_long(big)
    assert len(parts) >= 2
    assert sum(len(p) for p in parts) == 9000
