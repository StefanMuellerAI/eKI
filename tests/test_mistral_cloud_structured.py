"""Tests for MistralCloudProvider.generate_structured.

Mocks ``httpx.AsyncClient.post`` so no real Mistral API call is made.
Verifies:

* Request payload contains ``response_format={"type":"json_object"}``
* Schema is embedded in the locked system prompt
* Valid JSON matching the schema is returned as a dict
* Invalid JSON triggers exactly one self-correcting retry
* If retry also fails, an LLMException is raised
"""

from __future__ import annotations

import json
from typing import Any
from unittest.mock import AsyncMock, patch

import httpx
import pytest

from core.exceptions import LLMException
from llm.mistral_cloud import MistralCloudProvider

_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "answer": {"type": "string"},
        "count": {"type": "integer", "minimum": 0},
    },
    "required": ["answer", "count"],
    "additionalProperties": False,
}


def _make_provider() -> MistralCloudProvider:
    return MistralCloudProvider(
        {
            "api_key": "sk-test-12345",
            "model": "mistral-large-latest",
            "timeout": 30,
        }
    )


def _mock_chat_response(content: str) -> httpx.Response:
    request = httpx.Request("POST", "https://api.mistral.ai/v1/chat/completions")
    return httpx.Response(
        status_code=200,
        json={
            "model": "mistral-large-latest",
            "choices": [{"message": {"content": content}}],
        },
        request=request,
    )


@pytest.mark.asyncio
async def test_valid_json_returned_as_dict() -> None:
    provider = _make_provider()
    valid = json.dumps({"answer": "hi", "count": 3})

    with patch.object(httpx.AsyncClient, "post", new=AsyncMock(return_value=_mock_chat_response(valid))) as mock_post:
        result = await provider.generate_structured(
            prompt="Say hi", schema=_SCHEMA, temperature=0.1
        )

    assert result == {"answer": "hi", "count": 3}
    sent_payload = mock_post.call_args.kwargs["json"]
    assert sent_payload["response_format"] == {"type": "json_object"}
    assert sent_payload["model"] == "mistral-large-latest"
    # Schema is embedded in the system message
    sys_msg = sent_payload["messages"][0]["content"]
    assert "JSON Schema" in sys_msg
    assert "answer" in sys_msg and "count" in sys_msg


@pytest.mark.asyncio
async def test_markdown_fences_are_stripped() -> None:
    provider = _make_provider()
    wrapped = "```json\n" + json.dumps({"answer": "hi", "count": 1}) + "\n```"

    with patch.object(httpx.AsyncClient, "post", new=AsyncMock(return_value=_mock_chat_response(wrapped))):
        result = await provider.generate_structured(prompt="Say hi", schema=_SCHEMA)

    assert result == {"answer": "hi", "count": 1}


@pytest.mark.asyncio
async def test_invalid_schema_triggers_single_retry() -> None:
    provider = _make_provider()
    # First call: integer count is missing (schema violation)
    bad = json.dumps({"answer": "hello"})
    good = json.dumps({"answer": "hello", "count": 5})

    responses = [_mock_chat_response(bad), _mock_chat_response(good)]
    mock_post = AsyncMock(side_effect=responses)

    with patch.object(httpx.AsyncClient, "post", new=mock_post):
        result = await provider.generate_structured(prompt="Hello", schema=_SCHEMA)

    assert result == {"answer": "hello", "count": 5}
    assert mock_post.call_count == 2
    # The retry must include the prior failed JSON in the user message
    retry_user = mock_post.call_args_list[1].kwargs["json"]["messages"][1]["content"]
    assert "previous response was NOT valid" in retry_user.lower() or "not valid" in retry_user.lower()
    assert "hello" in retry_user


@pytest.mark.asyncio
async def test_retry_failure_raises_llm_exception() -> None:
    provider = _make_provider()
    bad1 = json.dumps({"answer": "x"})  # missing count
    bad2 = json.dumps({"answer": "y"})  # still missing count

    mock_post = AsyncMock(side_effect=[_mock_chat_response(bad1), _mock_chat_response(bad2)])
    with patch.object(httpx.AsyncClient, "post", new=mock_post):
        with pytest.raises(LLMException) as excinfo:
            await provider.generate_structured(prompt="Hi", schema=_SCHEMA)

    assert "after retry" in str(excinfo.value.message)
    assert mock_post.call_count == 2


@pytest.mark.asyncio
async def test_unparseable_json_raises_llm_exception() -> None:
    provider = _make_provider()
    with patch.object(
        httpx.AsyncClient, "post", new=AsyncMock(return_value=_mock_chat_response("not json at all"))
    ):
        with pytest.raises(LLMException) as excinfo:
            await provider.generate_structured(prompt="x", schema=_SCHEMA)

    assert "Invalid JSON" in str(excinfo.value.message)


def test_constructor_requires_api_key() -> None:
    with pytest.raises(ValueError, match="API key"):
        MistralCloudProvider({})
