# eKI API -- KI-gestuetzte Sicherheitspruefung fuer Drehbuecher

**Version:** 0.4.0 (Meilenstein M04 abgeschlossen)
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

### Zwei getrennte Workflows (M03)

Die Verarbeitung ist formatspezifisch aufgeteilt:

**FDX-Workflow** (strukturiertes XML):
```
parse_fdx → analyze_scene_risk x N → aggregate_report → deliver
```

**PDF-Workflow** (unstrukturiert, LLM-gestuetzt):
```
extract_text → split_scenes → structure_scene_llm x N → aggregate_script
  → analyze_scene_risk x N → aggregate_report → deliver
```

Beide Workflows teilen sich die Risikoanalyse (pro Szene) und Delivery-Activities.

### Datenfluss

1. **Upload**: API empfaengt Drehbuch (JSON/Base64 oder Multipart FDX/PDF-Upload)
2. **SecureBuffer**: Inhalt wird AES-verschluesselt in Redis gespeichert (TTL max 6h)
3. **Temporal**: Workflow erhaelt nur einen Redis-Referenzschluessel -- kein Skriptinhalt
4. **Parser**: FDX wird direkt geparsed, PDF wird per INT/EXT-Split + LLM strukturiert
5. **Risikoanalyse**: Jede Szene wird einzeln per LLM auf physische, umgebungs- und psychische Risiken geprueft
6. **Report**: Findings werden programmatisch aggregiert (risk_summary, Zaehler)
7. **Delivery**: Report wird an eProjekt zurueckgespielt, alle Buffer-Keys geloescht

---

## Technologie-Stack

| Komponente | Technologie | Zweck |
|---|---|---|
| Framework | Python 3.11+ / FastAPI | REST-API (ASGI) |
| Workflow-Engine | Temporal 1.23.0 | Asynchrone Verarbeitung mit Activities |
| Datenbank | PostgreSQL 16 + pgvector | Metadaten, Embeddings (spaeter) |
| Cache | Redis 7 | SecureBuffer, Rate Limiting |
| LLM | Ollama (Mistral Small 3.2) | Lokale Inferenz (Strukturierung + Risikoanalyse) |
| LLM (Cloud) | Mistral Cloud API | Entwicklungsumgebung |
| FDX-Parser | defusedxml | Sicheres XML-Parsing (XXE-Schutz) |
| PDF-Parser | pdfplumber (MIT) | Text-Extraktion aus PDFs |
| Prompt-Management | YAML + PromptManager | Versionierbare LLM-Prompts |
| Verschluesselung | cryptography (Fernet) | AES-Verschluesselung transienter Daten |
| Container | Docker / Docker Compose | Deployment |
| Monitoring | Prometheus, OpenTelemetry | Metriken, Tracing |

---

## Schnellstart

### Voraussetzungen

- Docker & Docker Compose
- Python 3.11+ (fuer lokale Entwicklung)
- Ollama mit `mistral-small3.2` fuer LLM-Inferenz

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
| `POST` | `/v1/security/check` | Synchroner Check -- JSON (Base64) oder Multipart FDX/PDF-Upload |
| `POST` | `/v1/security/check:async` | Asynchroner Check via Temporal Workflow (FDX oder PDF) |
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

### Upload-Formate

**JSON (Base64):**
```bash
curl -X POST http://localhost:8000/v1/security/check:async \
  -H "Authorization: Bearer eki_..." \
  -H "Content-Type: application/json" \
  -d '{"script_content": "<base64>", "project_id": "proj-1", "script_format": "fdx"}'
```

**Multipart (Datei-Upload):**
```bash
curl -X POST http://localhost:8000/v1/security/check:async \
  -H "Authorization: Bearer eki_..." \
  -F "file=@drehbuch.pdf" \
  -F "project_id=proj-1" \
  -F "script_format=pdf"
```

---

## FDX-Parser (M02)

Verarbeitet Final Draft XML (.fdx) Drehbuecher mit `defusedxml` (XXE-Schutz). Alle Paragraph-Typen werden extrahiert: Scene Heading, Action, Character, Dialogue, Parenthetical, Transition, Shot. Deutsche und englische Scene Headings werden unterstuetzt (INT/EXT, INNEN/AUSSEN).

## PDF-Parser (M03)

Verarbeitet PDF-Drehbuecher in drei Schritten:

1. **Text-Extraktion** (pdfplumber): Seitenweise, in-memory, OCR-Erkennung fuer gescannte Seiten
2. **Deterministischer Split** (Regex): Zuverlaessige Aufteilung an INT/EXT/INNEN/AUSSEN-Markern
3. **LLM-Strukturierung** (Ollama/Mistral Structured Output): Jeder Szenenblock wird per KI in das ParsedScene-Schema ueberfuehrt -- Location, Characters, Dialogue, Action

IDs, Zaehler und der Character-Index werden programmatisch vergeben, nicht von der KI.

## Risikoanalyse mit Taxonomie (M04)

Jede Szene wird **einzeln** per LLM auf Sicherheitsrisiken geprueft. Das LLM bekommt die formalisierte Risiko-Taxonomie als Kontext und liefert strukturierte Findings zurueck.

### Risiko-Kategorien und Klassen (23 Klassen)

- **PHYSICAL** (13): STUNTS, FALLS, FIGHTS, WEAPONS, VEHICLES, HEIGHT, WATER, FIRE, ELECTRICAL, ANIMALS, WEATHER, FATIGUE, CROWD
- **ENVIRONMENTAL** (4): DANGEROUS_LOCATION, CONFINED_SPACE, SMOKE_DUST, NOISE
- **PSYCHOLOGICAL** (6): VIOLENCE, DEATH_GRIEF, TRAUMA, SEXUALIZED, DISCRIMINATION, INTIMACY

### Scoring-Engine

Severity wird aus `Likelihood x Impact` (je 1-5) berechnet:

| Score | Severity |
|---|---|
| >= 16 | critical (z.B. 4x4, 5x5) |
| >= 10 | high (z.B. 2x5, 3x4) |
| >= 5 | medium (z.B. 1x5, 3x2) |
| >= 2 | low (z.B. 2x1) |
| < 2 | info (1x1) |

### Massnahmenkatalog (20 kodifizierte Massnahmen)

Jedes Finding enthaelt konkrete Massnahmen mit Code, Titel, verantwortlicher Rolle und Frist:

```
RIG-SAFETY      -> Stunt Coordination, shooting-3d
SFX-CLEARANCE   -> SFX Supervisor, shooting-5d
INTIMACY-COORD  -> Intimacy Coordination, pre-production
PSY-BRIEFING    -> Production, shooting-0d
WEAPON-CHECK    -> Weapons Master, shooting-1d
MEDICAL-STANDBY -> Production, shooting-1d
...
```

Taxonomie und Massnahmen liegen in `config/taxonomy/` als YAML und sind ohne Code-Aenderung anpassbar.

## Prompt-Management

Alle LLM-Prompts werden zentral in `config/prompts/prompts.yaml` verwaltet:

```yaml
pdf_structuring:        # Szenen-Strukturierung aus PDF-Rohtext
  scene: { system: ..., user: ... }
  preamble: { system: ..., user: ... }

risk_analysis:          # Risikoanalyse pro Szene
  scene: { system: ..., user: ... }

report_summary:         # Executive Summary (optional)
  executive: { system: ..., user: ... }
```

Prompts koennen angepasst werden ohne Code-Aenderung. Variablen (`{scene_text}`, `{location}`, etc.) werden zur Laufzeit substituiert.

---

## Sicherheitsarchitektur

### Transiente Datenverarbeitung

- **SecureBuffer**: AES-verschluesselter Redis-Store (Fernet/HMAC-SHA256)
- **TTL**: Maximal 6 Stunden, dann automatische Loeschung
- **Temporal-Schutz**: Workflow-History enthaelt nur Redis-Referenzschluessel, keinen Klartext
- **Explizite Loeschung**: Buffer-Keys werden nach jeder Verarbeitungsstufe bereinigt

### API-Sicherheit

- **Authentifizierung**: Database-backed API Keys (SHA-256 Hashing, Ablaufdatum)
- **Autorisierung**: Ownership-Checks gegen IDOR-Angriffe
- **Input Validation**: Base64 (FDX), PDF-Magic-Byte-Check, SSRF-Prevention
- **Rate Limiting**: IP-basiert (60/min) + API-Key-basiert (1000/h)
- **Prompt Injection Protection**: 15+ Muster-Erkennung fuer LLM-Inputs
- **XML-Sicherheit**: defusedxml (XXE, Entity Bombs, DTD blockiert)

---

## Projektstruktur

```
eKI_API/
├── api/                          # FastAPI Application
│   ├── main.py                   # App-Instanz, CORS, Error Handling
│   ├── config.py                 # Pydantic Settings
│   ├── dependencies.py           # Dependency Injection, Auth
│   ├── rate_limiting.py          # Rate Limiting
│   └── routers/
│       ├── health.py             # Health & Readiness
│       └── security.py           # Security Endpoints (JSON + Multipart)
├── config/
│   ├── prompts/
│   │   └── prompts.yaml          # Zentrale LLM-Prompt-Verwaltung (M03)
│   └── taxonomy/
│       ├── taxonomy.yaml         # Risiko-Taxonomie: 23 Klassen, Rule-IDs (M04)
│       └── measures.yaml         # Massnahmenkatalog: 20 Codes mit Rollen (M04)
├── core/                         # Kernkomponenten
│   ├── models.py                 # Pydantic Schemas + Szenenmodell + Confidence
│   ├── db_models.py              # SQLAlchemy Models
│   ├── exceptions.py             # Custom Exceptions
│   └── prompt_sanitizer.py       # Prompt Injection Protection
├── parsers/                      # Drehbuch-Parser
│   ├── base.py                   # Async ParserBase + Factory
│   ├── fdx.py                    # Final Draft XML Parser (M02)
│   ├── pdf.py                    # PDF Parser mit LLM-Strukturierung (M03)
│   ├── pdf_scene_splitter.py     # Deterministischer INT/EXT-Split (M03)
│   ├── pdf_llm_structurer.py     # LLM Structured Output pro Szene (M03)
│   ├── scene_heading.py          # Scene-Heading-Parser (DE/EN)
│   └── secure_xml.py             # defusedxml Wrapper
├── services/
│   ├── secure_buffer.py          # AES-verschluesselter Redis-Buffer
│   ├── taxonomy.py               # TaxonomyManager: Scoring, Validierung (M04)
│   └── security_service.py       # Security Service
├── workflows/                    # Temporal Workflows
│   ├── security_check.py         # FDX/PDF Workflow-Router (M03)
│   └── activities.py             # 8 Activities inkl. LLM-Risikoanalyse (M03)
├── worker/
│   └── main.py                   # Temporal Worker (8 Activities registriert)
├── llm/                          # LLM Provider Abstraktion
│   ├── base.py                   # BaseLLMProvider Interface
│   ├── factory.py                # Provider Factory
│   ├── prompt_manager.py         # YAML Prompt Loader (M03)
│   ├── ollama.py                 # Ollama Provider
│   ├── mistral_cloud.py          # Mistral Cloud Provider
│   └── local_mistral.py          # Local Mistral Alias
├── db/
│   ├── session.py                # Async Session Management
│   └── migrations/               # Alembic Migrations
├── tests/
│   ├── test_fdx_parser.py        # 41 FDX/Security/Buffer Tests
│   ├── test_pdf_parser.py        # 32 PDF/Splitter/LLM/PromptManager Tests
│   ├── test_taxonomy.py          # 38 Taxonomie/Scoring/Measures/Validation Tests (M04)
│   ├── test_api.py               # API Endpoint Tests
│   ├── test_security.py          # Security Feature Tests
│   ├── test_config.py            # Config Parsing Tests
│   ├── fixtures/fdx/             # 12 synthetische FDX-Testdateien
│   └── fixtures/pdf/             # 5 synthetische PDF-Testdateien
├── scripts/                      # Utilities
├── openapi/                      # OpenAPI 3.1.1 Spezifikation
├── postman/                      # Postman Collection
├── docker/                       # Dockerfiles (API + Worker)
└── docker-compose.yml            # 7 Services
```

---

## Tests

```bash
# Alle Parser-Tests (FDX + PDF)
pytest tests/test_fdx_parser.py tests/test_pdf_parser.py -v

# Nur PDF-Tests
pytest tests/test_pdf_parser.py -v

# Mit Coverage
pytest tests/ --cov --cov-report=html
```

### Testabdeckung

| Bereich | Tests | Beschreibung |
|---|---|---|
| Scene Heading Parser | 12 | DE/EN Formate, Edge Cases |
| Secure XML | 5 | XXE, Entity Bomb, Malformed, Oversize |
| FDX Parser | 12 | Alle Szenarien inkl. 55-Szenen-Performance |
| PDF Scene Splitter | 12 | INT/EXT Split, Praeambel, DE/EN, reale PDFs |
| PDF Text Extraction | 5 | pdfplumber, Oversize, Invalid, Benchmark |
| LLM Structurer | 5 | Schema-Validierung, Fallback, Mock |
| PromptManager | 6 | YAML laden, Variablen, Fehlerbehandlung |
| Parser Factory | 5 | FDX + PDF Routing |
| SecureBuffer | 7 | Encrypt/Decrypt, TTL, Cleanup |
| Taxonomy Loading | 6 | YAML laden, Klassen pruefen, Zaehlung |
| Class Lookups | 4 | Rule-IDs, Kategorien, Case-Insensitive |
| Measures Catalog | 6 | Lookup, Aufloesen, Klassen-Zuordnung |
| Severity Scoring | 12 | Likelihood x Impact Matrix, Grenzwerte |
| Finding Validation | 5 | Enrichment, Auto-Fill, Fallbacks |
| Prompt Context | 4 | Taxonomie-Kontext fuer LLM |
| Integration | 5 | Base64-Roundtrip, Serialisierung, Benchmark |
| **Gesamt** | **111** | Alle bestanden |

---

## LLM Provider

```bash
# Ollama (empfohlen fuer lokale Entwicklung/Produktion)
LLM_PROVIDER=ollama
OLLAMA_BASE_URL=http://host.docker.internal:11434
OLLAMA_MODEL=mistral-small3.2

# Mistral Cloud (Entwicklung)
LLM_PROVIDER=mistral_cloud
MISTRAL_API_KEY=your-key
```

---

## Meilensteine

| # | Meilenstein | Status | Artefakte |
|---|---|---|---|
| M01 | Projektgeruest & OpenAPI v0.1 | Abgeschlossen | API-Framework, Auth, CI/CD, Postman |
| M02 | Parser Basis (FDX) & Testdataset | Abgeschlossen | FDX-Parser, Szenenmodell, SecureBuffer, 41 Tests |
| M03 | PDF & Streaming-Parsing | Abgeschlossen | PDF-Parser, LLM-Strukturierung, Prompt-YAML, Workflow-Refactoring, Risikoanalyse pro Szene, 73 Tests |
| M04 | Risiko-Taxonomie v1 & Scoring | Abgeschlossen | 23 Risiko-Klassen, Scoring-Engine, 20 Massnahmen-Codes, TaxonomyManager, 111 Tests |
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
