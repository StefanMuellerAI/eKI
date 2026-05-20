"""Tests for OllamaProvider's schema-constrained structured output path.

Mocks ``httpx.AsyncClient.post`` directly via ``unittest.mock.AsyncMock`` so
no real Ollama server is required. Verifies that the request payload uses
Ollama's native ``format=<schema>`` (GBNF-constrained) output, that
``think`` is sent as a top-level parameter (workaround for ollama#14793),
that ``num_ctx`` is propagated from config to ``options``, and that the
response parser is robust against thinking-tag and markdown-fence wrappers.
"""

from typing import Any
from unittest.mock import AsyncMock, patch

import httpx
import pytest

from core.exceptions import LLMException
from llm.ollama import OllamaProvider

_SIMPLE_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {"x": {"type": "integer"}},
    "required": ["x"],
}


def _make_provider(**overrides: Any) -> OllamaProvider:
    """Create an OllamaProvider with explicit, predictable settings."""
    config: dict[str, Any] = {
        "base_url": "http://test-ollama:11434",
        "model": "gemma4:4b",
        "timeout": 60,
        "think": False,
        "num_ctx": 16384,
    }
    config.update(overrides)
    return OllamaProvider(config)


def _mock_chat_response(content: str) -> httpx.Response:
    """Build a fake /api/chat response with the given message content.

    A request object must be attached so ``response.raise_for_status()``
    inside the provider works without raising RuntimeError.
    """
    request = httpx.Request("POST", "http://test-ollama:11434/api/chat")
    return httpx.Response(
        status_code=200,
        json={
            "model": "gemma4:4b",
            "message": {"role": "assistant", "content": content},
            "done": True,
        },
        request=request,
    )


# ---------------------------------------------------------------------------
# Payload structure tests
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_generate_structured_passes_schema_as_format() -> None:
    """Schema must be sent in the top-level ``format`` field (GBNF-constrained)."""
    provider = _make_provider()
    fake = _mock_chat_response('{"x": 42}')

    with patch("httpx.AsyncClient.post", new=AsyncMock(return_value=fake)) as mock_post:
        result = await provider.generate_structured(
            prompt="Give me x", schema=_SIMPLE_SCHEMA, temperature=0.1
        )

    assert result == {"x": 42}
    sent_payload = mock_post.call_args.kwargs["json"]
    assert sent_payload["format"] == _SIMPLE_SCHEMA


@pytest.mark.asyncio
async def test_generate_structured_passes_think_as_top_level() -> None:
    """``think`` must be a top-level parameter, not nested inside ``options``.

    See ollama/ollama#14793: the generate API ignores ``think`` when passed
    inside ``options``; only top-level placement works.
    """
    provider = _make_provider(think=False)
    fake = _mock_chat_response('{"x": 1}')

    with patch("httpx.AsyncClient.post", new=AsyncMock(return_value=fake)) as mock_post:
        await provider.generate_structured(
            prompt="Test prompt", schema=_SIMPLE_SCHEMA, temperature=0.1
        )

    sent_payload = mock_post.call_args.kwargs["json"]
    assert sent_payload["think"] is False
    assert "think" not in sent_payload["options"]


@pytest.mark.asyncio
async def test_generate_structured_passes_num_ctx_in_options() -> None:
    """``num_ctx`` from provider config must land in ``options.num_ctx``."""
    provider = _make_provider(num_ctx=8192)
    fake = _mock_chat_response('{"x": 1}')

    with patch("httpx.AsyncClient.post", new=AsyncMock(return_value=fake)) as mock_post:
        await provider.generate_structured(
            prompt="Test prompt", schema=_SIMPLE_SCHEMA, temperature=0.1
        )

    sent_payload = mock_post.call_args.kwargs["json"]
    assert sent_payload["options"]["num_ctx"] == 8192
    assert sent_payload["options"]["temperature"] == 0.1


@pytest.mark.asyncio
async def test_generate_structured_targets_chat_endpoint() -> None:
    """Structured output must POST to /api/chat (not /api/generate)."""
    provider = _make_provider()
    fake = _mock_chat_response('{"x": 1}')

    with patch("httpx.AsyncClient.post", new=AsyncMock(return_value=fake)) as mock_post:
        await provider.generate_structured(
            prompt="Test prompt", schema=_SIMPLE_SCHEMA, temperature=0.1
        )

    called_url = mock_post.call_args.args[0]
    assert called_url == "http://test-ollama:11434/api/chat"


# ---------------------------------------------------------------------------
# Response parsing / robustness tests
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_generate_structured_strips_thinking_tags() -> None:
    """Embedded <think>...</think> blocks must be removed before JSON parsing."""
    provider = _make_provider()
    content = '<think>Let me figure this out step by step.</think>\n{"x": 7}'
    fake = _mock_chat_response(content)

    with patch("httpx.AsyncClient.post", new=AsyncMock(return_value=fake)):
        result = await provider.generate_structured(
            prompt="Test prompt", schema=_SIMPLE_SCHEMA, temperature=0.1
        )

    assert result == {"x": 7}


@pytest.mark.asyncio
async def test_generate_structured_strips_markdown_fences() -> None:
    """``` json fences must be removed before JSON parsing."""
    provider = _make_provider()
    fake = _mock_chat_response('```json\n{"x": 99}\n```')

    with patch("httpx.AsyncClient.post", new=AsyncMock(return_value=fake)):
        result = await provider.generate_structured(
            prompt="Test prompt", schema=_SIMPLE_SCHEMA, temperature=0.1
        )

    assert result == {"x": 99}


@pytest.mark.asyncio
async def test_generate_structured_raises_on_invalid_json() -> None:
    """Non-JSON model output must raise LLMException with response context."""
    provider = _make_provider()
    fake = _mock_chat_response("this is not json at all")

    with patch("httpx.AsyncClient.post", new=AsyncMock(return_value=fake)):
        with pytest.raises(LLMException) as exc_info:
            await provider.generate_structured(
                prompt="Test prompt", schema=_SIMPLE_SCHEMA, temperature=0.1
            )

    assert "Invalid JSON" in exc_info.value.message
    assert "response" in exc_info.value.details


@pytest.mark.asyncio
async def test_generate_structured_raises_on_http_error() -> None:
    """HTTP failures must surface as LLMException, not as bare httpx errors."""
    provider = _make_provider()
    network_error = httpx.ConnectError("connection refused")

    with patch("httpx.AsyncClient.post", new=AsyncMock(side_effect=network_error)):
        with pytest.raises(LLMException) as exc_info:
            await provider.generate_structured(
                prompt="Test prompt", schema=_SIMPLE_SCHEMA, temperature=0.1
            )

    assert "Ollama Chat API request failed" in exc_info.value.message


# ---------------------------------------------------------------------------
# _strip_thinking_tags unit tests
# ---------------------------------------------------------------------------


class TestStripThinkingTags:
    """Direct tests for the thinking-tag stripping helper."""

    def test_strips_single_tag(self) -> None:
        text = "<think>reasoning here</think>\n{\"x\": 1}"
        assert OllamaProvider._strip_thinking_tags(text) == '{"x": 1}'

    def test_strips_multiple_tags(self) -> None:
        text = "<think>first</think>middle<think>second</think>final"
        assert OllamaProvider._strip_thinking_tags(text) == "middlefinal"

    def test_passes_through_when_no_tag(self) -> None:
        text = '{"clean": true}'
        assert OllamaProvider._strip_thinking_tags(text) == '{"clean": true}'

    def test_is_case_insensitive(self) -> None:
        text = "<THINK>upper</THINK>{\"x\": 1}"
        assert OllamaProvider._strip_thinking_tags(text) == '{"x": 1}'

    def test_handles_multiline_tag_content(self) -> None:
        text = "<think>line one\nline two\nline three</think>{\"x\": 1}"
        assert OllamaProvider._strip_thinking_tags(text) == '{"x": 1}'

    def test_returns_empty_string_for_only_thinking(self) -> None:
        text = "<think>nothing else</think>"
        assert OllamaProvider._strip_thinking_tags(text) == ""
