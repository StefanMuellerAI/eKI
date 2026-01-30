"""Ollama local LLM provider."""

import json
import logging
from typing import Any

import httpx

from core.exceptions import LLMException
from core.prompt_sanitizer import PromptSanitizer
from llm.base import BaseLLMProvider

logger = logging.getLogger(__name__)


class OllamaProvider(BaseLLMProvider):
    """Ollama local LLM provider."""

    def __init__(self, config: dict[str, Any]) -> None:
        """Initialize Ollama provider."""
        super().__init__(config)
        self.base_url = config.get("base_url", "http://ollama:11434")
        self.model = config.get("model", "mistral")
        self.timeout = config.get("timeout", 120)

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
        clean_prompt = PromptSanitizer.validate_and_sanitize(prompt, raise_on_unsafe=False)

        # Use default system prompt if none provided
        if system_prompt is None:
            system_prompt = "You are a helpful film safety assistant analyzing scripts for potential risks."

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
        """Generate structured output using Ollama API."""
        # Build messages for chat endpoint
        messages = []

        if system_prompt:
            messages.append({"role": "system", "content": system_prompt})

        # Enhance prompt with JSON schema instruction
        enhanced_prompt = (
            f"{prompt}\n\n"
            f"Respond with valid JSON matching this schema:\n"
            f"```json\n{json.dumps(schema, indent=2)}\n```\n\n"
            f"Response (JSON only, no explanations):"
        )
        messages.append({"role": "user", "content": enhanced_prompt})

        # Use chat endpoint for better structured output
        response = await self.generate_chat(
            messages=messages,
            temperature=temperature,
            **kwargs,
        )

        # Extract JSON from response (handle markdown code blocks)
        response_text = response.strip()
        if response_text.startswith("```json"):
            response_text = response_text[7:]  # Remove ```json
        if response_text.startswith("```"):
            response_text = response_text[3:]  # Remove ```
        if response_text.endswith("```"):
            response_text = response_text[:-3]  # Remove ```
        response_text = response_text.strip()

        # Parse JSON response
        try:
            return json.loads(response_text)
        except json.JSONDecodeError as e:
            logger.error(f"Failed to parse JSON response: {e}\nResponse: {response_text}")
            raise LLMException(
                "Invalid JSON response from Ollama",
                details={"response": response_text, "error": str(e)},
            )

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
