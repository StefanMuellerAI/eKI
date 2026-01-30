# Ollama Integration Setup Guide

## Übersicht

Die eKI API unterstützt drei LLM-Provider:
1. **Mistral Cloud** - Cloud-basierte Mistral API (kostenpflichtig)
2. **Local Mistral** - Lokal via Ollama mit Mistral-Modell
3. **Ollama** - Lokal mit beliebigen Ollama-Modellen

## Quick Start mit Ollama

### 1. Ollama Service starten

Ollama ist bereits in `docker-compose.yml` integriert:

```bash
# Alle Services inkl. Ollama starten
docker compose up -d

# Status prüfen
docker compose ps ollama
```

### 2. Modell herunterladen

```bash
# Mistral herunterladen (empfohlen, ~4GB)
docker exec -it eki-ollama ollama pull mistral

# Alternativ: Andere Modelle
docker exec -it eki-ollama ollama pull llama2
docker exec -it eki-ollama ollama pull codellama
docker exec -it eki-ollama ollama pull mistral:7b-instruct
```

### 3. Provider konfigurieren

In `.env`:

```bash
# Option 1: Ollama mit Mistral (empfohlen)
LLM_PROVIDER=ollama
OLLAMA_BASE_URL=http://ollama:11434
OLLAMA_MODEL=mistral

# Option 2: Local Mistral (Alias für Ollama + Mistral)
LLM_PROVIDER=local_mistral
OLLAMA_BASE_URL=http://ollama:11434

# Option 3: Mistral Cloud
LLM_PROVIDER=mistral_cloud
MISTRAL_API_KEY=your-api-key-here
```

### 4. API neu starten

```bash
docker compose restart api worker
```

### 5. Testen

```bash
# Mit Docker
docker compose exec api python scripts/test_llm.py

# Oder lokal (wenn Dependencies installiert)
python scripts/test_llm.py
```

## Verfügbare Modelle

### Empfohlene Modelle für eKI

| Modell | Größe | Beschreibung | Pull-Befehl |
|--------|-------|--------------|-------------|
| **mistral** | 4.1GB | Bestes Preis-Leistungs-Verhältnis | `ollama pull mistral` |
| mistral:7b-instruct | 4.1GB | Optimiert für Instruktionen | `ollama pull mistral:7b-instruct` |
| llama2 | 3.8GB | Meta's LLaMA 2 | `ollama pull llama2` |
| llama2:13b | 7.4GB | Größeres LLaMA 2 | `ollama pull llama2:13b` |
| codellama | 3.8GB | Optimiert für Code | `ollama pull codellama` |

### Alle verfügbaren Modelle anzeigen

```bash
# Installierte Modelle
docker exec -it eki-ollama ollama list

# Verfügbare Modelle im Registry
# Siehe: https://ollama.com/library
```

## Provider Wechsel zur Laufzeit

Sie können jederzeit zwischen Providern wechseln:

```bash
# 1. .env bearbeiten
vim .env

# 2. Services neu starten
docker compose restart api worker

# 3. Testen
docker compose exec api python scripts/test_llm.py
```

## Performance & Hardware

### CPU-Only (Standard)

Funktioniert out-of-the-box, aber langsamer:
- Mistral 7B: ~15-30 Tokens/Sekunde
- Geeignet für Entwicklung und kleinere Workloads

### GPU-Beschleunigung (Optional)

Für Produktion empfohlen:

#### NVIDIA GPU

In `docker-compose.yml` auskommentieren:

```yaml
ollama:
  # ...
  deploy:
    resources:
      reservations:
        devices:
          - driver: nvidia
            count: 1
            capabilities: [gpu]
```

Voraussetzungen:
```bash
# NVIDIA Container Toolkit installieren
# Siehe: https://docs.nvidia.com/datacenter/cloud-native/container-toolkit/install-guide.html

# Docker neu starten
sudo systemctl restart docker

# Testen
docker run --rm --gpus all nvidia/cuda:11.8.0-base-ubuntu22.04 nvidia-smi
```

Performance mit GPU:
- Mistral 7B: ~100-200 Tokens/Sekunde
- 10-20x schneller als CPU

#### Apple Silicon (M1/M2/M3)

Ollama nutzt automatisch Metal:
```bash
# Keine spezielle Konfiguration nötig
# Performance: ~50-100 Tokens/Sekunde
```

## Monitoring & Debugging

### Ollama Logs anzeigen

```bash
docker compose logs -f ollama
```

### API-Endpunkte direkt testen

```bash
# Modelle auflisten
curl http://localhost:11434/api/tags

# Generation testen
curl http://localhost:11434/api/generate -d '{
  "model": "mistral",
  "prompt": "Hello, world!",
  "stream": false
}'
```

### Health Check

```bash
curl http://localhost:11434/api/tags
# Status 200 = Healthy
```

## Troubleshooting

### Problem: "Model not found"

**Lösung:** Modell herunterladen
```bash
docker exec -it eki-ollama ollama pull mistral
```

### Problem: Ollama startet nicht

**Diagnose:**
```bash
docker compose logs ollama
docker compose ps ollama
```

**Häufige Ursachen:**
- Nicht genug Speicherplatz (Modelle sind 3-7GB)
- Port 11434 bereits belegt

**Lösung:**
```bash
# Speicherplatz prüfen
df -h

# Port prüfen
lsof -i :11434

# Ollama neu starten
docker compose restart ollama
```

### Problem: Sehr langsam (CPU)

**Lösungen:**
1. GPU-Beschleunigung aktivieren (siehe oben)
2. Kleineres Modell verwenden
3. `OLLAMA_NUM_PARALLEL` erhöhen für Batch-Processing

### Problem: Out of Memory

**Lösungen:**
```bash
# Kleineres Modell verwenden
docker exec -it eki-ollama ollama pull mistral:7b-q4_0  # Quantisiert

# Oder Docker mehr RAM geben
# Docker Desktop -> Settings -> Resources -> Memory
```

## Provider-Vergleich

| Feature | Mistral Cloud | Ollama Local |
|---------|---------------|--------------|
| Setup | API-Key | Modell-Download |
| Kosten | Pay-per-token | Einmalig Hardware |
| Latenz | ~1-3s | ~0.1-1s |
| Privacy | Cloud | 100% lokal |
| Modelle | Mistral-Serie | 50+ Modelle |
| GPU | Nicht nötig | Empfohlen |
| Internet | Erforderlich | Optional |

## Best Practices

### Entwicklung
```bash
LLM_PROVIDER=ollama
OLLAMA_MODEL=mistral
# Schnell, kostenlos, lokal
```

### Staging
```bash
LLM_PROVIDER=mistral_cloud
# Gleiche Umgebung wie Produktion
```

### Produktion (Option A - Cloud)
```bash
LLM_PROVIDER=mistral_cloud
MISTRAL_API_KEY=<production-key>
# Einfach, skalierbar, kostenpflichtig
```

### Produktion (Option B - Self-hosted)
```bash
LLM_PROVIDER=ollama
OLLAMA_MODEL=mistral
# Mit GPU-Server
# Einmalige Kosten, volle Kontrolle
```

## Weitere Ressourcen

- **Ollama Dokumentation:** https://github.com/ollama/ollama
- **Modell-Library:** https://ollama.com/library
- **Mistral AI:** https://mistral.ai/
- **eKI API Docs:** `/docs/LLM_INTEGRATION.md`

## Support

Bei Fragen oder Problemen:
1. Logs prüfen: `docker compose logs ollama`
2. Health Check: `curl http://localhost:11434/api/tags`
3. Test-Skript: `python scripts/test_llm.py`
