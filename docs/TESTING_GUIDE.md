# eKI API – Testleitfaden

Wie die eKI getestet wird, welche Testebenen es gibt, wie man sie lokal ausführt und
was die CI prüft. Stand: Release 1.0.0. Aktuelle Zahlen: Abschnitt [Kennzahlen](#9-kennzahlen).

---

## 1. Testebenen im Überblick

| Ebene | Was wird geprüft | Infrastruktur | Wo |
|---|---|---|---|
| Unit/Komponente (Default) | Parser, Taxonomie, Reports, Provider, API-Routen, Activities, Workflows mit Stubs | SQLite in-memory, `MockRedis`, `MockTemporalClient`, gemocktes LLM | `pytest` |
| PostgreSQL-Modus | dieselbe Suite gegen den echten Dialekt + Migrationen, pgvector-KB, Zustell-Bookkeeping | PostgreSQL 16 + pgvector (`TEST_POSTGRES_URL`) | `pytest`, CI |
| Temporal-Tests (Marker `temporal`) | echte Workflows auf dem Time-Skipping-Testserver (6-h-Fenster, Pull-Watch, Fehlerzweige) | Temporal-Testserver (Download beim ersten Lauf) | `pytest` |
| E2E-Pipeline | API → Workflow → echte Activities → Redis → Zustellung, LLM nur an der Provider-Grenze ersetzt | Temporal-Testserver + echtes Redis (`TEST_REDIS_URL`) | `tests/test_e2e_pipeline.py` |
| OCR (Marker `ocr`) | Tesseract-Fallback für Scan-Seiten | `tesseract` mit `deu`+`eng` | `tests/test_pdf_ocr.py` |
| Vertrag | laufende App ↔ `openapi/eki-api-v1.0.yaml`; Spec-Lint | – / Node.js | `tests/test_m12_openapi_contract.py`, Spectral |
| Abnahme gegen laufenden Stack | Abnahmetests AT2–AT7, Prod-Smoke (AT9), Parität, Großdokument | Docker-Stack, ePro-Mock | `scripts/uat/`, `scripts/smoke_prod.py`, `scripts/run_parity.py`, `tests/run_*.py` |

pytest sammelt nur `tests/test_*.py`. Die Skripte `tests/run_*.py`,
`tests/build_large_fixture.py` und `tests/generate_pdf_fixtures.py` sind Werkzeuge,
keine Tests (§6.4).

---

## 2. Einrichtung

Python 3.11 wie CI und Docker-Images (3.13 funktioniert ebenfalls). Abhängigkeiten
immer mit der Lock-Datei als Constraint installieren:

```bash
python3.11 -m venv .venv && . .venv/bin/activate
pip install --upgrade pip
pip install -c requirements-dev.lock -e ".[dev]"

# Systempakete für die OCR-Tests (ohne sie werden diese Tests übersprungen)
sudo apt-get install -y tesseract-ocr tesseract-ocr-deu tesseract-ocr-eng
```

Optional für PostgreSQL-Modus und E2E-Tests (Wegwerf-Container; der Entwicklungs-Stack
veröffentlicht 5432/6379 nicht, es gibt also keinen Portkonflikt):

```bash
docker run -d --name eki-test-pg -p 5432:5432 \
  -e POSTGRES_DB=eki_test -e POSTGRES_USER=eki_user -e POSTGRES_PASSWORD=eki_password \
  pgvector/pgvector:pg16
docker run -d --name eki-test-redis -p 6379:6379 redis:7-alpine
```

Der DB-Nutzer braucht das Recht `CREATEDB` (die Migrationstests legen eigene
Wegwerf-Datenbanken `<db>_mig_<hex>` an und verbinden sich dafür mit der Datenbank
`postgres`) und die Extension `vector` – beides erfüllt der Superuser des
`pgvector`-Images.

---

## 3. Tests ausführen

```bash
pytest                                  # ganze Suite, SQLite-Modus, mit Coverage (addopts)
pytest --no-cov -q                      # schneller, wie der erste CI-Testschritt
pytest -m "not temporal"                # offline: ohne Temporal-Testserver-Download
pytest -m "not temporal and not ocr"    # zusätzlich ohne Tesseract
pytest -m temporal                      # nur Temporal-Tests
pytest tests/test_security.py::TestAuthentication -v
pytest -x --pdb tests/test_reports.py   # beim ersten Fehler in den Debugger
tests/scripts/run_tests.sh [pytest-Argumente]   # Wrapper mit Coverage-HTML (htmlcov/)
```

`tests/scripts/run_tests.sh` installiert vorab `.[dev]` **ohne** Lock-Constraint und kann
damit Pakete der venv verändern; in einer per Lock-Datei eingerichteten venv lieber
`pytest` direkt aufrufen.

**PostgreSQL-Modus mit echtem Redis** (entspricht dem zweiten CI-Testschritt):

```bash
export TEST_POSTGRES_URL=postgresql+asyncpg://eki_user:eki_password@localhost:5432/eki_test
export DATABASE_URL=$TEST_POSTGRES_URL
export TEST_REDIS_URL=redis://localhost:6379/4
export REDIS_URL=redis://localhost:6379/0
export ENV=test
pytest --cov --cov-report=term-missing --cov-fail-under=80
```

> **Achtung:** Die Datenbank hinter `TEST_POSTGRES_URL` wird zu Beginn der Session
> geleert (`drop_all`/`create_all`), die Redis-DB hinter `TEST_REDIS_URL` vor und nach
> jedem E2E-Test (`FLUSHDB`; DB 0 wird verweigert). Nie auf echte Daten zeigen lassen.

**Migrationen wie in der CI** (gegen dieselbe Wegwerf-DB):

```bash
export DATABASE_URL=postgresql+asyncpg://eki_user:eki_password@localhost:5432/eki_test ENV=test
alembic upgrade head && alembic check && alembic downgrade base && alembic upgrade head
```

**Coverage:** `pyproject.toml` misst `api`, `core`, `services`, `workflows`, `parsers`,
`llm` (greenlet-fähig, sonst fehlen Zeilen nach DB-Aufrufen). Die Schwelle
`--cov-fail-under=80` setzt nur die CI im PostgreSQL-Lauf; lokal gibt es kein Gate.

---

## 4. Die Ebenen im Detail

### 4.1 Unit-/Komponententests (Default)

`tests/conftest.py` stellt die gemeinsame Umgebung:

| Fixture / Hilfsobjekt | Zweck |
|---|---|
| `test_engine` (Session) | SQLite in-memory, im PG-Modus PostgreSQL |
| `db_session` | AsyncSession für Testdaten |
| `client`, `async_client` | FastAPI-Testclient; überschreibt `get_db`, `get_redis`, `get_temporal_client` |
| `mock_redis` (`MockRedis`) | In-Memory-Redis für SecureBuffer und Rate-Limiting |
| `mock_temporal` (`MockTemporalClient`) | zeichnet `start_workflow`-Aufrufe auf |
| `test_api_key`, `test_api_key_user2`, `test_admin_key` | API-Keys in der Test-DB (Klartext + Modell) |
| `auth_headers`, `auth_headers_user2`, `admin_headers` | passende `Authorization`-Header |
| `MINIMAL_PDF_BYTES`, `requires_postgres` | minimales gültiges PDF; Skip-Marker für PG-only-Tests |

`conftest.py` setzt außerdem `OMP_THREAD_LIMIT=1` (wie die Docker-Images), damit
Tesseract unter Last nicht in den Seiten-Timeout läuft.

Das LLM wird in Unit-Tests gemockt (Provider-Objekte oder `httpx.MockTransport`);
`asyncio_mode = "auto"`, async-Tests brauchen keinen Marker.

**Der blinde Fleck von SQLite.** SQLite erzwingt keine `VARCHAR(n)`-Längen und liefert
Zeitstempel ohne Zeitzone. Genau das hat zwei Produktionsfehler verdeckt, die die
SQLite-Suite grün ließ:

- `JobStatus.DELIVERING` (10 Zeichen) passte nicht in die Spalte `VARCHAR(9)` – auf
  PostgreSQL scheiterte jeder asynchrone Job in der Zustellphase
  (`value too long for type character varying(9)`; behoben mit Migration `a7c9e1f3b5d2`).
- naive Zeitstempel: Auf einem Host außerhalb von UTC blieben abgelaufene API-Keys
  stundenlang gültig.

Außerdem kann SQLite `JSONB` und `vector(1024)` nicht abbilden; die KB-Tabellen fehlen
dort ganz (so blieb auch der global statt je Mandant eindeutige `content_hash` unentdeckt,
behoben mit Migration `b8d0f2a4c6e3`). Deshalb läuft die CI die komplette Suite zusätzlich im PostgreSQL-Modus mit
Coverage-Gate. `tests/test_db_dialect_regressions.py` hält beide Fehler fest – die Tests
laufen auch auf SQLite, unterscheiden aber nur auf PostgreSQL.
`tests/test_api_consistency.py` sichert weitere Übergabe-Korrekturen ab (`request_id` in
jeder Fehlerantwort, gleicher Temporal-Namespace für API und Worker, echte
`engine_version` im Report).

### 4.2 PostgreSQL-Modus (`TEST_POSTGRES_URL`)

Mit gesetzter Variable legt `conftest.py` die Extension `vector` an, löscht und erzeugt
das Schema aus `core/db_models.py` (`Base.metadata`) und führt **dieselbe Suite** gegen
PostgreSQL aus (NullPool, eine Session je Request). Zusätzlich laufen nur dann:

- `tests/test_pg_integration.py`: Alembic `upgrade head` auf leerer DB, Drift-Prüfung
  gegen `core/db_models.py` (wie `alembic check`), vollständiges Downgrade/Re-Upgrade,
  die Verbreiterung von `job_metadata.status`; KB-Service auf echtem pgvector (Ingest,
  Dedupe, Chunking, Cosine-Ranking, Mandantentrennung, Tags, TTL-Cleanup, Verschlüsselung
  der Originale); `/v1/kb`-Endpunkte ohne Mocks; Zustell-Bookkeeping.
- mit `@requires_postgres` markierte Einzeltests anderer Dateien.

Ohne die Variable werden diese Tests übersprungen.

### 4.3 Temporal-Tests (Marker `temporal`)

`tests/test_m10_workflow_failover.py`, `tests/test_m09_context_propagation_e2e.py`,
`tests/test_cov_security_check_workflow.py` und `tests/test_e2e_pipeline.py` starten den
Time-Skipping-Testserver des Temporal-SDK (`WorkflowEnvironment.start_time_skipping()`).
Er wird beim **ersten** Lauf heruntergeladen und danach zwischengespeichert – einmal
Netzzugang nötig. Offline: `pytest -m "not temporal"`. Durable Timer (6-h-Retry-Fenster,
Pull-TTL) laufen dort in Sekunden ab.

### 4.4 E2E-Pipeline (`tests/test_e2e_pipeline.py`)

Läuft den echten Produktionspfad in einem Prozess: HTTP-API (echte API-Key-Prüfung) →
Temporal-Workflow über `worker/main.py:main()` (echte Registrierung) → echte Activities
und Parser auf `tests/fixtures/` → **echter SecureBuffer auf echtem Redis** → Zustellung
per One-Shot-GET (Pull) oder Multipart-POST an einen lokalen ePro-Mock (Push, inkl.
Webhook). Ersetzt ist nur das LLM, durch einen deterministischen Fake an der Grenze
`llm.factory.get_llm_provider`.

Nachgewiesen werden u. a. AT2 (Push + Delete-on-Delivery), AT3 (Pull One-Shot, danach
410), AT4 (4xx-Hard-Fail mit Dead Letter und Webhook), AT5 (Idempotenz),
Verarbeitungsfehler, der synchrone Endpunkt und die Invariante „Temporal sieht nur
Referenzschlüssel, keinen Inhalt“.

Voraussetzung: Redis unter `TEST_REDIS_URL` (Default `redis://localhost:6379/4`). Ist es
nicht erreichbar, wird das Modul übersprungen. Datenbank: wie die Suite (SQLite oder
PostgreSQL).

### 4.5 Grenzfälle und Lückentests

- `tests/test_edge_api.py`: Größen- und Kodierungsgrenzen, Header, SSRF-Filter für
  `callback_url`, UUID-Pfade, Bearer-Parsing, Rate-Limiting, One-Shot-Vertrag,
  Fehler-Envelope.
- `tests/test_edge_parsers_and_services.py`: FDX/Secure-XML/Scene-Heading, PDF-Split,
  Prompt-Sanitizer, SecureBuffer, Report-Generator, Taxonomie.
- `tests/test_cov_*.py`: Verhaltenstests für Zweige, die die Meilenstein-Suiten nicht
  erreichen (Activities, API, LLM-Provider auf Transportebene, Parser, Workflow).

Bekannte, noch nicht behobene Fehler sind dort als `xfail(strict=True)` festgehalten
(§7); Tests mit Suffix `_documented` halten überraschendes, aber aktuelles Verhalten fest.

### 4.6 OCR-Tests (Marker `ocr`)

`tests/test_pdf_ocr.py` braucht `tesseract` mit den Sprachpaketen `deu` und `eng`
(siehe §2). Fehlt das Binary oder `pytesseract`, werden die Tests übersprungen, nicht
rot. Abwählen: `-m "not ocr"`.

### 4.7 Vertragstests (OpenAPI)

`openapi/eki-api-v1.0.yaml` ist der Schnittstellenvertrag. `/openapi.json` der laufenden
App erzeugt FastAPI selbst (nur mit `DEBUG=true`); `tests/test_m12_openapi_contract.py`
hält beide deckungsgleich (Routen, Enums, Version gegen `core/version.py`). Lint der Spec
wie in der CI (Node.js nötig):

```bash
npx @stoplight/spectral-cli lint openapi/eki-api-v1.0.yaml --ruleset .spectral.yaml --fail-severity=error
```

---

## 5. CI-Pipeline (`.github/workflows/ci.yml`)

Läuft bei Push auf `main`/`develop`, bei Pull-Requests dorthin und bei Tags `v*`.

| Job | Inhalt | Blockierend |
|---|---|---|
| `lint` | `ruff check .`, `ruff format --check .`; `mypy api core services workflows worker` | Ruff ja; mypy nur beratend (`continue-on-error`) |
| `security-scan` | Bandit über `api core services workflows worker llm parsers` (bricht ab Schwere *medium*); `safety check` | Bandit ja; Safety beratend |
| `test` | Dienste `pgvector/pgvector:pg16` + `redis:7-alpine`; Tesseract installieren; `pip install -c requirements-dev.lock -e ".[dev]"`; Migrationszyklus `upgrade head → check → downgrade base → upgrade head`; `pytest --no-cov -q` (SQLite); `pytest` im PostgreSQL-Modus mit echtem Redis und `--cov-fail-under=80`; Coverage-Artefakt + Codecov | ja |
| `openapi-validation` | Spectral auf `openapi/eki-api-v1.0.yaml`, 0 Errors | ja |
| `observability-validation` | `promtool check rules`, `amtool check-config`, `docker compose config` für Basis + Observability-Overlay; Prüfung, dass das Prod-Overlay nur Port 8000 veröffentlicht und keinen Quellcode mountet | ja |
| `docker-build` | baut API- und Worker-Image (nach `lint`, `test`, `security-scan`) | ja |
| `integration-test` | `.env.example` → `.env.local`, `docker compose up -d --build`, `/health`, `alembic upgrade head` + `alembic check` **im API-Image**, `/docs`; `/ready` und Temporal UI nur informativ | ja |
| `release-images` | nur bei Tags `v*`: Tag muss `core/version.py` entsprechen; pusht `eki-api`/`eki-worker` (Version + `latest`) nach GHCR | – |

Lokal nachstellen: `ruff check . && ruff format --check .`,
`bandit -r api core services workflows worker llm parsers -ll -q`,
`mypy api core services workflows worker`, Spectral (§4.7), Migrationszyklus und
PostgreSQL-Lauf (§3).

---

## 6. Abnahme- und Lasttests gegen einen laufenden Stack

Diese Werkzeuge brauchen einen gestarteten Docker-Stack (README „Schnellstart“) und
einen API-Key. Auf dem Host genügt die venv aus §2 (enthält `httpx`, `fastapi`,
`uvicorn`, `python-multipart`, `redis`, `requests`).

### 6.1 Abnahmetests AT2–AT7 (`scripts/uat/`)

```bash
# 1. ePro-Mock auf dem Host (Terminal 1)
python scripts/uat/mock_epro_server.py --port 9999

# 2. Nur Linux: host.docker.internal für api/worker bekannt machen
cat > docker-compose.override.yml <<'EOF'
services:
  api:
    extra_hosts: ["host.docker.internal:host-gateway"]
  worker:
    extra_hosts: ["host.docker.internal:host-gateway"]
EOF

# 3. eKI auf den Mock zeigen lassen -- sonst gehen Push-Jobs an die echte Stage
#    (docker-compose.yml bildet staging.epro.filmakademie.de per extra_hosts ab).
#    In .env.local:
#      EPRO_BASE_URL=http://host.docker.internal:9999/api
#      EPRO_WEBHOOK_URL=http://host.docker.internal:9999/api/eki/scl/delivery-failed
docker compose up -d --force-recreate api worker

# 4. Lauf (Terminal 2)
export EKI_API_URL=http://localhost:8000 EKI_API_KEY=eki_... EKI_ADMIN_KEY=eki_... MOCK_URL=http://localhost:9999
python scripts/uat/run_acceptance_tests.py --log-cmd "docker compose logs api worker"
# -> tests/reports/uat_<timestamp>.json und .md (inhaltsfreies Protokoll)
```

- Die eigene `docker-compose.override.yml` wird ohne `-f` automatisch geladen. Nicht
  einfach `docker-compose.override.yml.example` kopieren: das Beispiel setzt
  `LLM_PROVIDER=ollama`/`OLLAMA_MODEL=mistral` und überschreibt damit `.env.local`.
- Redis ist nicht auf den Host veröffentlicht, `--redis-url` daher weglassen; der Lauf
  vermerkt dann den manuellen Buffer-Check:
  `docker compose exec redis redis-cli --scan --pattern 'eki:buf:*' | wc -l` → `0`.
- AT6 (Großdokument, bis 120 min) nur mit `--with-large-document`.
- AT1, AT8, AT9 sind manuelle Schritte: `docs/UAT/UAT_TESTPLAN.md`.

### 6.2 Prod-Smoke (AT9) und Parität

```bash
docker compose exec -e EKI_API_KEY=eki_... api \
  python scripts/smoke_prod.py --worker-metrics http://worker:9090   # RESULT: PASSED
```

Paritätslauf über das Golden-Set (`tests/parity/golden_scenes.yaml`): im API-Container,
weil Ollama nur im Docker-Netz erreichbar ist – Kommandos in
[`OPERATIONS_GUIDE.md`](OPERATIONS_GUIDE.md) §7. Auf dem Host liest
`scripts/run_parity.py` die Konfiguration aus `.env`/Umgebung (nicht `.env.local`) und
braucht ein erreichbares `OLLAMA_BASE_URL`. `mistral_cloud` braucht `MISTRAL_API_KEY`
und ist mit `ENV=production` gesperrt.

### 6.3 Großdokument-Benchmark (AT6)

```bash
python tests/build_large_fixture.py                     # erzeugt tests/fixtures/pdf/large_synthetic_300pp.pdf (liegt bereits bei)
EKI_API_KEY=eki_... python tests/run_pdf_m07_benchmark.py --concurrency 1
# -> tests/reports/m07_benchmark_<timestamp>.json + _stats.csv (docker stats)
```

### 6.4 Weitere Stack-Runner (`tests/run_*.py`)

| Skript | Zweck | braucht Stack |
|---|---|---|
| `tests/run_pdf_full_pipeline.py` | alle PDF-Fixtures nacheinander durch `check:async` | ja (`EKI_API_KEY`) |
| `tests/run_pdf_selected.py` | ausgewählte PDFs durch die Pipeline | ja (`EKI_API_KEY`) |
| `tests/run_security_check.py` | Einzel-PDF prüfen, Report-PDF speichern | ja (`--key`/`EKI_API_KEY`) |
| `tests/run_pdf_m07_benchmark.py` | Großdokument-Benchmark (§6.3) | ja |
| `tests/run_pdf_bulk_test.py` | nur Textextraktion + Szenen-Split aller PDF-Fixtures, ohne LLM | nein |

Ausgaben unter `tests/reports/` sind per `.gitignore` ausgeschlossen: Läufe mit echten
Drehbüchern können Report-Inhalte und Dateinamen enthalten. Hinweis: Die PDF-Fixtures
enthalten echte Drehbücher der Filmakademie (Freigabe-/Rechtefrage beim Auftraggeber,
siehe `DATENSCHUTZ.md`).

---

## 7. Konventionen für neue Tests

- **Ablage:** `tests/test_<bereich>.py`; Testdaten unter `tests/fixtures/` (synthetisch,
  keine echten Drehbuchtexte in Assertions oder Logs).
- **Fixtures** aus `tests/conftest.py` nutzen (`client`, `auth_headers`, `db_session`,
  `mock_redis` …) statt eigene Engines oder Clients zu bauen. Wer per `monkeypatch`
  Umgebungsvariablen ändert, ruft `get_settings.cache_clear()` vorher und nachher.
- **Kein echtes Netz:** LLM an der Provider-Grenze (`llm.factory.get_llm_provider`) oder
  per `httpx.MockTransport` ersetzen; ePro über `httpx.MockTransport` oder einen lokalen
  Mock. Nie echte Ollama-, Mistral- oder ePro-Endpunkte.
- **Marker:** `temporal` für alles mit `WorkflowEnvironment`, `ocr` für Tesseract (plus
  Skip, wenn das Binary fehlt). `--strict-markers` ist aktiv – neue Marker in
  `pyproject.toml` registrieren.
- **Beide DB-Modi:** Tests müssen auf SQLite **und** PostgreSQL grün sein; PG-only mit
  `requires_postgres`. Zeitstempel immer zeitzonenbewusst (`datetime.now(UTC)`), Längen
  wie im Modell – nicht auf die Nachsicht von SQLite verlassen.
- **Bekannte Fehler:** als `@pytest.mark.xfail(strict=True, reason="BUG: …")` mit dem
  *korrekten* Sollverhalten festhalten. Wird der Fehler behoben, schlägt der Test als
  XPASS fehl, und der Marker fällt mit dem Fix weg.
- **Migrationen:** Schemaänderungen brauchen eine Alembic-Revision; Ablauf in
  `OPERATIONS_GUIDE.md` §4.6.

---

## 8. Häufige Probleme

| Problem | Lösung |
|---|---|
| `The SQLAlchemy asyncio module requires … greenlet` | mit Lock-Datei installieren: `pip install -c requirements-dev.lock -e ".[dev]"` |
| Temporal-Tests hängen/scheitern offline | Testserver-Download braucht einmal Netz; sonst `-m "not temporal"` |
| E2E-Tests übersprungen | Redis unter `TEST_REDIS_URL` starten (§2) |
| `tests/test_pg_integration.py` übersprungen | `TEST_POSTGRES_URL` setzen |
| Migrationstests: `permission denied to create database` | Nutzer braucht `CREATEDB` (§2) |
| OCR-Tests übersprungen | `tesseract-ocr`, `-deu`, `-eng` installieren |
| OCR-Tests laufen in Timeouts | `OMP_THREAD_LIMIT=1` (setzt `conftest.py` per Default) |

---

## 9. Kennzahlen

<!-- KENNZAHLEN -->
