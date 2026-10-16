# eKI API -- KI-gestuetzte Sicherheitspruefung fuer Drehbuecher

**Version:** 1.0.1 (Patch-Release zur Uebergabe; Meilenstein M12 abgeschlossen)
**Auftraggeber:** Filmakademie Baden-Wuerttemberg
**Auftragnehmer:** StefanAI -- Research & Development

## Projektuebersicht

Die eKI ist eine modulare REST-API, die Drehbuecher aus dem eProjekt-System der Filmakademie entgegennimmt, KI-gestuetzt auf Sicherheitsrisiken analysiert und strukturierte Reports zurueckliefert. Die API fungiert als Processing-Only-Layer ohne dauerhafte Datenspeicherung von Drehbuechern oder Reports.

### Kernprinzipien

- **Design-First:** Der OpenAPI-3.1.1-Vertrag `openapi/eki-api-v1.0.yaml` ist massgeblich. Das von FastAPI erzeugte `/openapi.json` (nur mit `DEBUG=true` ausgeliefert) wird per Contract-Test (`tests/test_m12_openapi_contract.py`) mit ihm deckungsgleich gehalten
- **Processing-Only:** Keine persistente Speicherung von Drehbuechern/Reports (nur Metadaten, nach 90 Tagen automatisch geloescht)
- **Write-Through:** Ergebnisse werden sofort an eProjekt zurueckgespielt
- **Minimalinvasiv:** Integration nur ueber REST, keine direkten DB-Zugriffe
- **Security-First:** Verschluesselte transiente Daten, kein Klartext in Logs; Temporal-Payloads AES-256-GCM-verschluesselt (seit 1.0.1)

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
     │   Worker    │──▶ FDX/PDF Parser ──▶ LLM (externe Ollama-Instanz)
     └─────────────┘
```

Die LLM-Inferenz (und die KB-Embeddings) laufen auf einer **externen Ollama-Instanz** (GPU-Host),
die nicht Teil des Compose-Stacks ist; API und Worker erreichen sie ueber `OLLAMA_BASE_URL` bzw.
`LOCAL_MISTRAL_BASE_URL`. Fuer lokale Entwicklung/Offline gibt es das optionale Overlay
`docker-compose.ollama.yml`.

### Zwei getrennte Workflows (M03)

Die Verarbeitung ist formatspezifisch aufgeteilt:

**FDX-Workflow** (strukturiertes XML):
```
parse_fdx → analyze_scene_risk x N → aggregate_report (JSON+PDF) → deliver (push/pull)
```

**PDF-Workflow** (unstrukturiert, LLM-gestuetzt):
```
extract_text → split_scenes → structure_scene_llm x N → aggregate_script
  → analyze_scene_risk x N → aggregate_report (JSON+PDF) → deliver (push/pull)
```

Beide Workflows teilen sich die Risikoanalyse (pro Szene) und Delivery-Activities.

### Datenfluss

1. **Upload**: API empfaengt Drehbuch (JSON/Base64 oder Multipart FDX/PDF-Upload)
2. **SecureBuffer**: Inhalt wird AES-verschluesselt in Redis gespeichert (TTL max 6h)
3. **Temporal**: Workflow erhaelt nur einen Redis-Referenzschluessel -- kein Skriptinhalt; alle Temporal-Payloads sind zusaetzlich verschluesselt (AES-256-GCM)
4. **Parser**: FDX wird direkt geparsed, PDF wird per INT/EXT-Split + LLM strukturiert
5. **Risikoanalyse**: Jede Szene wird einzeln per LLM auf physische, umgebungs- und psychische Risiken geprueft
6. **Report**: Findings werden aggregiert, JSON-Report + PDF-Report (Executive Summary, To-Do-Liste) generiert
7. **Delivery**: Pull-Modus (ePro holt Report via One-Shot-GET) oder Push-Modus (eKI sendet an ePro)
8. **Cleanup**: Nach Abholung/Zustellung werden alle Buffer-Keys sofort geloescht

---

## Technologie-Stack

| Komponente | Technologie | Zweck |
|---|---|---|
| Framework | Python 3.11 / FastAPI | REST-API (ASGI); Abhaengigkeiten gepinnt in `requirements.lock` / `requirements-dev.lock` |
| Workflow-Engine | Temporal 1.29.4 | Asynchrone Verarbeitung, 6h-Retry-Fenster, Schedules |
| Datenbank | PostgreSQL 16 + pgvector | Metadaten, Dead Letters, Knowledge-Base-Embeddings |
| Cache | Redis 7.2 (`redis:7.2-alpine`, BSD-3-Clause) | SecureBuffer, Rate Limiting; Support-Risiko der 7.2-Linie: `docs/OPERATIONS_GUIDE.md` §4.9 |
| LLM | Ollama (mistral-small3.2 via LocalMistralProvider) auf externer Instanz | Lokale Inferenz in Produktion (Strukturierung + Risikoanalyse), kein Cloud-Verkehr |
| LLM (Cloud) | Mistral Cloud API | Stage-Umgebung (in Produktion gesperrt) |
| FDX-Parser | defusedxml | Sicheres XML-Parsing (XXE-Schutz) |
| PDF-Parser | pdfplumber (MIT) + Tesseract OCR | Text-Extraktion aus PDFs, OCR-Fallback fuer Scans |
| Prompt-Management | YAML + PromptManager | Versionierbare LLM-Prompts |
| Verschluesselung | cryptography (Fernet, AES-256-GCM) | Verschluesselung transienter Daten (Redis) und der Temporal-Payloads |
| Container | Docker / Docker Compose | Deployment |
| Monitoring | Prometheus, Grafana, Alertmanager, OpenTelemetry/Jaeger | Metriken, Dashboards, Alerts, Tracing |

---

## Schnellstart

Lokaler Entwicklungs-Stack unter Linux (Docker Desktop auf macOS/Windows funktioniert
analog). Produktion: `docs/OPERATIONS_GUIDE.md`.

### Voraussetzungen

- Docker mit Docker Compose >= 2.24 (`docker compose version`)
- Python 3 auf dem Host (nur fuer `scripts/generate_secrets.py`); fuer Entwicklung und Tests
  Python 3.11 wie CI/Docker (siehe `docs/TESTING_GUIDE.md`)
- Eine erreichbare **Ollama-Instanz** mit den Modellen `mistral-small3.2` (~15 GB) und
  `mxbai-embed-large`. Der Compose-Stack enthaelt kein Ollama mehr: im Normalfall eine externe
  Instanz auf einem GPU-Host (NVIDIA, >= 24 GB VRAM empfohlen), deren URL in `.env.local` steht.
  Fuer lokale Entwicklung oder offline: Overlay `docker-compose.ollama.yml` (startet
  `ollama/ollama:0.33.3` im Stack; GPU-Reservierung braucht das NVIDIA Container Toolkit, auf
  Hosts ohne NVIDIA-GPU den `deploy:`-Block im Overlay loeschen -- dann laeuft Ollama auf der CPU und
  eine Pruefung dauert entsprechend lange)
- Plattenplatz fuer Images, Datenbank und Logs; die Modelle (`mistral-small3.2` ~15 GB,
  `mxbai-embed-large` ~1 GB) liegen auf dem Ollama-Host bzw. mit dem Overlay im Volume `ollama_data`
- Fuer Tests ohne Docker: `tesseract-ocr tesseract-ocr-deu tesseract-ocr-eng` (OCR-Fallback; im Docker-Image enthalten)

### Setup

```bash
# 1. Konfiguration mit echten Secrets (Passwort muss in POSTGRES_PASSWORD, POSTGRES_PWD
#    und DATABASE_URL identisch sein -- der sed-Aufruf setzt alle drei)
cp .env.example .env.local
python3 scripts/generate_secrets.py
PW=$(cat secrets/db_password.txt); KEY=$(cat secrets/api_secret_key.txt)
sed -i.bak -e "s/CHANGE_ME_STRONG_PASSWORD_HERE/$PW/g" \
           -e "s/CHANGE_ME_256_BIT_SECRET_KEY_HERE/$KEY/" .env.local && rm .env.local.bak

# 2. Externe Ollama-Instanz: Modelle dort einmalig laden (auf dem Ollama-Host ausfuehren) ...
ollama pull mistral-small3.2      # LLM (LLM_PROVIDER=local_mistral)
ollama pull mxbai-embed-large     # KB-Embeddings (OLLAMA_EMBEDDING_MODEL)
#    ... und ihre URL in .env.local eintragen (gilt fuer Inferenz und Embeddings):
#    OLLAMA_BASE_URL=http://<ollama-host>:11434
#    (Variante ohne externe Instanz: Overlay docker-compose.ollama.yml, siehe unten)

# 3. Stack bauen und starten (6 Services; api/worker mounten den Quellcode)
docker compose up -d --build
docker compose ps                     # postgres, redis, temporal, api: healthy

# 4. Datenbankschema anlegen
docker compose exec api alembic upgrade head                 # head: c4e6a8b0d2f1

# 5. API-Keys erstellen (interaktiv, Klartext wird nur einmal angezeigt)
docker compose exec api python scripts/create_api_key.py --insert           # Nutzer-Key (Pruefungen)
docker compose exec api python scripts/create_api_key.py --insert --admin   # Admin-Key (/v1/kb/*, /v1/ops/*)
export EKI_API_KEY=eki_...

# 6. Pruefen
curl http://localhost:8000/health
curl -H "Authorization: Bearer $EKI_API_KEY" http://localhost:8000/ready

# 7. Erste Pruefung (FDX-Fixture, Pull-Modus)
B64=$(base64 -w0 tests/fixtures/fdx/stunt_heavy.fdx)
curl -s -X POST http://localhost:8000/v1/security/check:async \
  -H "Authorization: Bearer $EKI_API_KEY" -H "Content-Type: application/json" \
  -d "{\"script_content\": \"$B64\", \"script_format\": \"fdx\", \"project_id\": \"demo-1\", \"delivery\": \"pull\"}"
curl -s -H "Authorization: Bearer $EKI_API_KEY" http://localhost:8000/v1/security/jobs/<job_id>        # bis "completed"
curl -s -H "Authorization: Bearer $EKI_API_KEY" http://localhost:8000/v1/security/reports/<report_id>  # One-Shot: JSON + PDF
```

Aenderungen an `.env.local` wirken erst nach `docker compose up -d --force-recreate api worker`
(`docker compose restart` liest die Datei nicht neu ein). Variablen wie `GRAFANA_ADMIN_PASSWORD`,
`EKI_IMAGE_TAG` oder `GHCR_OWNER` liest Docker Compose aus der Shell bzw. einer Projekt-`.env`,
nicht aus `.env.local` (`docs/CONFIGURATION.md` §2).

**Ohne externe Ollama-Instanz (lokale Entwicklung, offline):** Das Overlay
`docker-compose.ollama.yml` fuegt einen Ollama-Container hinzu (gepinnt auf `ollama/ollama:0.33.3`,
Volume `ollama_data`) und setzt `OLLAMA_BASE_URL`/`LOCAL_MISTRAL_BASE_URL` fuer `api` und `worker`
selbst auf `http://ollama:11434`. Statt der Schritte 2 und 3:

```bash
export COMPOSE_FILE=docker-compose.yml:docker-compose.ollama.yml   # gilt fuer alle folgenden Aufrufe
docker compose up -d --build                                       # 7 Services inkl. ollama
docker compose exec ollama ollama pull mistral-small3.2
docker compose exec ollama ollama pull mxbai-embed-large
```

Der GPU-Block des Overlays braucht das NVIDIA Container Toolkit; ohne NVIDIA-GPU den `deploy:`-Block
des `ollama`-Dienstes loeschen, sonst startet der Container nicht. In Produktion wird das Overlay
nicht verwendet (`docs/OPERATIONS_GUIDE.md` §4.11).

### Services

Veroeffentlicht werden nur Port 8000 (API) und im Entwicklungs-Stack 8080 (Temporal UI).

| Service | Zugriff | Beschreibung |
|---|---|---|
| API | http://localhost:8000 | FastAPI; Swagger UI `/docs` nur mit `DEBUG=true` |
| Temporal UI | http://localhost:8080 | Workflow-Monitoring; nur Entwicklung (Produktion: SSH-Tunnel, `docs/OPERATIONS_GUIDE.md` §8.2) |
| PostgreSQL | nur intern: `docker compose exec postgres sh -c 'PGPASSWORD="$POSTGRES_PASSWORD" psql -U "$POSTGRES_USER" -d "$POSTGRES_DB"'` | Datenbanken `eki_db`, `temporal`, `temporal_visibility` |
| Redis | nur intern: `docker compose exec redis redis-cli` | SecureBuffer + Rate-Limit-Zaehler (Redis 7.2) |
| Ollama | extern (nicht Teil des Stacks): `curl http://<ollama-host>:11434/api/tags`; mit Overlay `docker-compose.ollama.yml`: `docker compose exec ollama ollama list` | LLM-Inferenz + Embeddings |
| Grafana / Jaeger | http://localhost:3000 / http://localhost:16686 | nur mit `docker-compose.observability.yml` |

---

## Dokumentation

| Dokument | Inhalt |
|---|---|
| `README.md` | Ueberblick, Schnellstart, Architektur (dieses Dokument) |
| `CHANGELOG.md` | Aenderungsprotokoll je Version |
| `docs/OPERATIONS_GUIDE.md` | Betrieb: Installation, Updates, Migrationen, Backup/Restore, Runbooks, Troubleshooting |
| `docs/CONFIGURATION.md` | Referenz aller Einstellungen (`.env.local`, Compose-Variablen) |
| `docs/TESTING_GUIDE.md` | Testebenen, lokale Ausfuehrung, CI-Pipeline |
| `docs/EPRO_INTEGRATION_GUIDE.md` | Schnittstelle fuer das eProjekt-Team |
| `docs/DATENSCHUTZ.md` | Datenhaltung: was wo wie lange gespeichert wird |
| `docs/M09_SLO.md` | SLOs, Alerts, Dashboards |
| `docs/M10_FAILOVER_RUNBOOK.md` | Zustellstoerungen, Dead Letters |
| `docs/M06_KB_GUIDE.md` | Knowledge Base befuellen und aktivieren |
| `docs/LLM_INTEGRATION.md` | LLM-Provider-Architektur |
| `docs/M11_PARITY_REPORT.md` | Paritaet Cloud <-> Lokal |
| `docs/UAT/UAT_TESTPLAN.md`, `docs/UAT/UAT_PROTOCOL_TEMPLATE.md` | Abnahmetests 1-9, Protokollvorlage |
| `docs/UAT/GO_LIVE_CHECKLIST.md`, `docs/UAT/TRAINING.md`, `docs/UAT/HANDOVER.md` | Go-Live, Schulung, Uebergabe |
| `openapi/eki-api-v1.0.yaml` | API-Vertrag (OpenAPI 3.1.1) |
| `postman/README.md` | Postman-Collection und Environment |
| `secrets/README.md` | Secrets erzeugen und einbinden |

Die Meilenstein-Nachweise (`docs/M01_COMPLETION_REPORT.md`, `docs/M06_ACCEPTANCE_EVIDENCE.md` bis
`docs/M12_ACCEPTANCE_EVIDENCE.md`, `docs/TEST_REPORT.md`, `docs/SECURITY_AUDIT_2026-02-06.md`) sind
historisch: Sie beschreiben den Stand ihres Datums und werden nicht fortgeschrieben. Als veraltet
markierte Dokumente (`docs/DEPLOYMENT_GUIDE.md`, `docs/OLLAMA_*.md`, `docs/QUICKSTART_OLLAMA.md`,
`docs/SECURITY_AUDIT_REPORT.md` u. a.) bleiben nur zur Nachverfolgung erhalten.

---

## API-Endpunkte

### Security Endpoints (Auth erforderlich)

| Methode | Endpunkt | Beschreibung |
|---|---|---|
| `POST` | `/v1/security/check:async` | Asynchroner Check via Temporal Workflow (FDX oder PDF) -- einziger Pruef-Endpunkt |
| `GET` | `/v1/security/jobs/{job_id}` | Echtes Job-Status-Tracking aus DB (M05) |
| `GET` | `/v1/security/reports/{id}` | One-Shot-Report: JSON + PDF, danach geloescht (M05); Header `X-One-Shot: true` (M10) |

Der fruehere synchrone Stub `POST /v1/security/check` (M01, ohne Analyse) ist seit 1.0.1 entfernt
und antwortet mit `404`.

Seit 1.0.1 wirken die Request-Felder `callback_url` und `priority`
(Details: `docs/EPRO_INTEGRATION_GUIDE.md` §2, §4b):

- **`callback_url`** ersetzt fuer diesen Job das Push-Ziel
  `EPRO_BASE_URL/eki/scl/set-risk-assessment/{project_id}` und impliziert `delivery=push`
  (`delivery=pull` zusammen mit `callback_url` → 422). Erlaubt sind nur HTTPS-URLs ohne
  Zugangsdaten auf Hosts aus `CALLBACK_URL_ALLOWED_HOSTS` (Standard `epro.filmakademie.de`,
  `staging.epro.filmakademie.de`) mit oeffentlicher IP; vor jedem Push prueft der Worker Host und
  DNS erneut und verbindet sich mit der geprueften Adresse.
- **`priority`** (1 = hoechste ... 10, Default 5) wird zur Temporal-Task-Queue-Prioritaet: Warten
  mehr Jobs als Worker-Slots frei sind, kommen hoeher priorisierte zuerst dran. Laufende Jobs werden
  nicht verdraengt.

### Knowledge Base Endpoints (M06, Admin-Key erforderlich)

Alle `/v1/kb/*`-Endpunkte verlangen seit 1.0.1 einen Admin-Key (`create_api_key.py --insert --admin`);
ein normaler Key (z. B. der ePro-Service-Key) erhaelt `403`.

| Methode | Endpunkt | Beschreibung |
|---|---|---|
| `POST` | `/v1/kb/documents` | Upload eines KB-Dokuments (PDF/MD/TXT, Multipart) |
| `GET` | `/v1/kb/documents` | Liste aller KB-Dokumente, optional gefiltert nach `tag` |
| `GET` | `/v1/kb/documents/{doc_id}` | Metadaten zu einem Dokument |
| `DELETE` | `/v1/kb/documents/{doc_id}` | Einzel-Delete (Chunks cascaden) |
| `DELETE` | `/v1/kb/documents?tag=...` | Bulk-Delete nach Tag (z.B. `placeholder` zum Wipe der Seed-Daten) |

### System Endpoints

| Methode | Endpunkt | Auth | Beschreibung |
|---|---|---|---|
| `GET` | `/` | Nein | API-Info |
| `GET` | `/health` | Nein | Liveness-Probe |
| `GET` | `/ready` | Ja | Readiness-Probe (DB, Redis, Temporal; Ollama wird nicht geprueft) |
| `GET` | `/metrics` | Ja | Prometheus Metrics (bei `METRICS_ENABLED=true`) |
| `GET` | `/docs`, `/redoc`, `/openapi.json` | Nein | nur mit `DEBUG=true` (in Produktion verboten) |

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

1. **Text-Extraktion** (pdfplumber): Seitenweise, in-memory
2. **OCR-Fallback** (Tesseract, Pflichtenheft §4.1): Seiten ohne Textebene (Scans) werden
   in-memory gerendert und per `pytesseract` erkannt (`deu+eng`). Der erkannte Text wird an der
   Seitenposition eingesetzt, sodass der Seiten-Fallback-Splitter ausgerichtet bleibt; Szenen aus
   OCR-Seiten erhalten Confidence x 0,7. Steuerung: `OCR_ENABLED` (Default `true`),
   `OCR_LANGUAGES`, `OCR_MAX_PAGES` (Laufzeit-Cap), `OCR_DPI`, `OCR_PAGE_TIMEOUT_SECONDS`.
   Fehlt Tesseract, wird die Seite mit Warnung uebersprungen (kein Abbruch).
3. **Deterministischer Split** (Regex): Zuverlaessige Aufteilung an INT/EXT/INNEN/AUSSEN-Markern
4. **LLM-Strukturierung** (Ollama/Mistral Structured Output): Jeder Szenenblock wird per KI in das ParsedScene-Schema ueberfuehrt -- Location, Characters, Dialogue, Action

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

## Report-Erzeugung und Delivery (M05)

### Zwei Report-Formate

Jeder Report wird in beiden Formaten gleichzeitig erzeugt:

- **JSON**: Maschinenlesbar mit allen Findings, Taxonomie-Feldern, Massnahmen (fuer ePro-Integration)
- **PDF**: Menschenlesbar mit Executive Summary, Szenen-Befunde, Massnahmen-Checkliste (fuer Produktion)

Beim Abruf via `GET /v1/security/reports/{id}` enthaelt die Response beides: `report` (JSON) + `pdf_base64` (PDF).

### Delivery-Modes (konfigurierbar)

| Modus | Beschreibung | Konfiguration |
|---|---|---|
| **Pull** (Default ohne `callback_url`) | Report bleibt in Redis (verschluesselt, TTL 6h). ePro holt ihn via One-Shot-GET. Nach erstem Abruf sofort geloescht. | `delivery: "pull"` |
| **Push** | eKI sendet Report per POST an `EPRO_BASE_URL/eki/scl/set-risk-assessment/{project_id}` bzw. an die `callback_url` des Requests. Bei 2xx sofort geloescht, bei Fehler Retry. | `delivery: "push"` oder `callback_url` |

Ein Abruf nach Ablauf der Buffer-TTL oder nach einer Push-Zustellung liefert `410 Gone` (der Inhalt
ist geloescht). Ist Redis nicht erreichbar, antwortet der Abruf mit `503`, und der Report bleibt
abrufbar.

### Idempotenz

Ein optionaler `idempotency_key` im Request verhindert doppelte Workflows. Gleicher Key = bestehender Job wird zurueckgegeben.

### Job-Status-Tracking

`GET /v1/security/jobs/{id}` liefert echte Daten aus der Datenbank: Status, Progress, Report-ID, Timestamps.

## LLM-Adapter und Knowledge Base (M06)

### Mistral Cloud als zweite Inferenz-Quelle

Der Mistral-Cloud-Adapter ist auf produktionsreifes Structured Output aufgeruestet (`llm/mistral_cloud.py`):

- Mistrals nativer JSON-Mode (`response_format={"type":"json_object"}`)
- Post-Hoc JSON-Schema-Validierung mit `jsonschema` (Draft 2020-12)
- Genau ein Self-Correcting-Retry mit Fehlerkontext im Prompt
- Identischer Rueckgabevertrag wie OllamaProvider

Damit kann `LLM_PROVIDER=mistral_cloud` fuer Stage-Tests (Pflichtenheft Abnahmetest 8) genutzt werden. Produktion laeuft lokal mit `LLM_PROVIDER=local_mistral` (Ollama, M11); `mistral_cloud` ist dort gesperrt und bietet keine KB-Embeddings. Achtung: Der Code-Default von `LLM_PROVIDER` ist `mistral_cloud` -- `.env.example` setzt deshalb explizit `local_mistral`.

### Knowledge Base mit pgvector

Die KB speichert verschluesselte Sicherheits-Dokumente (Stunt-SOPs, SFX-Leitfaeden, etc.) und reichert die Risikoanalyse via RAG-Retrieval an. Schema:

| Tabelle | Inhalt |
|---|---|
| `kb_documents` | Originaltext Fernet-verschluesselt, Metadaten (title, source, tags, tenant_id, expires_at, content_hash) |
| `kb_embeddings` | Chunks (~1200 Tokens, 100 Overlap) mit `vector(1024)` aus Ollama `mxbai-embed-large` (Default; Alternative `bge-m3`) und ivfflat-Cosine-Index |

**Aktivierung:** Per Feature-Flag `KB_RETRIEVAL_ENABLED` (Default `false`). Solange deaktiviert, ist der Risk-Flow bytewise identisch mit M05. Aktivierung erst nach Validierung.

**Embedding-Modell:** API (Ingest) und Worker (Retrieval) muessen dasselbe `OLLAMA_EMBEDDING_MODEL`
nutzen. Ein Wechsel nach dem ersten Ingest erfordert Leeren und Neubefuellen der KB, weil Vektoren
verschiedener Modelle nicht vergleichbar sind (`docs/OPERATIONS_GUIDE.md` §4.8, §5.6).

**Test-KB befuellen (Bernd-Stand-in)** -- im API-Container (Entwicklung: `config/` ist gemountet);
`EKI_API_KEY` muss ein **Admin-Key** sein (`/v1/kb/*` ist seit 1.0.1 admin-only):

```bash
docker compose exec -e EKI_API_KEY="eki_<admin>" api python scripts/seed_kb.py --seed-placeholders   # 6 Beispiel-SOPs
docker compose exec -e EKI_API_KEY="eki_<admin>" api python scripts/seed_kb.py --status              # Uebersicht
```

**Echte Inhalte einspielen** (`config/kb_seed/real/` anlegen; echte Dokumente nicht committen):

```bash
mkdir -p config/kb_seed/real && cp ~/Bernd-Stunt-Leitfaden.pdf config/kb_seed/real/
docker compose exec -e EKI_API_KEY="eki_<admin>" api python scripts/seed_kb.py --wipe-placeholders   # nur Tag 'placeholder' weg
docker compose exec -e EKI_API_KEY="eki_<admin>" api python scripts/seed_kb.py --reseed              # ingestet alles aus real/
```

Im Entwicklungs-Stack sieht der API-Container `config/kb_seed/real/` ueber den Bind-Mount; in Images
gelangt der Ordner nicht (`.dockerignore`). In Produktion (keine Bind-Mounts) laeuft
`scripts/seed_kb.py` daher auf dem Host gegen `http://localhost:8000` (braucht `requests`).
Details in `docs/M06_KB_GUIDE.md`.

## Grossdokument-Optimierung (M07)

### Pflichtenheft-Ziel

300-350-Seiten-Drehbuecher muessen asynchron in <= 120 Min verarbeitet
werden (Pflichtenheft §5 + §7 Abnahmetest 6). Erreicht wird das durch
optionale Parallelisierung der LLM-Strukturierung und der Risikoanalyse,
geschuetzt durch drei abgestufte Concurrency-Schichten.

### Drei-Schicht-Schutz gegen Ollama-Ueberlast

| Schicht | Variable | Default | Wirkung |
|---|---|---|---|
| Master-Flag | `LLM_PARALLEL_ENABLED` | `false` | Wenn `false`: strikt sequenziell, bytewise identisch zu M06. |
| Prozess-Cap | `OLLAMA_MAX_CONCURRENT_REQUESTS` | `1` | Modul-globaler Semaphore in `llm/ollama.py`. Greift ueber alle Workflows und Activities hinweg. |
| Throttle | `OLLAMA_MIN_INTERVAL_MS` | `0` | Optionaler Mindestabstand zwischen zwei Calls fuer knapp dimensionierte Shared-GPU-Systeme. |
| Workflow-Cap | `PDF_STRUCTURE_CONCURRENCY`, `RISK_ANALYSIS_CONCURRENCY` | `1` | Per-Workflow-Cap fuer parallel ausgefuehrte LLM-Activities. |

Parallel-Pfad wird nur aktiv, wenn `LLM_PARALLEL_ENABLED=true` UND
mindestens einer der `*_CONCURRENCY`-Werte > 1 ist.

### Empfohlene Concurrency-Stufen

| Stufe | Flag | OLLAMA-Cap | Workflow-Cap | Erwartete Wirkung |
|---|---|---|---|---|
| Default | `false` | 1 | 1 | identisch zu M06 |
| Konservativ | `true` | 2 | 2 | ca. 1.6-1.8x Speedup bei 1 GPU |
| Sportlich | `true` | 3 | 3 | nur lohnend mit reichlich VRAM |
| Verboten | `true` | `>=4` | beliebig | nicht empfohlen, VRAM-Risiko |

> **WICHTIG:** Ollama laeuft i.d.R. auf einer geteilten GPU-Instanz.
> Erhoehe `OLLAMA_MAX_CONCURRENT_REQUESTS` nur, wenn Du sicher weisst,
> dass die GPU genug VRAM fuer N gleichzeitige Inferenzen hat. Im
> Zweifel auf 1 lassen.

### Benchmark 300 Seiten

```bash
# Host-venv mit Dev-Extras (requests, reportlab), siehe docs/TESTING_GUIDE.md
# Fixture bauen (liegt bereits bei: tests/fixtures/pdf/large_synthetic_300pp.pdf)
python tests/build_large_fixture.py

# Baseline (Concurrency 1, M06-Verhalten)
export EKI_API_KEY="eki_..."
python tests/run_pdf_m07_benchmark.py --concurrency 1

# Parallel-Lauf (sed -i.bak funktioniert mit GNU- und BSD-sed)
sed -i.bak \
  -e 's/^LLM_PARALLEL_ENABLED=.*/LLM_PARALLEL_ENABLED=true/' \
  -e 's/^OLLAMA_MAX_CONCURRENT_REQUESTS=.*/OLLAMA_MAX_CONCURRENT_REQUESTS=2/' \
  -e 's/^PDF_STRUCTURE_CONCURRENCY=.*/PDF_STRUCTURE_CONCURRENCY=2/' \
  -e 's/^RISK_ANALYSIS_CONCURRENCY=.*/RISK_ANALYSIS_CONCURRENCY=2/' \
  .env.local && rm .env.local.bak
docker compose up -d --force-recreate api worker   # 'restart' liest .env.local nicht neu ein
python tests/run_pdf_m07_benchmark.py --concurrency 2
```

Ergebnisse landen in `tests/reports/m07_benchmark_<timestamp>.json` mit
`acceptance.passed_overall` (Pflichtenheft-Schwelle 7200 s).

### Konfigurierbare Limits

| Setting | Default | Wirkung |
|---|---|---|
| `MAX_PDF_PAGES` | 500 | PDF-Parser-Obergrenze in Seiten |
| `MAX_PDF_SIZE_BYTES` | 10 MB | PDF-Parser-Obergrenze in Bytes |
| `MAX_UPLOAD_SIZE_BYTES` | 10 MB | Multipart-Upload-Cap |
| `LLM_ACTIVITY_TIMEOUT_SECONDS` | 600 | start_to_close fuer LLM-Activities |
| `WORKER_MAX_CONCURRENT_WORKFLOW_TASKS` | 10 | Temporal-Worker-Cap |
| `WORKER_MAX_CONCURRENT_ACTIVITIES` | 20 | Temporal-Worker-Cap |

Details und Abnahme-Belege in `docs/M07_ACCEPTANCE_EVIDENCE.md`.

## Observability & SLOs (M09)

### Metriken

API und Worker exportieren dieselben Prometheus-Metrikfamilien aus `core/metrics.py`
(Prefix `eki_`): HTTP-Raten/-Latenzen je Route-Template, Job-Laufzeiten und
Terminalzustaende, LLM-Latenz/-Fehler/-Queue-Wait je Provider und Operation,
Push/Pull-Zustellversuche, One-Shot-Abrufe, Buffer-Loeschungen (Delete-on-Delivery-Nachweis),
Webhook-Ergebnisse, KB-Bestand, Sanitizer-Treffer, geloeschte Metadaten-Zeilen
(`eki_metadata_cleanup_deleted_total{table}`, nur Worker) und `eki_build_info`.

| Prozess | Endpoint | Schutz |
|---|---|---|
| API | `GET /metrics` | API-Key (Bearer) |
| Worker | `:9090/` (Port `PROMETHEUS_PORT`) | nur im internen Docker-Netz, nie auf den Host gemappt |

### Tracing und Log-Korrelation

- `OTEL_ENABLED=true` aktiviert OpenTelemetry fuer FastAPI, SQLAlchemy, httpx und Temporal
  (`core/tracing.py`); Export per OTLP/HTTP an `OTEL_EXPORTER_OTLP_ENDPOINT` (z.B. Jaeger).
- Der Interceptor `core/temporal_context.py` traegt die `request_id` des HTTP-Requests als
  Temporal-Header in den Workflow und in jede Activity. Jede Worker-Log-Zeile enthaelt
  `request_id`, `job_id`, `workflow_id`, `activity`, `attempt`.

### Stack starten

```bash
# Prometheus + Alertmanager + Grafana (3 provisionierte Dashboards) + Jaeger
# Scraper-Key VOR dem Start ablegen (sonst legt Docker ein Verzeichnis an), siehe secrets/README.md §3
printf '%s' 'eki_...' > secrets/prometheus_api_key.txt
sudo chown 65534 secrets/prometheus_api_key.txt && sudo chmod 600 secrets/prometheus_api_key.txt   # Prometheus laeuft als nobody
export GRAFANA_ADMIN_PASSWORD='<starkes-passwort>'   # Compose-Substitution; ohne: wortwoertlich CHANGE_ME
docker compose -f docker-compose.yml -f docker-compose.observability.yml up -d
# Grafana http://localhost:3000, Jaeger http://localhost:16686
```

Alert-Regeln: `docker/observability/alerts.yml`. SLO-Definitionen, Error-Budgets und
Alert-Mapping: `docs/M09_SLO.md`. Dashboards werden aus
`scripts/observability/build_dashboards.py` generiert.

### Wartungs-Schedules

Der Worker gleicht bei jedem Start zwei Temporal-Schedules mit den Einstellungen ab (fehlt einer,
wird er angelegt; abweichender Cron wird aktualisiert, `*_ENABLED=false` pausiert ihn):

- `eki-kb-cleanup` (`KB_CLEANUP_CRON`, Default taeglich 03:00 UTC): loescht abgelaufene
  KB-Dokumente und aktualisiert `eki_kb_documents`.
- `eki-metadata-cleanup` (`METADATA_CLEANUP_CRON`, Default taeglich 03:30 UTC, seit 1.0.1): loescht
  Job-/Report-Metadaten, Dead Letters, nicht mehr nutzbare API-Keys und Audit-Logs, deren
  Zeitstempel aelter als `METADATA_RETENTION_DAYS` (90 Tage) sind. Regeln: `docs/DATENSCHUTZ.md` §4,
  Betrieb: `docs/OPERATIONS_GUIDE.md` §5.10.

## Outbound-Adapter Hardening (M10)

### Zustell-Lebenszyklus

`pending -> delivering -> delivered` bzw. `dead_lettered`. Der Job erreicht `completed`
erst nach echtem 2xx von ePro (vorher `delivering`). Jeder Versuch wird in
`job_metadata` protokolliert (`delivery_attempts`, `delivery_last_status_code`,
`delivered_at`); `GET /v1/security/jobs/{id}` zeigt diese Felder in `metadata`.

| ePro-Antwort | Verhalten |
|---|---|
| 2xx | Buffer sofort geloescht, `delivered` |
| 5xx, 408/425/429, Netzwerkfehler | Temporal-Retry (2 s bis 10 min Backoff) bis 6 h |
| sonstige 4xx | Hard-Fail ohne Retry |
| 6 h erschoepft / Pull nicht abgeholt | Cleanup, Job `failed`, Webhook, **Dead Letter** |
| `callback_url` bei der Zustellung abgelehnt (Host nicht mehr erlaubt, DNS liefert private Adresse) | kein Request, kein Retry: Cleanup, Job `failed`, Webhook, Dead Letter `invalid_callback_url` |

Push-Requests tragen `Idempotency-Key: <report_id>`, `X-EKI-Job-Id`, `X-EKI-Attempt`
und `X-Request-ID`, damit ePro Retries dedupliziert. Idempotenz auf der Eingangsseite ist
je API-Key gescoped (`(user_id, idempotency_key)` unique) und race-sicher.

### Dead-Letter-Queue und Ops-Endpoints (Admin-Key)

| Methode | Endpunkt | Beschreibung |
|---|---|---|
| `GET` | `/v1/ops/summary` | Jobs nach Status/Zustellstatus, offene Dead Letters |
| `GET` | `/v1/ops/jobs` | Job-Uebersicht ohne Inhalte (Filter `status`, `delivery_status`, `project_id`) |
| `GET` | `/v1/ops/dead-letters` | Endgueltig gescheiterte Zustellungen (inhaltsarm) |
| `GET` | `/v1/ops/dead-letters/{id}` | Einzelansicht |
| `POST` | `/v1/ops/dead-letters/{id}:acknowledge` | Nach Analyse quittieren |

Admin-Keys: `docker compose exec api python scripts/create_api_key.py --insert --admin`. Ein Replay aus der DLQ ist
bewusst nicht vorgesehen -- der Inhalt wird beim endgueltigen Scheitern geloescht
(Delete-on-Delivery); ePro stoesst den Check neu an. Dead Letters werden nach 90 Tagen
geloescht, quittiert oder nicht (`eki-metadata-cleanup`) -- innerhalb dieser Frist bearbeiten.
Runbook: `docs/M10_FAILOVER_RUNBOOK.md`.

## Lokaler LLM-Adapter & Paritaetstests (M11)

Produktion laeuft ausschliesslich lokal (Pflichtenheft §4.3): `LLM_PROVIDER=local_mistral` nutzt
`llm/local_mistral.py` mit `LOCAL_MISTRAL_MODEL=mistral-small3.2` (Default) ueber Ollama; dessen
`health_check()` prueft, dass das Modell gepullt ist (genutzt von `scripts/test_llm.py` und
`scripts/run_parity.py`). In `ENV=production` wird `mistral_cloud` abgelehnt, solange nicht
`LLM_ALLOW_EXTERNAL_PROVIDERS=true` gesetzt ist (Abnahmetest 9). `gemma4` bleibt als Alternative
konfigurierbar (`LLM_PROVIDER=ollama`, `OLLAMA_MODEL=gemma4:31b`).

```bash
# Prod-Cutover-Smoke: E2E-Job, One-Shot, Delete-on-Delivery, keine externen LLM-Calls
docker compose exec -e EKI_API_KEY=eki_... api python scripts/smoke_prod.py --worker-metrics http://worker:9090
# Paritaet auf dem Golden-Set (16 Szenen) -- im API-Container (dort liegen Konfiguration und
# Ollama-URL bereit); Kommandos: docs/OPERATIONS_GUIDE.md §7
```

Report-Rahmen und Schwellen: `docs/M11_PARITY_REPORT.md`. Betriebsleitfaden:
`docs/OPERATIONS_GUIDE.md`. Release-Images werden bei `v*`-Tags nach GHCR gepusht
(`docker-compose.prod.yml` referenziert sie ueber `GHCR_OWNER`/`EKI_IMAGE_TAG`; beide liest
Docker Compose aus Shell bzw. Projekt-`.env`, nicht aus `.env.local`).

## UAT-Paket & Uebergabe (M12)

| Artefakt | Pfad |
|---|---|
| OpenAPI 1.0.1 (API-Vertrag, Spectral 0 Errors, Contract-Test) | `openapi/eki-api-v1.0.yaml`, `tests/test_m12_openapi_contract.py` |
| Postman Collection + Environment | `postman/eKI-API-v1.0.postman_collection.json`, `postman/eKI-API.postman_environment.json` |
| Aenderungsprotokoll | `CHANGELOG.md` |
| Testplan fuer die 9 Abnahmetests, Protokollvorlage | `docs/UAT/UAT_TESTPLAN.md`, `docs/UAT/UAT_PROTOCOL_TEMPLATE.md` |
| Go-Live-Checkliste, Schulungsunterlagen, Uebergabe | `docs/UAT/GO_LIVE_CHECKLIST.md`, `docs/UAT/TRAINING.md`, `docs/UAT/HANDOVER.md` |
| ePro-Mock und automatisierte Abnahmetests 2-7 | `scripts/uat/mock_epro_server.py`, `scripts/uat/run_acceptance_tests.py` |
| Prod-Cutover-Smoke, Paritaetslauf | `scripts/smoke_prod.py`, `scripts/run_parity.py` |

```bash
python scripts/uat/mock_epro_server.py --port 9999        # Terminal 1 (ePro-Attrappe auf dem Host)
# .env.local: EPRO_BASE_URL=http://host.docker.internal:9999/api
#             EPRO_WEBHOOK_URL=http://host.docker.internal:9999/api/eki/scl/delivery-failed
# Linux: zusaetzlich extra_hosts "host.docker.internal:host-gateway" fuer api/worker (TESTING_GUIDE §6.1)
docker compose up -d --force-recreate api worker
export EKI_API_URL=http://localhost:8000 EKI_API_KEY=eki_... EKI_ADMIN_KEY=eki_... MOCK_URL=http://localhost:9999
python scripts/uat/run_acceptance_tests.py --log-cmd "docker compose logs api worker"
# -> tests/reports/uat_<timestamp>.md (inhaltsfreies Protokoll)
```

**Achtung:** Ohne Umstellung von `EPRO_BASE_URL`/`EPRO_WEBHOOK_URL` gehen Push-Jobs an die echte
Stage -- `docker-compose.yml` bildet `staging.epro.filmakademie.de` per `extra_hosts` auf
`193.196.129.23` ab. Redis ist nicht auf den Host veroeffentlicht; den Buffer-Check daher per
`docker compose exec redis redis-cli --scan --pattern 'eki:buf:*' | wc -l` (Erwartung `0`).
Vollstaendige Anleitung: `docs/TESTING_GUIDE.md` §6.1.

Abnahmetests 1, 8 und 9 (Stage-ePro, Prod-Cutover) sind im Testplan als manuelle Schritte mit
Protokollvorlage beschrieben.

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
- **Temporal-Schutz**: Workflow-History enthaelt nur Redis-Referenzschluessel, keinen Klartext;
  seit 1.0.1 sind alle Temporal-Payloads (Ein-/Ausgaben, Header, Fehlermeldungen) AES-256-GCM-verschluesselt
  (Schluessel per HKDF aus `API_SECRET_KEY`; `core/temporal_codec.py`)
- **Explizite Loeschung**: Buffer-Keys werden nach jeder Verarbeitungsstufe bereinigt
- **Aufbewahrung**: Metadaten werden nach 90 Tagen geloescht (Schedule `eki-metadata-cleanup`, `docs/DATENSCHUTZ.md`)

### API-Sicherheit

- **Authentifizierung**: Database-backed API Keys (SHA-256 Hashing, Ablaufdatum)
- **Autorisierung**: Ownership-Checks gegen IDOR-Angriffe; `/v1/kb/*` und `/v1/ops/*` nur mit Admin-Key
- **Input Validation**: Base64 (FDX), PDF-Magic-Byte-Check, SSRF-Prevention fuer `callback_url`
  (HTTPS, Host-Allow-List, nur oeffentliche Adressen, DNS-Pruefung mit Verbindungs-Pinning bei der Zustellung)
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
│       ├── knowledge_base.py     # KB-Endpoints /v1/kb/* (M06)
│       ├── ops.py                # Ops-Endpoints /v1/ops/* (M10, Admin-Key)
│       └── security.py           # Security Endpoints (JSON + Multipart)
├── config/
│   ├── kb_seed/                  # KB-Seed-Dokumente (M06)
│   │   ├── placeholders/         # 6 Bernd-Stand-in SOPs (Tag: placeholder)
│   │   └── real/                 # Echte Sicherheits-Dokumente
│   ├── prompts/
│   │   └── prompts.yaml          # Zentrale LLM-Prompt-Verwaltung (M03/M06)
│   └── taxonomy/
│       ├── taxonomy.yaml         # Risiko-Taxonomie: 23 Klassen, Rule-IDs (M04)
│       └── measures.yaml         # Massnahmenkatalog: 20 Codes mit Rollen (M04)
├── core/                         # Kernkomponenten
│   ├── models.py                 # Pydantic Schemas + Szenenmodell + Confidence
│   ├── db_models.py              # SQLAlchemy Models
│   ├── exceptions.py             # Custom Exceptions
│   ├── version.py                # Single Source of Truth fuer die Version
│   ├── metrics.py                # Prometheus-Registry API + Worker (M09)
│   ├── tracing.py                # OpenTelemetry-Setup opt-in (M09)
│   ├── temporal_context.py       # request_id-Propagation in Activities (M09)
│   ├── temporal_codec.py         # AES-256-GCM-Payload-Codec fuer Temporal (1.0.1)
│   ├── callback_url.py           # callback_url-Policy: Allow-List, DNS-Pruefung (1.0.1)
│   └── prompt_sanitizer.py       # Prompt Injection Protection
├── parsers/                      # Drehbuch-Parser
│   ├── base.py                   # Async ParserBase + Factory
│   ├── fdx.py                    # Final Draft XML Parser (M02)
│   ├── pdf.py                    # PDF Parser mit LLM-Strukturierung (M03) + OCR-Fallback
│   ├── pdf_ocr.py                # Tesseract-OCR fuer bildbasierte Seiten
│   ├── pdf_scene_splitter.py     # Deterministischer INT/EXT-Split (M03)
│   ├── pdf_llm_structurer.py     # LLM Structured Output pro Szene (M03)
│   ├── scene_heading.py          # Scene-Heading-Parser (DE/EN)
│   └── secure_xml.py             # defusedxml Wrapper
├── services/
│   ├── secure_buffer.py          # AES-verschluesselter Redis-Buffer
│   ├── taxonomy.py               # TaxonomyManager: Scoring, Validierung (M04)
│   ├── report_generator.py       # JSON + PDF Report Generator (M05)
│   ├── knowledge_base.py         # KB Ingest/Search/Cleanup mit pgvector (M06)
│   ├── parity.py                 # Cloud<->Lokal Paritaets-Harness (M11)
├── workflows/                    # Temporal Workflows
│   ├── security_check.py         # FDX/PDF Workflow-Router (M03)
│   ├── maintenance.py            # KB-TTL- und Metadaten-Cleanup als Temporal-Schedules (2 Activities)
│   └── activities.py             # 13 Activities inkl. LLM-Risikoanalyse und Zustellung (M03-M10)
├── worker/
│   └── main.py                   # Temporal Worker (3 Workflows, 15 Activities registriert)
├── llm/                          # LLM Provider Abstraktion
│   ├── base.py                   # BaseLLMProvider Interface (+ embed() M06)
│   ├── factory.py                # Provider Factory
│   ├── prompt_manager.py         # YAML Prompt Loader (M03, system-format fix M06)
│   ├── ollama.py                 # Ollama Provider (Generation + Embeddings, M06)
│   ├── mistral_cloud.py          # Mistral Cloud Provider (Schema-validated JSON, M06)
│   └── local_mistral.py          # Lokaler Produktiv-Adapter (mistral-small3.2, M11)
├── db/
│   ├── session.py                # Async Session Management
│   └── migrations/               # Alembic Migrations
├── tests/
│   ├── conftest.py               # Fixtures: SQLite/PostgreSQL-Engine, MockRedis, MockTemporal, API-Keys
│   ├── test_*.py                 # pytest-Suite (Ebenen und Dateien: docs/TESTING_GUIDE.md)
│   ├── run_*.py                  # Runner gegen einen laufenden Stack (keine pytest-Tests)
│   ├── parity/                   # Golden-Set fuer den Paritaetslauf (M11)
│   ├── fixtures/fdx/             # 12 synthetische FDX-Testdateien
│   └── fixtures/pdf/             # nur synthetische PDFs; private/ fuer lokale echte Drehbuecher (git-/docker-ignoriert)
├── scripts/                      # create_api_key, generate_secrets, seed_kb, smoke_prod, run_parity, test_llm, uat/
├── openapi/                      # OpenAPI 3.1.1 Vertrag
├── postman/                      # Postman Collection + Environment
├── secrets/                      # lokal erzeugte Secrets (git-ignoriert), siehe secrets/README.md
├── docker/                       # Dockerfiles (API + Worker), Temporal-Dynamic-Config (Cache-Limits, Prioritaet), Observability
├── requirements.lock             # gepinnte Laufzeit-Abhaengigkeiten (Dockerfiles)
├── requirements-dev.lock         # gepinnte Dev-Abhaengigkeiten (CI, Entwicklung)
├── .dockerignore                 # haelt Secrets, .env*, PDFs, tests/fixtures/pdf, kb_seed/real u. a. aus dem Build-Kontext
├── docker-compose.yml            # 6 Services (Entwicklung; Basis fuer die Overlays), ohne Ollama
├── docker-compose.prod.yml       # Prod-Overlay: GHCR-Images, nur Port 8000, Gunicorn, Limits, Log-Rotation
├── docker-compose.ollama.yml     # optional: lokales Ollama (ollama/ollama:0.33.3) fuer Entwicklung/offline
└── docker-compose.observability.yml  # Prometheus, Alertmanager, Grafana, Jaeger
```

---

## Tests

```bash
python3.11 -m venv .venv && . .venv/bin/activate
pip install -c requirements-dev.lock -e ".[dev]"      # Lock-Datei als Constraint, wie die CI
pytest                                                # SQLite in-memory + Mocks, mit Coverage
pytest -m "not temporal and not ocr"                  # offline / ohne Tesseract
TEST_POSTGRES_URL=postgresql+asyncpg://eki_user:eki_password@localhost:5432/eki_test pytest   # echter Dialekt (DB wird geleert!)
```

Die CI laeuft die Suite zweimal: im SQLite-Modus und gegen PostgreSQL + pgvector mit echtem
Redis und Coverage-Gate 95 %. SQLite allein reicht nicht -- es hat zwei Produktionsfehler
(`VARCHAR(9)`-Statusspalte, Zeitzonen bei API-Key-Ablauf) verdeckt. Marker: `temporal`
(Time-Skipping-Testserver, Download beim ersten Lauf) und `ocr` (Tesseract `deu`+`eng`).
Ruff (Lint + Format) und mypy (strict, `api core services workflows worker llm parsers`, 0 Befunde)
sind blockierende CI-Gates.

**Stand 2026-10-16 (Release 1.0.1):** 2049 Tests – PostgreSQL-Lauf 2049 bestanden, SQLite-Lauf
2005 bestanden / 44 uebersprungen (reine PostgreSQL-Tests), kein `xfail` mehr; Line-Coverage
**98,71 %**. Details je Modul: `docs/COVERAGE_REPORT.md`.

Testebenen, E2E-Pipeline, Abnahmetests gegen den Stack, CI-Jobs und Konventionen:
`docs/TESTING_GUIDE.md`.

---

## LLM Provider

```bash
# Standard und Produktion (.env.example): lokales Mistral ueber die externe Ollama-Instanz
LLM_PROVIDER=local_mistral
LOCAL_MISTRAL_MODEL=mistral-small3.2
OLLAMA_BASE_URL=http://<ollama-host>:11434     # externe Instanz (Inferenz + Embeddings)
# LOCAL_MISTRAL_BASE_URL=http://<anderer-host>:11434   # optional: eigener Endpoint nur fuer local_mistral
# Mit docker-compose.ollama.yml setzt das Overlay beide URLs auf http://ollama:11434

# Alternative lokale Modelle ueber den generischen Ollama-Provider
LLM_PROVIDER=ollama
OLLAMA_MODEL=gemma4:31b

# Mistral Cloud (nur Stage, in ENV=production abgelehnt, keine KB-Embeddings)
LLM_PROVIDER=mistral_cloud
MISTRAL_API_KEY=your-key
```

Ollama direkt auf dem Docker-Host (`OLLAMA_BASE_URL=http://host.docker.internal:11434`) braucht unter
Linux `extra_hosts: ["host.docker.internal:host-gateway"]` fuer `api` und `worker` (die Compose-Dateien
setzen das nicht). Im Prod-Overlay setzt Docker Compose `LLM_PROVIDER` selbst (Default `local_mistral`);
Abweichungen per `export` oder Projekt-`.env`, nicht per `.env.local` (`docs/CONFIGURATION.md` §2).
Ist keine Ollama-URL erreichbar, startet der Stack trotzdem (`/ready` prueft Ollama nicht); Jobs
scheitern dann beim ersten LLM-Aufruf (fail-closed).

---

## Bekannte Grenzen (Release 1.0.1)

- **Echte Drehbuecher in Git-Historie und alten Images:** Seit 1.0.1 enthalten Repository
  (Arbeitsstand) und Images nur synthetische Fixtures. Die frueher committeten echten Drehbuecher und
  Projektmappen stecken aber weiter in der **Git-Historie** (seit Maerz 2026) und in bereits
  veroeffentlichten **GHCR-Images bis einschliesslich 1.0.0**. Die Historie wird nach
  Projektentscheidung nicht umgeschrieben; Images <= 1.0.0 loeschen bzw. nicht ausserhalb der Filmakademie
  verteilen (`docs/DATENSCHUTZ.md` §8).
- Die Tabelle `audit_logs` wird nicht beschrieben (sie unterliegt trotzdem der 90-Tage-Loeschung).
- `priority` ordnet nur **wartende** Aufgaben einer Task-Queue; laufende Jobs werden nicht verdraengt,
  bei mehreren Task-Queue-Partitionen (Temporal-Standard: 4) ist die Reihenfolge nur naeherungsweise.
  Wirkt nur mit der Temporal-Dynamic-Config aus `docker/temporal/dynamicconfig/eki.yaml`
  (`matching.useNewMatcher: true`, `matching.priorityLevels: 10`).
- `callback_url` funktioniert nur mit Hosts aus `CALLBACK_URL_ALLOWED_HOSTS`, die auf oeffentliche
  Adressen aufloesen und ein gueltiges Zertifikat haben; lokal laesst sich ein Push an `callback_url`
  nicht gegen den HTTP-ePro-Mock testen.
- Knowledge Base nur mit `local_mistral`/`ollama` (Mistral Cloud hat kein `embed()`).
- **Redis 7.2:** gepinnt als letzte BSD-lizenzierte Linie; upstream wird 7.2 nur so lange unterstuetzt
  wie 7.4 (keine Termine veroeffentlicht). Danach Wechsel auf Valkey oder Redis >= 8 (AGPLv3-Option)
  noetig (`docs/OPERATIONS_GUIDE.md` §4.9).
- **Ollama-Version:** Die eKI ist nicht gegen eine bestimmte Ollama-Version getestet (die Tests ersetzen
  Ollama an der HTTP-Grenze). Den Pin im Overlay (`0.33.3`) an die Version der externen
  Produktionsinstanz angleichen und nach Ollama-Updates den Paritaetslauf wiederholen.
- **Offen (zurueckgestellt):** Abstimmung des fail-closed-Verhaltens mit ePro --
  ePro muss `failed`, `cancelled` und 410 beim Abruf als „keine automatische Bewertung" behandeln,
  nie als „kein Risiko" (`docs/EPRO_INTEGRATION_GUIDE.md` §3).

---

## Meilensteine

| # | Meilenstein | Status | Artefakte |
|---|---|---|---|
| M01 | Projektgeruest & OpenAPI v0.1 | Abgeschlossen | API-Framework, Auth, CI/CD, Postman |
| M02 | Parser Basis (FDX) & Testdataset | Abgeschlossen | FDX-Parser, Szenenmodell, SecureBuffer, 41 Tests |
| M03 | PDF & Streaming-Parsing | Abgeschlossen | PDF-Parser, LLM-Strukturierung, Prompt-YAML, Workflow-Refactoring, Risikoanalyse pro Szene, 73 Tests |
| M04 | Risiko-Taxonomie v1 & Scoring | Abgeschlossen | 23 Risiko-Klassen, Scoring-Engine, 20 Massnahmen-Codes, TaxonomyManager, 111 Tests |
| M05 | Reports (JSON/PDF) & One-Shot-GET | Abgeschlossen | JSON+PDF-Report, One-Shot-GET, Push/Pull Delivery, Job-Tracking, Idempotenz, 124 Tests |
| M06 | LLM-Adapter (Mistral Cloud) & KB-Grundlage | Abgeschlossen | Mistral Cloud Structured Output (Schema-validiert, Retry), pgvector-KB (kb_documents + kb_embeddings), Ollama-Embeddings (mxbai-embed-large), KB-Endpoints /v1/kb/*, 6 Placeholder-SOPs, idempotenter Seeder, Feature-Flag KB_RETRIEVAL_ENABLED (Default OFF), PromptManager-Bugfix, 40 zusaetzliche Tests |
| M07 | Grossdokument-Optimierung | Abgeschlossen | Opt-in Parallelisierung von PDF-Strukturierung und Risikoanalyse, prozessweiter Ollama-Concurrency-Cap, konfigurierbare Limits/Timeouts, 300-Seiten-Fixture + Benchmark-Runner mit `docker stats`-Snapshot, 16 zusaetzliche Tests |
| M08 | Security/Privacy & Delete-on-Delivery | Abgeschlossen | 6h-Retry-Fenster (`schedule_to_close_timeout`), `cleanup_buffer_activity` im Failure-Branch, opt-in `security.delivery.failed`-Webhook (Anhang 1), `cleanup_buffer_activity`, zentrale Logging-Konfiguration mit `SensitiveContentFilter`, Request-ID-Middleware, OpenAPI-`webhooks`-Block, 34 zusaetzliche Tests |
| M09 | Observability & SLOs | Abgeschlossen | `core/metrics.py` (20+ Metrikfamilien, API + Worker), HTTP-Metrik-Middleware, OpenTelemetry-Tracing opt-in (`core/tracing.py`), request_id/job_id-Korrelation via Temporal-Interceptor, Observability-Compose-Overlay (Prometheus, Alertmanager, Grafana, Jaeger), 11 Alert-Regeln, 3 provisionierte Dashboards, SLO-Dokument, KB-TTL-Schedule, 40 zusaetzliche Tests |
| M10 | Outbound-Adapter Hardening | Abgeschlossen | Zustell-Lebenszyklus mit `DELIVERING`, Attempt-Bookkeeping, Idempotency-Header an ePro, 408/425/429 retryable, nutzerbezogene race-sichere Idempotenz, Dead-Letter-Tabelle + `/v1/ops/*`, Pull-TTL-Watch, `X-One-Shot`-Header, Workflow-Timeout 10 h, Failover-Runbook, 30 zusaetzliche Tests (inkl. echter Workflow auf Temporal-Testserver) |
| M11 | Lokaler LLM-Adapter & Paritaetstests | Abgeschlossen | Echter `LocalMistralProvider` (mistral-small3.2, Modell-Check, Embeddings aus Settings), Prod-Guard gegen Cloud-Provider, Provider-Paritaetsangleichung, Golden-Set (16 Szenen) + Paritaets-Harness/CLI, GHCR-Release-Images in CI, `docker-compose.prod.yml` (Gunicorn, GPU, Limits), Betriebsleitfaden, `scripts/smoke_prod.py`, 31 zusaetzliche Tests |
| M12 | UAT-Paket & Uebergabe | Abgeschlossen | OpenAPI 1.0.0 + Contract-Test, Postman v1.0 + Environment, `CHANGELOG.md`, `docs/UAT/` (Testplan fuer 9 Abnahmetests, Protokollvorlage, Go-Live-Checkliste, Schulungsunterlagen, Uebergabe), ePro-Mock + automatisierte Abnahmetests 2-7, Coverage-Gate 80 %, Version 1.0.0 |

---

## Lizenz

Proprietary -- Filmakademie Baden-Wuerttemberg

## Kontakt

StefanAI -- Research & Development
E-Mail: info@stefanai.de
