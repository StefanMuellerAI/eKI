"""Base class for LLM providers and shared response-envelope helpers."""

from abc import ABC, abstractmethod
from collections.abc import Sequence
from typing import Any

from core.exceptions import LLMException


def malformed_response(provider: str, label: str, reason: str, **extra: Any) -> LLMException:
    """Build the ``LLMException`` for a 2xx response the provider cannot use.

    The message only carries a short type hint (*reason*) -- never the body,
    which may echo screenplay content.
    """
    return LLMException(
        f"{label} returned a malformed response ({reason})",
        details={"provider": provider, "reason": reason, **extra},
    )


def decode_json_body(response: Any, *, provider: str, label: str) -> Any:
    """Return ``response.json()`` or raise ``LLMException`` for a non-JSON body.

    *response* is an ``httpx.Response`` (typed loosely to keep this module
    free of transport imports).
    """
    try:
        return response.json()
    except ValueError:  # json.JSONDecodeError, UnicodeDecodeError
        raise malformed_response(
            provider, label, "non-JSON body", status_code=response.status_code
        ) from None


def require_field(
    data: Any,
    path: Sequence[str | int],
    *,
    provider: str,
    label: str,
    expected: type | tuple[type, ...] | None = None,
) -> Any:
    """Walk *path* through a decoded JSON envelope or raise ``LLMException``.

    Missing keys, empty lists and wrong container types all map to a
    "missing field" error; if *expected* is given, the leaf value must be an
    instance of it.
    """
    field = "".join(f"[{p}]" if isinstance(p, int) else f".{p}" for p in path).lstrip(".")
    current = data
    for key in path:
        try:
            current = current[key]
        except (KeyError, IndexError, TypeError):
            raise malformed_response(provider, label, f"missing field '{field}'") from None
    if expected is not None and not isinstance(current, expected):
        raise malformed_response(
            provider, label, f"field '{field}' has type {type(current).__name__}"
        )
    return current


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
        temperature: float = 0.2,
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
        raise NotImplementedError(f"{self.provider_name} does not implement embeddings")

    @property
    @abstractmethod
    def provider_name(self) -> str:
        """Get provider name."""
        pass
