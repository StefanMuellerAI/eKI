# eKI API – Betriebsleitfaden (Stand Release 1.0.0)

Zielgruppe: IT-Betrieb der Filmakademie. Maßgebliches Betriebsdokument; das frühere
`DEPLOYMENT_GUIDE.md` ist veraltet. Ergänzend:
[`CONFIGURATION.md`](CONFIGURATION.md) (alle Einstellungen),
[`M09_SLO.md`](M09_SLO.md) (Monitoring, Alerts),
[`M10_FAILOVER_RUNBOOK.md`](M10_FAILOVER_RUNBOOK.md) (Zustellstörungen),
[`EPRO_INTEGRATION_GUIDE.md`](EPRO_INTEGRATION_GUIDE.md) (Schnittstelle),
[`DATENSCHUTZ.md`](DATENSCHUTZ.md) (Datenhaltung) und
[`../secrets/README.md`](../secrets/README.md) (Secrets).

> **Konvention:** Alle `docker compose`-Kommandos gehen davon aus, dass die
> Projekt-`.env` (§2.2) `COMPOSE_FILE=docker-compose.yml:docker-compose.prod.yml` setzt.
> Ohne diese Datei jedem Aufruf `-f docker-compose.yml -f docker-compose.prod.yml`
> mitgeben – ein `docker compose up` nur mit der Basisdatei startet den
> Entwicklungs-Stack (Quellcode-Mounts, lokale Builds, Temporal UI auf Port 8080).

---

## 1. Architektur im Betrieb

| Container | Image | Aufgabe | Port | Skalierbar |
|---|---|---|---|---|
| `eki-api` | `ghcr.io/<owner>/eki-api:<version>` | REST-API (Gunicorn + Uvicorn) | 8000 – **einziger veröffentlichter Port** | ja, `--scale api=N` hinter Reverse-Proxy |
| `eki-worker` | `ghcr.io/<owner>/eki-worker:<version>` | Temporal-Worker: Parsing, LLM, Report, Zustellung, KB-Cleanup | 9090 Metriken, nur intern | ja, `--scale worker=N` |
| `eki-temporal` | `temporalio/auto-setup:1.29.4` | Workflow-Engine, Retry-Fenster, Schedules | 7233 intern | – |
| `eki-temporal-ui` | `temporalio/ui:2.47.3` | Operator-UI ohne Authentifizierung | 8080 intern (Zugriff §8.2) | – |
| `eki-postgres` | `pgvector/pgvector:pg16` | Datenbanken `eki_db` (App, KB), `temporal`, `temporal_visibility` | 5432 intern | – |
| `eki-redis` | `redis:7-alpine` (Prod: AOF, `maxmemory 1gb`, `noeviction`) | SecureBuffer (verschlüsselt, TTL 6 h), Rate-Limit-Zähler | 6379 intern | – |
| `eki-ollama` | `ollama/ollama:latest` | lokale Inferenz + KB-Embeddings (GPU) | 11434 intern | – |

Persistente Volumes: `postgres_data`, `redis_data`, `ollama_data` (mit
Observability-Overlay zusätzlich `prometheus_data`, `alertmanager_data`, `grafana_data`;
veröffentlicht werden dort Grafana 3000 und Jaeger 16686). CI prüft, dass das
Prod-Overlay nur Port 8000 veröffentlicht und keinen Quellcode mountet.

Auf Infrastruktur-Dienste greift man nur per `docker compose exec` zu, z. B.
PostgreSQL (die Datenbank verlangt auch im Container ein Passwort, `-A md5`):

```bash
docker compose exec postgres sh -c 'PGPASSWORD="$POSTGRES_PASSWORD" psql -U "$POSTGRES_USER" -d "$POSTGRES_DB"'
docker compose exec redis redis-cli INFO persistence
docker compose exec ollama ollama list
```

Skripte laufen im API-Container (dort sind `scripts/`, `config/` und
`tests/fixtures/` enthalten und alle Dienste erreichbar):
`docker compose exec api python scripts/<skript>.py`.

---

## 2. Installation / Erstinbetriebnahme

### 2.1 Voraussetzungen

- Docker ≥ 24 mit **Docker Compose ≥ 2.24** (`docker compose version`; das Prod-Overlay
  nutzt `!reset`, ältere Versionen lehnen die Datei ab)
- NVIDIA Container Toolkit, GPU ≥ 24 GB VRAM empfohlen (Pflichtenheft §8)
- Plattenplatz: ca. 15 GB für `mistral-small3.2`, ~1 GB für `mxbai-embed-large`, plus
  Images, Datenbank und Logs (§5.5)
- Ausgehend nur: GHCR (Images), Ollama-Registry (Modell-Pull), ePro. **Kein**
  ausgehender LLM-Cloud-Verkehr
- Eingehend: ePro → `eki-api:8000` (TLS-Terminierung durch Reverse-Proxy)
- Python 3 auf dem Host nur für `scripts/generate_secrets.py` und optional
  `scripts/seed_kb.py` (§5.6)

### 2.2 Schritte

```bash
git clone <repo> eki && cd eki
git checkout v1.0.0                       # bzw. gewünschter Release-Tag

# 1. App-Konfiguration (.env.local) mit echten Secrets
cp .env.example .env.local
python3 scripts/generate_secrets.py       # schreibt secrets/db_password.txt, api_secret_key.txt, database_url.txt
PW=$(cat secrets/db_password.txt); KEY=$(cat secrets/api_secret_key.txt)
sed -i.bak -e "s/CHANGE_ME_STRONG_PASSWORD_HERE/$PW/g" \
           -e "s/CHANGE_ME_256_BIT_SECRET_KEY_HERE/$KEY/" .env.local && rm .env.local.bak
# .env.local weiter anpassen (§3): ENV=production, DEBUG=false, EPRO_BASE_URL,
# EPRO_WEBHOOK_URL, CORS_ORIGINS, TRUST_PROXY_HEADERS/TRUSTED_PROXY_IPS

# 2. Compose-Variablen: werden NICHT aus .env.local gelesen (CONFIGURATION.md §2)
cat > .env <<'EOF'
COMPOSE_FILE=docker-compose.yml:docker-compose.prod.yml
GHCR_OWNER=<github-owner>
EKI_IMAGE_TAG=1.0.0
EOF
docker compose config -q                  # Syntax/Compose-Version prüfen

# 3. Start
docker compose pull api worker
docker compose up -d --no-build

# 4. Modelle einmalig laden (Volume ollama_data)
docker compose exec ollama ollama pull mistral-small3.2      # Inferenz (LOCAL_MISTRAL_MODEL)
docker compose exec ollama ollama pull mxbai-embed-large     # KB-Embeddings (OLLAMA_EMBEDDING_MODEL)

# 5. Datenbankschema
docker compose exec api alembic upgrade head
docker compose exec api alembic current                      # -> b8d0f2a4c6e3 (head)

# 6. API-Keys (interaktiv, Klartext wird nur einmal angezeigt; §4.3)
docker compose exec api python scripts/create_api_key.py --insert          # ePro-Service-Key
docker compose exec api python scripts/create_api_key.py --insert --admin  # Ops-Key
docker compose exec api python scripts/create_api_key.py --insert          # Prometheus-Scraper-Key
```

Der Start-Validator verweigert mit `ENV=production` Platzhalter-Secrets
(`CHANGE_ME…`, `replace-me`), Schlüssel unter 32 Zeichen, `DEBUG=true` und
`mistral_cloud` – die Secrets trotzdem immer wie oben erzeugen, nie von Hand ausdenken.
Das DB-Passwort muss in `POSTGRES_PASSWORD`, `POSTGRES_PWD` und `DATABASE_URL`
identisch sein; es gilt nur bei der ersten Initialisierung des Volumes (§5.9).
Den `API_SECRET_KEY` zusätzlich im Passwort-Tresor ablegen (für Backups nötig, §4.4).

**Observability (optional):** zuerst `secrets/prometheus_api_key.txt` mit dem
Scraper-Key anlegen (`secrets/README.md` §3 – fehlt die Datei beim Start, legt Docker
ein Verzeichnis an), dann in der `.env` ergänzen und neu starten:

```
COMPOSE_FILE=docker-compose.yml:docker-compose.prod.yml:docker-compose.observability.yml
GRAFANA_ADMIN_PASSWORD=<starkes-passwort>
```

(ohne `GRAFANA_ADMIN_PASSWORD` gilt wörtlich `CHANGE_ME`)

```bash
docker compose up -d --no-build
```

**Verifikation:**

```bash
curl -s localhost:8000/health                                    # status=healthy, version=1.0.0
curl -s -H "Authorization: Bearer eki_..." localhost:8000/ready  # database/redis/temporal = true
docker compose exec -e EKI_API_KEY=eki_... api \
  python scripts/smoke_prod.py --worker-metrics http://worker:9090   # RESULT: PASSED
```

---

## 3. Konfigurationsmatrix

Vollständige Referenz: [`CONFIGURATION.md`](CONFIGURATION.md). `.env.local` gilt für
`api`, `worker`, `postgres`, `temporal`; Änderungen erst nach
`docker compose up -d --force-recreate api worker` wirksam (`restart` reicht nicht).

| Bereich | Variable | Prod-Empfehlung | Hinweis |
|---|---|---|---|
| Umgebung | `ENV` / `DEBUG` | `production` / `false` | Prod-Overlay setzt beides fest; Validator prüft Secrets |
| LLM | `LLM_PROVIDER` | `local_mistral` | im Prod-Overlay per Compose-Substitution gesetzt (Default `local_mistral`); ein Wert in `.env.local` wird dort **überschrieben**. `mistral_cloud` wird in Prod abgelehnt |
| LLM | `LOCAL_MISTRAL_MODEL` | `mistral-small3.2` | Alternative: `LLM_PROVIDER=ollama` + `OLLAMA_MODEL=gemma4:31b` (per `export`/`.env`, Paritätsreport beachten) |
| LLM | `OLLAMA_NUM_CTX` | 32768 | bei VRAM-Engpass 16384 |
| LLM | `OLLAMA_MAX_CONCURRENT_REQUESTS` | 1–2 | nur erhöhen, wenn `eki_llm_queue_wait_seconds` hoch **und** VRAM frei (M07) |
| LLM | `LLM_PARALLEL_ENABLED`, `*_CONCURRENCY` | `false` / 1 | Stufen siehe README M07; gelten für neue Jobs |
| KB | `OLLAMA_EMBEDDING_MODEL`, `OLLAMA_EMBEDDING_MAX_CHARS` | `mxbai-embed-large`, 1000 | Alternative `bge-m3` + 30000 – nur zusammen setzen und **vor** dem ersten Ingest; späterer Wechsel §5.6 |
| KB | `KB_RETRIEVAL_ENABLED` | `true` erst nach Freigabe der Inhalte | sonst `false` |
| Zustellung | `EPRO_BASE_URL`, `EPRO_WEBHOOK_URL` | Prod-URLs | Webhook opt-in (M08) |
| Zustellung | `TEMPORAL_WORKFLOW_EXECUTION_TIMEOUT` | 36000 | ≥ Verarbeitung + 6 h (M10) |
| Buffer | `BUFFER_TTL_SECONDS` | 21600 | Pflichtenheft ≤ 6 h |
| OCR | `OCR_ENABLED`, `OCR_MAX_PAGES` | `true`, 50 | Images setzen `OMP_THREAD_LIMIT=1` (§8) |
| Security | `TRUST_PROXY_HEADERS`, `TRUSTED_PROXY_IPS` | `true`, Proxy-IP | nur hinter eigenem Reverse-Proxy; sonst teilen sich alle Clients das IP-Limit |
| Security | `RATE_LIMIT_*` | Defaults | 60/min je IP, 1000/h je Key |
| Observability | `METRICS_ENABLED`, `OTEL_ENABLED` | `true`, `false` | das Observability-Overlay setzt `OTEL_ENABLED=true` |
| Logs | `LOG_FORMAT`, `LOG_LEVEL` | `json`, `INFO` | Prod-Overlay erzwingt `json`; Inhalte werden maskiert (M08) |
| Compose | `EKI_IMAGE_TAG`, `GHCR_OWNER`, `GRAFANA_ADMIN_*` | Release-Tag, Owner, starkes Passwort | nur Projekt-`.env` oder Shell, **nicht** `.env.local` |

---

## 4. Laufender Betrieb

### 4.1 Tägliche Checks (5 Minuten)

1. Grafana `eki-overview`: API/Worker up, Jobs failed (24 h) = 0, Dead Letters = 0.
2. `GET /v1/ops/summary` (Admin-Key): keine `dead_lettered`, keine hängenden `delivering` > 6 h.
3. Alertmanager: keine offenen Alerts. Fehlgeschlagene Jobs: `error_message` in
   `GET /v1/ops/jobs?status=failed` prüfen (§5.4, §8).

### 4.2 Skalierung

- Mehr Durchsatz bei LLM-Wartezeit: erst `OLLAMA_MAX_CONCURRENT_REQUESTS` (GPU-abhängig),
  dann `LLM_PARALLEL_ENABLED=true` mit `*_CONCURRENCY=2`, zuletzt zweiter Worker
  (`--scale worker=2`; beide teilen die Ollama-Instanz, der Cap wirkt pro Prozess).
- API-Replikas: `--scale api=2` hinter Reverse-Proxy; jedes Replikat exportiert eigene Metriken.

### 4.3 API-Key-Verwaltung

API-Keys liegen nur als SHA-256-Hash in `api_keys`; der Klartext wird einmal angezeigt.

| Aufgabe | Vorgehen |
|---|---|
| Anlegen | `docker compose exec api python scripts/create_api_key.py --insert` (interaktiv: User ID, Organisation, Name, Beschreibung, Gültigkeit – Default **365 Tage**). `--admin` erlaubt zusätzlich `/v1/ops/*`. Ohne `--insert` wird nur ein Key erzeugt, nicht gespeichert |
| Auflisten | psql (§1): `SELECT name, user_id, is_admin, is_active, expires_at, last_used_at, usage_count FROM api_keys ORDER BY created_at;` |
| Sperren | `UPDATE api_keys SET is_active = false WHERE name = '<Name>';` – wirkt sofort (jede Anfrage prüft die DB) |
| Verlängern | `UPDATE api_keys SET expires_at = now() + interval '365 days' WHERE name = '<Name>';` |
| Rotieren | neuen Key **mit derselben User ID** anlegen (Jobs, Reports und Idempotenz-Keys sind an die User ID gebunden – mit anderer ID findet ePro laufende Jobs/Reports nicht mehr), ePro umstellen, alten Key sperren |

Abgelaufene oder gesperrte Keys liefern `401 Invalid or expired API key`. Rotationsplan
(z. B. jährlich, vor Ablauf) im Betriebskalender führen.

### 4.4 Backup & Restore

**Was gesichert wird**

| Daten | Ort | Sicherung |
|---|---|---|
| App-Metadaten, Dead Letters, API-Key-Hashes, KB (`kb_documents`, `kb_embeddings`) | DB `eki_db` | täglich `pg_dump` |
| Temporal-Zustand (laufende Workflows, Schedules, History) | DBs `temporal`, `temporal_visibility` (gleiche PostgreSQL-Instanz) | täglich `pg_dump`, zeitgleich mit `eki_db` |
| `API_SECRET_KEY` | `.env.local` / Tresor | Tresor – ohne ihn sind die KB-Originaltexte im Backup (Fernet) nicht lesbar |
| `.env.local`, Projekt-`.env` | Host | verschlüsselt, getrennt von den Dumps |
| Redis (SecureBuffer) | Volume `redis_data` (AOF im Prod-Overlay) | **nicht** sichern: nur transiente, verschlüsselte Inhalte ≤ 6 h (Processing-Only). AOF sorgt dafür, dass Inhalte einen Container-Neustart überstehen |
| Ollama-Modelle | Volume `ollama_data` | nicht sichern, neu pullen |

Dumps enthalten Metadaten (Projekt-/User-IDs) und KB-Chunktexte im Klartext
(`kb_embeddings.chunk_text`): verschlüsselt ablegen und Aufbewahrungsfrist festlegen
(`DATENSCHUTZ.md`).

**Sichern** (Custom-Format, binärsicher dank `-T`):

```bash
mkdir -p backup
for db in eki_db temporal temporal_visibility; do
  docker compose exec -T postgres sh -c \
    'PGPASSWORD="$POSTGRES_PASSWORD" pg_dump -U "$POSTGRES_USER" -Fc -d '"$db" \
    > backup/${db}_$(date +%F).dump
done
```

**Wiederherstellen** (legt die Datenbank neu an und ersetzt sie):

```bash
docker compose stop api worker temporal temporal-ui      # keine offenen Verbindungen
docker compose up -d postgres                            # bei neuem Volume: initialisiert eki_user/eki_db
for db in eki_db temporal temporal_visibility; do
  docker compose exec -T postgres sh -c \
    'PGPASSWORD="$POSTGRES_PASSWORD" pg_restore -U "$POSTGRES_USER" -d postgres --clean --if-exists --create' \
    < backup/${db}_<JJJJ-MM-TT>.dump
done
docker compose up -d --no-build
docker compose exec api alembic current                  # Revision des Backups; ggf. alembic upgrade head
```

Rollen und Passwörter sind nicht im Dump: Bei einem neuen Volume legt der
Postgres-Container Nutzer und `eki_db` mit den Werten aus `.env.local` an (dort also
konsistente Werte, §2.2). `API_SECRET_KEY` muss derselbe sein wie zur Sicherungszeit. Jobs, die zum
Sicherungszeitpunkt liefen, scheitern nach dem Restore (ihre Redis-Inhalte fehlen) –
`/v1/ops/jobs` prüfen und ePro neu anstoßen lassen. Restore regelmäßig auf einem
Testsystem üben.

### 4.5 Updates / Rollback

```bash
# 1. Release Notes/CHANGELOG lesen: Ändert das Release Workflow-Code (workflows/security_check.py,
#    workflows/maintenance.py)? Dann vorher drainen (§5.8).
git fetch --tags && git checkout v1.0.1                  # Compose-Dateien, Doku, Skripte des Releases
sed -i.bak 's/^EKI_IMAGE_TAG=.*/EKI_IMAGE_TAG=1.0.1/' .env && rm .env.bak
docker compose pull api worker
docker compose up -d --no-build api worker
docker compose exec api alembic upgrade head
docker compose exec api alembic current
curl -s localhost:8000/health                            # neue Version
```

Seit dem `!reset` im Prod-Overlay ersetzt dieser Ablauf Code **und** Migrationen
vollständig durch das Image (vorher überlagerten Quellcode-Mounts aus dem Checkout das
Image). `.env.example` des neuen Releases mit der eigenen `.env.local` vergleichen
(`diff .env.example .env.local`), neue Variablen übernehmen.

**Rollback:** `EKI_IMAGE_TAG` auf die vorige Version, `docker compose up -d --no-build api worker`.
Migrationen sind bislang additiv bzw. erweiternd; das ältere Image läuft daher auf dem
neueren Schema. Ist ein Downgrade nötig, **vor** dem Zurückschalten mit dem *neuen* Image
ausführen – das alte Image kennt die neuere Revision nicht
(`Can't locate revision identified by …`):

```bash
docker compose exec api alembic downgrade <revision-des-alten-releases>   # vorher Backup (§4.4)
```

Laufende Workflows überstehen Worker-Neustarts (Temporal-Replay), solange der
Workflow-Code unverändert bleibt (§5.8).

### 4.6 Datenbank-Migrationen

Aktueller Stand (head): **`b8d0f2a4c6e3`**. Kette:

| Revision | Datei | Inhalt |
|---|---|---|
| `b7ed8ab1d224` | `20260130_0912_b7ed8ab1d224_add_security_models_and_authorization.py` | API-Keys, Job-/Report-Metadaten, Audit-Log |
| `c3a5f8d12e01` | `20260208_1158_m05_delivery_idempotency.py` | M05: Zustellmodus, Idempotenz, Report-Referenz |
| `d4b7e9f23a01` | `20260324_1400_add_script_id_to_job_metadata.py` | `script_id` für ePro |
| `e8f1c2d3a401` | `20260521_m06_knowledge_base.py` | M06: pgvector, `kb_documents`, `kb_embeddings` (`vector(1024)`) |
| `f1a2b3c4d5e6` | `20260916_m10_delivery_hardening.py` | M10: Zustell-Bookkeeping, Dead Letters, Admin-Keys, Idempotenz je User |
| `a7c9e1f3b5d2` | `20261015_m13_widen_job_status.py` | `job_metadata.status` VARCHAR(9) → VARCHAR(20) (`DELIVERING` passte nicht) |
| `b8d0f2a4c6e3` | `20261015_m13b_kb_tenant_dedupe.py` | KB-Dedupe je Mandant: Unique `(tenant_id, content_hash)` statt global eindeutigem `content_hash`. Downgrade bricht ab, wenn ein Hash bei mehreren Mandanten vorkommt |

`docker compose exec api alembic history` zeigt die Kette, `alembic current` den Stand der DB.

**Richtlinie**

- Jede Schemaänderung nur per Alembic-Migration; nie Tabellen von Hand ändern.
- Migrationen vorwärts kompatibel halten (Spalten/Tabellen ergänzen, Typen erweitern),
  damit das vorige Image beim Rollback weiterläuft. Entfernen/Umbenennen in zwei Releases
  aufteilen.
- Jede Migration braucht ein funktionierendes `downgrade()`. Downgrades löschen Spalten
  bzw. Tabellen (Datenverlust) – nur nach Backup. Ist ein Downgrade ohne Datenverlust
  nicht möglich, bricht es mit Fehlermeldung ab statt Daten zu löschen (z. B. `b8d0f2a4c6e3`).
- In Produktion läuft `alembic upgrade head` nach jedem Update (§4.5).

**Neue Migration erstellen** (Entwicklung, gegen eine Wegwerf-Datenbank, z. B. die
Test-PostgreSQL aus `TESTING_GUIDE.md`):

```bash
export DATABASE_URL=postgresql+asyncpg://eki_user:eki_password@localhost:5432/eki_test
alembic upgrade head
# Modell in core/db_models.py ändern, dann:
alembic revision --autogenerate -m "kurze beschreibung"
# Datei in db/migrations/versions/ prüfen und nacharbeiten: Typen/Längen,
# server_default, Indizes, pgvector-Typen, Datenmigration, downgrade().
# Achtung: Enum(native_enum=False) wird als VARCHAR(<längster Wert>) angelegt –
# neue Enum-Werte brauchen ggf. eine Verbreiterung (siehe a7c9e1f3b5d2).
alembic upgrade head && alembic check            # "No new upgrade operations detected."
alembic downgrade -1 && alembic upgrade head     # Downgrade testen
TEST_POSTGRES_URL=$DATABASE_URL pytest           # PG-Modus inkl. tests/test_pg_integration.py
```

`alembic` liest die URL aus `Settings` (`DATABASE_URL`, auch `DATABASE_URL_FILE`) und
nutzt intern `psycopg2`. CI führt `upgrade head → check → downgrade base → upgrade head`
aus und wendet die Migrationen zusätzlich im gebauten API-Image an.

### 4.7 Abhängigkeiten & Lock-Dateien

`requirements.lock` (Laufzeit) und `requirements-dev.lock` (inkl. `[dev]`) pinnen alle
transitiven Pakete; sie werden als Constraints benutzt
(Dockerfiles: `pip install -c requirements.lock -e .`, CI/Entwicklung:
`pip install -c requirements-dev.lock -e ".[dev]"`). Ein Paket, das nicht in der
Lock-Datei steht, würde ungepinnt installiert – nach jeder Änderung an `pyproject.toml`
neu erzeugen.

```bash
pip install uv                                   # einmalig
uv pip compile pyproject.toml --universal --python-version 3.11 --no-header \
  --upgrade -o requirements.lock
uv pip compile pyproject.toml --extra dev --universal --python-version 3.11 --no-header \
  --upgrade -o requirements-dev.lock
# gezielt nur ein Paket: --upgrade-package <name> statt --upgrade
pip install -c requirements-dev.lock -e ".[dev]"
pytest                                           # SQLite-Modus
TEST_POSTGRES_URL=... TEST_REDIS_URL=... pytest  # PG-Modus (TESTING_GUIDE.md)
docker compose -f docker-compose.yml build api worker   # Images bauen sich mit den neuen Pins
```

Beide Lock-Dateien gemeinsam committen. Bekannt: `pyproject.toml` enthält ungenutzte
Pakete (u. a. `python-jose` mit bekannten CVEs, `PyJWT`, `passlib`, `bcrypt`, `aiohttp`)
– Entfernen empfohlen; `requests` nutzen nur Skripte (transitiv installiert).

### 4.8 Embedding-Modell (Knowledge Base)

API (Ingest) und Worker (Retrieval) müssen dasselbe Embedding-Modell nutzen; beide lesen
`OLLAMA_EMBEDDING_MODEL` aus `.env.local`. Standard ist `mxbai-embed-large`
(Code-Default, `.env.example`) mit `OLLAMA_EMBEDDING_MAX_CHARS=1000`. `bge-m3` ist die
dokumentierte Alternative für längere Chunks (`OLLAMA_EMBEDDING_MAX_CHARS=30000`).
Beide liefern 1024 Dimensionen, wie die Spalte `vector(1024)` verlangt – andere Modelle
nur mit gleicher Dimension. Vektoren verschiedener Modelle sind nicht vergleichbar:
Ein Wechsel nach dem ersten Ingest erfordert Leeren und Neubefüllen der KB (§5.6).
Mit `LLM_PROVIDER=mistral_cloud` ist die KB nicht nutzbar (kein `embed()`).

---

## 5. Runbooks

Kurzübersicht (Alert → erste Maßnahme):

| Symptom | Erste Maßnahme | Details |
|---|---|---|
| `EkiApiDown` | `docker compose ps`, `docker compose logs api --tail 200` | Startfehler meist Konfig-Validator oder `*_FILE` (§8) |
| `EkiWorkerDown` | `docker compose logs worker --tail 200` | Temporal erreichbar? Namespace? (§5.3, §8) |
| `EkiLlmErrorRateHigh`, Jobs `failed` mit LLM-Fehler | §5.4 | Modell gepullt? VRAM? |
| Jobs hängen in `running` | Temporal UI (§8.2): Pending Activities; Worker-Logs nach `job_id` filtern | Korrelation via `request_id`/`job_id` in jeder Logzeile |
| `EkiDeliveryFailureRateHigh` / Dead Letters | [`M10_FAILOVER_RUNBOOK.md`](M10_FAILOVER_RUNBOOK.md) §2 | ePro-Erreichbarkeit, 4xx-Ursachen |
| Redis voll (`noeviction`) | `docker compose exec redis redis-cli --scan --pattern 'eki:buf:*' \| wc -l`; TTL läuft; ggf. `maxmemory` erhöhen | Nie flushen, solange Jobs laufen |
| OCR langsam / Scan-Seiten übersprungen | §8 (OCR) | `OMP_THREAD_LIMIT=1`, `OCR_MAX_PAGES`, `OCR_DPI=200` |

### 5.1 PostgreSQL-Ausfall

- **Symptome:** `/ready` meldet `database: false`; alle authentifizierten Endpunkte
  antworten `503 ServiceUnavailable` (die Key-Prüfung braucht die DB). Temporal nutzt
  dieselbe Instanz und fällt mit aus (§5.3).
- **Maßnahmen:** `docker compose ps postgres`, `docker compose logs postgres --tail 100`,
  Plattenplatz (§5.5), `docker compose up -d postgres`. Danach `/ready` und Worker-Logs.
- **Nachlauf:** Job-Statusupdates des Workers sind best effort – während des Ausfalls
  kann der Status in `job_metadata` hinter dem Temporal-Zustand zurückbleiben.
  `GET /v1/ops/jobs?status=running` mit der Temporal UI abgleichen; gescheiterte Jobs
  stößt ePro neu an.

### 5.2 Redis-Ausfall

- **Symptome:** `POST /v1/security/check:async` → 503; `/ready` meldet `redis: false`.
  Rate-Limiting lässt Anfragen ohne Redis durch. Laufende Jobs können Buffer-Inhalte nicht
  lesen/schreiben → Retries, danach `failed` bzw. Push-Retry bis 6 h und Dead Letter
  (`M10_FAILOVER_RUNBOOK.md` §2.7).
- **Pull:** `GET /v1/security/reports/{id}` antwortet während des Ausfalls mit `503`; der
  One-Shot wird nicht verbraucht, der Report bleibt bis zum Ende der TTL abrufbar. Ist der
  Redis-Inhalt verloren (Volume weg), liefert der Abruf `410`.
- **Maßnahmen:** `docker compose logs redis --tail 100`;
  `docker compose exec redis redis-cli INFO memory` (bei `OOM command not allowed`:
  `maxmemory` erhöhen); Plattenplatz (AOF, §5.5); `docker compose up -d redis`.
  Nie `FLUSHALL`, solange Jobs laufen.

### 5.3 Temporal-Ausfall

- **Symptome:** `check:async` → `503` „Workflow engine is not reachable“; `/ready`
  meldet `temporal: false`; Worker-Logs zeigen Verbindungsfehler.
- **Maßnahmen:** zuerst PostgreSQL prüfen (§5.1), dann
  `docker compose logs temporal --tail 200`, `docker compose up -d temporal`.
  Der Worker verbindet sich neu oder wird von `restart: unless-stopped` neu gestartet;
  sonst `docker compose restart worker` (ohne Konfigurationsänderung genügt `restart`).
- **Nachlauf:** Workflows setzen nach der Wiederherstellung fort (Zustand liegt in den
  Datenbanken `temporal*`); Timer (6-h-Pull-Fenster) laufen weiter.

### 5.4 Ollama-Ausfall / LLM-Fehler

- **Verhalten (fail-closed):** Transiente LLM-Fehler (Ollama nicht erreichbar, Timeout)
  werden in der Risikoanalyse wiederholt; bleiben sie bestehen, endet der Job mit
  `failed`. Es entsteht kein Report, der fälschlich „keine Risiken“ ausweist. (Vor dieser
  Korrektur bekam eine Szene bei LLM-Fehlern stillschweigend 0 Findings – Reports aus
  bekannten früheren Ausfallzeiträumen nachprüfen.) Blockiert der
  Prompt-Injection-Filter eine Szene, endet der Job ohne Retry mit `failed` (§8).
- **Diagnose:**
  ```bash
  docker compose exec ollama ollama list                 # Modelle gepullt?
  docker compose exec api python scripts/test_llm.py     # Health-Check + Testgenerierung mit der aktuellen Konfiguration
  docker compose logs ollama --tail 200
  nvidia-smi                                             # VRAM/GPU auf dem Host
  ```
- **Maßnahmen:** fehlendes Modell pullen (§2.2), `docker compose up -d ollama`; bei
  VRAM-Engpass `OLLAMA_NUM_CTX` bzw. `OLLAMA_MAX_CONCURRENT_REQUESTS` senken
  (`up -d --force-recreate api worker`). Danach betroffene Jobs
  (`GET /v1/ops/jobs?status=failed`) von ePro neu anstoßen lassen.

### 5.5 Platte voll

```bash
df -h /var/lib/docker
docker system df -v                     # Images, Volumes, Build-Cache
```

| Verbraucher | Maßnahme |
|---|---|
| alte Images | `docker image ls --filter 'reference=ghcr.io/*/eki-*'`, nicht mehr benötigte Tags mit `docker image rm` entfernen (vorige Version für Rollback behalten); `docker builder prune` |
| Container-Logs (json-file, ohne Rotation unbegrenzt) | in `/etc/docker/daemon.json` `"log-opts": {"max-size": "50m", "max-file": "5"}` setzen, Docker neu starten, Container neu erzeugen |
| `ollama_data` | ungenutzte Modelle: `docker compose exec ollama ollama rm <modell>` |
| `prometheus_data` | Retention ist 30 Tage (`docker-compose.observability.yml`) |
| `postgres_data` | Metadaten-Tabellen werden nie automatisch bereinigt (bekannte Grenze); Wachstum beobachten |
| `redis_data` (AOF) | `docker compose exec redis redis-cli BGREWRITEAOF` |

Folgen: PostgreSQL stoppt Schreibzugriffe (→ 503, §5.1), Redis verweigert Schreibzugriffe
bei AOF-Fehlern (→ neue Jobs 503, §5.2).

### 5.6 Embedding-Modell wechseln (KB leeren und neu befüllen)

Nötig beim Wechsel von `OLLAMA_EMBEDDING_MODEL` und nach einer `API_SECRET_KEY`-Rotation (§5.7).

```bash
# 1. Retrieval während des Umbaus aus: KB_RETRIEVAL_ENABLED=false in .env.local
# 2. Neues Modell laden und konfigurieren (beide Werte!)
docker compose exec ollama ollama pull bge-m3
#    .env.local: OLLAMA_EMBEDDING_MODEL=bge-m3, OLLAMA_EMBEDDING_MAX_CHARS=30000
docker compose up -d --force-recreate api worker
# 3. KB leeren (Embeddings werden per Cascade mitgelöscht), in psql (§1):
#    DELETE FROM kb_documents;
# 4. Neu befüllen – auf dem Host (Python 3 + `pip install requests`) aus den Quelldokumenten:
export EKI_API_KEY=eki_...                            # EKI_API_URL default http://localhost:8000
python3 scripts/seed_kb.py --reseed                   # alle Dateien aus config/kb_seed/real/
python3 scripts/seed_kb.py --add <datei> --title "<Titel>" --tags <tags>   # weitere Dokumente
python3 scripts/seed_kb.py --status
# 5. Retrieval prüfen, dann KB_RETRIEVAL_ENABLED=true und up -d --force-recreate api worker
```

Die Quelldokumente müssen außerhalb der eKI vorliegen (die KB speichert Originale nur
verschlüsselt und bietet keinen Export). `config/kb_seed/real/` ist im Repository nicht
angelegt; echte Dokumente dort nicht committen.

### 5.7 `API_SECRET_KEY` rotieren

Aus dem Schlüssel werden die Fernet-Schlüssel für den SecureBuffer und die
KB-Originaltexte abgeleitet. Folgen eines Wechsels: Inhalte laufender Jobs und nicht
abgeholte Pull-Reports sind nicht mehr entschlüsselbar (Jobs scheitern), KB-Originale
in DB und Backups ebenfalls nicht (Retrieval nutzt die separat gespeicherten Chunks und
läuft weiter).

1. Drainen, bis kein Prüf-Workflow mehr offen ist (§5.8).
2. Neuen Schlüssel erzeugen: `python3 -c "import secrets; print(secrets.token_urlsafe(32))"`,
   in `.env.local` (bzw. Secret-Datei) eintragen.
3. `docker compose up -d --force-recreate api worker`.
4. KB leeren und neu befüllen (§5.6 ab Schritt 3), damit die Originale mit dem neuen
   Schlüssel verschlüsselt sind.
5. Alten Schlüssel im Tresor behalten, solange Backups mit ihm existieren.

### 5.8 Laufende Workflows drainen (vor Workflow-Code-Änderungen)

Temporal spielt offene Workflows nach einem Worker-Neustart mit dem **neuen** Code nach.
Ändert ein Release `workflows/security_check.py` oder `workflows/maintenance.py`, schlagen
offene Workflows mit Nichtdeterminismus-Fehlern fehl. Reine Activity-Änderungen
(`workflows/activities.py`) sind unkritisch, solange Activity-Namen und Ein-/Ausgabeformate
kompatibel bleiben. Prüfen: `git diff --stat v<alt> v<neu> -- workflows/`.

1. Neue Jobs anhalten (Absprache mit ePro oder am Reverse-Proxy).
2. Warten, bis keine Prüf-Workflows mehr offen sind: Temporal UI (§8.2), Filter
   *Running*; oder `docker compose exec temporal tctl --address temporal:7233 workflow list --open`.
   Der Jobstatus genügt nicht: Pull-Workflows bleiben nach `completed` noch
   `BUFFER_TTL_SECONDS` (6 h) offen (Pull-Watch). Obergrenze ist
   `TEMPORAL_WORKFLOW_EXECUTION_TIMEOUT` (10 h).
3. Update einspielen (§4.5), Jobs wieder freigeben.

Für Entwickler: Statt zu drainen kann Workflow-Code mit `workflow.patched()` versioniert werden.

### 5.9 Datenbank-Passwort ändern

`POSTGRES_PASSWORD` wirkt nur bei der Erstinitialisierung des Volumes. Wechsel:

```bash
NEW=$(python3 -c "import secrets; print(secrets.token_urlsafe(32))")
docker compose exec postgres sh -c 'PGPASSWORD="$POSTGRES_PASSWORD" psql -U "$POSTGRES_USER" -d "$POSTGRES_DB" -c "ALTER USER $POSTGRES_USER PASSWORD '"'$NEW'"'"'
# danach in .env.local POSTGRES_PASSWORD, POSTGRES_PWD und DATABASE_URL auf $NEW setzen
docker compose up -d --force-recreate postgres temporal api worker
```

---

## 6. Cutover Cloud → Lokal (Abnahmetest 9)

1. Stage-Sign-off (Abnahmetest 8) mit `LLM_PROVIDER=mistral_cloud` auf der Stage-Umgebung liegt vor.
2. Paritätsreport (`scripts/run_parity.py --providers mistral_cloud,local_mistral`, auf der
   Stage – in Produktion ist `mistral_cloud` gesperrt) bestanden; Ergebnis in
   `docs/M11_PARITY_REPORT.md` dokumentiert.
3. Prod: `LLM_PROVIDER` nicht überschreiben (Overlay-Default `local_mistral`), kein
   `MISTRAL_API_KEY`, `LLM_ALLOW_EXTERNAL_PROVIDERS` nicht gesetzt.
4. Firewall: ausgehend `api.mistral.ai` blockiert (Defense in depth).
5. Smoke-Test (§7) → `RESULT: PASSED`, Ausgabe dem Protokoll beilegen.
6. Erste produktive Prüfung gemeinsam mit ePro-Team beobachten (Brückenmeeting §13).

---

## 7. Nachweise für die Abnahme

| Test | Kommando | Erwartung |
|---|---|---|
| Health | `curl -s localhost:8000/health` | `status=healthy`, Version = Release-Tag |
| Smoke E2E + keine externen Calls | `docker compose exec -e EKI_API_KEY=eki_... api python scripts/smoke_prod.py --worker-metrics http://worker:9090` | `RESULT: PASSED` |
| Parität | siehe unten | alle Schwellen ✅ |
| Großdokument | auf dem Host: `EKI_API_KEY=eki_... python3 tests/run_pdf_m07_benchmark.py --concurrency 1` (braucht `requests`, Zugriff auf `docker stats`) | `acceptance.passed_overall=true` |
| Log-Hygiene | `docker compose logs api worker \| grep -ci "<Textprobe aus Drehbuch>"` | `0` |

Parität im API-Container (das Golden-Set liegt nicht im Image und wird hineinkopiert;
Ollama ist nur im Docker-Netz erreichbar):

```bash
docker compose cp tests/parity/golden_scenes.yaml api:/tmp/golden_scenes.yaml
docker compose exec api python scripts/run_parity.py --providers local_mistral \
  --golden /tmp/golden_scenes.yaml --out /tmp/parity
docker compose cp api:/tmp/parity ./tests/reports/
```

---

## 8. Troubleshooting

### 8.1 Fehlerbilder

| Fehlerbild | Ursache | Lösung |
|---|---|---|
| `The SQLAlchemy asyncio module requires that the Python 'greenlet' library is installed` bei jedem DB-Zugriff | Installation ohne `sqlalchemy[asyncio]`/ohne Lock-Datei (SQLAlchemy ≥ 2.1 bringt greenlet nicht mehr mit) | aktuellen Stand installieren: `pip install -c requirements.lock -e .` bzw. Image neu bauen/ziehen |
| `alembic upgrade head` → `No module named 'psycopg'` | Stand vor der Korrektur in `db/migrations/env.py` | aktuellen Stand verwenden (`+psycopg2`, `psycopg2-binary` ist Abhängigkeit) |
| Jobs scheitern in der Zustellphase, Log: `value too long for type character varying(9)` | Schema nicht auf head (`DELIVERING` passt nicht in VARCHAR(9)) | `docker compose exec api alembic upgrade head` (head `b8d0f2a4c6e3`; die Korrektur ist `a7c9e1f3b5d2`) |
| Startabbruch `API_SECRET_KEY must be a strong non-default secret in production` / `DATABASE_URL contains insecure placeholder credentials` | Platzhalter aus `.env.example` | Secrets erzeugen (§2.2) |
| Startabbruch `… points to unreadable file` / `… points to empty file` | `*_FILE` gesetzt, Datei nicht gemountet oder nicht lesbar für UID 1000 | Mount ergänzen (`secrets/README.md` §2) oder `*_FILE` entfernen |
| Compose meldet Fehler zu `!reset` | Docker Compose < 2.24 | Compose aktualisieren |
| Geänderte `.env.local`-Werte wirken nicht | `docker compose restart` liest `env_file` nicht neu; oder die Variable wird per Compose-Substitution/Overlay gesetzt | `docker compose up -d --force-recreate api worker`; `LLM_PROVIDER`, `EKI_IMAGE_TAG`, `GHCR_OWNER`, `GRAFANA_ADMIN_*`, `OTEL_ENABLED` über Projekt-`.env`/`export` (`CONFIGURATION.md` §2) |
| Grafana-Login nur mit `admin`/`CHANGE_ME` | `GRAFANA_ADMIN_PASSWORD` stand nur in `.env.local` | in Projekt-`.env` setzen, Grafana neu erzeugen; ist `grafana_data` schon initialisiert, Passwort in Grafana selbst ändern |
| `password authentication failed for user "eki_user"` nach Passwortwechsel | `POSTGRES_PASSWORD` gilt nur bei Erstinitialisierung | §5.9 |
| `psql` im Container fragt nach Passwort | Cluster mit `-A md5` initialisiert | `sh -c 'PGPASSWORD="$POSTGRES_PASSWORD" psql …'` (§1) |
| Jobs bleiben `pending`, Worker-Log `Namespace … is not found` | `TEMPORAL_NAMESPACE` ≠ `default`, Namespace existiert nicht | `TEMPORAL_NAMESPACE=default` oder Namespace anlegen: `DEFAULT_NAMESPACE=<ns>` in `.env.local`, `docker compose up -d --force-recreate temporal api worker` |
| `host.docker.internal` nicht auflösbar (Linux, z. B. ePro-Mock auf dem Host) | nur Docker Desktop kennt den Namen automatisch | `docker-compose.override.yml` (Entwicklung) mit `extra_hosts: ["host.docker.internal:host-gateway"]` für `api` und `worker` |
| Temporal UI in Produktion nicht erreichbar | gewollt: Prod veröffentlicht nur 8000 | SSH-Tunnel (§8.2) |
| Prometheus-Target `eki-api` down/401, oder `secrets/prometheus_api_key.txt` ist ein Verzeichnis | Key-Datei fehlte beim `up` bzw. für Prometheus (UID 65534) nicht lesbar | Verzeichnis löschen, Key anlegen (`secrets/README.md` §3), Overlay neu starten |
| OCR-Seiten werden übersprungen, Log `OCR … timeout` | Tesseract nutzt OpenMP über alle Kerne; unter Last steigt eine Seite von ~1 s auf > 60 s (`OCR_PAGE_TIMEOUT_SECONDS`) | Images setzen `OMP_THREAD_LIMIT=1`; bei anderen Installationen (Bare Metal, eigene Images) ebenfalls `OMP_THREAD_LIMIT=1` setzen; Sprachpakete `tesseract-ocr-deu`/`-eng` prüfen |
| Job `failed`: `Szene <nr>: vom Prompt-Injection-Filter blockiert – manuelle Prüfung erforderlich` | der Filter (`core/prompt_sanitizer.py`) hält den Szenentext für eine Prompt-Injection; kein Retry | Diese Szene manuell sicherheitsfachlich prüfen (der übrige Report entsteht nicht). Die ausgelöste Regel steht ohne Inhalt im Worker-Log: `docker compose logs worker \| grep "Potential prompt injection"` (`rule=…`). Fehlalarme sind möglich bei Szenen, die selbst mit KI/Chatbots spielen („act as an AI“, `SYSTEM: you must …`), Bildschirm-/Code-Text (`<script>`, `eval(`) oder dreifache Anführungszeichen enthalten; der Filter vor den Übergabe-Korrekturen schlug zusätzlich bei „act as …“, Zeilen mit `System:`/`User:` und Trennlinien aus `---`/`===` an |
| Job `failed` mit LLM-/Timeout-Meldung | LLM dauerhaft nicht erreichbar (fail-closed) | §5.4 |
| `/docs` liefert 404 | `DEBUG=false` (Produktion) | Vertrag: `openapi/eki-api-v1.0.yaml` |
| `401 Invalid or expired API key` | Key abgelaufen (Default 365 Tage) oder gesperrt | §4.3 |
| `403 X-Actor-User-Id does not match …` | Header passt nicht zur User ID des Keys | Header weglassen oder korrekt setzen |
| KB-Upload `500` mit `LLM_PROVIDER=mistral_cloud` | Provider ohne Embeddings | KB nur mit `local_mistral`/`ollama` |
| KB-Upload: Ollama antwortet 500 beim Embedding | Eingabe länger als das Embedding-Modell erlaubt | `OLLAMA_EMBEDDING_MAX_CHARS` passend zum Modell (§4.8) |

Jede Fehlerantwort (4xx/5xx) enthält `request_id` im Body und im Header `X-Request-ID`.
Damit die zugehörigen Logzeilen finden:
`docker compose logs api worker | grep <request_id>`.

### 8.2 Temporal UI in Produktion (SSH-Tunnel)

Die UI hat keine Authentifizierung und wird in Produktion nicht veröffentlicht. Zugriff:

```bash
# auf dem Server: Container-IP ermitteln
docker inspect -f '{{range .NetworkSettings.Networks}}{{.IPAddress}}{{end}}' eki-temporal-ui
# vom Arbeitsplatz
ssh -N -L 8080:<container-ip>:8080 <user>@<server>
# Browser: http://localhost:8080
```
