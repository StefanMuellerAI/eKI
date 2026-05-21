"""Mistral Cloud API provider.

Production-grade structured output: uses Mistral's native
``response_format={"type": "json_object"}`` JSON mode, then validates
the response against the caller-supplied JSON Schema via ``jsonschema``.
On validation failure, performs exactly one self-correcting retry with
the failed JSON and the validator error appended to the prompt.

This module covers the M06 Pflichtenheft point "LLM-Adapter (Mistral API)"
and is the basis for the Stage-Sign-off (Abnahmetest 8).
"""

import json
import logging
from typing import Any

import httpx
from jsonschema import Draft202012Validator
from jsonschema.exceptions import ValidationError as JSONSchemaValidationError

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
        try:
            clean_prompt = PromptSanitizer.validate_and_sanitize(prompt, raise_on_unsafe=True)
        except ValueError as e:
            raise LLMException(
                "Prompt blocked by security policy",
                details={"provider": "mistral_cloud", "reason": str(e)},
            )

        if system_prompt is None:
            system_prompt = (
                "You are a helpful film safety assistant analyzing scripts for potential risks."
            )

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
        temperature: float = 0.2,
        **kwargs: Any,
    ) -> dict[str, Any]:
        """Schema-constrained output via Mistral JSON mode + post-hoc validation.

        Strategy:
        1. Sanitize prompt and lock the system message (prompt-injection guard).
        2. Embed the JSON Schema verbatim in the system prompt so the model
           knows the target structure.
        3. Request ``response_format={"type": "json_object"}`` — Mistral
           guarantees the response is *parseable* JSON.
        4. Validate the parsed JSON against the schema via ``jsonschema``.
        5. On ValidationError, perform exactly one retry that appends the
           failed JSON and the error to the prompt with a clear instruction
           to fix it.  This is bounded to keep latency and cost predictable.
        """
        try:
            clean_prompt = PromptSanitizer.validate_and_sanitize(prompt, raise_on_unsafe=True)
        except ValueError as e:
            raise LLMException(
                "Prompt blocked by security policy",
                details={"provider": "mistral_cloud", "reason": str(e)},
            )

        base_system = system_prompt or (
            "You are a helpful film safety assistant analyzing scripts for potential risks."
        )
        schema_str = json.dumps(schema, ensure_ascii=False, indent=2)
        schema_system = (
            f"{base_system}\n\n"
            "Respond with valid JSON only. The JSON MUST validate against this "
            f"JSON Schema (Draft 2020-12):\n```json\n{schema_str}\n```\n"
            "Do NOT include any explanatory text, markdown fences, or comments. "
            "Output ONLY the JSON object."
        )
        locked_system, final_prompt = PromptSanitizer.wrap_with_system_lock(
            clean_prompt, schema_system
        )

        validator = Draft202012Validator(schema)

        # First attempt
        raw = await self._chat_json_mode(
            system=locked_system,
            user=final_prompt,
            temperature=temperature,
            **kwargs,
        )
        parsed = self._parse_json(raw)
        validation_error = self._validate(validator, parsed)
        if validation_error is None:
            return parsed

        # Single self-correcting retry with the failed response in context
        logger.warning(
            "Mistral structured output failed schema validation, retrying once: %s",
            validation_error,
        )
        retry_user = (
            f"{final_prompt}\n\n"
            "Your previous response was NOT valid per the schema. "
            f"It produced this validation error:\n{validation_error}\n\n"
            "Previous JSON:\n"
            f"{json.dumps(parsed, ensure_ascii=False)}\n\n"
            "Please return a corrected JSON object that strictly matches the schema."
        )
        raw_retry = await self._chat_json_mode(
            system=locked_system,
            user=retry_user,
            temperature=temperature,
            **kwargs,
        )
        parsed_retry = self._parse_json(raw_retry)
        validation_error_retry = self._validate(validator, parsed_retry)
        if validation_error_retry is None:
            return parsed_retry

        raise LLMException(
            "Mistral structured output failed schema validation after retry",
            details={
                "provider": "mistral_cloud",
                "first_error": validation_error,
                "retry_error": validation_error_retry,
            },
        )

    async def _chat_json_mode(
        self,
        *,
        system: str,
        user: str,
        temperature: float,
        **kwargs: Any,
    ) -> str:
        """POST /chat/completions with response_format=json_object."""
        payload: dict[str, Any] = {
            "model": self.model,
            "messages": [
                {"role": "system", "content": system},
                {"role": "user", "content": user},
            ],
            "temperature": temperature,
            "response_format": {"type": "json_object"},
        }
        # Pass through caller overrides (e.g. max_tokens) but never let them
        # silently disable response_format -- structured callers depend on it.
        for k, v in kwargs.items():
            if k != "response_format":
                payload[k] = v

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
        except httpx.HTTPError as e:
            logger.error(f"Mistral Cloud structured API error: {e}")
            raise LLMException(
                f"Mistral Cloud API request failed: {str(e)}",
                details={"provider": "mistral_cloud"},
            )

        return (result["choices"][0]["message"]["content"] or "").strip()

    @staticmethod
    def _parse_json(text: str) -> dict[str, Any]:
        """Tolerant JSON parser: strip optional markdown fences, then parse."""
        cleaned = text.strip()
        if cleaned.startswith("```json"):
            cleaned = cleaned[7:]
        elif cleaned.startswith("```"):
            cleaned = cleaned[3:]
        if cleaned.endswith("```"):
            cleaned = cleaned[:-3]
        cleaned = cleaned.strip()
        try:
            return json.loads(cleaned)
        except json.JSONDecodeError as e:
            raise LLMException(
                "Invalid JSON response from Mistral",
                details={"response": cleaned[:1000], "error": str(e)},
            )

    @staticmethod
    def _validate(
        validator: Draft202012Validator, instance: dict[str, Any]
    ) -> str | None:
        """Return human-readable error message, or None if valid."""
        try:
            validator.validate(instance)
            return None
        except JSONSchemaValidationError as exc:
            path = ".".join(str(p) for p in exc.absolute_path) or "<root>"
            return f"At {path}: {exc.message}"

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
