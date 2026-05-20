"""Ollama local LLM provider."""

import json
import logging
import re
from typing import Any

import httpx

from core.exceptions import LLMException
from core.prompt_sanitizer import PromptSanitizer
from llm.base import BaseLLMProvider

logger = logging.getLogger(__name__)

# Matches <think>...</think> blocks that thinking-capable models (gemma4,
# qwen3.x) may emit before the actual answer, even with think=False set.
# Acts as a safety net so the downstream JSON parser does not break.
_THINKING_TAG_PATTERN = re.compile(r"<think>.*?</think>\s*", re.DOTALL | re.IGNORECASE)


class OllamaProvider(BaseLLMProvider):
    """Ollama local LLM provider."""

    def __init__(self, config: dict[str, Any]) -> None:
        """Initialize Ollama provider."""
        super().__init__(config)
        self.base_url = config.get("base_url", "http://ollama:11434")
        self.model = config.get("model", "mistral")
        self.timeout = config.get("timeout", 300)
        # Thinking-capable models (gemma4, qwen3.x) reason before answering by
        # default. Disabled here for structured output to keep responses
        # deterministic, faster, and free of <think>...</think> wrappers.
        self.think = config.get("think", False)
        # Default context covers system+taxonomy+schema+long scenes comfortably.
        # Non-thinking models (e.g. mistral) accept this without issue.
        self.num_ctx = config.get("num_ctx", 32768)

    async def generate(
        self,
        prompt: str,
        system_prompt: str | None = None,
        temperature: float = 0.7,
        max_tokens: int = 2000,
        **kwargs: Any,
    ) -> str:
        """Generate text using Ollama API with prompt injection protection."""
        # Sanitize and validate prompt
        try:
            clean_prompt = PromptSanitizer.validate_and_sanitize(prompt, raise_on_unsafe=True)
        except ValueError as e:
            raise LLMException(
                "Prompt blocked by security policy",
                details={"provider": "ollama", "reason": str(e)},
            )

        # Use default system prompt if none provided
        if system_prompt is None:
            system_prompt = (
                "You are a helpful film safety assistant analyzing scripts for potential risks."
            )

        # Lock system prompt to prevent override
        locked_system, final_prompt = PromptSanitizer.wrap_with_system_lock(
            clean_prompt, system_prompt
        )

        payload = {
            "model": self.model,
            "prompt": final_prompt,
            "system": locked_system,
            "stream": False,
            "options": {
                "temperature": temperature,
                "num_predict": max_tokens,
                "num_ctx": self.num_ctx,
            },
        }

        # Add any additional options
        payload["options"].update(kwargs)

        try:
            async with httpx.AsyncClient(timeout=self.timeout) as client:
                response = await client.post(
                    f"{self.base_url}/api/generate",
                    json=payload,
                )
                response.raise_for_status()
                result = response.json()
                return result["response"]

        except httpx.HTTPError as e:
            logger.error(f"Ollama API error: {e}")
            raise LLMException(
                f"Ollama API request failed: {str(e)}",
                details={"provider": "ollama", "base_url": self.base_url},
            )

    async def generate_chat(
        self,
        messages: list[dict[str, str]],
        temperature: float = 0.7,
        max_tokens: int = 2000,
        **kwargs: Any,
    ) -> str:
        """
        Generate text using Ollama Chat API.

        Args:
            messages: List of message dicts with 'role' and 'content'
            temperature: Sampling temperature
            max_tokens: Maximum tokens to generate
            **kwargs: Additional options

        Returns:
            Generated text
        """
        payload = {
            "model": self.model,
            "messages": messages,
            "stream": False,
            "options": {
                "temperature": temperature,
                "num_predict": max_tokens,
                "num_ctx": self.num_ctx,
            },
        }

        payload["options"].update(kwargs)

        try:
            async with httpx.AsyncClient(timeout=self.timeout) as client:
                response = await client.post(
                    f"{self.base_url}/api/chat",
                    json=payload,
                )
                response.raise_for_status()
                result = response.json()
                return result["message"]["content"]

        except httpx.HTTPError as e:
            logger.error(f"Ollama Chat API error: {e}")
            raise LLMException(
                f"Ollama Chat API request failed: {str(e)}",
                details={"provider": "ollama", "base_url": self.base_url},
            )

    async def generate_structured(
        self,
        prompt: str,
        schema: dict[str, Any],
        system_prompt: str | None = None,
        temperature: float = 0.7,
        **kwargs: Any,
    ) -> dict[str, Any]:
        """Generate structured output using Ollama's native schema-constrained format.

        Uses Ollama's GBNF-grammar-constrained output: the JSON schema is
        passed via the top-level ``format`` field so the model literally
        cannot emit invalid JSON. Thinking is disabled by default (via the
        top-level ``think`` parameter) for thinking-capable models like
        gemma4 / qwen3.x to keep responses clean, fast, and deterministic.
        """
        try:
            clean_prompt = PromptSanitizer.validate_and_sanitize(prompt, raise_on_unsafe=True)
        except ValueError as e:
            raise LLMException(
                "Prompt blocked by security policy",
                details={"provider": "ollama", "reason": str(e)},
            )

        effective_system_prompt = system_prompt
        if effective_system_prompt is None:
            effective_system_prompt = (
                "You are a helpful film safety assistant analyzing scripts for potential risks."
            )
        locked_system, final_prompt = PromptSanitizer.wrap_with_system_lock(
            clean_prompt, effective_system_prompt
        )

        messages = [
            {"role": "system", "content": locked_system},
            {"role": "user", "content": final_prompt},
        ]

        payload: dict[str, Any] = {
            "model": self.model,
            "messages": messages,
            "stream": False,
            "format": schema,
            "think": self.think,
            "options": {
                "temperature": temperature,
                "num_ctx": self.num_ctx,
            },
        }
        payload["options"].update(kwargs)

        try:
            async with httpx.AsyncClient(timeout=self.timeout) as client:
                response = await client.post(
                    f"{self.base_url}/api/chat",
                    json=payload,
                )
                response.raise_for_status()
                result = response.json()
        except httpx.HTTPError as e:
            logger.error(f"Ollama Chat API error: {e}")
            raise LLMException(
                f"Ollama Chat API request failed: {str(e)}",
                details={"provider": "ollama", "base_url": self.base_url},
            )

        response_text = (result.get("message", {}).get("content") or "").strip()

        # Safety net: some thinking-capable models embed <think>...</think>
        # blocks in their content despite think=False. Strip them before
        # parsing so they cannot break the JSON parser.
        response_text = self._strip_thinking_tags(response_text)

        # Safety net: strip markdown fences in case a model wraps the JSON
        # despite the format constraint.
        if response_text.startswith("```json"):
            response_text = response_text[7:]
        if response_text.startswith("```"):
            response_text = response_text[3:]
        if response_text.endswith("```"):
            response_text = response_text[:-3]
        response_text = response_text.strip()

        try:
            return json.loads(response_text)
        except json.JSONDecodeError as e:
            logger.error(f"Failed to parse JSON response: {e}\nResponse: {response_text}")
            raise LLMException(
                "Invalid JSON response from Ollama",
                details={"response": response_text, "error": str(e)},
            )

    @staticmethod
    def _strip_thinking_tags(text: str) -> str:
        """Remove <think>...</think> blocks from a model response."""
        return _THINKING_TAG_PATTERN.sub("", text).strip()

    async def health_check(self) -> bool:
        """Check Ollama availability."""
        try:
            async with httpx.AsyncClient(timeout=10) as client:
                response = await client.get(f"{self.base_url}/api/tags")
                return response.status_code == 200
        except Exception as e:
            logger.error(f"Ollama health check failed: {e}")
            return False

    async def list_models(self) -> list[str]:
        """
        List available models in Ollama.

        Returns:
            List of model names
        """
        try:
            async with httpx.AsyncClient(timeout=10) as client:
                response = await client.get(f"{self.base_url}/api/tags")
                response.raise_for_status()
                result = response.json()
                return [model["name"] for model in result.get("models", [])]
        except Exception as e:
            logger.error(f"Failed to list Ollama models: {e}")
            return []

    async def pull_model(self, model: str) -> bool:
        """
        Pull a model from Ollama registry.

        Args:
            model: Model name to pull

        Returns:
            True if successful
        """
        try:
            async with httpx.AsyncClient(timeout=600) as client:  # 10 min timeout
                response = await client.post(
                    f"{self.base_url}/api/pull",
                    json={"name": model, "stream": False},
                )
                response.raise_for_status()
                return True
        except Exception as e:
            logger.error(f"Failed to pull Ollama model {model}: {e}")
            return False

    @property
    def provider_name(self) -> str:
        """Get provider name."""
        return "ollama"
