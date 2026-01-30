"""Mistral Cloud API provider."""

import logging
from typing import Any

import httpx

from core.exceptions import LLMException
from core.prompt_sanitizer import PromptSanitizer
from llm.base import BaseLLMProvider

logger = logging.getLogger(__name__)


class MistralCloudProvider(BaseLLMProvider):
    """Mistral Cloud API provider."""

    def __init__(self, config: dict[str, Any]) -> None:
        """Initialize Mistral Cloud provider."""
        super().__init__(config)
        self.api_key = config.get("api_key")
        self.model = config.get("model", "mistral-large-latest")
        self.base_url = "https://api.mistral.ai/v1"
        self.timeout = config.get("timeout", 120)

        if not self.api_key:
            raise ValueError("Mistral API key is required")

    async def generate(
        self,
        prompt: str,
        system_prompt: str | None = None,
        temperature: float = 0.7,
        max_tokens: int = 2000,
        **kwargs: Any,
    ) -> str:
        """Generate text using Mistral Cloud API with prompt injection protection."""
        # Sanitize and validate prompt
        clean_prompt = PromptSanitizer.validate_and_sanitize(prompt, raise_on_unsafe=False)

        # Use default system prompt if none provided
        if system_prompt is None:
            system_prompt = "You are a helpful film safety assistant analyzing scripts for potential risks."

        # Lock system prompt to prevent override
        locked_system, final_prompt = PromptSanitizer.wrap_with_system_lock(
            clean_prompt, system_prompt
        )

        messages = [
            {"role": "system", "content": locked_system},
            {"role": "user", "content": final_prompt},
        ]

        payload = {
            "model": self.model,
            "messages": messages,
            "temperature": temperature,
            "max_tokens": max_tokens,
            **kwargs,
        }

        try:
            async with httpx.AsyncClient(timeout=self.timeout) as client:
                response = await client.post(
                    f"{self.base_url}/chat/completions",
                    json=payload,
                    headers={
                        "Authorization": f"Bearer {self.api_key}",
                        "Content-Type": "application/json",
                    },
                )
                response.raise_for_status()
                result = response.json()
                return result["choices"][0]["message"]["content"]

        except httpx.HTTPError as e:
            logger.error(f"Mistral Cloud API error: {e}")
            raise LLMException(
                f"Mistral Cloud API request failed: {str(e)}",
                details={"provider": "mistral_cloud"},
            )

    async def generate_structured(
        self,
        prompt: str,
        schema: dict[str, Any],
        system_prompt: str | None = None,
        temperature: float = 0.7,
        **kwargs: Any,
    ) -> dict[str, Any]:
        """Generate structured output using Mistral Cloud API."""
        # Add JSON schema instruction to prompt
        enhanced_prompt = f"{prompt}\n\nRespond with valid JSON matching this schema:\n{schema}"

        response = await self.generate(
            enhanced_prompt,
            system_prompt=system_prompt,
            temperature=temperature,
            **kwargs,
        )

        # Parse JSON response
        import json
        try:
            return json.loads(response)
        except json.JSONDecodeError as e:
            logger.error(f"Failed to parse JSON response: {e}")
            raise LLMException(
                "Invalid JSON response from Mistral",
                details={"response": response},
            )

    async def health_check(self) -> bool:
        """Check Mistral Cloud API availability."""
        try:
            async with httpx.AsyncClient(timeout=10) as client:
                response = await client.get(
                    f"{self.base_url}/models",
                    headers={"Authorization": f"Bearer {self.api_key}"},
                )
                return response.status_code == 200
        except Exception as e:
            logger.error(f"Mistral Cloud health check failed: {e}")
            return False

    @property
    def provider_name(self) -> str:
        """Get provider name."""
        return "mistral_cloud"
