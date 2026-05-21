"""Regression and feature tests for the KB integration in the risk activity.

Key invariants we want to preserve:

* ``KB_RETRIEVAL_ENABLED=false`` (default) -> the risk activity helper
  returns the literal placeholder ``"(none)"`` without touching the KB,
  the database, or the LLM provider.  This keeps the M05 risk path
  byte-identical.
* ``KB_RETRIEVAL_ENABLED=true`` -> the helper calls into the KB service,
  formats hits with title + truncated chunk text, and returns the result.
* ANY exception inside the KB lookup falls back to ``"(none)"`` so the
  risk activity remains robust.
"""

from __future__ import annotations

from dataclasses import dataclass
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch
from uuid import UUID, uuid4

import pytest

from workflows.activities import _build_kb_context


def _settings(*, enabled: bool, top_k: int = 3, max_chars: int = 600) -> SimpleNamespace:
    return SimpleNamespace(
        kb_retrieval_enabled=enabled,
        kb_default_tenant_id="00000000-0000-0000-0000-000000000001",
        kb_top_k=top_k,
        kb_max_chunk_chars_in_prompt=max_chars,
        database_url="postgresql+asyncpg://test/test",
        api_secret_key="unit-test-secret",
        llm_provider="ollama",
    )


@dataclass
class _Hit:
    doc_id: UUID
    title: str
    tags: list[str]
    chunk_id: str
    chunk_text: str
    distance: float


class _DummyEngine:
    async def dispose(self) -> None:
        return None


class _DummySession:
    async def __aenter__(self) -> "_DummySession":
        return self

    async def __aexit__(self, *exc: object) -> None:
        return None


def _session_factory(*args: object, **kwargs: object) -> _DummySession:
    return _DummySession()


@pytest.mark.asyncio
async def test_kb_disabled_returns_none_marker_without_side_effects() -> None:
    """Default path: flag OFF means no DB or LLM access whatsoever."""
    # If the function tried to use the DB or KB service when disabled,
    # these mocks would throw because they assert call counts below.
    with patch("sqlalchemy.ext.asyncio.create_async_engine") as engine_mock, patch(
        "services.knowledge_base.KnowledgeBaseService"
    ) as kb_mock:
        result = await _build_kb_context(
            scene_text="A man falls from a roof at night.",
            settings=_settings(enabled=False),
        )

    assert result == "(none)"
    engine_mock.assert_not_called()
    kb_mock.assert_not_called()


@pytest.mark.asyncio
async def test_kb_disabled_returns_none_for_empty_scene_text() -> None:
    result = await _build_kb_context(
        scene_text="",
        settings=_settings(enabled=True),
    )
    assert result == "(none)"


@pytest.mark.asyncio
async def test_kb_failure_is_non_fatal() -> None:
    """If anything inside the KB lookup raises, we must return '(none)'."""
    with patch(
        "sqlalchemy.ext.asyncio.create_async_engine", side_effect=RuntimeError("boom")
    ):
        result = await _build_kb_context(
            scene_text="Stunt fall sequence.",
            settings=_settings(enabled=True),
        )
    assert result == "(none)"


@pytest.mark.asyncio
async def test_kb_enabled_formats_hits_with_title_and_truncated_text() -> None:
    """Happy path: title + chunk text appear, truncated at max chars."""
    hits = [
        _Hit(
            doc_id=uuid4(),
            title="Stunt-SOP",
            tags=["placeholder", "stunt"],
            chunk_id="c1",
            chunk_text="A" * 2000,  # will be truncated
            distance=0.12,
        ),
        _Hit(
            doc_id=uuid4(),
            title="Fire SFX Safety",
            tags=["placeholder", "fire"],
            chunk_id="c2",
            chunk_text="short hit",
            distance=0.25,
        ),
    ]

    kb_service_mock = SimpleNamespace(search=AsyncMock(return_value=hits))

    with patch(
        "sqlalchemy.ext.asyncio.create_async_engine", return_value=_DummyEngine()
    ), patch(
        "sqlalchemy.ext.asyncio.async_sessionmaker", return_value=_session_factory
    ), patch("llm.factory.get_llm_provider"), patch(
        "services.knowledge_base.KnowledgeBaseService", return_value=kb_service_mock
    ):
        result = await _build_kb_context(
            scene_text="Stunt fall sequence at night.",
            settings=_settings(enabled=True, top_k=2, max_chars=500),
        )

    assert "[Stunt-SOP]" in result
    assert "[Fire SFX Safety]" in result
    assert "short hit" in result
    assert "..." in result  # long chunk got truncated and ellipsis suffix
    assert "A" * 600 not in result  # truncated below max+ellipsis


@pytest.mark.asyncio
async def test_kb_enabled_returns_none_when_no_hits() -> None:
    kb_service_mock = SimpleNamespace(search=AsyncMock(return_value=[]))

    with patch(
        "sqlalchemy.ext.asyncio.create_async_engine", return_value=_DummyEngine()
    ), patch(
        "sqlalchemy.ext.asyncio.async_sessionmaker", return_value=_session_factory
    ), patch("llm.factory.get_llm_provider"), patch(
        "services.knowledge_base.KnowledgeBaseService", return_value=kb_service_mock
    ):
        result = await _build_kb_context(
            scene_text="Unrelated calm dialogue.",
            settings=_settings(enabled=True),
        )

    assert result == "(none)"
