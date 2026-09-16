"""Local Mistral provider for production inference (M11).

Pflichtenheft §4.3: "eKI arbeitet ohne externe Cloud, nur mit lokalem
Mistral Small/Medium" via an Ollama/llama.cpp HTTP endpoint. This adapter
is the production counterpart of :class:`llm.mistral_cloud.MistralCloudProvider`
and shares the transport with :class:`llm.ollama.OllamaProvider` (semaphore,
throttle, metrics, structured output) while adding:

* a fixed default model (``mistral-small3.2``) that can only be changed via
  ``LOCAL_MISTRAL_MODEL`` -- no accidental fallback to ``"mistral"``,
* embeddings config taken from the settings (was silently ignored before M11),
* ``health_check`` that verifies the configured model is actually pulled and
  ``ensure_model_available`` which produces an actionable error,
* ``describe()`` for parity reports and the ``eki_build_info`` metric.
"""

from __future__ import annotations

import logging
from typing import Any

import httpx

from core.exceptions import LLMException
from llm.ollama import OllamaProvider

logger = logging.getLogger(__name__)

DEFAULT_LOCAL_MODEL = "mistral-small3.2"


class LocalMistralProvider(OllamaProvider):
    """Ollama-backed Mistral adapter for the air-gapped production deployment."""

    def __init__(self, config: dict[str, Any]) -> None:
        config = dict(config)
        config["model"] = (config.get("model") or DEFAULT_LOCAL_MODEL).strip()
        super().__init__(config)
        self._model_checked = False

    @property
    def provider_name(self) -> str:
        return "local_mistral"

    # ------------------------------------------------------------------
    # Model availability
    # ------------------------------------------------------------------

    @staticmethod
    def _tag_matches(installed: str, wanted: str) -> bool:
        """``mistral-small3.2`` matches ``mistral-small3.2:latest`` and vice versa."""
        norm = lambda t: t[:-7] if t.endswith(":latest") else t  # noqa: E731
        return norm(installed) == norm(wanted)

    async def installed_models(self) -> list[str]:
        return await self.list_models()

    async def is_model_available(self) -> bool:
        models = await self.installed_models()
        return any(self._tag_matches(m, self.model) for m in models)

    async def ensure_model_available(self) -> None:
        """Raise ``LLMException`` with a pull hint when the model is missing."""
        if self._model_checked:
            return
        if not await self.is_model_available():
            raise LLMException(
                f"Local model '{self.model}' is not available on {self.base_url}. "
                f"Pull it first: `ollama pull {self.model}`",
                details={
                    "provider": self.provider_name,
                    "model": self.model,
                    "base_url": self.base_url,
                    "hint": f"ollama pull {self.model}",
                },
            )
        self._model_checked = True

    async def health_check(self) -> bool:
        """Endpoint reachable **and** configured model pulled."""
        try:
            async with httpx.AsyncClient(timeout=10) as client:
                response = await client.get(f"{self.base_url}/api/tags")
                if response.status_code != 200:
                    return False
                models = [m.get("name", "") for m in response.json().get("models", [])]
        except Exception as exc:
            logger.error("LocalMistral health check failed: %s", type(exc).__name__)
            return False
        available = any(self._tag_matches(m, self.model) for m in models)
        if not available:
            logger.error(
                "LocalMistral model '%s' not pulled on %s (installed: %s)",
                self.model,
                self.base_url,
                ", ".join(models) or "-",
            )
        return available

    # ------------------------------------------------------------------
    # Parity helpers
    # ------------------------------------------------------------------

    def describe(self) -> dict[str, Any]:
        return {
            "provider": self.provider_name,
            "model": self.model,
            "base_url": self.base_url,
            "num_ctx": self.num_ctx,
            "think": self.think,
            "embedding_model": self.embedding_model,
            "embedding_max_chars": self.embedding_max_chars,
            "external": False,
        }


__all__ = ["DEFAULT_LOCAL_MODEL", "LocalMistralProvider"]
