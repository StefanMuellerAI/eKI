"""Local Mistral provider (via Ollama or similar)."""

import logging
from typing import Any

from llm.ollama import OllamaProvider

logger = logging.getLogger(__name__)


class LocalMistralProvider(OllamaProvider):
    """
    Local Mistral provider using Ollama.

    This is essentially an alias for OllamaProvider with Mistral model.
    """

    def __init__(self, config: dict[str, Any]) -> None:
        """Initialize Local Mistral provider."""
        # Force mistral model
        config["model"] = config.get("model", "mistral")
        super().__init__(config)

    @property
    def provider_name(self) -> str:
        """Get provider name."""
        return "local_mistral"
