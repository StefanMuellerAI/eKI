> **Veraltet** – Stand Januar 2026 (M01), wird nicht mehr gepflegt. Maßgeblich sind die
> Schnellstart-Anleitung im [README](../README.md) und [LLM_INTEGRATION.md](LLM_INTEGRATION.md).
> **Widerspricht den Produktionsregeln:** Der Wechsel auf `mistral_cloud` ist nur für Stage/Test
> zulässig; die eKI liest außerdem in Docker `.env.local` (nicht `.env`), und `docker compose
> restart` übernimmt geänderte Werte nicht (`up -d --force-recreate api worker`).
>
> Seit 1.0.1 enthält der Compose-Stack keinen Ollama-Dienst mehr: Inferenz auf einer externen
> Instanz (`OLLAMA_BASE_URL`), lokal optional per `docker-compose.ollama.yml`.

# 🚀 Ollama Quick Start - 5 Minuten Setup

## Schritt 1: Services starten

```bash
cd eki-api
docker compose up -d
```

**Warten Sie ~30 Sekunden, bis alle Services bereit sind.**

## Schritt 2: Ollama-Modell herunterladen

```bash
# Mistral herunterladen (~4GB, dauert 2-5 Min je nach Internet)
docker exec -it eki-ollama ollama pull mistral
```

**Fortschritt wird angezeigt:**
```
pulling manifest
pulling 61e88e884507... 100%
...
success
```

## Schritt 3: Provider konfigurieren

```bash
# .env bearbeiten oder diese Defaults nutzen:
echo "LLM_PROVIDER=ollama" >> .env
echo "OLLAMA_MODEL=mistral" >> .env

# API und Worker neu starten
docker compose restart api worker
```

## Schritt 4: Testen! 🎉

```bash
docker compose exec api python scripts/test_llm.py
```

**Erfolgreiche Ausgabe:**
```
🔧 Testing LLM Provider: ollama
============================================================
✅ Provider initialized: ollama
🏥 Running health check...
✅ Provider is healthy
🤖 Testing text generation...
Response: Hello from eKI!
🤖 Testing with system prompt...
Response: 4
📊 Testing structured generation...
Structured response: {'answer': 'Yes, the sky is blue', 'confidence': 0.95}
📋 Available Ollama models:
  - mistral:latest
✅ All tests passed!
```

## Alternative: Mistral Cloud nutzen

Wenn Sie lieber Mistral Cloud verwenden:

```bash
# .env bearbeiten
echo "LLM_PROVIDER=mistral_cloud" >> .env
echo "MISTRAL_API_KEY=your-api-key" >> .env

# Ollama kann auskommentiert werden in docker-compose.yml
# Services neu starten
docker compose restart api worker

# Testen
docker compose exec api python scripts/test_llm.py
```

## Provider wechseln (jederzeit)

```bash
# Einfach .env ändern:
nano .env
# LLM_PROVIDER=ollama → LLM_PROVIDER=mistral_cloud

# Neu starten
docker compose restart api worker
```

## Troubleshooting

### "Model not found"
```bash
docker exec -it eki-ollama ollama list
# Wenn leer: ollama pull mistral
```

### "Connection refused"
```bash
docker compose ps ollama
# Sollte "Up" sein

docker compose logs ollama
# Logs prüfen
```

### Ollama ist langsam (CPU)
Das ist normal ohne GPU. Für Produktion:
- GPU-Server verwenden (10-20x schneller)
- Oder Mistral Cloud nutzen

## Fertig! 🎉

Sie können jetzt:
- ✅ Zwischen Ollama und Mistral Cloud wechseln
- ✅ Verschiedene Modelle testen
- ✅ Die API mit echten LLMs entwickeln

Weitere Infos:
- 📖 [docs/OLLAMA_SETUP.md](OLLAMA_SETUP.md) - Ausführliche Anleitung
- 📖 [docs/LLM_INTEGRATION.md](LLM_INTEGRATION.md) - API-Dokumentation
- 🔧 [scripts/test_llm.py](../scripts/test_llm.py) - Test-Script
