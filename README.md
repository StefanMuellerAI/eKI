# eKI API - KI-gestützte Sicherheitsprüfung für Drehbücher

**Version:** 0.1.0 (Meilenstein M01)  
**Status:** ✅ Production Ready (Security Score: 8.5/10)  
**Auftraggeber:** Filmakademie Baden-Württemberg

## Projektübersicht

Die eKI ist eine modulare REST-API, die Drehbücher aus dem eProjekt-System der Filmakademie entgegennimmt, KI-gestützt auf Sicherheitsrisiken analysiert und strukturierte Reports zurückliefert. Die API fungiert als Processing-Only-Layer ohne dauerhafte Datenspeicherung von Drehbüchern oder Reports.

## 🔒 Security Features

Die API implementiert Enterprise-Grade-Sicherheit:

- ✅ **Authentifizierung**: Database-backed API Keys mit SHA-256 Hashing
- ✅ **Autorisierung**: Ownership-Checks, IDOR-Prevention
- ✅ **Input Validation**: Base64, SSRF, SQL Injection Prevention
- ✅ **Rate Limiting**: IP-based (60/min) + API key-based (1000/hour)
- ✅ **Prompt Injection Protection**: 15+ Pattern Detection
- ✅ **Secrets Management**: Docker Secrets, keine hardcoded Credentials
- ✅ **Production Hardening**: Debug-Mode aus, Swagger UI versteckt

📖 Details: [SECURITY_AUDIT_SUMMARY.md](SECURITY_AUDIT_SUMMARY.md)

## Kernprinzipien

- **Design-First:** OpenAPI 3.1.1 ist die Single Source of Truth
- **Processing-Only:** Keine persistente Speicherung von Drehbüchern/Reports (nur Audit-Metadaten)
- **Write-Through:** Ergebnisse werden sofort an eProjekt zurückgespielt
- **Minimalinvasiv:** Integration nur über REST, keine direkten DB-Zugriffe
- **Security-First:** Enterprise-Grade-Sicherheit von Anfang an

## Technologie-Stack

### Core
- **Framework:** Python 3.11+ mit FastAPI (ASGI)
- **Server:** Uvicorn + Gunicorn
- **Datenbank:** PostgreSQL 16 mit pgvector
- **Cache/Queue:** Redis 7 mit hiredis
- **Workflow-Engine:** Temporal 1.23.0

### LLM Integration
- **Ollama:** Lokale LLM-Inferenz (Mistral, Llama2, CodeLlama)
- **Mistral Cloud:** Cloud-basierte API
- **Provider Pattern:** Einfacher Wechsel zwischen Providern

### Security & Monitoring
- **Authentication:** JWT + Database API Keys
- **Rate Limiting:** Redis-backed
- **Observability:** Prometheus, OpenTelemetry
- **Logging:** Strukturierte JSON-Logs

### Deployment
- **Containerisierung:** Docker / Docker Compose
- **Secrets Management:** Docker Secrets + .env.local
- **Database Migrations:** Alembic

## Schnellstart

### Voraussetzungen

- Docker & Docker Compose
- Python 3.11+ (für lokale Entwicklung)

### Development Setup

```bash
# Repository klonen
cd eki-api

# Services starten
docker compose up -d

# API-Key erstellen
python scripts/create_api_key.py

# API testen
curl http://localhost:8000/health

# Swagger UI (nur Development)
open http://localhost:8000/docs
```

**Services:**
- API: http://localhost:8000
- Temporal UI: http://localhost:8080
- PostgreSQL: localhost:5432
- Redis: localhost:6379
- Ollama: http://localhost:11434

### Production Deployment

```bash
# 1. Secrets generieren
python scripts/generate_secrets.py

# 2. Environment konfigurieren
cp .env.example .env.local
# .env.local mit generierten Secrets füllen

# 3. Production Stack starten
docker compose -f docker-compose.yml -f docker-compose.prod.yml up -d

# 4. Migrationen ausführen
docker compose exec api alembic upgrade head

# 5. API-Keys erstellen
python scripts/create_api_key.py
```

📖 Vollständige Anleitung: [DEPLOYMENT_GUIDE.md](DEPLOYMENT_GUIDE.md)

## LLM Provider konfigurieren

Die API unterstützt drei LLM-Provider mit automatischem Prompt Injection Protection:

### Option 1: Ollama (Empfohlen für Development)

```bash
# In .env:
LLM_PROVIDER=ollama
OLLAMA_MODEL=mistral

# Modell herunterladen
docker exec eki-ollama ollama pull mistral

# Testen
docker compose exec api python scripts/test_llm.py
```

### Option 2: Mistral Cloud (Production)

```bash
# In .env.local:
LLM_PROVIDER=mistral_cloud
MISTRAL_API_KEY=your-api-key-here
```

### Option 3: Local Mistral

```bash
# In .env:
LLM_PROVIDER=local_mistral
```

## API-Endpunkte

### Security Endpoints

| Methode | Endpunkt | Auth | Beschreibung |
|---------|----------|------|--------------|
| `POST` | `/v1/security/check` | ✅ | Synchroner Check (≤1MB/50 Szenen) |
| `POST` | `/v1/security/check:async` | ✅ | Asynchroner Check (große Dateien) |
| `GET` | `/v1/security/jobs/{job_id}` | ✅ | Job-Status abfragen (mit Ownership-Check) |
| `GET` | `/v1/security/reports/{id}` | ✅ | One-Shot-Abholung (Pull-Modus) |

### System Endpoints

| Methode | Endpunkt | Auth | Beschreibung |
|---------|----------|------|--------------|
| `GET` | `/health` | - | Liveness-Probe |
| `GET` | `/ready` | - | Readiness-Probe mit Service-Status |
| `GET` | `/metrics` | - | Prometheus Metrics |

### Authentication

```bash
# Header erforderlich für alle /v1/security/* Endpoints
Authorization: Bearer eki_<your_api_key>
```

**API-Key erstellen:**
```bash
python scripts/create_api_key.py
```

**Rate Limits:**
- Unauthentifiziert: Keine (nur /health, /ready, /metrics)
- IP-based: 60 requests/Minute
- API Key-based: 1000 requests/Stunde

## Tests ausführen

```bash
# Alle Tests mit Coverage
./scripts/run_tests.sh

# Nur Security-Tests
pytest tests/test_security.py -v

# Nur API-Tests
pytest tests/test_api.py -v

# Mit Coverage-Report
pytest tests/ --cov --cov-report=html
open htmlcov/index.html
```

**Test-Coverage:**
- API: 94%
- Core: 96%
- Services: TBD
- Workflows: TBD

📖 Vollständige Anleitung: [TESTING_GUIDE.md](TESTING_GUIDE.md)

## Projektstruktur

```
eki-api/
├── api/                      # FastAPI Application
│   ├── main.py              # App-Instanz mit CORS & Error Handling
│   ├── config.py            # Pydantic Settings
│   ├── dependencies.py      # Dependency Injection & Auth
│   ├── rate_limiting.py     # Rate Limiting Middleware
│   └── routers/             # API-Routers
│       ├── health.py        # Health & Readiness Checks
│       └── security.py      # Security Check Endpoints
├── core/                    # Kernkomponenten
│   ├── models.py            # Pydantic Request/Response Schemas
│   ├── db_models.py         # SQLAlchemy Models (Audit, Jobs, Reports)
│   ├── exceptions.py        # Custom Exceptions
│   └── prompt_sanitizer.py  # Prompt Injection Protection
├── services/                # Business-Logik
├── workflows/               # Temporal Workflows
│   └── security_check.py    # Security Check Workflow
├── worker/                  # Temporal Worker
│   └── main.py             # Worker Entry Point
├── llm/                     # LLM Provider Abstraction
│   ├── base.py             # Base Provider Interface
│   ├── factory.py          # Provider Factory
│   ├── ollama.py           # Ollama Provider
│   ├── mistral_cloud.py    # Mistral Cloud Provider
│   └── local_mistral.py    # Local Mistral Alias
├── db/                      # Datenbank
│   ├── session.py          # Session Management
│   └── migrations/         # Alembic Migrations
├── tests/                   # Tests
│   ├── conftest.py         # Test Fixtures
│   ├── test_api.py         # API Endpoint Tests
│   ├── test_security.py    # Security Feature Tests
│   └── test_workflows.py   # Workflow Tests
├── scripts/                 # Utility Scripts
│   ├── generate_secrets.py # Secret Generation
│   ├── create_api_key.py   # API Key Creation
│   └── run_tests.sh        # Test Runner
├── openapi/                 # OpenAPI Specification
│   └── openapi.yaml        # OpenAPI 3.1.1 Schema
└── docker/                  # Docker Configuration
    ├── Dockerfile          # API Container
    └── Dockerfile.worker   # Worker Container
```

## Entwicklung

### Code-Qualität

```bash
# Linting
ruff check .

# Type checking
mypy .

# Formatting
ruff format .

# Security Scan
bandit -r api core services
```

### Datenbank

```bash
# Migration erstellen
docker compose exec api alembic revision --autogenerate -m "description"

# Migration ausführen
docker compose exec api alembic upgrade head

# Migration zurückrollen
docker compose exec api alembic downgrade -1

# Aktuellen Stand anzeigen
docker compose exec api alembic current
```

### Logs

```bash
# Alle Services
docker compose logs -f

# Nur API
docker compose logs -f api

# Nur Worker
docker compose logs -f worker

# Letzte 100 Zeilen
docker compose logs --tail=100 api
```

## Sicherheitsrichtlinien

### API-Key-Management

- API-Keys werden als SHA-256-Hash gespeichert (niemals Klartext)
- Keys haben Ablaufdatum und können deaktiviert werden
- Usage-Tracking für Monitoring und Abuse-Prevention
- Keys sind user- und optional organization-spezifisch

### Secrets

- **NIE** Secrets in Git committen
- `.env.local` ist in `.gitignore`
- `secrets/` Verzeichnis ist in `.gitignore`
- Docker Secrets für Production verwenden
- Secrets regelmäßig rotieren (empfohlen: 90 Tage)

### Input Validation

- Alle Inputs werden validiert (Pydantic)
- Base64-Encoding für Script-Content
- SSRF-Prevention durch IP-Blocking und Domain-Whitelist
- SQL-Injection-Prevention durch Parameterized Queries
- Prompt Injection Protection für LLM-Inputs

### Rate Limiting

- IP-based: 60 Requests/Minute (DoS-Prevention)
- API Key-based: 1000 Requests/Stunde (Abuse-Prevention)
- Redis-backed mit TTL
- Retry-After Headers bei Limit-Überschreitung

## Troubleshooting

### Container startet nicht

```bash
# Logs prüfen
docker compose logs api

# Container neu bauen
docker compose build api
docker compose up -d api
```

### Datenbank-Fehler

```bash
# PostgreSQL Status prüfen
docker compose exec postgres pg_isready

# Verbindung testen
docker compose exec -e PGPASSWORD=<password> postgres \
  psql -U eki_user -d eki_db -c "SELECT 1;"
```

### LLM-Provider-Fehler

```bash
# Ollama Status prüfen
docker compose exec ollama ollama list

# Ollama Logs
docker compose logs ollama

# LLM Provider testen
docker compose exec api python scripts/test_llm.py
```

### Tests schlagen fehl

```bash
# Dev-Dependencies installieren
pip install -e ".[dev]"

# Tests einzeln ausführen
pytest tests/test_security.py::TestAuthentication::test_valid_api_key_success -v

# Mit Debug-Output
pytest tests/ -v -s --tb=long
```

## Git Workflow

- `main` - Production-ready releases
- `develop` - Development branch
- Feature-Branches: `feature/beschreibung`
- Bugfix-Branches: `bugfix/beschreibung`

## Dokumentation

- **README.md** - Dieses Dokument
- **DEPLOYMENT_GUIDE.md** - Production Deployment
- **TESTING_GUIDE.md** - Testing & Test Development
- **SECURITY_AUDIT_SUMMARY.md** - Security Audit Ergebnisse
- **SECURITY_IMPLEMENTATION_COMPLETE.md** - Security Implementation Details
- **M01_COMPLETION_REPORT.md** - M01 Milestone Report
- **FINAL_SUMMARY.md** - Vollständige Zusammenfassung

## Meilensteine

- **M01** ✅ Projektgerüst, OpenAPI v0.1, Security Implementation (Abgeschlossen)
- **M02** 🔄 FDX-Parser & Szenenmodell
- **M03** 🔄 PDF-Parsing & OCR
- **M04** 🔄 Risiko-Taxonomie & Scoring
- **M06** 🔄 LLM-Adapter (erweitert: Mistral, Ollama)

## Status

- ✅ **M01 Acceptance Criteria:** Alle erfüllt
- ✅ **Security Score:** 8.5/10
- ✅ **Test Coverage:** 94%+
- ✅ **Documentation:** Vollständig
- ✅ **Production Ready:** Ja

## Lizenz

Proprietary - Filmakademie Baden-Württemberg

## Kontakt

Bei Fragen wenden Sie sich bitte an das Entwicklungsteam.
