# Changelog

Alle wesentlichen Änderungen der eKI API. Format nach [Keep a Changelog](https://keepachangelog.com/de/1.1.0/),
Versionierung nach [SemVer](https://semver.org/lang/de/). Ein Minor-Release pro Pflichtenheft-Meilenstein
(M0x → 0.x.0), `1.0.0` mit dem UAT-Paket (M12). Die Versionsquelle ist `core/version.py`.

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
