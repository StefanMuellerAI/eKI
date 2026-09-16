# ✅ Ollama Integration - Fertiggestellt

## Zusammenfassung

Die eKI API unterstützt jetzt **drei LLM-Provider** mit einfachem Wechsel:

1. **Mistral Cloud** - Cloud API (kostenpflichtig)
2. **Ollama** - Lokale LLMs (kostenlos)
3. **Local Mistral** - Ollama mit Mistral-Modell (empfohlen)

## Was wurde implementiert

### 📦 Provider-System

```
llm/
├── __init__.py              # Package exports
├── base.py                  # BaseLLMProvider (Abstract)
├── mistral_cloud.py         # Mistral Cloud API
├── ollama.py                # Ollama Provider
├── local_mistral.py         # Local Mistral (Ollama + Mistral)
└── factory.py               # Provider Factory Pattern
```

**Features pro Provider:**
- ✅ `generate()` - Text generation
- ✅ `generate_structured()` - JSON output mit Schema
- ✅ `health_check()` - Verfügbarkeitsprüfung
- ✅ Ollama-spezifisch: `list_models()`, `pull_model()`, `generate_chat()`

### 🐳 Docker Integration

```yaml
# docker-compose.yml
ollama:
  image: ollama/ollama:latest
  ports: ["11434:11434"]
  volumes: [ollama_data:/root/.ollama]
  # Optional: GPU-Support
```

### ⚙️ Konfiguration

```bash
# .env.example
LLM_PROVIDER=ollama               # mistral_cloud | local_mistral | ollama
OLLAMA_BASE_URL=http://ollama:11434
OLLAMA_MODEL=mistral
OLLAMA_TIMEOUT=120
```

### 🧪 Test-Tools

```bash
# scripts/test_llm.py
docker compose exec api python scripts/test_llm.py
```

**Testet:**
- Provider Initialization
- Health Check
- Text Generation
- Structured Output
- System Prompts
- Model Listing (Ollama)

### 📚 Dokumentation

1. **docs/OLLAMA_SETUP.md**
   - Quick Start
   - Modell-Downloads
   - GPU-Setup
   - Troubleshooting

2. **docs/LLM_INTEGRATION.md**
   - Provider API
   - Custom Provider erstellen
   - Best Practices
   - Performance-Tuning

3. **README.md**
   - LLM Provider Schnellstart

## Quick Start

### 1. Services starten

```bash
docker compose up -d
```

### 2. Ollama-Modell herunterladen

```bash
# Mistral (empfohlen, 4GB)
docker exec -it eki-ollama ollama pull mistral

# Oder andere Modelle
docker exec -it eki-ollama ollama pull llama2
docker exec -it eki-ollama ollama pull codellama
```

### 3. Provider in .env setzen

```bash
# .env
LLM_PROVIDER=ollama
OLLAMA_MODEL=mistral
```

### 4. API neu starten

```bash
docker compose restart api worker
```

### 5. Testen

```bash
docker compose exec api python scripts/test_llm.py
```

**Erwartete Ausgabe:**
```
🔧 Testing LLM Provider: ollama
============================================================
✅ Provider initialized: ollama
🏥 Running health check...
✅ Provider is healthy
🤖 Testing text generation...
Response: Hello from eKI!
...
✅ All tests passed!
```

## Provider wechseln

Jederzeit zwischen Providern wechseln:

```bash
# Option 1: Ollama (lokal, kostenlos)
LLM_PROVIDER=ollama
OLLAMA_MODEL=mistral

# Option 2: Mistral Cloud
LLM_PROVIDER=mistral_cloud
MISTRAL_API_KEY=sk-...

# Option 3: Local Mistral (Alias)
LLM_PROVIDER=local_mistral

# Neu starten
docker compose restart api worker
```

## API-Verwendung

### In Workflows/Activities

```python
from llm.factory import get_llm_provider
from api.config import get_settings

settings = get_settings()
provider = get_llm_provider(settings)

# Einfache Generation
response = await provider.generate(
    prompt="Analyze this script scene for safety risks: ...",
    system_prompt="You are a safety expert for film productions.",
    temperature=0.3,
)

# Strukturierte Ausgabe
schema = {
    "type": "object",
    "properties": {
        "risk_level": {"type": "string", "enum": ["low", "medium", "high"]},
        "findings": {"type": "array", "items": {"type": "string"}},
        "confidence": {"type": "number"},
    },
}

result = await provider.generate_structured(prompt="Analyze: ...", schema=schema)
# Returns: {"risk_level": "medium", "findings": [...], "confidence": 0.85}
```

## Performance

### CPU (Standard)

- **Mistral 7B:** ~15-30 tokens/s
- **LLaMA 2:** ~10-25 tokens/s
- Geeignet für: Entwicklung, kleine Workloads

### GPU (Optional)

Für Produktion aktivieren:

```yaml
# docker-compose.override.yml
services:
  ollama:
    deploy:
      resources:
        reservations:
          devices:
            - driver: nvidia
              count: 1
              capabilities: [gpu]
```

Performance mit NVIDIA GPU:
- **Mistral 7B:** ~100-200 tokens/s (10x schneller)
- **LLaMA 2 13B:** ~50-100 tokens/s

### Apple Silicon (M1/M2/M3)

Nutzt automatisch Metal:
- **Mistral 7B:** ~50-100 tokens/s
- Keine zusätzliche Konfiguration nötig

## Verfügbare Modelle

| Modell | Größe | Beschreibung | Command |
|--------|-------|--------------|---------|
| **mistral** | 4.1GB | Empfohlen | `ollama pull mistral` |
| mistral:7b-instruct | 4.1GB | Optimiert für Instructions | `ollama pull mistral:7b-instruct` |
| llama2 | 3.8GB | Meta's LLaMA 2 | `ollama pull llama2` |
| llama2:13b | 7.4GB | Größeres Modell | `ollama pull llama2:13b` |
| codellama | 3.8GB | Code-optimiert | `ollama pull codellama` |

Siehe: https://ollama.com/library

## Monitoring

```bash
# Ollama Logs
docker compose logs -f ollama

# API Logs
docker compose logs -f api

# Alle verfügbaren Modelle
docker exec -it eki-ollama ollama list

# Ollama Health Check
curl http://localhost:11434/api/tags
```

## Integration in M06

Wenn M06 (LLM-Adapter) implementiert wird, sind die Provider bereits fertig:

```python
# workflows/activities.py
from llm.factory import get_llm_provider


@activity.defn(name="analyze_risks")
async def analyze_risks_activity(parsed_data: dict) -> dict:
    settings = get_settings()
    provider = get_llm_provider(settings)

    # Echte LLM-Analyse statt Stub
    result = await provider.generate_structured(
        prompt=f"Analyze scenes for safety risks: {parsed_data['scenes']}",
        schema=RISK_SCHEMA,
        system_prompt=SAFETY_EXPERT_PROMPT,
    )

    return result
```

## Vorteile

### Entwicklung
✅ Kostenlos (keine API-Kosten)
✅ Offline-fähig
✅ Schnelle Iteration
✅ 100% Datenschutz

### Produktion
✅ Flexible Provider-Wahl
✅ Cloud oder Self-hosted
✅ Keine Vendor Lock-in
✅ Einfacher Wechsel bei Bedarf

## Nächste Schritte

1. ✅ Provider-System implementiert
2. ✅ Ollama integriert
3. ✅ Dokumentation erstellt
4. ⏳ M02-M04: Parser & Risiko-Modell
5. ⏳ M06: LLM-Integration in Activities

---

**Status:** ✅ Vollständig implementiert und getestet
**Dokumentation:** docs/OLLAMA_SETUP.md, docs/LLM_INTEGRATION.md
**Test-Script:** scripts/test_llm.py
