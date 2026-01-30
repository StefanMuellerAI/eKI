#!/usr/bin/env python3
"""Test script for LLM providers."""

import asyncio
import sys
from pathlib import Path

# Add parent directory to path
sys.path.insert(0, str(Path(__file__).parent.parent))

from api.config import get_settings
from llm.factory import get_llm_provider, test_llm_provider


async def main() -> None:
    """Test LLM provider."""
    settings = get_settings()

    print(f"🔧 Testing LLM Provider: {settings.llm_provider}")
    print(f"=" * 60)

    try:
        # Create provider
        provider = get_llm_provider(settings)
        print(f"✅ Provider initialized: {provider.provider_name}")

        # Health check
        print(f"\n🏥 Running health check...")
        is_healthy = await provider.health_check()
        if is_healthy:
            print(f"✅ Provider is healthy")
        else:
            print(f"❌ Provider health check failed")
            return

        # Test simple generation
        print(f"\n🤖 Testing text generation...")
        response = await provider.generate(
            prompt="Say 'Hello from eKI!' and nothing else.",
            temperature=0.1,
            max_tokens=50,
        )
        print(f"Response: {response}")

        # Test with system prompt
        print(f"\n🤖 Testing with system prompt...")
        response = await provider.generate(
            prompt="What is 2+2?",
            system_prompt="You are a helpful math assistant. Answer concisely.",
            temperature=0.1,
            max_tokens=50,
        )
        print(f"Response: {response}")

        # Test structured generation
        print(f"\n📊 Testing structured generation...")
        schema = {
            "type": "object",
            "properties": {
                "answer": {"type": "string"},
                "confidence": {"type": "number"}
            },
            "required": ["answer", "confidence"]
        }

        structured_response = await provider.generate_structured(
            prompt="Is the sky blue? Answer with confidence 0-1.",
            schema=schema,
            temperature=0.1,
        )
        print(f"Structured response: {structured_response}")

        # If Ollama, show available models
        if provider.provider_name == "ollama":
            print(f"\n📋 Available Ollama models:")
            models = await provider.list_models()
            if models:
                for model in models:
                    print(f"  - {model}")
            else:
                print("  No models found. Pull a model first:")
                print("  docker exec -it eki-ollama ollama pull mistral")

        print(f"\n✅ All tests passed!")

    except Exception as e:
        print(f"\n❌ Error: {e}")
        import traceback
        traceback.print_exc()
        sys.exit(1)


if __name__ == "__main__":
    asyncio.run(main())
