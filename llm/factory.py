"""Factory for creating LLM providers."""

import logging
from typing import Any

from api.config import Settings
from llm.base import BaseLLMProvider
from llm.mistral_cloud import MistralCloudProvider
from llm.local_mistral import LocalMistralProvider
from llm.ollama import OllamaProvider

logger = logging.getLogger(__name__)


def get_llm_provider(settings: Settings) -> BaseLLMProvider:
    """
    Create LLM provider based on settings.

    Args:
        settings: Application settings

    Returns:
        Initialized LLM provider

    Raises:
        ValueError: If provider type is invalid
    """
    provider_type = settings.llm_provider.lower()

    logger.info(f"Initializing LLM provider: {provider_type}")

    if provider_type == "mistral_cloud":
        config = {
            "api_key": settings.mistral_api_key,
            "model": settings.mistral_model,
            "timeout": settings.mistral_timeout,
        }
        return MistralCloudProvider(config)

    elif provider_type == "local_mistral":
        config = {
            "base_url": settings.ollama_base_url,
            "model": "mistral",
            "timeout": settings.ollama_timeout,
        }
        return LocalMistralProvider(config)

    elif provider_type == "ollama":
        config = {
            "base_url": settings.ollama_base_url,
            "model": settings.ollama_model,
            "timeout": settings.ollama_timeout,
        }
        return OllamaProvider(config)

    else:
        raise ValueError(
            f"Invalid LLM provider: {provider_type}. "
            f"Valid options: mistral_cloud, local_mistral, ollama"
        )


async def test_llm_provider(provider: BaseLLMProvider) -> bool:
    """
    Test LLM provider with a simple generation.

    Args:
        provider: LLM provider to test

    Returns:
        True if test successful
    """
    try:
        # Health check
        if not await provider.health_check():
            logger.error(f"Provider {provider.provider_name} health check failed")
            return False

        # Simple generation test
        response = await provider.generate(
            prompt="Say 'Hello, eKI!' and nothing else.",
            temperature=0.1,
            max_tokens=50,
        )

        if response and len(response) > 0:
            logger.info(f"Provider {provider.provider_name} test successful")
            logger.debug(f"Test response: {response}")
            return True
        else:
            logger.error(f"Provider {provider.provider_name} returned empty response")
            return False

    except Exception as e:
        logger.error(f"Provider {provider.provider_name} test failed: {e}")
        return False
