# UAT-Testplan – eKI API v1.0.0

Pflichtenheft v1 §7 „Abnahme des Projektes": neun verbindliche Abnahmetests,
Abnahmekriterien (gültige OpenAPI, Auth und Monitoring laufen, alle 9 Tests
bestanden, Betriebsdoku und Postman-Collection liegen vor). Dieser Plan ordnet
jedem Test Automatisierung, manuelle Schritte, Nachweis und Verantwortliche zu.

> **Wichtig:** Die Abnahmetests 2–7 sind auf dem aktuellen Stand (Migrations-Head `b8d0f2a4c6e3`,
> bzw. dem empfohlenen Patch-Release) auszuführen bzw. zu wiederholen. Auf einem per
> `alembic` aufgebauten PostgreSQL-Stack scheiterte vorher die Zustellphase
> (`job_metadata.status` zu kurz für `delivering`, `HANDOVER.md` §1.1). Ergebnisse älterer
> Läufe gelten nicht als Nachweis.

---

## 1. Übersicht

| # | Abnahmetest (Pflichtenheft §7) | Umgebung | Automatisierung | Nachweis |
|---|---|---|---|---|
| 1 | Automatischer Start: Upload im eProjekt startet eKI-Job | **Stage-ePro** | manuell (ePro-Team) | Protokoll §4.1 + `GET /v1/ops/jobs` |
| 2 | Push-Fluss: nach 2xx keine Inhalte in eKI | lokal/Stage | `run_acceptance_tests.py` AT2 | Redis-Scan = 0, Mock-Empfang |
| 3 | Pull-Fluss: einmalig abrufbar, danach gelöscht | lokal/Stage | AT3 | 200 + `X-One-Shot`, dann 410 |
| 4 | Retries & TTL: bis 6 h Retry, dann Löschung + Webhook | lokal (Mock) + Unit | AT4 + `tests/test_m10_workflow_failover.py` | Attempts, Webhook, Dead Letter, Time-Skipping-Test |
| 5 | Idempotenz: gleicher Key → keine Duplikate | lokal/Stage | AT5 | gleiche `job_id`, Mock zählt 1 Zustellung |
| 6 | Großdokumente 300–350 Seiten ≤ 120 min | **Zielhardware** | AT6 (`--with-large-document`) / `tests/run_pdf_m07_benchmark.py` | `acceptance.passed_overall=true`, `docker stats` |
| 7 | Log-Hygiene: keine Drehbuchtexte/Findings in Logs | lokal/Stage | AT7 (`--log-cmd`) | 0 Marker-Treffer, `tests/test_m08_log_hygiene.py` |
| 8 | Stage-Sign-off: E2E mit Mistral-API gegen Stage-ePro | **Stage** | manuell (`LLM_PROVIDER=mistral_cloud`) | Protokoll §4.2, Paritätsreport |
| 9 | Prod-Cutover: Paritäts-/Smoke-Tests mit lokalem Mistral | **Prod** | `scripts/smoke_prod.py`, `scripts/run_parity.py` | `RESULT: PASSED`, Paritätstabelle |

Querschnittskriterien:

| Kriterium | Nachweis |
|---|---|
| OpenAPI gültig | `spectral lint openapi/eki-api-v1.0.yaml` → 0 Errors (CI-Gate); `tests/test_m12_openapi_contract.py` |
| Authentifizierung läuft | Postman „5. Security Tests", `tests/test_security.py` |
| Monitoring läuft | Grafana-Dashboards `eki-*`, Alertmanager erreichbar, `/metrics` liefert `eki_*` |
| Betriebsdoku | `docs/OPERATIONS_GUIDE.md`, `docs/CONFIGURATION.md`, `docs/DATENSCHUTZ.md`, Runbooks M09/M10 |
| Postman-Collection | `postman/eKI-API-v1.0.postman_collection.json` + Environment |

---

## 2. Vorbereitung (lokale/Stage-Durchführung)

### 2.1 Voraussetzungen auf dem Rechner, der die Skripte startet

Die Skripte laufen auf dem Host und sprechen die API über Port 8000 an. Benötigt werden
Python ≥ 3.11 und:

```bash
python3 -m venv .venv-uat && . .venv-uat/bin/activate
pip install httpx redis fastapi uvicorn python-multipart requests reportlab
# alternativ alles: pip install -c requirements-dev.lock -e ".[dev]"   (docs/TESTING_GUIDE.md)
```

(`mock_epro_server.py`: fastapi, uvicorn, python-multipart; `run_acceptance_tests.py`: httpx,
optional redis; AT6-Benchmark: requests; `tests/build_large_fixture.py`: reportlab.)

### 2.2 Stack, Secrets, Modell, Keys

```bash
# 1) Konfiguration und echte Secrets
cp .env.example .env.local
python3 scripts/generate_secrets.py
#   Werte aus secrets/*.txt in .env.local übernehmen: dasselbe DB-Passwort in
#   POSTGRES_PASSWORD, POSTGRES_PWD und DATABASE_URL; API_SECRET_KEY (secrets/README.md)

# 2) Basis-Stack (Observability erst nach dem Prometheus-Key, Schritt 4)
docker compose up -d
docker compose exec api alembic upgrade head
docker compose exec api alembic current            # -> b8d0f2a4c6e3 (head)
docker compose exec ollama ollama pull mistral-small3.2

# 3) Keys (interaktiv; unterschiedliche user_id für den zweiten Nutzer)
docker compose exec api python scripts/create_api_key.py --insert          # EKI_API_KEY
docker compose exec api python scripts/create_api_key.py --insert          # zweiter Nutzer (IDOR)
docker compose exec api python scripts/create_api_key.py --insert --admin  # EKI_ADMIN_KEY
docker compose exec api python scripts/create_api_key.py --insert          # Prometheus-Scraper

# 4) Prometheus-Key ablegen (secrets/README.md), DANN das Observability-Overlay starten.
#    Fehlt die Datei beim Start, legt Docker an ihrer Stelle ein Verzeichnis an.
export GRAFANA_ADMIN_PASSWORD='<starkes Passwort>'   # Compose liest das NICHT aus .env.local
docker compose -f docker-compose.yml -f docker-compose.observability.yml up -d
```

Ohne laufendes Modell schlagen die Tests fehl: Die Risikoanalyse ist fail-closed, ein Job
ohne erreichbares LLM endet mit `failed`.

### 2.3 ePro-Mock einbinden

Push und Webhook schickt der **Worker** an `EPRO_BASE_URL` bzw. `EPRO_WEBHOOK_URL`. Beide
Werte **müssen** auf den Mock zeigen – sonst gehen Test-Pushes an das echte Stage-ePro
(`docker-compose.yml` bildet `staging.epro.filmakademie.de` per `extra_hosts` fest auf
193.196.129.23 ab).

```bash
# Terminal 2
python scripts/uat/mock_epro_server.py --port 9999

# .env.local
#   EPRO_BASE_URL=http://host.docker.internal:9999/api
#   EPRO_WEBHOOK_URL=http://host.docker.internal:9999/api/eki/scl/delivery-failed
```

Unter **Linux** löst `host.docker.internal` in Containern nur mit einem Hosts-Eintrag auf.
Dafür eine Datei `docker-compose.uat.yml` anlegen (nicht committen):

```yaml
# docker-compose.uat.yml – nur für UAT-Läufe mit dem ePro-Mock
services:
  api:
    extra_hosts:
      - "host.docker.internal:host-gateway"
  worker:
    extra_hosts:
      - "host.docker.internal:host-gateway"
  # optional: Redis für den Buffer-Scan des Runners auf dem Host erreichbar machen
  # redis:
  #   ports:
  #     - "127.0.0.1:6379:6379"
```

Neu erzeugen – ein `docker compose restart` liest `.env.local` **nicht** neu ein:

```bash
docker compose -f docker-compose.yml -f docker-compose.observability.yml -f docker-compose.uat.yml \
    up -d --force-recreate api worker
docker compose exec worker env | grep -E 'EPRO_(BASE|WEBHOOK)_URL'   # Kontrolle: Mock-URLs
```

Auf macOS/Windows (Docker Desktop) ist `host.docker.internal` ohne diesen Eintrag verfügbar.

### 2.4 Automatisierte Tests 2–7

```bash
export EKI_API_URL=http://localhost:8000 EKI_API_KEY=eki_... EKI_ADMIN_KEY=eki_... MOCK_URL=http://localhost:9999
python scripts/uat/run_acceptance_tests.py --log-cmd "docker compose logs api worker"
#   mit veröffentlichtem Redis-Port (docker-compose.uat.yml) zusätzlich:
#   --redis-url redis://localhost:6379/0
# Protokoll: tests/reports/uat_<timestamp>.md (+ .json)
```

Redis ist im Standard-Compose nicht vom Host erreichbar. Ohne `--redis-url` vermerkt der
Runner den Buffer-Scan als manuell; dann nach den Läufen im Container prüfen:
`docker compose exec redis redis-cli --scan --pattern 'eki:buf:*' | wc -l` → `0`
(solange kein anderer Job läuft).

---

## 3. Detailschritte je Test

### AT1 – Automatischer Start (Stage, ePro-Team)
1. Service-Account-Key der eKI im ePro hinterlegen.
2. Drehbuch (FDX oder PDF) im Stage-ePro zu einem Testfilmprojekt hochladen.
3. Erwartung: ePro ruft `POST /v1/security/check:async` → 202; in eKI erscheint der Job
   (`GET /v1/ops/jobs?project_id=<ePro-Projekt>`), ohne Nutzerinteraktion.
4. Nachweis: Screenshot ePro-Upload + `job_id`, Zeitstempel, Auszug `/v1/ops/jobs`.

### AT2 – Push-Fluss
Automatisiert (`AT2`): Job mit `delivery=push` → Mock empfängt Multipart mit PDF und
`Idempotency-Key` → `metadata.delivery_status=delivered`, `delivery_last_status_code=201` →
Redis `eki:buf:*` = 0. Auf Stage identisch mit echtem ePro-Endpunkt (Mock entfällt);
Inhalt-Nachweis dann per `redis-cli` im Container.

### AT3 – Pull-Fluss
Automatisiert (`AT3`): erster `GET /v1/security/reports/{id}` → 200, Header `X-One-Shot: true`,
`pdf_base64` gefüllt; zweiter Abruf → 410; Job `delivery_status=delivered`.

### AT4 – Retries & TTL
- Teil A (automatisiert): Mock antwortet 503 → `delivery_status=delivering`, `delivery_attempts` steigt
  (Backoff 2 s → 10 min) → Mock auf `ok` → `delivered`.
- Teil B (automatisiert): Mock antwortet 422 → Job `failed` (`delivery_failed:hard_4xx`), Webhook
  `security.delivery.failed` beim Mock, Dead Letter in `/v1/ops/dead-letters`, Redis leer.
- Teil C (Unit, reproduzierbar): `pytest tests/test_m10_workflow_failover.py -m temporal -v --no-cov` –
  der echte `SecurityCheckWorkflow` durchläuft auf dem Temporal-Testserver das komplette 6-h-Fenster
  (Time-Skipping) und die Pull-TTL; Ausgabe dem Protokoll beilegen. Benötigt die Dev-Abhängigkeiten
  und beim ersten Lauf Internetzugang (Download des Temporal-Testservers), siehe
  `docs/TESTING_GUIDE.md`. Ergänzend deckt `tests/test_e2e_pipeline.py` (Marker `temporal`) den
  Weg API → Workflow → Activities → Redis → Push/Pull ab.

### AT5 – Idempotenz
Automatisiert (`AT5`): zwei `POST` mit gleichem `idempotency_key` → gleiche `job_id`, Mock verzeichnet
genau eine erfolgreiche Zustellung mit identischem `Idempotency-Key`-Header.
Postman „10. Abnahmetests" enthält denselben Nachweis interaktiv.

### AT6 – Großdokumente
Auf Zielhardware. Die Fixture `tests/fixtures/pdf/large_synthetic_300pp.pdf` liegt im Repository
(neu erzeugen mit `python tests/build_large_fixture.py`). Die Nebenläufigkeit bestimmt die
**Server-Konfiguration**; die Option `--concurrency` des Benchmarks ist nur der Erwartungswert
für die Auswertung.

```bash
# Stufe 1 (Baseline): in .env.local
#   LLM_PARALLEL_ENABLED=false
#   OLLAMA_MAX_CONCURRENT_REQUESTS=1
docker compose up -d --force-recreate api worker     # nicht "restart"
python tests/run_pdf_m07_benchmark.py --concurrency 1

# Stufe 2 (gemäß M07-Stufen): in .env.local
#   LLM_PARALLEL_ENABLED=true
#   OLLAMA_MAX_CONCURRENT_REQUESTS=2
#   PDF_STRUCTURE_CONCURRENCY=2
#   RISK_ANALYSIS_CONCURRENCY=2
docker compose up -d --force-recreate api worker
python tests/run_pdf_m07_benchmark.py --concurrency 2
```

`.env.local` mit einem Editor ändern oder portabel per `sed -i.bak 's/^LLM_PARALLEL_ENABLED=.*/LLM_PARALLEL_ENABLED=true/' .env.local`
(`sed -i ''` funktioniert nur mit BSD-sed unter macOS). Die API übernimmt die Werte beim Jobstart in den
Workflow, deshalb API **und** Worker neu erzeugen. Ergebnis-JSON (`acceptance.passed_overall`,
`elapsed_sec`, `stats_summary`) beilegen. Alternativ `run_acceptance_tests.py --with-large-document`
(führt Stufe 1 aus).

### AT7 – Log-Hygiene
Automatisiert (`AT7`): jeder eingereichte Test-Text enthält einen eindeutigen Marker
(`UATMARKER-…`); nach den Läufen werden API- und Worker-Logs durchsucht → 0 Treffer, keine
Szenentexte/Dialoge. Ergänzend `pytest tests/test_m08_log_hygiene.py`.

### AT8 – Stage-Sign-off (Mistral-API)
0. Vorab klären: AVV mit Mistral AI oder ausschließlich freigegebene Testdrehbücher
   (`docs/DATENSCHUTZ.md` §5).
1. Stage-eKI mit `LLM_PROVIDER=mistral_cloud`, `MISTRAL_API_KEY` (nur Stage!). Läuft die Stage
   mit dem Prod-Overlay, setzt dieses `ENV=production`: dann zusätzlich
   `LLM_ALLOW_EXTERNAL_PROVIDERS=true` in `.env.local` und `export LLM_PROVIDER=mistral_cloud`
   in der Shell (das Overlay überschreibt den Wert aus `.env.local`). Mit `mistral_cloud` ist die
   Knowledge Base nicht nutzbar (`KB_RETRIEVAL_ENABLED=false` lassen).
2. Mindestens ein FDX- und ein PDF-Testfilmprojekt über ePro anstoßen (Push) und ein Pull-Projekt.
3. Erwartung: Reports im Stage-ePro sichtbar (Erfolgskriterium §3.4 „Report-Verfügbarkeit").
4. Paritätslauf im API-Container (dort sind `MISTRAL_API_KEY` und Ollama erreichbar; das
   Golden-Set liegt nicht im Image):

   ```bash
   docker compose cp tests/parity/golden_scenes.yaml api:/tmp/golden_scenes.yaml
   docker compose exec api python scripts/run_parity.py --providers mistral_cloud,local_mistral \
       --golden /tmp/golden_scenes.yaml --out /tmp/parity
   docker compose cp api:/tmp/parity ./tests/reports/
   ```

   Tabelle in `docs/M11_PARITY_REPORT.md` §3 übernehmen.
5. Protokoll §4.2 ausfüllen, gemeinsames Review (Pflichtenheft §7 „Sichtung im Review").

### AT9 – Prod-Cutover (lokales Mistral)
1. Prod gemäß `docs/OPERATIONS_GUIDE.md` und `GO_LIVE_CHECKLIST.md` aufsetzen
   (`LLM_PROVIDER=local_mistral`, kein Cloud-Key).
2. `EKI_API_URL=… EKI_API_KEY=… python scripts/smoke_prod.py` → `RESULT: PASSED`
   (E2E, One-Shot, 410, keine externen LLM-Calls).
3. Paritätslauf wie bei AT8 Schritt 4 mit `--providers local_mistral` → alle Schwellen erfüllt.
4. Firewall-Nachweis: ausgehender Verkehr zu `api.mistral.ai` blockiert.
5. Protokoll §4.3.

---

## 4. Abnahme-Ablauf

1. Auftragnehmer führt §2/§3 aus, füllt `UAT_PROTOCOL_TEMPLATE.md` und legt Artefakte
   (`tests/reports/uat_*.md`, Benchmark-JSON, Paritätstabelle, Smoke-Ausgabe, Screenshots) ab.
   `tests/reports/` ist git-ignoriert und kann Report-Inhalte enthalten – nur die Protokolle
   weitergeben.
2. Trello-Karte „M12" → „Zur Abnahme" mit Links zu den Nachweisen (Pflichtenheft §7 Ablauf).
3. Gemeinsames Review; Auftraggeber verschiebt auf „Abgenommen".
4. Go-Live gemäß `GO_LIVE_CHECKLIST.md`.
