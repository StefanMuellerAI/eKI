# eKI API -- KI-gestuetzte Sicherheitspruefung fuer Drehbuecher

**Version:** 0.2.0 (Meilenstein M02 abgeschlossen)
**Auftraggeber:** Filmakademie Baden-Wuerttemberg
**Auftragnehmer:** StefanAI -- Research & Development

## Projektuebersicht

Die eKI ist eine modulare REST-API, die Drehbuecher aus dem eProjekt-System der Filmakademie entgegennimmt, KI-gestuetzt auf Sicherheitsrisiken analysiert und strukturierte Reports zurueckliefert. Die API fungiert als Processing-Only-Layer ohne dauerhafte Datenspeicherung von Drehbuechern oder Reports.

### Kernprinzipien

- **Design-First:** OpenAPI 3.1.1 ist die Single Source of Truth
- **Processing-Only:** Keine persistente Speicherung von Drehbuechern/Reports (nur Audit-Metadaten)
- **Write-Through:** Ergebnisse werden sofort an eProjekt zurueckgespielt
- **Minimalinvasiv:** Integration nur ueber REST, keine direkten DB-Zugriffe
- **Security-First:** Verschluesselte transiente Daten, kein Klartext in Logs oder Workflow-History

---

## Architektur

```
                        ┌─────────────┐
                        │  eProjekt   │
                        │  (PHP/SQL)  │
                        └──────┬──────┘
                               │ REST (Bearer Auth)
                        ┌──────▼──────┐
                        │   eKI API   │ :8000
                        │  (FastAPI)  │
                        └──┬───┬───┬──┘
                           │   │   │
              ┌────────────┘   │   └────────────┐
              ▼                ▼                 ▼
     ┌────────────┐   ┌──────────────┐   ┌────────────┐
     │  Temporal   │   │    Redis     │   │ PostgreSQL │
     │  Workflow   │   │ SecureBuffer │   │  pgvector  │
     │   :7233     │   │ (AES-encr.) │   │   :5432    │
     └──────┬─────┘   └──────────────┘   └────────────┘
            │
     ┌──────▼──────┐
     │   Worker    │──▶ FDX/PDF Parser ──▶ LLM (Ollama/Mistral)
     └─────────────┘
```

### Datenfluss (Async)

1. **Upload**: API empfaengt Drehbuch (JSON/Base64 oder Multipart FDX-Upload)
2. **SecureBuffer**: Inhalt wird AES-verschluesselt in Redis gespeichert (TTL max 6h)
3. **Temporal**: Workflow erhaelt nur einen Redis-Referenzschluessel -- kein Skriptinhalt
4. **Parser**: Worker holt Daten aus Redis, parst FDX in strukturiertes Szenenmodell
5. **Analyse**: Risikoanalyse per LLM (Stub, echte Integration in M06)
6. **Delivery**: Report wird an eProjekt zurueckgespielt, alle Buffer-Keys geloescht

---

## Technologie-Stack

| Komponente | Technologie | Zweck |
|---|---|---|
| Framework | Python 3.11+ / FastAPI | REST-API (ASGI) |
| Workflow-Engine | Temporal 1.23.0 | Asynchrone Verarbeitung |
| Datenbank | PostgreSQL 16 + pgvector | Metadaten, Embeddings (spaeter) |
| Cache | Redis 7 | SecureBuffer, Rate Limiting |
| LLM | Ollama (Mistral Small 3.2) | Lokale Inferenz |
| LLM (Cloud) | Mistral Cloud API | Entwicklungsumgebung |
| Parser | defusedxml | Sicheres XML-Parsing (XXE-Schutz) |
| Verschluesselung | cryptography (Fernet) | AES-Verschluesselung transienter Daten |
| Container | Docker / Docker Compose | Deployment |
| Monitoring | Prometheus, OpenTelemetry | Metriken, Tracing |

---

## Schnellstart

### Voraussetzungen

- Docker & Docker Compose
- Python 3.11+ (fuer lokale Entwicklung)
- Optional: Ollama mit `mistral-small3.2` fuer lokale LLM-Inferenz

### Setup

```bash
# 1. Environment konfigurieren
cp .env.example .env.local
# .env.local mit sicheren Werten fuellen

# 2. Services starten
docker compose up -d

# 3. Migrationen ausfuehren
docker compose exec api alembic upgrade head

# 4. API-Key erstellen
docker compose exec api python scripts/create_api_key.py --insert

# 5. Testen
curl http://localhost:8000/health
```

### Services

| Service | URL | Beschreibung |
|---|---|---|
| API | http://localhost:8000 | FastAPI mit Swagger UI (`/docs`) |
| Temporal UI | http://localhost:8080 | Workflow-Monitoring |
| PostgreSQL | localhost:5432 | Datenbank (intern) |
| Redis | localhost:6379 | SecureBuffer + Cache (intern) |
| Ollama | localhost:11434 | Lokale LLM-Inferenz |

---

## API-Endpunkte

### Security Endpoints (Auth erforderlich)

| Methode | Endpunkt | Beschreibung |
|---|---|---|
| `POST` | `/v1/security/check` | Synchroner Check -- JSON (Base64) oder Multipart FDX-Upload |
| `POST` | `/v1/security/check:async` | Asynchroner Check via Temporal Workflow |
| `GET` | `/v1/security/jobs/{job_id}` | Job-Status abfragen (Ownership-Check) |
| `GET` | `/v1/security/reports/{id}` | One-Shot-Report-Abholung (Pull-Modus) |

### System Endpoints

| Methode | Endpunkt | Auth | Beschreibung |
|---|---|---|---|
| `GET` | `/` | Nein | API-Info |
| `GET` | `/health` | Nein | Liveness-Probe |
| `GET` | `/ready` | Ja | Readiness-Probe (DB, Redis, Temporal) |
| `GET` | `/metrics` | Ja | Prometheus Metrics |
| `GET` | `/docs` | Nein | Swagger UI (nur Development) |

### Authentication

```bash
# Bearer-Token fuer alle /v1/security/* Endpoints
curl -H "Authorization: Bearer eki_<your_api_key>" \
     http://localhost:8000/v1/security/check ...
```

### Upload-Formate

**JSON (Base64):**
```bash
curl -X POST http://localhost:8000/v1/security/check \
  -H "Authorization: Bearer eki_..." \
  -H "Content-Type: application/json" \
  -d '{"script_content": "<base64>", "project_id": "proj-1", "script_format": "fdx"}'
```

**Multipart (Datei-Upload):**
```bash
curl -X POST http://localhost:8000/v1/security/check \
  -H "Authorization: Bearer eki_..." \
  -F "file=@drehbuch.fdx" \
  -F "project_id=proj-1"
```

---

## FDX-Parser (M02)

Der Parser verarbeitet Final Draft XML (.fdx) Drehbuecher und extrahiert strukturierte Szenendaten.

### Unterstuetzte Elemente

| FDX Paragraph-Type | Extrahierte Information |
|---|---|
| `Scene Heading` | Location, Innen/Aussen, Tageszeit, Szenennummer |
| `Action` | Handlungsbeschreibung |
| `Character` | Sprechende Figur |
| `Dialogue` | Dialogtext |
| `Parenthetical` | Regieanweisung im Dialog |
| `Transition` | Szenenwechsel (CUT TO, FADE IN) |
| `Shot` | Kameraanweisung |

### Sprachunterstuetzung (Scene Headings)

| Deutsch | Englisch | Ergebnis |
|---|---|---|
| `INNEN. BUERO - TAG` | `INT. OFFICE - DAY` | LocationType.INT, TimeOfDay.DAY |
| `AUSSEN. WALD - NACHT` | `EXT. FOREST - NIGHT` | LocationType.EXT, TimeOfDay.NIGHT |
| `INNEN/AUSSEN. AUTO - DAEMMERUNG` | `INT./EXT. CAR - DUSK` | LocationType.INT_EXT, TimeOfDay.DUSK |

### XML-Sicherheit

Alle XML-Verarbeitung laeuft ueber `defusedxml`:
- XXE (XML External Entity) Angriffe blockiert
- Entity Expansion Bombs (Billion Laughs) blockiert
- DTD-Processing deaktiviert
- Groessenlimit: 10 MB

---

## Sicherheitsarchitektur

### Transiente Datenverarbeitung

Drehbuchinhalte werden **niemals** dauerhaft gespeichert:

- **SecureBuffer**: AES-verschluesselter Redis-Store (Fernet/HMAC-SHA256)
- **TTL**: Maximal 6 Stunden, dann automatische Loeschung
- **Temporal-Schutz**: Workflow-History enthaelt nur Redis-Referenzschluessel, keinen Klartext
- **Explizite Loeschung**: Buffer-Keys werden nach jeder Verarbeitungsstufe bereinigt

### API-Sicherheit

- **Authentifizierung**: Database-backed API Keys (SHA-256 Hashing, Ablaufdatum)
- **Autorisierung**: Ownership-Checks gegen IDOR-Angriffe
- **Input Validation**: Base64, SSRF-Prevention, SQL-Injection-Schutz
- **Rate Limiting**: IP-basiert (60/min) + API-Key-basiert (1000/h)
- **Prompt Injection Protection**: 15+ Muster-Erkennung fuer LLM-Inputs
- **Secrets Management**: Docker Secrets, `.env.local` (nie in Git)

---

## Projektstruktur

```
eKI_API/
├── api/                          # FastAPI Application
│   ├── main.py                   # App-Instanz, CORS, Error Handling
│   ├── config.py                 # Pydantic Settings (inkl. buffer_ttl)
│   ├── dependencies.py           # Dependency Injection, Auth
│   ├── rate_limiting.py          # Rate Limiting
│   └── routers/
│       ├── health.py             # Health & Readiness
│       └── security.py           # Security Endpoints (JSON + Multipart)
├── core/                         # Kernkomponenten
│   ├── models.py                 # Pydantic Schemas + Szenenmodell (M02)
│   ├── db_models.py              # SQLAlchemy Models (Audit, Jobs, Reports)
│   ├── exceptions.py             # Custom Exceptions
│   └── prompt_sanitizer.py       # Prompt Injection Protection
├── parsers/                      # Drehbuch-Parser (M02)
│   ├── base.py                   # Abstrakte ParserBase + Factory
│   ├── fdx.py                    # Final Draft XML Parser
│   ├── scene_heading.py          # Scene-Heading-Parser (DE/EN)
│   └── secure_xml.py             # defusedxml Wrapper (XXE-Schutz)
├── services/
│   ├── secure_buffer.py          # AES-verschluesselter Redis-Buffer (M02)
│   └── security_service.py       # Security Service (Stub)
├── workflows/                    # Temporal Workflows
│   ├── security_check.py         # SecurityCheckWorkflow (4 Activities)
│   └── activities.py             # Activities mit SecureBuffer-Integration
├── worker/
│   └── main.py                   # Temporal Worker Entry Point
├── llm/                          # LLM Provider Abstraktion
│   ├── base.py                   # BaseLLMProvider Interface
│   ├── factory.py                # Provider Factory
│   ├── ollama.py                 # Ollama Provider
│   ├── mistral_cloud.py          # Mistral Cloud Provider
│   └── local_mistral.py          # Local Mistral Alias
├── db/
│   ├── session.py                # Async Session Management
│   └── migrations/               # Alembic Migrations
├── tests/
│   ├── test_fdx_parser.py        # 41 Parser/Security/Buffer Tests (M02)
│   ├── test_api.py               # API Endpoint Tests
│   ├── test_security.py          # Security Feature Tests
│   ├── test_config.py            # Config Parsing Tests
│   ├── test_workflows.py         # Workflow Tests
│   └── fixtures/fdx/             # 12 synthetische FDX-Testdateien
├── scripts/                      # Utilities
├── openapi/                      # OpenAPI 3.1.1 Spezifikation
├── postman/                      # Postman Collection
├── docker/                       # Dockerfiles (API + Worker)
└── docker-compose.yml            # 7 Services
```

---

## Tests

```bash
# Alle Tests
pytest tests/ -v

# Nur FDX-Parser Tests (M02)
pytest tests/test_fdx_parser.py -v

# Nur Security-Tests
pytest tests/test_security.py -v

# Mit Coverage
pytest tests/ --cov --cov-report=html
```

### Testabdeckung (M02)

| Bereich | Tests | Beschreibung |
|---|---|---|
| Scene Heading Parser | 12 | DE/EN Formate, Edge Cases |
| Secure XML | 5 | XXE, Entity Bomb, Malformed, Oversize |
| FDX Parser | 12 | Alle Szenarien inkl. 55-Szenen-Performance |
| Parser Factory | 3 | Format-Routing, Fehlerbehandlung |
| SecureBuffer | 7 | Encrypt/Decrypt, TTL, Cleanup |
| Integration | 2 | Base64-Roundtrip, JSON-Serialisierung |
| **Gesamt** | **41** | Alle bestanden |

### Testdateien (FDX Fixtures)

12 synthetische Drehbuecher fuer verschiedene Szenarien:
- `simple_scene.fdx` -- Minimales Drehbuch (1 Szene)
- `multi_scene.fdx` -- 5 Szenen, Parentheticals
- `stunt_heavy.fdx` -- Physische Risiken (Stunts, Feuer, Hoehe)
- `psychological.fdx` -- Psychische Belastungen
- `german_format.fdx` -- Deutsche Szenenkoepfe (INNEN/AUSSEN/TAG/NACHT)
- `large_script.fdx` -- 55 Szenen (Performance-Test)
- `xxe_attack.fdx` -- XXE-Angriffsversuch (wird blockiert)
- `entity_bomb.fdx` -- Billion-Laughs-Angriff (wird blockiert)

---

## LLM Provider

Die API unterstuetzt drei LLM-Provider mit automatischem Prompt Injection Protection:

```bash
# Ollama (empfohlen fuer lokale Entwicklung/Produktion)
LLM_PROVIDER=ollama
OLLAMA_BASE_URL=http://host.docker.internal:11434
OLLAMA_MODEL=mistral-small3.2

# Mistral Cloud (Entwicklung)
LLM_PROVIDER=mistral_cloud
MISTRAL_API_KEY=your-key

# Local Mistral (Alias fuer Ollama)
LLM_PROVIDER=local_mistral
```

---

## Entwicklung

### Code-Qualitaet

```bash
ruff check .          # Linting
ruff format .         # Formatting
mypy .                # Type Checking
bandit -r api core    # Security Scan
```

### Datenbank

```bash
docker compose exec api alembic upgrade head       # Migrationen ausfuehren
docker compose exec api alembic revision --autogenerate -m "beschreibung"  # Neue Migration
docker compose exec api alembic downgrade -1       # Zurueckrollen
```

### Logs

```bash
docker compose logs -f api       # API-Logs
docker compose logs -f worker    # Worker-Logs (Parser-Aktivitaet)
docker compose logs --tail=50    # Letzte 50 Zeilen aller Services
```

---

## Meilensteine

| # | Meilenstein | Status | Artefakte |
|---|---|---|---|
| M01 | Projektgeruest & OpenAPI v0.1 | Abgeschlossen | API-Framework, Auth, CI/CD, Postman |
| M02 | Parser Basis (FDX) & Testdataset | Abgeschlossen | FDX-Parser, Szenenmodell, SecureBuffer, 41 Tests |
| M03 | PDF & Streaming-Parsing | Ausstehend | |
| M04 | Risiko-Taxonomie v1 & Scoring | Ausstehend | |
| M05 | Reports (JSON/PDF) & One-Shot-GET | Ausstehend | |
| M06 | LLM-Adapter (Mistral API) & KB | Ausstehend | |
| M07 | Grossdokument-Optimierung | Ausstehend | |
| M08 | Security/Privacy & Delete-on-Delivery | Ausstehend | |
| M09 | Observability & SLOs | Ausstehend | |
| M10 | Outbound-Adapter Hardening | Ausstehend | |
| M11 | Lokaler LLM-Adapter & Paritaetstests | Ausstehend | |
| M12 | UAT-Paket & Uebergabe | Ausstehend | |

---

## Lizenz

Proprietary -- Filmakademie Baden-Wuerttemberg

## Kontakt

StefanAI -- Research & Development
E-Mail: info@stefanai.de
