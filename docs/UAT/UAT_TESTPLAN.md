# UAT-Testplan – eKI API v1.0.0

Pflichtenheft v1 §7 „Abnahme des Projektes": neun verbindliche Abnahmetests,
Abnahmekriterien (gültige OpenAPI, Auth und Monitoring laufen, alle 9 Tests
bestanden, Betriebsdoku und Postman-Collection liegen vor). Dieser Plan ordnet
jedem Test Automatisierung, manuelle Schritte, Nachweis und Verantwortliche zu.

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
| Betriebsdoku | `docs/OPERATIONS_GUIDE.md`, `DEPLOYMENT_GUIDE.md`, Runbooks M09/M10 |
| Postman-Collection | `postman/eKI-API-v1.0.postman_collection.json` + Environment |

---

## 2. Vorbereitung (lokale/Stage-Durchführung)

```bash
# 1) Stack mit Observability
cp .env.example .env.local && python scripts/generate_secrets.py   # Werte eintragen
docker compose -f docker-compose.yml -f docker-compose.observability.yml up -d
docker compose exec api alembic upgrade head
docker compose exec ollama ollama pull mistral-small3.2

# 2) Keys
docker compose exec api python scripts/create_api_key.py --insert          # EKI_API_KEY
docker compose exec api python scripts/create_api_key.py --insert          # zweiter Nutzer (IDOR)
docker compose exec api python scripts/create_api_key.py --insert --admin  # EKI_ADMIN_KEY

# 3) ePro-Mock (Terminal 2) und eKI darauf zeigen lassen
python scripts/uat/mock_epro_server.py --port 9999
#   .env.local: EPRO_BASE_URL=http://host.docker.internal:9999/api
#               EPRO_WEBHOOK_URL=http://host.docker.internal:9999/api/eki/scl/delivery-failed
docker compose up -d --force-recreate api worker

# 4) Automatisierte Tests 2,3,4,5,7 (6 optional)
export EKI_API_URL=http://localhost:8000 EKI_API_KEY=eki_... EKI_ADMIN_KEY=eki_... MOCK_URL=http://localhost:9999
python scripts/uat/run_acceptance_tests.py \
    --redis-url redis://localhost:6379/0 \
    --log-cmd "docker compose logs api worker"
# Protokoll: tests/reports/uat_<timestamp>.md
```

Für Redis-Zugriff von außen den Port temporär mappen (`docker-compose.override.yml`)
oder den Scan manuell ausführen:
`docker compose exec redis redis-cli --scan --pattern 'eki:buf:*' | wc -l` → `0`.

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
- Teil C (Unit, reproduzierbar): `pytest tests/test_m10_workflow_failover.py -m temporal -v` – der echte
  `SecurityCheckWorkflow` durchläuft auf dem Temporal-Testserver das komplette 6-h-Fenster
  (Time-Skipping) und die Pull-TTL; Ausgabe dem Protokoll beilegen.

### AT5 – Idempotenz
Automatisiert (`AT5`): zwei `POST` mit gleichem `idempotency_key` → gleiche `job_id`, Mock verzeichnet
genau eine erfolgreiche Zustellung mit identischem `Idempotency-Key`-Header.
Postman „10. Abnahmetests" enthält denselben Nachweis interaktiv.

### AT6 – Großdokumente
Auf Zielhardware: `python tests/build_large_fixture.py` (300 Seiten) und
`python tests/run_pdf_m07_benchmark.py --concurrency 1` (Baseline) sowie `--concurrency 2`
(gemäß M07-Stufen). Ergebnis-JSON (`acceptance.passed_overall`, `elapsed_sec`, `stats_summary`)
beilegen. Alternativ `run_acceptance_tests.py --with-large-document`.

### AT7 – Log-Hygiene
Automatisiert (`AT7`): jeder eingereichte Test-Text enthält einen eindeutigen Marker
(`UATMARKER-…`); nach den Läufen werden API- und Worker-Logs durchsucht → 0 Treffer, keine
Szenentexte/Dialoge. Ergänzend `pytest tests/test_m08_log_hygiene.py`.

### AT8 – Stage-Sign-off (Mistral-API)
1. Stage-eKI mit `ENV=stage`, `LLM_PROVIDER=mistral_cloud`, `MISTRAL_API_KEY` (nur Stage!).
2. Mindestens ein FDX- und ein PDF-Testfilmprojekt über ePro anstoßen (Push) und ein Pull-Projekt.
3. Erwartung: Reports im Stage-ePro sichtbar (Erfolgskriterium §3.4 „Report-Verfügbarkeit").
4. `python scripts/run_parity.py --providers mistral_cloud,local_mistral` → Tabelle in
   `docs/M11_PARITY_REPORT.md` §3 übernehmen.
5. Protokoll §4.2 ausfüllen, gemeinsames Review (Pflichtenheft §7 „Sichtung im Review").

### AT9 – Prod-Cutover (lokales Mistral)
1. Prod gemäß `docs/OPERATIONS_GUIDE.md` §2/§6 aufsetzen (`LLM_PROVIDER=local_mistral`, kein Cloud-Key).
2. `python scripts/smoke_prod.py` → `RESULT: PASSED` (E2E, One-Shot, 410, keine externen LLM-Calls).
3. `python scripts/run_parity.py --providers local_mistral` → alle Schwellen erfüllt.
4. Firewall-Nachweis: ausgehender Verkehr zu `api.mistral.ai` blockiert.
5. Protokoll §4.3.

---

## 4. Abnahme-Ablauf

1. Auftragnehmer führt §2/§3 aus, füllt `UAT_PROTOCOL_TEMPLATE.md` und legt Artefakte
   (`tests/reports/uat_*.md`, Benchmark-JSON, Paritätstabelle, Smoke-Ausgabe, Screenshots) ab.
2. Trello-Karte „M12" → „Zur Abnahme" mit Links zu den Nachweisen (Pflichtenheft §7 Ablauf).
3. Gemeinsames Review; Auftraggeber verschiebt auf „Abgenommen".
4. Go-Live gemäß `GO_LIVE_CHECKLIST.md`.
