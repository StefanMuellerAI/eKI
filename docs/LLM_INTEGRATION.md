# LLM Integration Guide

## Architektur

Die eKI API verwendet ein Provider-Pattern für LLM-Integration:

```
┌─────────────┐
│ API Endpoint│
└──────┬──────┘
       │
       ▼
┌─────────────────┐
│ LLM Factory     │
└──────┬──────────┘
       │
       ├─────────────────┬──────────────┬─────────────────┐
       ▼                 ▼              ▼                 ▼
┌──────────────┐  ┌─────────────┐  ┌──────────┐  ┌──────────┐
│Mistral Cloud │  │Local Mistral│  │  Ollama  │  │  Custom  │
└──────────────┘  └─────────────┘  └──────────┘  └──────────┘
```

## Provider-Implementierungen

### 1. Mistral Cloud Provider

**Verwendung:** Cloud-basierte Mistral AI API

```python
from llm.factory import get_llm_provider
from api.config import get_settings

settings = get_settings()
settings.llm_provider = "mistral_cloud"
settings.mistral_api_key = "your-api-key"

provider = get_llm_provider(settings)
response = await provider.generate("Your prompt here")
```

**Vorteile:**
- Keine lokale Hardware nötig
- Neueste Modelle
- Skalierbar

**Nachteile:**
- Kosten pro Token
- Internet erforderlich
- Datenschutz (Cloud)

### 2. Ollama Provider

**Verwendung:** Lokale LLMs via Ollama

```python
settings.llm_provider = "ollama"
settings.ollama_base_url = "http://ollama:11434"
settings.ollama_model = "mistral"

provider = get_llm_provider(settings)
response = await provider.generate("Your prompt here")
```

**Vorteile:**
- 100% lokal
- Keine laufenden Kosten
- Volle Datenkontrolle
- Viele Modelle verfügbar

**Nachteile:**
- Hardware-Anforderungen
- Initiales Setup
- Modell-Download nötig

### 3. Local Mistral Provider

**Verwendung:** Alias für Ollama mit Mistral-Modell

```python
settings.llm_provider = "local_mistral"
# Verwendet automatisch Mistral-Modell via Ollama
```

## API-Methoden

### Basic Generation

```python
response = await provider.generate(
    prompt="What is the capital of France?",
    system_prompt="You are a helpful geography assistant.",
    temperature=0.7,
    max_tokens=100
)
```

### Structured Generation

```python
schema = {
    "type": "object",
    "properties": {
        "risk_level": {"type": "string"},
        "confidence": {"type": "number"},
        "findings": {
            "type": "array",
            "items": {"type": "string"}
        }
    }
}

result = await provider.generate_structured(
    prompt="Analyze this script for safety risks: ...",
    schema=schema,
    temperature=0.3
)
# Returns: {"risk_level": "medium", "confidence": 0.85, "findings": [...]}
```

### Health Check

```python
is_healthy = await provider.health_check()
if not is_healthy:
    logger.error("Provider is unavailable")
```

## Konfiguration

### Environment Variables

```bash
# Provider auswählen
LLM_PROVIDER=ollama  # mistral_cloud, local_mistral, ollama

# Mistral Cloud
MISTRAL_API_KEY=sk-...
MISTRAL_MODEL=mistral-large-latest
MISTRAL_TIMEOUT=120

# Ollama
OLLAMA_BASE_URL=http://ollama:11434
OLLAMA_MODEL=mistral
OLLAMA_TIMEOUT=120
```

### Programmatische Konfiguration

```python
from api.config import Settings

settings = Settings(
    llm_provider="ollama",
    ollama_model="llama2",
    ollama_timeout=60
)
```

## Custom Provider erstellen

### 1. Provider-Klasse erstellen

```python
# llm/custom_provider.py
from llm.base import BaseLLMProvider

class CustomProvider(BaseLLMProvider):
    async def generate(self, prompt: str, **kwargs) -> str:
        # Ihre Implementierung
        pass

    async def generate_structured(self, prompt: str, schema: dict, **kwargs) -> dict:
        # Ihre Implementierung
        pass

    async def health_check(self) -> bool:
        # Ihre Implementierung
        pass

    @property
    def provider_name(self) -> str:
        return "custom"
```

### 2. Factory erweitern

```python
# llm/factory.py
from llm.custom_provider import CustomProvider

def get_llm_provider(settings: Settings) -> BaseLLMProvider:
    if settings.llm_provider == "custom":
        return CustomProvider(config={...})
    # ...
```

## Best Practices

### 1. Error Handling

```python
from core.exceptions import LLMException

try:
    response = await provider.generate(prompt)
except LLMException as e:
    logger.error(f"LLM error: {e.message}")
    # Fallback-Logik
```

### 2. Retry-Logik

```python
from tenacity import retry, stop_after_attempt, wait_exponential

@retry(
    stop=stop_after_attempt(3),
    wait=wait_exponential(multiplier=1, min=4, max=10)
)
async def generate_with_retry(provider, prompt):
    return await provider.generate(prompt)
```

### 3. Caching

```python
from functools import lru_cache

@lru_cache(maxsize=1000)
def get_cached_response(prompt_hash: str):
    # Cache für wiederholte Anfragen
    pass
```

### 4. Rate Limiting

```python
from aiolimiter import AsyncLimiter

rate_limiter = AsyncLimiter(max_rate=10, time_period=60)

async def generate_rate_limited(provider, prompt):
    async with rate_limiter:
        return await provider.generate(prompt)
```

## Testing

### Unit Tests

```python
import pytest
from llm.ollama import OllamaProvider

@pytest.mark.asyncio
async def test_ollama_generation():
    provider = OllamaProvider({
        "base_url": "http://localhost:11434",
        "model": "mistral"
    })

    response = await provider.generate("Test prompt")
    assert isinstance(response, str)
    assert len(response) > 0
```

### Integration Tests

```bash
# Test mit Docker
docker compose exec api python scripts/test_llm.py
```

## Performance-Optimierung

### 1. Batch Processing

```python
async def process_batch(provider, prompts: list[str]):
    tasks = [provider.generate(p) for p in prompts]
    return await asyncio.gather(*tasks)
```

### 2. Streaming (Ollama)

```python
# Für lange Antworten
async for chunk in provider.generate_stream(prompt):
    print(chunk, end='', flush=True)
```

### 3. Temperature-Tuning

```python
# Für deterministische Outputs
response = await provider.generate(prompt, temperature=0.1)

# Für kreative Outputs
response = await provider.generate(prompt, temperature=0.9)
```

## Monitoring

### Prometheus Metrics

```python
from prometheus_client import Counter, Histogram

llm_requests = Counter('llm_requests_total', 'Total LLM requests')
llm_latency = Histogram('llm_request_duration_seconds', 'LLM request latency')

@llm_latency.time()
async def monitored_generate(provider, prompt):
    llm_requests.inc()
    return await provider.generate(prompt)
```

### Logging

```python
import structlog

logger = structlog.get_logger()

logger.info(
    "llm_request",
    provider=provider.provider_name,
    prompt_length=len(prompt),
    temperature=temperature
)
```

## Siehe auch

- [Ollama Setup Guide](OLLAMA_SETUP.md)
- [API Documentation](../openapi/eki-api-v0.1.yaml)
- [Temporal Workflows](../workflows/README.md)
