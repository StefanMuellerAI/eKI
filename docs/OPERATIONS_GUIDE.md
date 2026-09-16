# eKI API – Betriebsleitfaden (M11)

Zielgruppe: IT-Betrieb der Filmakademie. Ergänzt `DEPLOYMENT_GUIDE.md`
(Security-Setup), `M09_SLO.md` (Monitoring), `M10_FAILOVER_RUNBOOK.md`
(Zustellstörungen) und `EPRO_INTEGRATION_GUIDE.md` (Schnittstelle).

---

## 1. Architektur im Betrieb

| Container | Image | Aufgabe | Port (intern) | Skalierbar |
|---|---|---|---|---|
| `eki-api` | `ghcr.io/<owner>/eki-api:<version>` | REST-API (Gunicorn + Uvicorn) | 8000 (published) | ja, `--scale api=N` hinter Reverse-Proxy |
| `eki-worker` | `ghcr.io/<owner>/eki-worker:<version>` | Temporal-Worker: Parsing, LLM, Report, Zustellung | 9090 (Metriken) | ja, `--scale worker=N` |
| `eki-temporal` | `temporalio/auto-setup` | Workflow-Engine, Retry-Fenster, Schedules | 7233 | – |
| `eki-postgres` | `pgvector/pgvector:pg16` | Metadaten, Dead Letters, KB-Embeddings | 5432 | – |
| `eki-redis` | `redis:7` (appendonly) | SecureBuffer (verschlüsselt, TTL 6 h) | 6379 | – |
| `eki-ollama` | `ollama/ollama` | lokale Inferenz (GPU) | 11434 | – |

Einzig `eki-api:8000` (und optional Grafana/Jaeger aus dem Observability-Overlay)
wird auf den Host veröffentlicht.

---

## 2. Installation / Erstinbetriebnahme

### 2.1 Voraussetzungen

- Docker ≥ 24 mit Compose v2, NVIDIA Container Toolkit (GPU ≥ 24 GB VRAM empfohlen, Pflichtenheft §8)
- Ausgehend nur: GHCR (Images), Ollama-Registry (Modell-Pull). **Kein** ausgehender LLM-Cloud-Verkehr
- Eingehend: ePro → `eki-api:8000` (Intranet/TLS-Terminierung durch Reverse-Proxy)

### 2.2 Schritte

```bash
git clone <repo> eki && cd eki
git checkout v1.0.0                       # bzw. gewünschter Release-Tag

cp .env.example .env.local
python3 scripts/generate_secrets.py       # POSTGRES_PASSWORD, API_SECRET_KEY etc.
# .env.local anpassen (siehe §3), mindestens:
#   ENV=production, DEBUG=false, LLM_PROVIDER=local_mistral,
#   EPRO_BASE_URL, CORS_ORIGINS, TRUST_PROXY_HEADERS/TRUSTED_PROXY_IPS

export GHCR_OWNER=<github-owner> EKI_IMAGE_TAG=1.0.0
docker compose -f docker-compose.yml -f docker-compose.prod.yml pull api worker
docker compose -f docker-compose.yml -f docker-compose.prod.yml up -d --no-build

# Modelle einmalig laden
docker compose exec ollama ollama pull mistral-small3.2      # Inferenz (Default)
docker compose exec ollama ollama pull bge-m3                # KB-Embeddings
# optional: docker compose exec ollama ollama pull gemma4:31b # dokumentierte Alternative

docker compose exec api alembic upgrade head
docker compose exec api python scripts/create_api_key.py --insert          # ePro-Service-Key
docker compose exec api python scripts/create_api_key.py --insert --admin  # Ops-Key
docker compose exec api python scripts/create_api_key.py --insert          # Prometheus-Scraper-Key
```

Verifikation: `curl -s localhost:8000/health`, danach `python scripts/smoke_prod.py` (§7).

---

## 3. Konfigurationsmatrix

| Bereich | Variable | Prod-Empfehlung | Hinweis |
|---|---|---|---|
| Umgebung | `ENV` / `DEBUG` | `production` / `false` | Startet sonst nicht (Validator) |
| LLM | `LLM_PROVIDER` | `local_mistral` | `mistral_cloud` wird in Prod abgelehnt (`LLM_ALLOW_EXTERNAL_PROVIDERS=false`) |
| LLM | `LOCAL_MISTRAL_MODEL` | `mistral-small3.2` | Alternative: `LLM_PROVIDER=ollama` + `OLLAMA_MODEL=gemma4:31b` (Paritätsreport beachten) |
| LLM | `OLLAMA_NUM_CTX` | 32768 | bei VRAM-Engpass 16384 |
| LLM | `OLLAMA_MAX_CONCURRENT_REQUESTS` | 1–2 | nur erhöhen, wenn `eki_llm_queue_wait_seconds` hoch **und** VRAM frei (M07) |
| LLM | `LLM_PARALLEL_ENABLED`, `*_CONCURRENCY` | `false` / 1 | Stufen siehe README M07 |
| KB | `KB_RETRIEVAL_ENABLED` | `true` nach Validierung der Inhalte | `OLLAMA_EMBEDDING_MODEL=bge-m3`, `OLLAMA_EMBEDDING_MAX_CHARS=30000` |
| Zustellung | `EPRO_BASE_URL`, `EPRO_WEBHOOK_URL` | Prod-URLs | Webhook opt-in (M08) |
| Zustellung | `TEMPORAL_WORKFLOW_EXECUTION_TIMEOUT` | 36000 | ≥ Verarbeitung + 6 h (M10) |
| Buffer | `BUFFER_TTL_SECONDS` | 21600 | Pflichtenheft ≤ 6 h |
| OCR | `OCR_ENABLED`, `OCR_MAX_PAGES` | `true`, 50 | Scans; Laufzeit-Cap |
| Security | `TRUST_PROXY_HEADERS`, `TRUSTED_PROXY_IPS` | `true`, Proxy-IP | nur hinter eigenem Reverse-Proxy |
| Security | `RATE_LIMIT_*` | Defaults | 60/min IP, 1000/h Key |
| Observability | `METRICS_ENABLED`, `OTEL_ENABLED` | `true`, `false` (bzw. `true` mit Overlay) | siehe `M09_SLO.md` |
| Logs | `LOG_FORMAT`, `LOG_LEVEL` | `json`, `INFO` | Inhalte werden maskiert (M08) |

---

## 4. Laufender Betrieb

### 4.1 Tägliche Checks (5 Minuten)

1. Grafana `eki-overview`: API/Worker up, Jobs failed (24 h) = 0, Dead Letters = 0.
2. `GET /v1/ops/summary` (Admin-Key): keine `dead_lettered`, keine hängenden `delivering` > 6 h.
3. Alertmanager: keine offenen Alerts.

### 4.2 Skalierung

- Mehr Durchsatz bei LLM-Wartezeit: erst `OLLAMA_MAX_CONCURRENT_REQUESTS` (GPU-abhängig),
  dann `LLM_PARALLEL_ENABLED=true` mit `*_CONCURRENCY=2`, zuletzt zweiter Worker
  (`--scale worker=2`; beide teilen die Ollama-Instanz, der Cap wirkt pro Prozess).
- API-Replikas: `--scale api=2` hinter Reverse-Proxy; jedes Replikat exportiert eigene Metriken.

### 4.3 Key-Rotation

```bash
docker compose exec api python scripts/create_api_key.py --insert   # neuen Key erzeugen
# ePro auf neuen Key umstellen, dann alten deaktivieren:
docker compose exec postgres psql -U eki_user -d eki_db -c "UPDATE api_keys SET is_active=false WHERE name='<alter Name>';"
```

`API_SECRET_KEY` (Fernet-Ableitung für SecureBuffer und KB) nur rotieren, wenn kein Job läuft
und die KB neu ingestiert wird (`scripts/seed_kb.py --reseed`).

### 4.4 Backups

Persistent sind nur Metadaten und die KB: `pg_dump` der Datenbank (täglich) genügt.
Redis enthält ausschließlich transiente, verschlüsselte Inhalte mit ≤ 6 h TTL und wird
**nicht** gesichert (Processing-Only, Pflichtenheft §4.1).

### 4.5 Updates / Rollback

```bash
export EKI_IMAGE_TAG=1.0.1
docker compose -f docker-compose.yml -f docker-compose.prod.yml pull api worker
docker compose -f docker-compose.yml -f docker-compose.prod.yml up -d --no-build api worker
docker compose exec api alembic upgrade head
```

Rollback: `EKI_IMAGE_TAG` auf die vorige Version setzen, `up -d --no-build`; Migrationen sind
additiv – ein `alembic downgrade` ist nur nötig, wenn das Release-Notes-Dokument es verlangt.
Laufende Workflows überstehen Worker-Neustarts (Temporal-Replay).

---

## 5. Runbooks (Kurzform)

| Symptom | Erste Maßnahme | Details |
|---|---|---|
| `EkiApiDown` | `docker compose ps`, `docker compose logs api --tail 200` | Startfehler meist Konfig-Validator (`ENV=production` mit Default-Secret) |
| `EkiLlmErrorRateHigh` | `docker compose exec ollama ollama list` → Modell fehlt? `ollama pull`; `nvidia-smi` VRAM | Provider-Healthcheck prüft Modell |
| Jobs hängen in `running` | Temporal UI (Tunnel auf 8080): Pending Activities; Worker-Logs nach `job_id` filtern | Korrelation via `request_id`/`job_id` in jeder Zeile |
| `EkiDeliveryFailureRateHigh` / Dead Letters | `M10_FAILOVER_RUNBOOK.md` §2 | ePro-Erreichbarkeit, 4xx-Ursachen |
| Redis voll (`noeviction`) | `redis-cli --scan --pattern 'eki:buf:*' \| wc -l`; TTL läuft; ggf. `maxmemory` erhöhen | Nie flushen, solange Jobs laufen |
| OCR langsam | `OCR_MAX_PAGES` senken, `OCR_DPI=200` | Scan-Seiten sind CPU-lastig |

---

## 6. Cutover Cloud → Lokal (Abnahmetest 9)

1. Stage-Sign-off (Abnahmetest 8) mit `LLM_PROVIDER=mistral_cloud` auf der Stage-Umgebung liegt vor.
2. Paritätsreport (`scripts/run_parity.py --providers mistral_cloud,local_mistral`) bestanden;
   Ergebnis in `docs/M11_PARITY_REPORT.md` dokumentiert.
3. Prod-`.env.local`: `LLM_PROVIDER=local_mistral`, kein `MISTRAL_API_KEY`, `LLM_ALLOW_EXTERNAL_PROVIDERS` nicht gesetzt.
4. Firewall: ausgehend `api.mistral.ai` blockiert (Defense in depth).
5. `python scripts/smoke_prod.py` → `RESULT: PASSED`, Ausgabe dem Protokoll beilegen.
6. Erste produktive Prüfung gemeinsam mit ePro-Team beobachten (Brückenmeeting §13).

---

## 7. Nachweise für die Abnahme

| Test | Kommando | Erwartung |
|---|---|---|
| Health | `curl -s $URL/health` | `status=healthy`, Version = Release-Tag |
| Smoke E2E + keine externen Calls | `python scripts/smoke_prod.py` | `RESULT: PASSED` |
| Parität | `python scripts/run_parity.py --providers local_mistral` | alle Schwellen ✅ |
| Großdokument | `python tests/run_pdf_m07_benchmark.py --concurrency 1` | `acceptance.passed_overall=true` |
| Log-Hygiene | `docker compose logs worker \| rg -i "<Textprobe aus Drehbuch>"` | 0 Treffer |
