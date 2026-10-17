# Changelog

Alle wesentlichen Änderungen der eKI API. Format nach [Keep a Changelog](https://keepachangelog.com/de/1.1.0/),
Versionierung nach [SemVer](https://semver.org/lang/de/). Ein Minor-Release pro Pflichtenheft-Meilenstein
(M0x → 0.x.0), `1.0.0` mit dem UAT-Paket (M12). Die Versionsquelle ist `core/version.py`.

## [Unreleased]

Anlass: Auf der externen Ollama-Instanz liefen mehrere Drehbücher gleichzeitig; die Instanz wurde so
langsam, dass keines fertig wurde. Die eKI begrenzte nur gleichzeitige KI-Aufrufe je Prozess, nicht die
Zahl gleichzeitig verarbeiteter Dokumente: Alle Jobs liefen verzahnt, ihre KI-Schritte warteten auf den
Semaphore, und diese Wartezeit lief gegen das Activity-Timeout. Keine Migration.

### Neu
- **Dokument-Limit** (`MAX_CONCURRENT_DOCUMENTS`, Standard `1`; `0` = kein Limit wie bis 1.0.1): Höchstens
  so viele Jobs werden gleichzeitig verarbeitet. Alle weiteren warten als `pending` in einer
  Warteschlange (Priorität, dann Eingang). Der Workflow fragt mit dauerhaften Timern nach
  (`acquire_document_slot`, sofort, dann im Abstand von 5 s bis 60 s); ein wartender Job belegt keinen
  Worker-Platz, und es läuft kein Activity-Timeout. Freigabe nach der Risikoanalyse bzw. bei Fehler/Abbruch (`release_document_slot`).
  Zustand in Redis (`eki:docslot:*`, nur Job-IDs), Plätze mit 30-Minuten-Lease, die die KI-Schritte
  verlängern. Die Grenze liest der Worker beim Start (nach Neuerzeugung gilt ein neuer Wert auch für schon
  wartende Jobs); ob ein Job sich anstellt, friert die API beim Jobstart ein.
- **Höchstwartezeit** (`DOCUMENT_QUEUE_TIMEOUT_SECONDS`, Standard 7200): danach endet der Job mit `failed`
  und der inhaltsfreien Meldung „Kein freier Verarbeitungsplatz innerhalb von … min …“ (Webhook-Grund
  `processing_failed`). Der Start-Validator prüft, dass Wartezeit + `BUFFER_TTL_SECONDS` unter
  `TEMPORAL_WORKFLOW_EXECUTION_TIMEOUT` bleiben und die Wartezeit kürzer als `BUFFER_TTL_SECONDS` ist
  (nur bei Limit > 0; Zeit für die Verarbeitung reserviert er nicht).
- **Heartbeats in den KI-Schritten** (`structure_scene_llm`, `analyze_scene_risk`, alle 10 s): Endet der
  Workflow (Fehler, Abbruch, Terminate), bricht ein laufender KI-Schritt jetzt nach spätestens etwa 30 s
  ab (so oft sendet das SDK Heartbeats höchstens), statt bis zu seinem eigenen Timeout (600 s) weiter auf
  Ollama zu warten bzw. Ollama rechnen zu lassen.
- Metriken `eki_document_slots_in_use` und `eki_document_queue_length` (Worker); das Startlog des Workers
  nennt zusätzlich das Dokument-Limit und `OLLAMA_MAX_CONCURRENT_REQUESTS`.

### Für ePro relevant
- Jobs können länger `pending` bleiben (Warteschlange). Polling-Timeout auf mindestens 4,5 h erhöhen
  (bis 2 h Wartezeit + rund 2 h Verarbeitung), wartende Jobs seltener abfragen (1000 Anfragen/h je
  API-Key), `docs/EPRO_INTEGRATION_GUIDE.md` §3.

## [1.0.1] – 2026-10-16 – Stabilisierung zur Übergabe

Ergebnis eines Test- und Doku-Reviews vor der Übergabe und der daraus folgenden Projektentscheidungen.
Tests 465 → 2049, Coverage 80,78 % → 98,71 %; die Suite läuft auch gegen echtes PostgreSQL. Mehrere Fehler traten nur dort bzw. bei einer frischen Installation auf und waren für die
bisherige SQLite-Suite unsichtbar. **Die Abnahmetests AT2–AT7 sind auf diesem Stand zu wiederholen.**
DB-Revision (head): `c4e6a8b0d2f1`. Upgrade-Schritte: `docs/OPERATIONS_GUIDE.md` → „Upgrade 1.0.0 → 1.0.1".

### Entfernt / geändert – für Clients sichtbar
- **`POST /v1/security/check` entfernt** (Antwort 404). Der Endpunkt war ein M01-Stub ohne Analyse und
  lieferte immer einen festen Platzhalter-Report; produktiv ist ausschließlich `check:async`.
- **`/v1/kb/*` nur noch mit Admin-API-Key** (vorher genügte jeder Key, auch der ePro-Service-Key).
- **`callback_url` ist jetzt aktiv:** Push-Ziel des Jobs statt `EPRO_BASE_URL/...`; impliziert
  `delivery=push`, `delivery=pull` + `callback_url` → 422. Nur HTTPS, keine Zugangsdaten in der URL,
  Hosts aus `CALLBACK_URL_ALLOWED_HOSTS`, nur öffentliche IP-Adressen (bei der Zustellung erneut geprüft).
- **`priority` ist jetzt aktiv:** 1 (höchste) … 10 wird als Temporal-Task-Queue-Priorität übergeben
  (`TEMPORAL_PRIORITY_LEVELS`; Temporal-Dynamic-Config `matching.useNewMatcher`/`matching.priorityLevels`).

### Sicherheit & Datenschutz
- **Temporal-Payloads verschlüsselt** (AES-256-GCM, Schlüssel aus `API_SECRET_KEY` abgeleitet): Workflow-
  und Activity-Daten, Fehlermeldungen und Header stehen nicht mehr im Klartext in der Temporal-History
  bzw. -UI. Ältere Klartext-Historien bleiben lesbar.
- **Aufbewahrung 90 Tage** (`METADATA_RETENTION_DAYS`): nächtlicher Lösch-Zeitplan `eki-metadata-cleanup`
  für Job-/Report-Metadaten, Dead Letters, inaktive/abgelaufene API-Keys und Audit-Logs (Migration
  `c4e6a8b0d2f1` mit Indizes). Zeitplan-Änderungen (Cron/aktiviert) greifen jetzt beim Worker-Start,
  auch für `eki-kb-cleanup`.
- **Echte Drehbücher entfernt** aus Repository und Docker-Images (synthetische Ersatz-Fixtures; lokale
  Testdaten in `tests/fixtures/pdf/private/`, git-ignoriert). In der Git-Historie und in Images ≤ 1.0.0
  sind sie weiterhin enthalten.

### Betrieb & Abhängigkeiten
- **Ollama läuft extern:** der `ollama`-Dienst ist nicht mehr Teil des Compose-Stacks; optionales Overlay
  `docker-compose.ollama.yml` für Entwicklung/Offline, gepinnt auf `ollama/ollama:0.33.3`.
- **Redis auf `7.2-alpine` gepinnt** (letzte BSD-lizenzierte Linie). Bestehende Volumes aus Redis 7.4
  sind mit 7.2 nicht lesbar → vor dem Upgrade leeren (siehe Upgrade-Schritte). 7.2 wird upstream nicht
  unbegrenzt unterstützt; danach Wechsel auf Valkey oder Redis ≥ 8 (AGPLv3) nötig.
- **Unbenutzte Abhängigkeiten entfernt** (u. a. `python-jose` mit bekannten CVEs, PyJWT, passlib, bcrypt,
  aiohttp; 99 → 78 Laufzeitpakete); `requests` explizit deklariert.
- **Typprüfung:** mypy (strict) ohne Befunde (71 → 0) über `api core services workflows worker llm parsers`;
  der CI-Schritt ist jetzt blockierend.

### Behoben – kritisch
- **Frische Installation lief nicht:** `sqlalchemy` ohne `[asyncio]`-Extra; SQLAlchemy ≥ 2.1 installiert
  `greenlet` nicht mehr → jeder DB-Zugriff brach ab. Jetzt `sqlalchemy[asyncio]`.
- **`alembic upgrade head` brach ab:** SQLAlchemy 2.1 löst `postgresql://` auf psycopg (v3) auf, installiert
  ist psycopg2. `db/migrations/env.py` nutzt jetzt explizit `+psycopg2`.
- **Asynchrone Jobs scheiterten in der Zustellphase:** `job_metadata.status` war `VARCHAR(9)`, `DELIVERING` hat
  10 Zeichen (PostgreSQL: „value too long"). Migration `a7c9e1f3b5d2` (`VARCHAR(20)`).
- **Risikoanalyse „fail-open":** jeder LLM-Fehler (Ollama nicht erreichbar, Timeout, Prompt-Filter) wurde
  verschluckt, die Szene bekam 0 Befunde und der Report wirkte risikofrei. Jetzt werden transiente Fehler
  wiederholt und lassen den Job danach sichtbar scheitern; ein Filter-Treffer beendet den Job mit
  „Szene <nr>: vom Prompt-Injection-Filter blockiert – manuelle Prüfung erforderlich".
- **Prompt-Injection-Filter blockte normalen Drehbuchtext** („User:innen" in einem echten Fixture-Drehbuch,
  „act as if", „---", „==="). Muster präzisiert, Chat-Template-Tokens ergänzt; ein Regressionstest jagt jede
  Fixture-Szene durch den Filter.
- **Lange Szenen wurden ab 10 000 Prompt-Zeichen still abgeschnitten**; jetzt Analyse in überlappenden
  Abschnitten.
- **Inhalte gingen vor der Analyse verloren:** FDX ohne Szenenüberschrift ergab 0 Szenen, Text vor der ersten
  Überschrift und `<DualDialogue>` wurden verworfen; OCR-Seiten liefen unter Last in den Timeout
  (`OMP_THREAD_LIMIT=1` in beiden Images und im Code).
- **Produktions-Overlay band den Quellcode vom Host ein und veröffentlichte die Temporal-UI:**
  `volumes: []`/`ports: []` werden von Compose gemergt; jetzt `!reset []` (Docker Compose ≥ 2.24).
- **Produktions-Validator akzeptierte die Platzhalter aus `.env.example`**; Platzhalter werden abgelehnt.
- **Pfad-Traversal:** Multipart-`project_id` wurde nicht validiert und landete in der ePro-Push-URL.

### Behoben
- **API-Eingaben:** über 15 Fälle führten zu 500/503 statt 4xx (kaputtes JSON → 400, Nicht-Objekt → 422,
  `delivery` nur `pull|push`, Multipart wie JSON validiert, `script_id` int32, Steuerzeichen im
  Idempotency-Key, Multibyte-Zeichen an Byte 1000, Ops-`offset`); Starlette-404/405 im Fehler-Envelope.
- **`GET /reports/{id}` nach Pufferablauf/Push** liefert 410 statt eines erfundenen Reports mit 0 Befunden.
- **Idempotenz:** scheitert der Jobstart (Redis/Temporal), wird der Key freigegeben – vorher bekam der
  Retry nach dem 503 dauerhaft den toten Job.
- **Rate-Limit pro Key** ließ sich über die Schreibweise des `Authorization`-Headers umgehen.
- **Datenschutz:** Findings (mit Drehbuchzitaten) laufen nicht mehr durch die Temporal-History; nach
  Verarbeitungsfehlern wird der Puffer geleert (blieb bis zu 6 h); der Filter loggt keine Textvorschau mehr.
- **ePro:** Fehler-Webhook auch bei Verarbeitungsfehlern (`prompt_blocked`, `parsing_failed`,
  `processing_failed`); Abbruch setzt `cancelled`; Pull-Reports gelten erst nach dem Abruf als zugestellt.
- **Betrieb:** Metrik „completed" wurde seit M10 nie erfasst (SLO-Erfolgsquote 0); DB-Fehler bei
  Statusupdates wurden verschluckt (Jobs hingen in RUNNING); Parsing-Fehler nicht mehr wiederholt; echte
  Verarbeitungszeit statt fest 1,0 s; Engines im `finally` freigegeben; Log-Rotation im Prod-Overlay.
- **Alarm `EkiDeadLettersPending`** feuerte nach dem Quittieren weiter (veralteter Gauge-Wert des
  Workers); der Wert wird jetzt bei jedem `/metrics`-Scrape aus der Datenbank berechnet.
- **KB:** Dedupe pro Mandant auch im Schema (Migration `b8d0f2a4c6e3`; vorher 503 für den zweiten Mandanten),
  paralleler Duplikat-Upload → 409.
- **Robustheit gegen LLM-Ausgaben:** fehlerhafte Felder degradieren eine Szene statt den Parse abzubrechen;
  kaputte 2xx-Antworten → `LLMException`; `validate_finding` toleriert ungültige Werte.
- **Reports:** Werte im PDF-Markup escaped (ein `<` ließ das PDF still wegfallen), exotische Ziffern in
  Szenennummern, echte Engine-Version statt fest `0.6.0`.
- Zeitzonenbewusste UTC-Zeitstempel (abgelaufene API-Keys blieben auf Hosts mit TZ ≠ UTC gültig); API nutzt
  `TEMPORAL_NAMESPACE`; Fehlerantworten tragen immer `request_id`; unbehandelte Fehler korrekt als 503/500
  gezählt; PDF-Splitter erkennt „1. INT.", „12.1", „I/E.", „INNEN-AUSSEN".

### Hinzugefügt
- Tests: E2E-Pipeline (`test_e2e_pipeline.py`), PostgreSQL-Integration inkl. Migrationszyklus
  (`test_pg_integration.py`), Edge-Cases (`test_edge_*.py`), Unit-Lücken (`test_cov_*.py`), Regressionen.
  `TEST_POSTGRES_URL` / `TEST_REDIS_URL` schalten echten Dialekt bzw. echtes Redis zu.
- CI: Migrationszyklus (upgrade → check → downgrade → upgrade), SQLite- **und** PostgreSQL-Lauf, `alembic`
  im Docker-Image, Prüfung des Prod-Overlays (nur API-Port, keine Code-Mounts); Coverage-Gate 80 → 95 %.
- Lockfiles `requirements.lock` / `requirements-dev.lock` (Python 3.11) als Constraints in Dockerfiles und CI.
- Doku: `docs/CONFIGURATION.md` (alle Einstellungen), `docs/DATENSCHUTZ.md` (Datenbestand, Fristen,
  offene Entscheidungen), neu geschriebene `TESTING_GUIDE`, `OPERATIONS_GUIDE` (Runbooks, Backup/Restore,
  Migrationen, Troubleshooting), `LLM_INTEGRATION`, `COVERAGE_REPORT`; OpenAPI an das Verhalten angeglichen;
  historische und veraltete Dokumente mit Hinweisbanner.

## [1.0.0] – 2026-09-16 – M12 UAT-Paket & Übergabe

### Hinzugefügt
- OpenAPI **1.0.0** (`openapi/eki-api-v1.0.yaml`): alle Endpunkte inkl. `/v1/ops/*`, `X-One-Shot`-Header,
  `JobStatus.delivering`, `DeliveryStatus`, Dead-Letter-Schemata, `Forbidden`-Response, `X-Request-ID`-Header.
- Contract-Test `tests/test_m12_openapi_contract.py`: FastAPI-Routen ⇄ Spec, Enums, Version, Webhook-Payload.
- Postman Collection **v1.0** + Environment-Datei; neue Ordner „9. Operations" und „10. Abnahmetests".
- UAT-Paket `docs/UAT/`: Testplan für die 9 Abnahmetests, Protokollvorlage, Go-Live-Checkliste,
  Schulungsunterlagen, Übergabedokument.
- `scripts/uat/mock_epro_server.py` (ePro-Attrappe für Push/Webhook) und
  `scripts/uat/run_acceptance_tests.py` (Abnahmetests 2, 3, 4, 5, 6, 7 automatisiert gegen laufenden Stack).
- Dieses CHANGELOG.

### Geändert
- Coverage-Gate in der CI auf 80 % für die Kernpakete angehoben.
- README: Meilensteintabelle vollständig, Version 1.0.0.

### Entfernt
- Postman Collections v0.5/v0.6 (durch v1.0 ersetzt).

## [0.11.0] – 2026-09-16 – M11 Lokaler LLM-Adapter & Paritätstests

### Hinzugefügt
- `llm/local_mistral.py`: echter Produktiv-Adapter (Default `mistral-small3.2`, `LOCAL_MISTRAL_MODEL`,
  `LOCAL_MISTRAL_BASE_URL`), Healthcheck prüft gepulltes Modell, `ensure_model_available()` mit Pull-Hinweis.
- Guard `LLM_ALLOW_EXTERNAL_PROVIDERS` (Default `false`): `mistral_cloud` wird in `ENV=production` abgelehnt.
- Paritäts-Harness `services/parity.py`, CLI `scripts/run_parity.py`, Golden-Set `tests/parity/golden_scenes.yaml`
  (16 Szenen), Report-Rahmen `docs/M11_PARITY_REPORT.md`.
- CI-Job `release-images`: GHCR-Images `eki-api`/`eki-worker` bei `v*`-Tags (Tag ⇄ `core/version.py`).
- `docker-compose.prod.yml`: GHCR-Images, Gunicorn + UvicornWorker, GPU-Reservierung, Redis `appendonly`, Limits.
- `scripts/smoke_prod.py` (Abnahmetest 9: E2E, One-Shot, 410, keine externen LLM-Calls).
- `docs/OPERATIONS_GUIDE.md` (Betriebsleitfaden).

### Geändert
- Factory übergibt dem lokalen Adapter jetzt die Embedding-Einstellungen (vorher ignoriert).
- Provider-Parität: `generate_structured` Temperature-Default 0.2 überall, `generate_chat` mit Sanitizer und
  System-Lock, `embed` unter dem Ollama-Concurrency-Cap, `provider_name` in Fehlerdetails.

## [0.10.1] – 2026-09-16 – OCR-Fallback

### Hinzugefügt
- `parsers/pdf_ocr.py`: Tesseract-OCR für Seiten ohne Textebene (`OCR_ENABLED`, `OCR_LANGUAGES`,
  `OCR_MAX_PAGES`, `OCR_DPI`, `OCR_PAGE_TIMEOUT_SECONDS`), in-memory Rendering, Auto-Degradation ohne Binary.
- Fixtures `scanned_screenplay.pdf`, `blank_middle_page.pdf`; Tesseract (`deu`, `eng`) in beiden Docker-Images.

### Behoben
- Seitenindex-Drift: leere/gescannte Seiten wurden aus `page_texts` entfernt und verschoben den Seiten-Fallback-Splitter.
  OCR-Text bzw. Leerstring wird jetzt an der Seitenposition eingesetzt.

### Geändert
- `extract_pdf_text` liefert `PdfExtraction` (tuple-kompatibel); Szenen aus OCR-Seiten erhalten Confidence × 0,7.

## [0.10.0] – 2026-09-16 – M10 Outbound-Adapter Hardening

### Hinzugefügt
- Zustell-Lebenszyklus `pending → delivering → delivered | dead_lettered` mit `job_metadata.delivery_*`-Spalten,
  `JobStatus.delivering`, Tabelle `delivery_dead_letters` (inhaltsarm), `api_keys.is_admin`
  (Migration `f1a2b3c4d5e6`).
- Activities `record_dead_letter`, `check_report_retrieved`; Pull-TTL-Watch im Workflow (Nichtabholung →
  Cleanup + Dead Letter + Webhook).
- `/v1/ops/summary`, `/v1/ops/jobs`, `/v1/ops/dead-letters[/{id}[:acknowledge]]` (Admin-Key).
- Header an ePro: `Idempotency-Key` (= `report_id`), `X-EKI-Job-Id`, `X-EKI-Attempt`, `X-Request-ID`.
- `X-One-Shot: true` beim One-Shot-GET; Zustell-Metadaten im Job-Status.
- `docs/M10_FAILOVER_RUNBOOK.md`; Failover-Tests mit echtem Workflow auf Temporal-Testserver (Time-Skipping).

### Behoben
- Job wurde vor dem Push auf `COMPLETED` gesetzt; `ReportMetadata` bei jedem Retry neu eingefügt.
- `attempts_used` war hartkodiert 1; 408/425/429 galten als Hard-Fail.
- Idempotenz-Key war mandantenübergreifend unique; Race zweier gleicher POSTs endete im 500.
- Buffer-Eintrag blieb bei fehlgeschlagenem Workflow-Start bestehen.
- `TEMPORAL_WORKFLOW_EXECUTION_TIMEOUT` (4 h) lag unter dem 6-h-Retry-Fenster → jetzt 10 h.

## [0.9.0] – 2026-09-16 – M09 Observability & SLOs

### Hinzugefügt
- `core/metrics.py`: Prometheus-Registry für API und Worker (HTTP, Jobs, Szenen/Findings, LLM inkl. Queue-Wait,
  Delivery, Webhook, One-Shot-Abrufe, Buffer-Deletes, KB, Sanitizer, Dead Letters, Build-Info).
- HTTP-Metrik-Middleware mit Route-Templates; Worker-Exporter (`PROMETHEUS_PORT`, nur internes Netz).
- `core/tracing.py`: OpenTelemetry opt-in (FastAPI, SQLAlchemy, httpx, Temporal-TracingInterceptor).
- `core/temporal_context.py`: `request_id` als Temporal-Header bis in jede Activity; structlog-Bindung von
  `request_id`, `job_id`, `workflow_id`, `activity`, `attempt`.
- `docker-compose.observability.yml` (Prometheus, Alertmanager, Grafana, Jaeger), 11 Alert-Regeln,
  3 generierte Dashboards, `docs/M09_SLO.md`.
- KB-TTL-Cleanup als Temporal-Schedule `eki-kb-cleanup` (`workflows/maintenance.py`).
- CI-Job `observability-validation` (promtool, amtool, compose config).

### Geändert
- `OTEL_ENABLED` Default `false` (war `true` ohne Wirkung).

## [0.8.0] – 2026-09-16 – Hygiene-Sprint

### Hinzugefügt
- `core/version.py` als einzige Versionsquelle (pyproject dynamic, FastAPI, `/health`, OpenAPI).
- Marker `temporal`, `ocr` in pytest; Test-Runner-Skripte ins Repo.

### Behoben
- 8 vorbestehende Testdefekte; `priority` (1–10) wird auf JSON- und Multipart-Pfad von `check:async`
  serverseitig validiert (vorher ungeprüft).
- `.spectral.yaml` referenzierte nicht existierende Regeln.

### Geändert
- Enums auf `StrEnum`; gesamtes Repo Ruff-formatiert; CI-Gates hart (Ruff, Bandit `-ll`, Spectral,
  `--cov-fail-under=70`); Security-Audit-Status dokumentiert (7/8 behoben, #7 begründet).

## [0.7.0] – 2026-05-25 – M08 Security/Privacy & Delete-on-Delivery

- 6-h-Retry-Fenster (`schedule_to_close_timeout`), `cleanup_buffer_activity` im Failure-Branch,
  opt-in Webhook `security.delivery.failed` (Anhang 1), zentrale Logging-Konfiguration mit
  `SensitiveContentFilter`, Request-ID-Middleware, OpenAPI-`webhooks`-Block.

## [0.7.0-pre] – 2026-05-22 – M07 Großdokument-Optimierung

- Opt-in Parallelisierung (`LLM_PARALLEL_ENABLED`, `*_CONCURRENCY`), prozessweiter Ollama-Cap und Throttle,
  konfigurierbare Limits/Timeouts, 300-Seiten-Fixture und Benchmark-Runner, `LLM_PROVIDER`-Validierung.

## [0.6.0] – 2026-05-21 – M06 LLM-Adapter (Mistral Cloud) & KB-Grundlage

- Mistral-Cloud-Adapter mit JSON-Mode, Schema-Validierung und Self-Correcting-Retry; Knowledge Base mit pgvector
  (`kb_documents`, `kb_embeddings`), Ollama-Embeddings, `/v1/kb/*`, Seeder, Feature-Flag `KB_RETRIEVAL_ENABLED`;
  Ollama schema-constrained Output und Thinking-Model-Support.

## [0.5.0] – 2026-02-08 … 2026-03-26 – M05 Reports (JSON/PDF) & One-Shot-GET

- JSON- und PDF-Report, One-Shot-GET mit atomarem Update, Push/Pull-Delivery, Idempotenz-Key, Job-Status-Tracking,
  ePro-Push via `set-risk-assessment`, Integrationsleitfaden, Postman v0.5, Temporal 1.29.4.

## [0.4.0] – 2026-02-08 – M04 Risiko-Taxonomie v1 & Scoring

- 23 Risikoklassen in 3 Kategorien, Scoring-Engine (Likelihood × Impact), 20 Maßnahmen-Codes, `TaxonomyManager`,
  Pflichtenheft-Felder `evidence`, `vulnerability`, `complexity`, `exposure_duration`.

## [0.3.0] – 2026-02-08 – M03 PDF & Streaming-Parsing

- PDF-Parser (pdfplumber), deterministischer INT/EXT-Split, LLM-Strukturierung pro Szene, YAML-Prompt-Management,
  getrennte FDX-/PDF-Workflows, Risikoanalyse pro Szene.

## [0.2.0] – 2026-02-07 – M02 Parser Basis (FDX) & Testdataset

- FDX-Parser (defusedxml), Szenenmodell, `SecureBuffer` (Fernet/Redis, TTL), 12 FDX-Fixtures.

## [0.1.0] – 2026-01-30 … 2026-02-06 – M01 Projektgerüst & OpenAPI v0.1

- FastAPI-Grundgerüst, API-Key-Auth (SHA-256), Rate Limiting, OpenAPI 3.1.1, CI/CD, Docker Compose,
  Postman-Collection, Security-Audit und Härtung (Proxy-Header, Compose-Ports, Secrets).
