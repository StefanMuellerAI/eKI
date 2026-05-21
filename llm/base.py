"""Base class for LLM providers."""

from abc import ABC, abstractmethod
from typing import Any


class BaseLLMProvider(ABC):
    """Base class for all LLM providers."""

    def __init__(self, config: dict[str, Any]) -> None:
        """
        Initialize LLM provider.

        Args:
            config: Provider-specific configuration
        """
        self.config = config

    @abstractmethod
    async def generate(
        self,
        prompt: str,
        system_prompt: str | None = None,
        temperature: float = 0.7,
        max_tokens: int = 2000,
        **kwargs: Any,
    ) -> str:
        """
        Generate text from prompt.

        Args:
            prompt: User prompt
            system_prompt: Optional system prompt
            temperature: Sampling temperature (0.0-1.0)
            max_tokens: Maximum tokens to generate
            **kwargs: Additional provider-specific parameters

        Returns:
            Generated text
        """
        pass

    @abstractmethod
    async def generate_structured(
        self,
        prompt: str,
        schema: dict[str, Any],
        system_prompt: str | None = None,
        temperature: float = 0.7,
        **kwargs: Any,
    ) -> dict[str, Any]:
        """
        Generate structured output matching a JSON schema.

        Args:
            prompt: User prompt
            schema: JSON schema for output validation
            system_prompt: Optional system prompt
            temperature: Sampling temperature
            **kwargs: Additional provider-specific parameters

        Returns:
            Structured output matching schema
        """
        pass

    @abstractmethod
    async def health_check(self) -> bool:
        """
        Check if the LLM provider is available.

        Returns:
            True if provider is healthy, False otherwise
        """
        pass

    async def embed(self, text: str) -> list[float]:
        """Return an embedding vector for *text*.

        Optional capability used by the M06 knowledge base.  Providers
        that cannot embed (e.g. text-only Mistral Cloud chat models)
        raise :class:`NotImplementedError`; the KB service catches that
        and instructs the operator to switch ``EMBEDDING_PROVIDER``.

        Subclasses override this when they expose an embedding endpoint.
        """
        raise NotImplementedError(
            f"{self.provider_name} does not implement embeddings"
        )

    @property
    @abstractmethod
    def provider_name(self) -> str:
        """Get provider name."""
        pass
