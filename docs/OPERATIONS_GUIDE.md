# eKI API – Betriebsleitfaden (Stand Release 1.0.1)

Zielgruppe: IT-Betrieb der Filmakademie. Maßgebliches Betriebsdokument; das frühere
`DEPLOYMENT_GUIDE.md` ist veraltet. Ergänzend:
[`CONFIGURATION.md`](CONFIGURATION.md) (alle Einstellungen),
[`M09_SLO.md`](M09_SLO.md) (Monitoring, Alerts),
[`M10_FAILOVER_RUNBOOK.md`](M10_FAILOVER_RUNBOOK.md) (Zustellstörungen),
[`EPRO_INTEGRATION_GUIDE.md`](EPRO_INTEGRATION_GUIDE.md) (Schnittstelle),
[`LASTSTEUERUNG.md`](LASTSTEUERUNG.md) (Last, Parallelität, Zeitlimits, Ollama-Server),
[`DATENSCHUTZ.md`](DATENSCHUTZ.md) (Datenhaltung) und
[`../secrets/README.md`](../secrets/README.md) (Secrets).

> **Konvention:** Alle `docker compose`-Kommandos gehen davon aus, dass die
> Projekt-`.env` (§2.2) `COMPOSE_FILE=docker-compose.yml:docker-compose.prod.yml` setzt.
> Ohne diese Datei jedem Aufruf `-f docker-compose.yml -f docker-compose.prod.yml`
> mitgeben – ein `docker compose up` nur mit der Basisdatei startet den
> Entwicklungs-Stack (Quellcode-Mounts, lokale Builds, Temporal UI auf Port 8080).

> **Update von 1.0.0:** Release 1.0.1 verlangt einmalige Schritte (Redis-Volume, externe
> Ollama-URL, Temporal-Dynamic-Config, Migration `c4e6a8b0d2f1`) – Checkliste in
> [§4.10 „Upgrade 1.0.0 → 1.0.1"](#410-upgrade-100--101).

---

## 1. Architektur im Betrieb

| Container | Image | Aufgabe | Port | Skalierbar |
|---|---|---|---|---|
| `eki-api` | `ghcr.io/<owner>/eki-api:<version>` | REST-API (Gunicorn + Uvicorn) | 8000 – **einziger veröffentlichter Port** | ja, `--scale api=N` hinter Reverse-Proxy |
| `eki-worker` | `ghcr.io/<owner>/eki-worker:<version>` | Temporal-Worker: Parsing, LLM, Report, Zustellung, KB-Cleanup, Metadaten-Cleanup | 9090 Metriken, nur intern | ja, `--scale worker=N` |
| `eki-temporal` | `temporalio/auto-setup:1.29.4` | Workflow-Engine, Retry-Fenster, Schedules, Task-Queue-Priorität (Dynamic Config `docker/temporal/dynamicconfig/eki.yaml`) | 7233 intern | – |
| `eki-temporal-ui` | `temporalio/ui:2.47.3` | Operator-UI ohne Authentifizierung; zeigt Payloads seit 1.0.1 nur verschlüsselt | 8080 intern (Zugriff §8.2) | – |
| `eki-postgres` | `pgvector/pgvector:pg16` | Datenbanken `eki_db` (App, KB), `temporal`, `temporal_visibility` | 5432 intern | – |
| `eki-redis` | `redis:7.2-alpine` (Prod: AOF, `maxmemory 1gb`, `noeviction`) | SecureBuffer (verschlüsselt, TTL 6 h), Rate-Limit-Zähler. Versionswahl und Support-Risiko: §4.9 | 6379 intern | – |

**Inferenz und KB-Embeddings** laufen auf einer **externen Ollama-Instanz** (GPU-Host), die
nicht Teil des Stacks ist; API und Worker erreichen sie über `OLLAMA_BASE_URL` bzw.
`LOCAL_MISTRAL_BASE_URL` aus `.env.local` (§4.11). Das Overlay `docker-compose.ollama.yml`
(Ollama-Container) ist nur für Entwicklung/Offline gedacht und wird in Produktion nicht verwendet.

Persistente Volumes: `postgres_data`, `redis_data` (mit
Observability-Overlay zusätzlich `prometheus_data`, `alertmanager_data`, `grafana_data`;
veröffentlicht werden dort Grafana 3000 und Jaeger 16686). CI prüft, dass das
Prod-Overlay nur Port 8000 veröffentlicht, keinen Quellcode mountet und keinen
`ollama`-Dienst enthält.

Auf Infrastruktur-Dienste greift man nur per `docker compose exec` zu, z. B.
PostgreSQL (die Datenbank verlangt auch im Container ein Passwort, `-A md5`):

```bash
docker compose exec postgres sh -c 'PGPASSWORD="$POSTGRES_PASSWORD" psql -U "$POSTGRES_USER" -d "$POSTGRES_DB"'
docker compose exec redis redis-cli INFO persistence
curl -s http://<ollama-host>:11434/api/tags          # Modelle der externen Instanz (vom eKI-Host)
```

Skripte laufen im API-Container (dort sind `scripts/`, `config/` und die synthetischen
FDX-Dateien `tests/fixtures/fdx/` – Standardeingabe von `scripts/smoke_prod.py` – enthalten
und alle Dienste erreichbar): `docker compose exec api python scripts/<skript>.py`. Das
Worker-Image enthält keine Testdaten.

---

## 2. Installation / Erstinbetriebnahme

### 2.1 Voraussetzungen

- Docker ≥ 24 mit **Docker Compose ≥ 2.24** (`docker compose version`; das Prod-Overlay
  nutzt `!reset`, ältere Versionen lehnen die Datei ab)
- **Externe Ollama-Instanz** (§4.11) auf einem GPU-Host: NVIDIA-GPU ≥ 24 GB VRAM empfohlen
  (Pflichtenheft §8), Plattenplatz dort ca. 15 GB für `mistral-small3.2` und ~1 GB für
  `mxbai-embed-large`. Der eKI-Host selbst braucht keine GPU.
- Plattenplatz auf dem eKI-Host: Images, Datenbank und Logs (§5.5)
- Ausgehend vom eKI-Host nur: GHCR (Images), ePro (Push, Webhook) und die Ollama-Instanz
  (`<ollama-host>:11434`, internes Netz). Die Ollama-Registry (Modell-Pull) braucht nur der
  Ollama-Host. **Kein** ausgehender LLM-Cloud-Verkehr
- Eingehend: ePro → `eki-api:8000` (TLS-Terminierung durch Reverse-Proxy)
- Python 3 auf dem Host nur für `scripts/generate_secrets.py` und optional
  `scripts/seed_kb.py` (§5.6)

### 2.2 Schritte

```bash
git clone <repo> eki && cd eki
git checkout v1.0.1                       # bzw. gewünschter Release-Tag

# 1. App-Konfiguration (.env.local) mit echten Secrets
cp .env.example .env.local
python3 scripts/generate_secrets.py       # schreibt secrets/db_password.txt, api_secret_key.txt, database_url.txt
PW=$(cat secrets/db_password.txt); KEY=$(cat secrets/api_secret_key.txt)
sed -i.bak -e "s/CHANGE_ME_STRONG_PASSWORD_HERE/$PW/g" \
           -e "s/CHANGE_ME_256_BIT_SECRET_KEY_HERE/$KEY/" .env.local && rm .env.local.bak
# .env.local weiter anpassen (§3): ENV=production, DEBUG=false, EPRO_BASE_URL,
# EPRO_WEBHOOK_URL, CORS_ORIGINS, TRUST_PROXY_HEADERS/TRUSTED_PROXY_IPS und
# OLLAMA_BASE_URL=http://<ollama-host>:11434 (externe Instanz, §4.11; der Platzhalter
# aus .env.example löst nicht auf)

# 2. Compose-Variablen: werden NICHT aus .env.local gelesen (CONFIGURATION.md §2)
cat > .env <<'EOF'
COMPOSE_FILE=docker-compose.yml:docker-compose.prod.yml
GHCR_OWNER=<github-owner>
EKI_IMAGE_TAG=1.0.1
EOF
docker compose config -q                  # Syntax/Compose-Version prüfen

# 3. Modelle einmalig auf der externen Ollama-Instanz laden (auf dem Ollama-Host)
ollama pull mistral-small3.2              # Inferenz (LOCAL_MISTRAL_MODEL)
ollama pull mxbai-embed-large             # KB-Embeddings (OLLAMA_EMBEDDING_MODEL)

# 4. Start
docker compose pull api worker
docker compose up -d --no-build

# 5. Datenbankschema
docker compose exec api alembic upgrade head
docker compose exec api alembic current                      # -> c4e6a8b0d2f1 (head)

# 6. API-Keys (interaktiv, Klartext wird nur einmal angezeigt; §4.3)
docker compose exec api python scripts/create_api_key.py --insert          # ePro-Service-Key
docker compose exec api python scripts/create_api_key.py --insert --admin  # Ops-/KB-Key (/v1/ops/*, /v1/kb/*)
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
curl -s localhost:8000/health                                    # status=healthy, version=1.0.1
curl -s -H "Authorization: Bearer eki_..." localhost:8000/ready  # database/redis/temporal = true
# Ollama aus dem Worker erreichbar? (/ready prüft Ollama nicht) -> 200
docker compose exec worker python -c "import os,httpx;print(httpx.get((os.environ.get('LOCAL_MISTRAL_BASE_URL') or os.environ['OLLAMA_BASE_URL'])+'/api/tags',timeout=10).status_code)"
docker compose logs worker | grep -E "payload encryption active|Schedule eki-"   # Codec-Key-ID, beide Schedules
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
| LLM | `OLLAMA_BASE_URL` (optional `LOCAL_MISTRAL_BASE_URL`) | `http://<ollama-host>:11434` | URL der externen Ollama-Instanz (§4.11); gilt für Inferenz und Embeddings, in API **und** Worker. Der Code-Default `http://ollama:11434` passt nur zum Entwicklungs-Overlay |
| LLM | `LOCAL_MISTRAL_MODEL` | `mistral-small3.2` | Alternative: `LLM_PROVIDER=ollama` + `OLLAMA_MODEL=gemma4:31b` (per `export`/`.env`, Paritätsreport beachten) |
| LLM | `OLLAMA_NUM_CTX` | 32768 | bei VRAM-Engpass 16384 |
| LLM | `OLLAMA_MAX_CONCURRENT_REQUESTS` | 1–2 | nur erhöhen, wenn `eki_llm_queue_wait_seconds` hoch **und** VRAM frei (M07) |
| LLM | `LLM_PARALLEL_ENABLED`, `*_CONCURRENCY` | `false` / 1 | Stufen siehe README M07; gelten für neue Jobs |
| KB | `OLLAMA_EMBEDDING_MODEL`, `OLLAMA_EMBEDDING_MAX_CHARS` | `mxbai-embed-large`, 1000 | Alternative `bge-m3` + 30000 – nur zusammen setzen und **vor** dem ersten Ingest; späterer Wechsel §5.6 |
| KB | `KB_RETRIEVAL_ENABLED` | `true` erst nach Freigabe der Inhalte | sonst `false` |
| Zustellung | `EPRO_BASE_URL`, `EPRO_WEBHOOK_URL` | Prod-URLs | Webhook opt-in (M08) |
| Zustellung | `CALLBACK_URL_ALLOWED_HOSTS` | `epro.filmakademie.de,staging.epro.filmakademie.de` (Default) | Hosts, auf die ein `callback_url` zeigen darf; API und Worker brauchen denselben Wert (der Worker prüft vor jedem Push erneut). Leer = `callback_url` abgelehnt |
| Zustellung | `TEMPORAL_WORKFLOW_EXECUTION_TIMEOUT` | 36000 | ≥ Verarbeitung + 6 h (M10) |
| Priorität | `TEMPORAL_PRIORITY_LEVELS` | 10 (Default) | muss `matching.priorityLevels` der Temporal-Dynamic-Config entsprechen (§5.11) |
| Buffer | `BUFFER_TTL_SECONDS` | 21600 | Pflichtenheft ≤ 6 h |
| Aufbewahrung | `METADATA_RETENTION_DAYS`, `METADATA_CLEANUP_CRON` | 90, `30 3 * * *` (Defaults) | Metadaten-Löschung (§5.10, `DATENSCHUTZ.md` §4); Cron in UTC, wird beim Start geprüft |
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
   Offene Dead Letters zeitnah bearbeiten und quittieren – nach 90 Tagen löscht
   `eki-metadata-cleanup` sie ohnehin, quittiert oder nicht (§5.10).
3. Alertmanager: keine offenen Alerts. Fehlgeschlagene Jobs: `error_message` in
   `GET /v1/ops/jobs?status=failed` prüfen (§5.4, §8).

### 4.2 Skalierung

Alle Stellschrauben mit Wechselwirkungen und Profilen: [`LASTSTEUERUNG.md`](LASTSTEUERUNG.md).

- **Dokument-Limit** (`MAX_CONCURRENT_DOCUMENTS`, Standard 1): so viele Jobs nutzen gleichzeitig die
  KI, alle weiteren warten als `pending` (§5.12). Erhöhen nur, wenn die Ollama-Instanz parallele
  Anfragen verkraftet (`OLLAMA_NUM_PARALLEL` > 1 und genug VRAM, §4.11), und dann zusammen mit
  `OLLAMA_MAX_CONCURRENT_REQUESTS`. Bei einer GPU bringt 1 den besten Durchsatz: Dokumente werden
  nacheinander fertig statt alle gemeinsam spät.
- Mehr Durchsatz je Job bei LLM-Wartezeit: erst `OLLAMA_MAX_CONCURRENT_REQUESTS` (GPU-abhängig),
  dann `LLM_PARALLEL_ENABLED=true` mit `*_CONCURRENCY=2`, zuletzt zweiter Worker
  (`--scale worker=2`, dafür `container_name` des Dienstes entfernen; beide teilen die Ollama-Instanz,
  der Cap wirkt pro Prozess, das Dokument-Limit gilt über alle Worker).
- API-Replikas: `--scale api=2` hinter Reverse-Proxy; jedes Replikat exportiert eigene Metriken.

### 4.3 API-Key-Verwaltung

API-Keys liegen nur als SHA-256-Hash in `api_keys`; der Klartext wird einmal angezeigt.

| Aufgabe | Vorgehen |
|---|---|
| Anlegen | `docker compose exec api python scripts/create_api_key.py --insert` (interaktiv: User ID, Organisation, Name, Beschreibung, Gültigkeit – Default **365 Tage**). `--admin` erlaubt zusätzlich `/v1/ops/*` und (seit 1.0.1 ausschließlich Admin-Keys) `/v1/kb/*`, also auch `scripts/seed_kb.py`. Ohne `--insert` wird nur ein Key erzeugt, nicht gespeichert |
| Auflisten | psql (§1): `SELECT name, user_id, is_admin, is_active, expires_at, last_used_at, usage_count FROM api_keys ORDER BY created_at;` |
| Sperren | `UPDATE api_keys SET is_active = false WHERE name = '<Name>';` – wirkt sofort (jede Anfrage prüft die DB) |
| Verlängern | `UPDATE api_keys SET expires_at = now() + interval '365 days' WHERE name = '<Name>';` |
| Rotieren | neuen Key **mit derselben User ID** anlegen (Jobs, Reports und Idempotenz-Keys sind an die User ID gebunden – mit anderer ID findet ePro laufende Jobs/Reports nicht mehr), ePro umstellen, alten Key sperren |

Abgelaufene oder gesperrte Keys liefern `401 Invalid or expired API key`. Rotationsplan
(z. B. jährlich, vor Ablauf) im Betriebskalender führen.

Automatische Löschung (`eki-metadata-cleanup`, §5.10): Ein **gesperrter** Key verschwindet aus
`api_keys`, sobald seine letzte Nutzung (bzw. Anlage, falls nie genutzt) länger als
`METADATA_RETENTION_DAYS` (90 Tage) zurückliegt – der Zeitpunkt der Sperrung wird nicht
gespeichert; ein seit über 90 Tagen ungenutzter Key wird also schon in der Nacht nach der Sperrung
gelöscht. Ein **abgelaufener** Key wird 90 Tage nach `expires_at` gelöscht (und frühestens
90 Tage nach letzter Nutzung). Aktive, nicht abgelaufene Keys werden nie gelöscht. Wer einen
gesperrten Key später reaktivieren will, muss das innerhalb dieser Frist tun.

### 4.4 Backup & Restore

**Was gesichert wird**

| Daten | Ort | Sicherung |
|---|---|---|
| App-Metadaten, Dead Letters, API-Key-Hashes, KB (`kb_documents`, `kb_embeddings`) | DB `eki_db` | täglich `pg_dump` |
| Temporal-Zustand (laufende Workflows, Schedules, History) | DBs `temporal`, `temporal_visibility` (gleiche PostgreSQL-Instanz) | täglich `pg_dump`, zeitgleich mit `eki_db` |
| `API_SECRET_KEY` | `.env.local` / Tresor | Tresor – ohne ihn sind die KB-Originaltexte im Backup (Fernet) **und (seit 1.0.1) die Temporal-Payloads** nicht lesbar |
| `.env.local`, Projekt-`.env` | Host | verschlüsselt, getrennt von den Dumps |
| Redis (SecureBuffer) | Volume `redis_data` (AOF im Prod-Overlay) | **nicht** sichern: nur transiente, verschlüsselte Inhalte ≤ 6 h (Processing-Only). AOF sorgt dafür, dass Inhalte einen Container-Neustart überstehen |
| Ollama-Modelle | externe Ollama-Instanz | nicht sichern, bei Bedarf dort neu pullen |

Dumps enthalten Metadaten (Projekt-/User-IDs) und KB-Chunktexte im Klartext
(`kb_embeddings.chunk_text`): verschlüsselt ablegen und Aufbewahrungsfrist festlegen
(`DATENSCHUTZ.md`). In der Datenbank selbst werden Metadaten nach 90 Tagen gelöscht (§5.10) –
Backups behalten sie bis zu ihrer eigenen Löschung. Die Temporal-Dumps enthalten die
Workflow-Payloads seit 1.0.1 nur verschlüsselt; Historien von Workflows, die vor dem
1.0.1-Update gestartet wurden, bleiben im Klartext, bis die Namespace-Retention sie löscht.

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
konsistente Werte, §2.2). `API_SECRET_KEY` muss derselbe sein wie zur Sicherungszeit – sonst
sind die KB-Originale nicht lesbar, wiederhergestellte offene Workflows nicht dekodierbar
(wiederholte Workflow-Task-Fehler, `PayloadDecryptionError` im Worker-Log, §8.1) und die
Temporal-Historien nicht einsehbar. Jobs, die zum Sicherungszeitpunkt liefen, scheitern nach dem
Restore (ihre Redis-Inhalte fehlen) – `/v1/ops/jobs` prüfen und ePro neu anstoßen lassen. Restore
regelmäßig auf einem Testsystem üben.

### 4.5 Updates / Rollback

Allgemeiner Ablauf (Beispiel: Patch-Release `v1.0.2`). **Für das Update von 1.0.0 auf 1.0.1
gilt zusätzlich die Checkliste in §4.10.**

```bash
# 1. Release Notes/CHANGELOG lesen: Ändert das Release Workflow-Code (workflows/security_check.py,
#    workflows/maintenance.py)? Dann vorher drainen (§5.8). Release-spezifische Schritte beachten.
git fetch --tags && git checkout v1.0.2                  # Compose-Dateien, Dynamic Config, Doku, Skripte
sed -i.bak 's/^EKI_IMAGE_TAG=.*/EKI_IMAGE_TAG=1.0.2/' .env && rm .env.bak
docker compose pull api worker
docker compose up -d --no-build api worker               # API und Worker immer gemeinsam
docker compose exec api alembic upgrade head
docker compose exec api alembic current
curl -s localhost:8000/health                            # neue Version
```

Seit dem `!reset` im Prod-Overlay ersetzt dieser Ablauf Code **und** Migrationen
vollständig durch das Image (vorher überlagerten Quellcode-Mounts aus dem Checkout das
Image). Aus dem Checkout kommen weiterhin die Compose-Dateien und die Temporal-Dynamic-Config
(`docker/temporal/dynamicconfig/`, in den `temporal`-Container gemountet). `.env.example` des
neuen Releases mit der eigenen `.env.local` vergleichen (`diff .env.example .env.local`), neue
Variablen übernehmen.

**API und Worker gemeinsam aktualisieren:** Seit 1.0.1 verschlüsseln beide die Temporal-Payloads
mit einem aus `API_SECRET_KEY` abgeleiteten Schlüssel. Ein Worker älter als 1.0.1 kann Workflows,
die eine 1.0.1-API gestartet hat, nicht lesen (Worker-Log `Unknown payload encoding
binary/encrypted`, die Jobs bleiben `pending`, bis der neue Worker läuft). Beide brauchen
denselben `API_SECRET_KEY` (§5.7, §8.1).

**Rollback:** `EKI_IMAGE_TAG` auf die vorige Version, `docker compose up -d --no-build api worker`.
Migrationen sind bislang additiv bzw. erweiternd; das ältere Image läuft daher auf dem
neueren Schema. Ist ein Downgrade nötig, **vor** dem Zurückschalten mit dem *neuen* Image
ausführen – das alte Image kennt die neuere Revision nicht
(`Can't locate revision identified by …`):

```bash
docker compose exec api alembic downgrade <revision-des-alten-releases>   # vorher Backup (§4.4)
```

Laufende Workflows überstehen Worker-Neustarts (Temporal-Replay), solange der
Workflow-Code unverändert bleibt (§5.8). Rollback von 1.0.1 auf 1.0.0: siehe §4.10, Abschnitt
„Rollback auf 1.0.0".

### 4.6 Datenbank-Migrationen

Aktueller Stand (head): **`c4e6a8b0d2f1`** (Release 1.0.1). Kette:

| Revision | Datei | Inhalt |
|---|---|---|
| `b7ed8ab1d224` | `20260130_0912_b7ed8ab1d224_add_security_models_and_authorization.py` | API-Keys, Job-/Report-Metadaten, Audit-Log |
| `c3a5f8d12e01` | `20260208_1158_m05_delivery_idempotency.py` | M05: Zustellmodus, Idempotenz, Report-Referenz |
| `d4b7e9f23a01` | `20260324_1400_add_script_id_to_job_metadata.py` | `script_id` für ePro |
| `e8f1c2d3a401` | `20260521_m06_knowledge_base.py` | M06: pgvector, `kb_documents`, `kb_embeddings` (`vector(1024)`) |
| `f1a2b3c4d5e6` | `20260916_m10_delivery_hardening.py` | M10: Zustell-Bookkeeping, Dead Letters, Admin-Keys, Idempotenz je User |
| `a7c9e1f3b5d2` | `20261015_m13_widen_job_status.py` | `job_metadata.status` VARCHAR(9) → VARCHAR(20) (`DELIVERING` passte nicht) |
| `b8d0f2a4c6e3` | `20261015_m13b_kb_tenant_dedupe.py` | KB-Dedupe je Mandant: Unique `(tenant_id, content_hash)` statt global eindeutigem `content_hash`. Downgrade bricht ab, wenn ein Hash bei mehreren Mandanten vorkommt |
| `c4e6a8b0d2f1` | `20261016_metadata_retention_indexes.py` | 1.0.1: Indizes `ix_job_metadata_created_at`, `ix_report_metadata_created_at`, `ix_delivery_dead_letters_created_at`, `ix_audit_logs_timestamp` für den Metadaten-Cleanup (§5.10). Rein additiv (`CREATE INDEX`, kurze Sperre); Downgrade entfernt nur die Indizes |

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

Beide Lock-Dateien gemeinsam committen. Seit 1.0.1 enthält `pyproject.toml` keine
ungenutzten Pakete mehr (entfernt u. a. `python-jose`, `PyJWT`, `passlib`, `bcrypt`, `aiohttp`;
`requirements.lock` 99 → 78 Pakete); `requests` ist direkt deklariert, weil
`scripts/seed_kb.py` und die Stack-Runner es brauchen.

### 4.8 Embedding-Modell (Knowledge Base)

API (Ingest) und Worker (Retrieval) müssen dasselbe Embedding-Modell nutzen; beide lesen
`OLLAMA_EMBEDDING_MODEL` aus `.env.local` und rufen dafür die externe Ollama-Instanz auf (das
Modell muss dort gepullt sein). Standard ist `mxbai-embed-large`
(Code-Default, `.env.example`) mit `OLLAMA_EMBEDDING_MAX_CHARS=1000`. `bge-m3` ist die
dokumentierte Alternative für längere Chunks (`OLLAMA_EMBEDDING_MAX_CHARS=30000`).
Beide liefern 1024 Dimensionen, wie die Spalte `vector(1024)` verlangt – andere Modelle
nur mit gleicher Dimension. Vektoren verschiedener Modelle sind nicht vergleichbar:
Ein Wechsel nach dem ersten Ingest erfordert Leeren und Neubefüllen der KB (§5.6).
Mit `LLM_PROVIDER=mistral_cloud` ist die KB nicht nutzbar (kein `embed()`).

### 4.9 Redis-Version und Support

`docker-compose.yml` und die CI pinnen **`redis:7.2-alpine`**. Die 7.2-Linie ist die letzte
Redis-Version unter BSD-3-Clause; ab 7.4 steht Redis unter RSALv2/SSPLv1 (source-available, keine
OSI-Lizenz), Redis 8.x bietet zusätzlich AGPLv3 an. Bis 1.0.0 stand im Compose `redis:7-alpine`,
das bereits auf **7.4** zeigte – bestehende Installationen liefen also mit Redis 7.4 (Folgen für das
Update: §4.10). Die eKI nutzt nur Grundbefehle (`SETEX`, `GET`, `DEL`, `SCAN`, `INCR`, `EXPIRE`,
Pipelines); die Test-Suite läuft auch gegen Redis 7.0.

**Support-Risiko.** Laut `SECURITY.md` des Redis-Projekts
(<https://github.com/redis/redis/security/policy>, Stand 16.10.2026) werden Sicherheitskorrekturen
„generally“ nur in eine vorherige Major-Version zurückportiert; 7.2.x ist als „support extended
till 7.4 end of support“ geführt. **Ein Enddatum ist weder für 7.4 noch für 7.2 veröffentlicht.**
Sobald 7.4 aus dem Support fällt, endet auch der Support für 7.2 – danach gibt es keine
Sicherheitskorrekturen mehr, und Redis muss ersetzt werden. Die Seite bei jedem Release und
mindestens halbjährlich prüfen (Betriebskalender).

Ersatzoptionen:

| Option | Lizenz | Hinweis |
|---|---|---|
| **Valkey** (Linux-Foundation-Fork von Redis 7.2, z. B. Image `valkey/valkey:<version>-alpine`) | BSD-3-Clause | protokoll- und befehlskompatibel zu Redis 7.2; naheliegender Ersatz |
| **Redis ≥ 8** | Wahl zwischen RSALv2, SSPLv1 und AGPLv3 | interne Nutzung unproblematisch; Lizenzwahl vorher rechtlich prüfen |

Vorgehen bei einem Wechsel:

1. Test-Suite gegen die Zielversion laufen lassen: Container des Ziel-Images starten und
   `TEST_REDIS_URL` darauf zeigen lassen, PostgreSQL-Modus wie in `TESTING_GUIDE.md` §3.
2. Image in `docker-compose.yml` (Dienst `redis`) und in `.github/workflows/ci.yml` ändern (Dienst
   des Jobs `test` und die Image-Prüfung im Job `observability-validation`).
3. Persistenzformat beachten: Kann die Zielversion die vorhandenen RDB/AOF-Dateien nicht lesen
   (z. B. bei einem Versionsrückschritt wie 7.4 → 7.2), startet Redis nicht. Dann wie in §4.10
   (Schritte 6, 9) drainen und das Volume `redis_data` leeren – der Inhalt ist ohnehin nur
   transient (≤ 6 h).

### 4.10 Upgrade 1.0.0 → 1.0.1

Einmalige Checkliste zusätzlich zum allgemeinen Ablauf (§4.5). Was sich für den Betrieb ändert:

| Änderung in 1.0.1 | Folge für das Update |
|---|---|
| Redis auf 7.2 gepinnt (vorher `redis:7-alpine` = 7.4) | Redis 7.4 schreibt RDB-Format 12, Redis 7.2 liest höchstens Format 11: Ein von 7.4 geschriebenes `redis_data`-Volume (im Prod-Overlay AOF mit RDB-Präambel) lädt 7.2 nicht, Redis startet nicht (`Can't handle RDB format version 12`). Volume nach dem Drain leeren. Neuinstallationen sind nicht betroffen |
| Ollama nicht mehr im Stack | `OLLAMA_BASE_URL` (ggf. `LOCAL_MISTRAL_BASE_URL`) auf die externe Instanz setzen; der bisherige Wert `http://ollama:11434` zeigt auf einen Dienst, den es nicht mehr gibt |
| Temporal-Payload-Verschlüsselung | API und Worker gemeinsam aktualisieren; ein Rollback auf 1.0.0 nur nach Drain |
| Priorität (`priority`) | neue Temporal-Dynamic-Config → `temporal` neu starten |
| Migration `c4e6a8b0d2f1` | vier Indizes für den Metadaten-Cleanup |
| Neuer Schedule `eki-metadata-cleanup` | löscht in der ersten Nacht (03:30 UTC) alle Metadaten, die älter als 90 Tage sind – auch **unquittierte** Dead Letters |
| `/v1/kb/*` nur mit Admin-Key | `scripts/seed_kb.py` und KB-Werkzeuge brauchen einen Admin-Key |
| `POST /v1/security/check` entfernt | antwortet 404; ePro nutzt laut Integration nur `check:async` |
| Cron-Werte werden beim Start geprüft | ein ungültiger `KB_CLEANUP_CRON`/`METADATA_CLEANUP_CRON` verhindert jetzt den Start von API und Worker |

**A. Vorbereitung (vor dem Wartungsfenster)**

1. `CHANGELOG.md` (1.0.1) lesen; ePro-Team informieren: Sync-Endpunkt entfernt (404),
   `callback_url` und `priority` wirken jetzt, `/v1/kb/*` nur noch mit Admin-Key
   (`EPRO_INTEGRATION_GUIDE.md`).
2. `docker compose version` → mindestens 2.24.
3. Externe Ollama-Instanz bereitstellen (§4.11): Modelle dort pullen
   (`ollama pull mistral-small3.2` und **dasselbe** Embedding-Modell wie bisher, Standard
   `ollama pull mxbai-embed-large` – ein anderes Modell hieße KB neu aufbauen, §5.6), Port 11434
   vom eKI-Host aus freigeben, Version notieren: `curl -s http://<ollama-host>:11434/api/version`.
4. Admin-Key für die KB-Werkzeuge vorhanden? psql (§1):
   `SELECT name, user_id FROM api_keys WHERE is_admin AND is_active;` – sonst nach dem Update mit
   `create_api_key.py --insert --admin` anlegen.
5. Erste Metadaten-Löschung entscheiden: Offene Dead Letters bearbeiten und quittieren. Werden
   ältere Metadaten noch gebraucht (z. B. Auswertungen), vorher exportieren oder bis dahin
   `METADATA_CLEANUP_ENABLED=false` bzw. ein größeres `METADATA_RETENTION_DAYS` setzen (§5.10).

**B. Wartungsfenster: drainen und sichern**

6. Neue Jobs anhalten und **drainen**, bis kein Prüf-Workflow mehr offen ist (§5.8; Pull-Workflows
   bleiben bis zu 6 h nach `completed` offen). Pflicht, weil Schritt 9 den Redis-Inhalt löscht
   (laufende Jobs verlören ihre Buffer-Inhalte) – und Voraussetzung dafür, dass ein Rollback auf
   1.0.0 ohne hängende Workflows möglich bleibt.
7. Backup (§4.4): `pg_dump` von `eki_db`, `temporal`, `temporal_visibility`; `.env.local` und `.env`
   sichern; `API_SECRET_KEY` liegt im Tresor.

**C. Update**

8. API und Worker stoppen: `docker compose stop api worker`.
9. Redis-Volume leeren (der 7.2-Container könnte die 7.4-Daten nicht laden):

   ```bash
   docker compose stop redis && docker compose rm -f redis
   docker volume ls | grep redis_data                 # Name <Projekt>_redis_data, z. B. eki_redis_data
   docker volume rm <Projekt>_redis_data
   ```

10. Release auschecken (Compose-Dateien mit Redis 7.2 und ohne Ollama, Dynamic Config):
    `git fetch --tags && git checkout v1.0.1`.
11. `.env.local` anpassen (`diff .env.example .env.local`):
    - `OLLAMA_BASE_URL=http://<ollama-host>:11434` (externe Instanz); `LOCAL_MISTRAL_BASE_URL` nur
      setzen, wenn `local_mistral` einen anderen Endpoint nutzen soll.
    - optional (ohne Eintrag gelten die Defaults): `CALLBACK_URL_ALLOWED_HOSTS`,
      `TEMPORAL_PRIORITY_LEVELS=10`, `METADATA_RETENTION_DAYS=90`, `METADATA_CLEANUP_ENABLED=true`,
      `METADATA_CLEANUP_CRON="30 3 * * *"` (`CONFIGURATION.md`).
    - `KB_CLEANUP_CRON` prüfen: genau 5 Felder, in Anführungszeichen (§5.10).
    - API und Worker müssen denselben `API_SECRET_KEY` sehen (bei `*_FILE`-Mounts dieselbe Datei).
12. Projekt-`.env`: `EKI_IMAGE_TAG=1.0.1` setzen, dann `docker compose config -q`.
13. Images ziehen: `docker compose pull api worker redis`.
14. Infrastruktur starten und den alten Ollama-Container entfernen:

    ```bash
    docker compose up -d --no-build --remove-orphans postgres redis temporal temporal-ui
    docker compose exec redis redis-cli INFO server | grep redis_version   # 7.2.x
    ```

    `--remove-orphans` entfernt den Container `eki-ollama`, den es im Compose nicht mehr gibt. Das
    Volume `<Projekt>_ollama_data` (Modelle) bleibt bestehen; nicht mehr benötigt:
    `docker volume rm <Projekt>_ollama_data`.
15. Temporal-Dynamic-Config für die Priorität laden (`matching.useNewMatcher: true`,
    `matching.priorityLevels: 10`). Die Datei ist in den Container gemountet; ein Neustart wendet sie
    sofort an (wartende Aufgaben bleiben erhalten), `restart` genügt, weil sich keine
    Umgebungsvariablen ändern:

    ```bash
    docker compose restart temporal
    docker compose exec temporal grep -A1 -E "useNewMatcher|priorityLevels" /etc/temporal/config/dynamicconfig/eki.yaml
    docker compose ps temporal                                             # healthy abwarten
    ```

    Bei einem extern betriebenen Temporal-Cluster muss dessen Betreiber beide Schlüssel setzen,
    sonst wird `priority` gespeichert, aber ignoriert (§5.11).
16. API und Worker **gemeinsam** starten: `docker compose up -d --no-build api worker`.
17. Migration: `docker compose exec api alembic upgrade head`, danach
    `docker compose exec api alembic current` → `c4e6a8b0d2f1 (head)`.

**D. Prüfen und freigeben**

18. Rauchtest:

    ```bash
    curl -s localhost:8000/health                                    # version=1.0.1
    curl -s -H "Authorization: Bearer eki_..." localhost:8000/ready  # database/redis/temporal = true
    docker compose exec worker python -c "import os,httpx;print(httpx.get((os.environ.get('LOCAL_MISTRAL_BASE_URL') or os.environ['OLLAMA_BASE_URL'])+'/api/tags',timeout=10).status_code)"   # 200
    docker compose logs worker | grep -E "payload encryption active|Schedule eki-"
    #   Temporal payload encryption active: key id eki-v1-<16 hex>
    #   Schedule eki-kb-cleanup updated: ...        (einmalig nach dem Update, erwartet)
    #   Schedule eki-metadata-cleanup created: cron='30 3 * * *'
    docker compose exec api python -c "from api.config import get_settings; from core.temporal_codec import EncryptionCodec; print(EncryptionCodec(get_settings().api_secret_key).key_id)"
    #   -> dieselbe Key-ID wie im Worker-Log
    docker compose exec temporal temporal schedule describe --schedule-id eki-metadata-cleanup --address temporal:7233
    docker compose exec -e EKI_API_KEY=eki_<admin> api python scripts/seed_kb.py --status   # KB mit Admin-Key
    docker compose exec -e EKI_API_KEY=eki_... api \
      python scripts/smoke_prod.py --worker-metrics http://worker:9090   # RESULT: PASSED
    ```

19. Jobs wieder freigeben (ePro/Reverse-Proxy) und die ersten Prüfungen beobachten.
20. Abnahmetests AT2–AT7 auf diesem Stand wiederholen (`UAT/UAT_TESTPLAN.md`).
21. Nacharbeiten: veröffentlichte GHCR-Images ≤ 1.0.0 löschen bzw. den Zugriff beschränken – sie
    enthalten noch echte Drehbücher (`DATENSCHUTZ.md` §8); alte lokale Images entfernen (§5.5).

**Rollback auf 1.0.0**

- Workflows, die unter 1.0.1 gestartet oder fortgesetzt wurden, enthalten verschlüsselte Ereignisse,
  die ein 1.0.0-Worker nicht lesen kann: vorher drainen (§5.8) oder diese Workflows beenden, sonst
  hängen sie bis zum Timeout.
- Schedule `eki-metadata-cleanup` pausieren (der 1.0.0-Worker kennt `MetadataCleanupWorkflow` nicht;
  sonst wartet der nächtliche Lauf bis zu seinem Timeout von 1 h in der Task-Queue):
  `docker compose exec temporal temporal schedule toggle --schedule-id eki-metadata-cleanup --pause --reason rollback --address temporal:7233`
- Nur `EKI_IMAGE_TAG=1.0.0` setzen und `docker compose up -d --no-build api worker`; die
  Compose-Dateien und die Dynamic Config von 1.0.1 bleiben (Redis 7.2, externes Ollama und der neue
  Matcher funktionieren auch mit 1.0.0). **Nicht** `v1.0.0` auschecken: dessen Compose-Datei
  startet wieder einen Ollama-Container mit `ollama/ollama:latest` und Redis 7.4.
- Migration `c4e6a8b0d2f1` darf bleiben (1.0.0 ignoriert die Indizes). Alembic-Befehle mit dem
  1.0.0-Image scheitern dann mit `Can't locate revision identified by 'c4e6a8b0d2f1'`; wer sie
  braucht, führt vorher mit dem 1.0.1-Image `alembic downgrade b8d0f2a4c6e3` aus.
- `API_SECRET_KEY` nicht ändern.

### 4.11 Externe Ollama-Instanz

Seit 1.0.1 enthält der Compose-Stack kein Ollama. Inferenz (`local_mistral`, `ollama`) und
KB-Embeddings laufen auf einer externen Instanz:

- **Konfiguration:** `OLLAMA_BASE_URL=http://<ollama-host>:11434` in `.env.local` (gilt für beide
  Provider, Inferenz und KB-Embeddings); `LOCAL_MISTRAL_BASE_URL` überschreibt den Endpoint für
  `local_mistral` (dann auch für dessen Embeddings). API (KB-Ingest, `scripts/test_llm.py`) und
  Worker (Analyse, KB-Retrieval) brauchen Zugriff. Änderung:
  `docker compose up -d --force-recreate api worker`.
- **Keine Startprüfung:** Weder API noch Worker kontaktieren Ollama beim Start, `/ready` prüft es
  nicht. Ist die URL falsch oder die Instanz weg, scheitern Jobs erst beim ersten LLM-Aufruf
  (fail-closed, §5.4). Erreichbarkeit nach jeder Änderung prüfen (Kommando in §4.10, Schritt 18).
- **Einstellungen der Instanz** (wie der frühere Ollama-Dienst im Prod-Overlay):
  `OLLAMA_NUM_PARALLEL=1`, `OLLAMA_KEEP_ALIVE=30m`, GPU ≥ 24 GB VRAM, `OLLAMA_DEBUG` nicht setzen.
  Diese Variablen werden **auf dem Ollama-Host** gesetzt (systemd: `systemctl edit ollama.service`,
  Abschnitt `[Service]`, `Environment="OLLAMA_NUM_PARALLEL=1"`; Docker: `-e`/`environment:`), nicht in
  der `.env.local` der eKI; prüfen mit `journalctl -u ollama | grep "server config"` und `ollama ps`
  (Spalte PROCESSOR: `100% GPU`). Jeder Parallel-Platz belegt ein eigenes Kontextfenster
  (`OLLAMA_NUM_CTX`, 32768) im VRAM; reicht der Speicher nicht, rechnet Ollama teilweise auf der CPU.
  `OLLAMA_MAX_CONCURRENT_REQUESTS` der eKI begrenzt pro API-/Worker-Prozess, das Dokument-Limit
  (§5.12) die Zahl gleichzeitig verarbeiteter Jobs; teilen sich weitere eKI-Instanzen (Stage,
  Entwicklung) oder andere Anwendungen die Instanz, die Summe beachten.
- **Netz:** Die Ollama-API hat keine Anmeldung. Port 11434 nur für die eKI-Hosts freigeben, nie
  öffentlich. Die Instanz verarbeitet Drehbuchszenen und gehört damit zur Verarbeitungsumgebung
  (`DATENSCHUTZ.md` §5). Nur der Ollama-Host braucht Zugang zur Ollama-Registry (Modell-Pull).
- **Version abgleichen:** Die eKI ist nicht gegen eine bestimmte Ollama-Version getestet (die Tests
  ersetzen Ollama an der HTTP-Grenze). Version der Produktionsinstanz dokumentieren
  (`curl -s http://<ollama-host>:11434/api/version`) und den Pin im Overlay
  `docker-compose.ollama.yml` (derzeit `ollama/ollama:0.33.3`, Release vom 03.09.2026) auf dieselbe
  Version setzen, damit Entwicklung und UAT mit der Produktionsversion laufen. Nach jedem
  Ollama-Update der Produktionsinstanz den Paritätslauf wiederholen (§7).
- **Ollama auf demselben Host, aber außerhalb von Compose:**
  `OLLAMA_BASE_URL=http://host.docker.internal:11434` und für `api`/`worker`
  `extra_hosts: ["host.docker.internal:host-gateway"]` in einer eigenen Compose-Datei, die per
  `COMPOSE_FILE` mitgeladen wird; Ollama muss dann auf einer für Container erreichbaren Adresse
  lauschen (Firewall beachten).
- **Overlay `docker-compose.ollama.yml`** (nur Entwicklung/Offline, nicht Produktion): Dienst
  `ollama` (`eki-ollama`, Volume `ollama_data`, `OLLAMA_KEEP_ALIVE=30m`, `OLLAMA_NUM_PARALLEL=1`,
  NVIDIA-GPU-Reservierung, Healthcheck `ollama list`, Log-Rotation 20 MB × 5) und
  `OLLAMA_BASE_URL`/`LOCAL_MISTRAL_BASE_URL=http://ollama:11434` für `api`/`worker` (schlägt
  `.env.local`). Ohne NVIDIA-GPU bzw. NVIDIA Container Toolkit den `deploy:`-Block des Dienstes
  löschen, sonst startet der Container nicht. Aufruf: README „Schnellstart“.

---

## 5. Runbooks

Kurzübersicht (Alert → erste Maßnahme):

| Symptom | Erste Maßnahme | Details |
|---|---|---|
| `EkiApiDown` | `docker compose ps`, `docker compose logs api --tail 200` | Startfehler meist Konfig-Validator oder `*_FILE` (§8) |
| `EkiWorkerDown` | `docker compose logs worker --tail 200` | Temporal erreichbar? Namespace? (§5.3, §8) |
| `EkiLlmErrorRateHigh`, Jobs `failed` mit LLM-Fehler | §5.4 | Ollama-Instanz erreichbar? Modell gepullt? VRAM? |
| Jobs bleiben lange `pending`, Worker-Log `Document limit reached, job waits` | Warteschlange ansehen | §5.12 |
| Jobs hängen in `running` | Temporal UI (§8.2): Pending Activities; Worker-Logs nach `job_id` filtern | Korrelation via `request_id`/`job_id` in jeder Logzeile |
| Jobs bleiben `pending`, Temporal UI zeigt wiederholt *Workflow Task Failed* | Worker-Log nach `PayloadDecryptionError` durchsuchen | API und Worker mit unterschiedlichem `API_SECRET_KEY` (§8.1) |
| Dead Letter `invalid_callback_url` | `CALLBACK_URL_ALLOWED_HOSTS` und DNS des Callback-Hosts prüfen | [`M10_FAILOVER_RUNBOOK.md`](M10_FAILOVER_RUNBOOK.md) §2.9 |
| Worker-Log `Metadata retention cleanup: abandoned jobs NOT deleted …` | `METADATA_RETENTION_DAYS` erhöhen | §5.10 |
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
- **Diagnose** (Ollama läuft extern, §4.11):
  ```bash
  # Erreichbarkeit und Modelle aus Sicht des Workers (Statuscode 200)
  docker compose exec worker python -c "import os,httpx;print(httpx.get((os.environ.get('LOCAL_MISTRAL_BASE_URL') or os.environ['OLLAMA_BASE_URL'])+'/api/tags',timeout=10).status_code)"
  docker compose exec api python scripts/test_llm.py     # Health-Check + Testgenerierung mit der aktuellen Konfiguration
  # auf dem Ollama-Host:
  ollama list                                            # Modelle gepullt?
  nvidia-smi                                             # VRAM/GPU
  ```
  Die Ollama-Logs liegen beim Betreiber der Instanz (z. B. `journalctl -u ollama` bei einer
  systemd-Installation).
- **Maßnahmen:** fehlendes Modell auf dem Ollama-Host pullen (`ollama pull <modell>`), Instanz
  neu starten bzw. Netzweg/Firewall prüfen; bei falscher URL `OLLAMA_BASE_URL` korrigieren
  (`up -d --force-recreate api worker`). Bei VRAM-Engpass `OLLAMA_NUM_CTX` bzw.
  `OLLAMA_MAX_CONCURRENT_REQUESTS` senken (`up -d --force-recreate api worker`). Danach
  betroffene Jobs (`GET /v1/ops/jobs?status=failed`) von ePro neu anstoßen lassen.

### 5.5 Platte voll

```bash
df -h /var/lib/docker
docker system df -v                     # Images, Volumes, Build-Cache
```

| Verbraucher | Maßnahme |
|---|---|
| alte Images | `docker image ls --filter 'reference=ghcr.io/*/eki-*'`, nicht mehr benötigte Tags mit `docker image rm` entfernen (vorige Version für Rollback behalten); `docker builder prune` |
| Container-Logs (json-file) | Prod-Overlay: Rotation nach Größe, 20 MB × 5 Dateien je Container (≈ 100 MB). Entwicklungs-Stack ohne Rotation: in `/etc/docker/daemon.json` `"log-opts": {"max-size": "50m", "max-file": "5"}` setzen, Docker neu starten, Container neu erzeugen |
| Ollama-Modelle | liegen auf der externen Instanz (dort `ollama rm <modell>`); mit dem Entwicklungs-Overlay im Volume `ollama_data` |
| `prometheus_data` | Retention ist 30 Tage (`docker-compose.observability.yml`) |
| `postgres_data` | Metadaten werden nach `METADATA_RETENTION_DAYS` (90 Tage) gelöscht (§5.10); KB-Dokumente nach ihrer TTL. Freier Platz kehrt erst nach `VACUUM` an das Dateisystem zurück (Autovacuum gibt ihn zur Wiederverwendung frei) |
| `redis_data` (AOF) | `docker compose exec redis redis-cli BGREWRITEAOF` |

Folgen: PostgreSQL stoppt Schreibzugriffe (→ 503, §5.1), Redis verweigert Schreibzugriffe
bei AOF-Fehlern (→ neue Jobs 503, §5.2).

### 5.6 Embedding-Modell wechseln (KB leeren und neu befüllen)

Nötig beim Wechsel von `OLLAMA_EMBEDDING_MODEL` und nach einer `API_SECRET_KEY`-Rotation (§5.7).

```bash
# 1. Retrieval während des Umbaus aus: KB_RETRIEVAL_ENABLED=false in .env.local
# 2. Neues Modell auf der externen Ollama-Instanz laden (auf dem Ollama-Host) und konfigurieren (beide Werte!)
ollama pull bge-m3
#    .env.local: OLLAMA_EMBEDDING_MODEL=bge-m3, OLLAMA_EMBEDDING_MAX_CHARS=30000
docker compose up -d --force-recreate api worker
# 3. KB leeren (Embeddings werden per Cascade mitgelöscht), in psql (§1):
#    DELETE FROM kb_documents;
# 4. Neu befüllen – auf dem Host (Python 3 + `pip install requests`) aus den Quelldokumenten:
export EKI_API_KEY=eki_<admin>                        # Admin-Key; EKI_API_URL default http://localhost:8000
python3 scripts/seed_kb.py --reseed                   # alle Dateien aus config/kb_seed/real/
python3 scripts/seed_kb.py --add <datei> --title "<Titel>" --tags <tags>   # weitere Dokumente
python3 scripts/seed_kb.py --status
# 5. Retrieval prüfen, dann KB_RETRIEVAL_ENABLED=true und up -d --force-recreate api worker
```

Die Quelldokumente müssen außerhalb der eKI vorliegen (die KB speichert Originale nur
verschlüsselt und bietet keinen Export). `config/kb_seed/real/` ist git-ignoriert und per
`.dockerignore` vom Image-Build ausgeschlossen; echte Dokumente dort nicht committen.

### 5.7 `API_SECRET_KEY` rotieren

Aus dem Schlüssel werden abgeleitet: die Fernet-Schlüssel für den SecureBuffer und die
KB-Originaltexte sowie (seit 1.0.1) der AES-256-GCM-Schlüssel des Temporal-Payload-Codecs
(HKDF-SHA256, Label `eki-temporal-codec-v1`). Folgen eines Wechsels: Inhalte laufender Jobs und
nicht abgeholte Pull-Reports sind nicht mehr entschlüsselbar (Jobs scheitern), KB-Originale in DB
und Backups ebenfalls nicht (Retrieval nutzt die separat gespeicherten Chunks und läuft weiter).
**Offene Workflows** lassen sich nicht mehr dekodieren: Ihre Workflow-Tasks scheitern mit
`PayloadDecryptionError` (alte und neue Key-ID im Worker-Log) und werden bis
`TEMPORAL_WORKFLOW_EXECUTION_TIMEOUT` wiederholt. Historien abgeschlossener Workflows (bis zum
Ablauf der Namespace-Retention) sind danach nicht mehr einsehbar. Die Schedules
(`eki-kb-cleanup`, `eki-metadata-cleanup`) tragen keine Payloads und brauchen nichts.

1. Drainen, bis kein Prüf-Workflow mehr offen ist (§5.8) – **Pflicht**, nicht nur wegen der
   Buffer-Inhalte, sondern weil offene Workflows sonst hängen bleiben.
2. Neuen Schlüssel erzeugen: `python3 -c "import secrets; print(secrets.token_urlsafe(32))"`,
   in `.env.local` (bzw. Secret-Datei) eintragen – derselbe Wert für API und Worker.
3. `docker compose up -d --force-recreate api worker`; Key-IDs von API und Worker vergleichen (§8.1).
4. KB leeren und neu befüllen (§5.6 ab Schritt 3), damit die Originale mit dem neuen
   Schlüssel verschlüsselt sind.
5. Alten Schlüssel im Tresor behalten, solange Backups mit ihm existieren (KB-Originale in
   `eki_db`-Dumps und Temporal-Payloads in den `temporal`-Dumps).

### 5.8 Laufende Workflows drainen (vor Workflow-Code-Änderungen)

Temporal spielt offene Workflows nach einem Worker-Neustart mit dem **neuen** Code nach.
Ändert ein Release `workflows/security_check.py` oder `workflows/maintenance.py`, schlagen
offene Workflows mit Nichtdeterminismus-Fehlern fehl. Reine Activity-Änderungen
(`workflows/activities.py`) sind unkritisch, solange Activity-Namen und Ein-/Ausgabeformate
kompatibel bleiben. Prüfen: `git diff --stat v<alt> v<neu> -- workflows/`. Drainen ist außerdem
nötig vor einer `API_SECRET_KEY`-Rotation (§5.7), vor einem Rollback von 1.0.1 auf 1.0.0 und vor
dem Leeren des Redis-Volumes (§4.10). Das Update von 1.0.0 auf 1.0.1 selbst bräuchte wegen des
Workflow-Codes keinen Drain (die Änderungen lassen die Befehlsfolge unverändert; 13
aufgezeichnete 1.0.0-Historien spielen mit dem 1.0.1-Code fehlerfrei ab, auch mit dem neuen
Payload-Codec) – wohl aber wegen des Redis-Volumes.

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

### 5.10 Metadaten-Aufbewahrung (`eki-metadata-cleanup`)

**Was läuft:** Der Temporal-Schedule `eki-metadata-cleanup` (Workflow `MetadataCleanupWorkflow`,
Activity `metadata_cleanup_expired`) läuft standardmäßig täglich um 03:30 UTC
(`METADATA_CLEANUP_CRON`; der KB-Cleanup bleibt bei 03:00 UTC). Stichtag = jetzt minus
`METADATA_RETENTION_DAYS` (Standard 90). Gelöscht wird eine Zeile nur, wenn **alle** ihre
Zeitstempel vor dem Stichtag liegen:

| Tabelle | gelöscht, wenn | nie gelöscht |
|---|---|---|
| `report_metadata` | `created_at` und ggf. `retrieved_at` vor dem Stichtag | solange der zugehörige Job noch läuft |
| `delivery_dead_letters` | `created_at` und ggf. `acknowledged_at` vor dem Stichtag – **quittiert oder nicht** | – |
| `job_metadata` | Endzustand (`completed`, `failed`, `cancelled`), `created_at` und `updated_at` vor dem Stichtag | – (Jobs mit Änderung nach dem Stichtag bleiben vorerst) |
| `job_metadata` (verwaist) | noch `pending`/`running`/`delivering`, aber `created_at` und `updated_at` vor dem Stichtag | nur wenn der Stichtag älter ist als `TEMPORAL_WORKFLOW_EXECUTION_TIMEOUT` + `BUFFER_TTL_SECONDS` (Standard 16 h) |
| `api_keys` | gesperrt (`is_active=false`) oder vor dem Stichtag abgelaufen, **und** `created_at` sowie ggf. `last_used_at` vor dem Stichtag | aktive, nicht abgelaufene Keys |
| `audit_logs` | `timestamp` vor dem Stichtag | – |

Gelöscht wird in Stapeln zu 1000 Zeilen je Transaktion; das `DELETE` prüft die Bedingungen
erneut, sodass eine zwischenzeitlich geänderte Zeile (z. B. ein reaktivierter Key) bleibt.
Activity-Timeout 15 min, 3 Versuche; Überlappung wird übersprungen (`SKIP`). Die Temporal-Historie
und das Log enthalten nur Zähler und den Stichtag, keine IDs. Datenschutzsicht: `DATENSCHUTZ.md` §4.

**Frist ändern:** `.env.local` → `METADATA_RETENTION_DAYS=<n>` (≥ 1), dann
`docker compose up -d --force-recreate worker` (nur der Worker liest den Wert). Wirkt beim
nächsten Lauf. Eine kürzere Frist löscht beim nächsten Lauf entsprechend mehr – vorher sichern (§4.4).

**Zeitpunkt ändern / abschalten:** `METADATA_CLEANUP_CRON="<Cron>"` bzw.
`METADATA_CLEANUP_ENABLED=false`, dann `docker compose up -d --force-recreate api worker` (beide
prüfen den Cron beim Start). Der Worker gleicht den Schedule bei **jedem** Start an die Einstellungen
an: fehlt er, wird er angelegt; weichen Cron, Task-Queue oder Zielworkflow ab, wird er aktualisiert;
`false` **pausiert** ihn (Notiz `METADATA_CLEANUP_ENABLED=false` in der Temporal UI), `true` setzt
ihn fort. Dasselbe gilt seit 1.0.1 für `KB_CLEANUP_CRON`/`KB_CLEANUP_ENABLED` (früher musste der
Schedule dafür gelöscht werden). Schedules nicht in der Temporal UI pausieren oder ändern – der
nächste Worker-Start setzt sie auf die Einstellungen zurück. Der Worker schreibt den Cron als
`<cron> # eki-cron=<cron>`; die Temporal UI zeigt deshalb den Kommentar `eki-cron=…`.

Cron-Format (beide Schedules, in `.env`-Dateien in Anführungszeichen): genau 5 Felder
`Minute Stunde Tag Monat Wochentag` in **UTC**; je Feld `*`, Zahlen, Bereiche `a-b`, Schritte
`*/n`, `a-b/n`, `a/n`, Listen mit Komma, Monatsnamen `JAN`–`DEC`, Wochentage `SUN`–`SAT`
(Wochentag 0–6, 0 = Sonntag, `7` wird abgelehnt). Nicht erlaubt: Sekunden-/Jahresfelder,
`@daily`, `?`, `L`, `#`, `CRON_TZ=`. Sind Tag **und** Wochentag eingeschränkt, müssen bei Temporal
beide passen (UND, anders als klassisches cron). Ein ungültiger Wert verhindert seit 1.0.1 den Start
von API und Worker (Validierungsfehler mit dem Variablennamen).

**Sofort ausführen** (Temporal-CLI im Container; Produktion alternativ Temporal UI → Schedules →
`eki-metadata-cleanup` → „Trigger“, Zugriff §8.2):

```bash
docker compose exec temporal temporal schedule trigger --schedule-id eki-metadata-cleanup --address temporal:7233
```

Ein Trigger, während noch ein Lauf aktiv ist, wird übersprungen.

**Prüfen:**

```bash
docker compose exec temporal temporal schedule describe --schedule-id eki-metadata-cleanup --address temporal:7233
#   -> Kommentar "eki-cron=30 3 * * *", nicht pausiert, letzte/nächste Läufe
docker compose exec temporal temporal workflow list --address temporal:7233 --query "WorkflowType='MetadataCleanupWorkflow'"
docker compose logs worker | grep "Metadata retention cleanup"
#   -> retention_days=90 cutoff=… deleted report_metadata=… delivery_dead_letters=… job_metadata=…
#      job_metadata_abandoned=… api_keys=… audit_logs=…
docker compose exec api python -c "import httpx;print(httpx.get('http://worker:9090',timeout=10).text)" | grep eki_metadata_cleanup_deleted_total
```

Kontrolle per SQL (psql, §1) – nach einem Lauf `0`. `job_metadata.status` speichert die
Enum-Namen **in Großbuchstaben** (`'COMPLETED'`, `'FAILED'`, `'CANCELLED'` …):

```sql
SELECT count(*) FROM job_metadata
 WHERE status IN ('COMPLETED', 'FAILED', 'CANCELLED')
   AND created_at < now() - interval '90 days' AND updated_at < now() - interval '90 days';
```

**Verwaiste Jobs:** Ein Job, der noch `pending`/`running`/`delivering` zeigt, aber seit der Frist
keine Änderung hatte, kann nicht mehr laufen (ein Workflow lebt höchstens 10 h, plus 6 h
Pull-Fenster). Der Cleanup löscht ihn (Zähler `job_metadata_abandoned`) und warnt im Log:
`Metadata retention cleanup: deleted <n> abandoned job(s) …` – ein Hinweis, dass Workflows ohne
abschließendes DB-Update endeten (Worker-/Temporal-Logs des Zeitraums prüfen). Hängende Jobs
lassen sich schon früher finden:
`SELECT job_id, status, updated_at FROM job_metadata WHERE status IN ('PENDING','RUNNING','DELIVERING') AND updated_at < now() - interval '16 hours';`
(älter als Workflow-Timeout + Pull-Fenster: Workflow ist weg; Temporal bzw. `/v1/ops/jobs` prüfen).
Meldet das Log `Metadata retention cleanup: abandoned jobs NOT deleted -- …`, ist
`METADATA_RETENTION_DAYS` nicht länger als `TEMPORAL_WORKFLOW_EXECUTION_TIMEOUT` +
`BUFFER_TTL_SECONDS` (Fehlkonfiguration): Frist erhöhen oder die Timeouts senken.

**Dead Letters** innerhalb der Frist bearbeiten (`/v1/ops/dead-letters…`, quittieren): Nach
90 Tagen werden sie gelöscht, quittiert oder nicht. Der Zähler `eki_dead_letters_unacknowledged`
(Alarm `EkiDeadLettersPending`) wird nicht vom Cleanup neu berechnet, sondern von den
`/v1/ops/*`-Endpunkten der API und beim Anlegen eines Dead Letters im Worker – nach einer Löschung
also z. B. `GET /v1/ops/summary` aufrufen.

**Fehlersuche:** Keine Läufe → Worker-Log `Metadata cleanup schedule bootstrap failed (non-fatal): …`
(der Worker läuft weiter, der nächste Start versucht es erneut) oder Schedule pausiert
(`METADATA_CLEANUP_ENABLED=false`). Startabbruch mit Hinweis auf `METADATA_CLEANUP_CRON` bzw.
`KB_CLEANUP_CRON` → Cron-Format oben.

### 5.11 Temporal: Namespace-Retention und Priorität

**Namespace-Retention.** Abgeschlossene Workflow-Historien löscht Temporal nach der Retention des
Namespace. Der Compose setzt `DEFAULT_NAMESPACE_RETENTION` nicht; `temporalio/auto-setup` legt den
Namespace `default` deshalb mit **24 h** an – aber nur beim **ersten** Anlegen; eine spätere
Variable ändert einen bestehenden Namespace nicht. Prüfen und ändern:

```bash
docker compose exec temporal temporal operator namespace describe --namespace default --address temporal:7233
docker compose exec temporal temporal operator namespace update --namespace default --retention 72h --address temporal:7233
```

Die Historien sind seit 1.0.1 verschlüsselt; die Retention bleibt trotzdem eine Datenschutz-Einstellung
(`DATENSCHUTZ.md` §3.3) und wird vom Metadaten-Cleanup nicht erfasst.

**Priorität.** Das Request-Feld `priority` (1 = höchste … 10, Default 5) wird als
Temporal-Task-Queue-Priorität übergeben: API-Wert *p* → Temporal-Schlüssel
`((p - 1) * N) // 10 + 1` mit `N = TEMPORAL_PRIORITY_LEVELS` (Default 10, also identisch).
Aufgaben ohne Priorität (die Wartungs-Schedules) erhalten den Standardschlüssel `(N + 1) // 2` = 5.
Voraussetzung ist die Dynamic Config `docker/temporal/dynamicconfig/eki.yaml`:
`matching.useNewMatcher: true` (in Temporal 1.29 standardmäßig aus) und
`matching.priorityLevels: 10` (muss `TEMPORAL_PRIORITY_LEVELS` entsprechen). Ohne sie wird die
Priorität gespeichert, aber ignoriert (FIFO). Bei einem extern betriebenen Temporal-Cluster setzt
dessen Betreiber beide Schlüssel.

Wirkung: Nur **wartende** Aufgaben werden nach Priorität vergeben (mehr Jobs als Worker-Slots);
laufende Activities werden nicht verdrängt, gleiche Priorität bleibt FIFO. Die Reihenfolge gilt je
Task-Queue-Partition (Temporal-Standard 4, also unter Last näherungsweise). Wer strikte Ordnung
braucht, reduziert die Partitionen in zwei Schritten (erst
`matching.numTaskqueueWritePartitions: 1`, warten bis die übrigen Partitionen leer sind, dann
`matching.numTaskqueueReadPartitions: 1`, sonst bleiben Aufgaben liegen) – ausgeliefert ist das nicht.
`job_metadata.priority` speichert weiterhin den API-Wert 1–10.

```bash
docker compose exec temporal temporal task-queue describe --task-queue security-check --address temporal:7233
docker compose exec temporal temporal workflow describe -w <job_id> --address temporal:7233   # zeigt die Priorität
```

Temporal liest die Dynamic-Config-Datei auch im laufenden Betrieb periodisch neu ein; nach einer
Änderung `docker compose restart temporal` übernimmt sie sofort (wartende Aufgaben bleiben erhalten).

### 5.12 Warteschlange (Dokument-Limit)

**Was läuft:** Vor der Verarbeitung holt sich jeder Job einen von `MAX_CONCURRENT_DOCUMENTS`
Verarbeitungsplätzen (Activity `acquire_document_slot`). Ist keiner frei, bleibt der Job `pending` und
fragt mit dauerhaften Timern nach (5 s, verdoppelt bis 60 s); er belegt dabei keinen Worker-Platz, und
kein Activity-Timeout läuft. Reihenfolge: Priorität (1 zuerst), dann Eingang. Nach der Risikoanalyse
gibt der Job den Platz frei (`release_document_slot`), ebenso bei Fehler und bei einem Workflow-Cancel.
Nach `DOCUMENT_QUEUE_TIMEOUT_SECONDS` (Standard 2 h) ohne Platz endet der Job mit `failed`
(„Kein freier Verarbeitungsplatz innerhalb von … min …“), ePro reicht ihn später neu ein.

Der Zustand liegt in Redis (nur Job-IDs, keine Inhalte): `eki:docslot:holders` (Job → Ende der Lease in
Epoch-Millisekunden), `eki:docslot:queue` (Wartende), `eki:docslot:seen` (letzte Nachfrage). Eine Lease
gilt 30 min; die KI-Schritte verlängern sie jede Minute. Wartende, die 5 min nicht mehr nachgefragt
haben, fallen aus der Schlange; fragen sie wieder nach, stehen sie wieder an ihrer alten Stelle.

**Ansehen:**

```bash
docker compose exec redis redis-cli ZRANGE eki:docslot:holders 0 -1 WITHSCORES   # Jobs mit Platz
docker compose exec redis redis-cli ZRANGE eki:docslot:queue 0 -1                # Wartende in Reihenfolge
docker compose logs worker | grep -E "Document slot|Document limit"              # Vergabe, Position, Freigabe
```

Metriken (Worker): `eki_document_slots_in_use`, `eki_document_queue_length`; wartende Jobs zeigt auch
`GET /v1/ops/jobs?status=pending`.

**Maßnahmen:**

- *Schlange abbauen:* `MAX_CONCURRENT_DOCUMENTS` erhöhen (nur wenn Ollama es verkraftet, §4.2) oder
  auf `0` setzen, dann `docker compose up -d --force-recreate api worker`. Wartende Jobs bekommen beim
  nächsten Nachfragen (≤ 60 s) einen Platz.
- *Platz bleibt belegt, obwohl der Job beendet ist* (z. B. nach `terminate` statt `cancel`, der Workflow
  gibt dann nichts frei): Die Lease läuft spätestens nach 30 min ab. Sofort freigeben:
  `docker compose exec redis redis-cli ZREM eki:docslot:holders <job_id>`. Jobs zum Entlasten der KI
  daher per Cancel beenden, nicht per Terminate.
- *Redis-Inhalt verloren* (Volume geleert): Die Belegung ist vergessen; kurzzeitig laufen mehr Jobs als
  erlaubt, die Schlange baut sich beim nächsten Nachfragen in der alten Reihenfolge neu auf.
- *Jobs scheitern mit „Kein freier Verarbeitungsplatz“:* Die Schlange war länger als die Wartezeit. Last
  verteilen (ePro staffeln), das Limit erhöhen (§4.2) oder `DOCUMENT_QUEUE_TIMEOUT_SECONDS` anheben – dann
  aber `TEMPORAL_WORKFLOW_EXECUTION_TIMEOUT` mit erhöhen (Start-Validator, `CONFIGURATION.md` §3.4).

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

Parität im API-Container (dort liegen Konfiguration und Ollama-URL bereit; das Golden-Set liegt
nicht im Image und wird hineinkopiert). Nach jedem Ollama-Update der externen Instanz wiederholen (§4.11):

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
| Jobs scheitern in der Zustellphase, Log: `value too long for type character varying(9)` | Schema nicht auf head (`DELIVERING` passt nicht in VARCHAR(9)) | `docker compose exec api alembic upgrade head` (head `c4e6a8b0d2f1`; die Korrektur ist `a7c9e1f3b5d2`) |
| Redis startet nicht, Log `Can't handle RDB format version 12` | `redis_data` wurde von Redis 7.4 geschrieben (Stand vor 1.0.1), 7.2 kann es nicht laden | drainen, Volume leeren (§4.10, Schritte 6 und 9) |
| Jobs bleiben `pending`/`running`; Temporal UI: wiederholt *Workflow Task Failed* mit `Encoded failure`; Worker-Log `PayloadDecryptionError: Temporal payload was encrypted with key id '…', but this process derives key id '…' from API_SECRET_KEY` | API und Worker haben verschiedene `API_SECRET_KEY` (z. B. nur ein Container neu erzeugt, unterschiedliche `*_FILE`), Key mit offenen Workflows rotiert, oder Restore mit anderem Key | denselben Key in beiden Containern setzen, `docker compose up -d --force-recreate api worker`. Key-IDs vergleichen (nur Fingerabdrücke, kein Schlüssel): `docker compose logs worker \| grep "payload encryption active"` und `docker compose exec api python -c "from api.config import get_settings; from core.temporal_codec import EncryptionCodec; print(EncryptionCodec(get_settings().api_secret_key).key_id)"`. Nach einer Rotation mit offenen Workflows: alten Key vorübergehend zurück, bis sie gedrained sind, oder die Workflows beenden |
| Worker-Log `Unknown payload encoding binary/encrypted` | Worker älter als 1.0.1 liest Workflows einer 1.0.1-API (oder Rollback mit offenen 1.0.1-Workflows) | Worker auf 1.0.1 bringen bzw. vor dem Rollback drainen (§4.10) |
| Jobs `failed` mit LLM-Fehler direkt nach dem Update auf 1.0.1 (Verbindungsfehler, Name nicht auflösbar) | `OLLAMA_BASE_URL` zeigt noch auf den entfernten Dienst `http://ollama:11434` (auch der Code-Default) | externe URL eintragen (§4.11), `docker compose up -d --force-recreate api worker`; prüfen: `docker compose exec worker env \| grep -E "OLLAMA_BASE_URL\|LOCAL_MISTRAL_BASE_URL"` |
| `403 Admin API key required for this endpoint` bei `/v1/kb/*` (auch `scripts/seed_kb.py`) | seit 1.0.1 nur Admin-Keys | Admin-Key verwenden: `create_api_key.py --insert --admin` (§4.3) |
| `404` auf `POST /v1/security/check` | synchroner Stub in 1.0.1 entfernt | `POST /v1/security/check:async` |
| API/Worker starten nicht, Validierungsfehler zu `KB_CLEANUP_CRON` bzw. `METADATA_CLEANUP_CRON` | Cron-Wert ungültig (seit 1.0.1 beim Start geprüft) | 5-Felder-Cron in UTC, in Anführungszeichen (§5.10) |
| Push-Job `failed` mit `delivery_failed:invalid_callback_url` | `callback_url`-Host nicht mehr in `CALLBACK_URL_ALLOWED_HOSTS` oder DNS liefert eine private Adresse; es wurde nichts gesendet | [`M10_FAILOVER_RUNBOOK.md`](M10_FAILOVER_RUNBOOK.md) §2.9 |
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

**Was die UI seit 1.0.1 zeigt:** Workflow-IDs (= `job_id`), Workflow- und Activity-Namen,
Task-Queue, Zeiten, Versuchszähler, Retry-Zustand, Timeouts, Priorität und Fehlertypen
(Exception-Klassen wie `ParsingFailed`) sowie die Schedules. Eingaben, Ergebnisse und Header
erscheinen nur als `binary/encrypted` (Base64-Chiffrat), Fehlermeldungen als `Encoded failure`
mit leerem Stacktrace. Für den Drain (§5.8) und die Diagnose hängender Activities genügt das;
die eigentliche Fehlermeldung steht im Worker-Log (Suche nach `job_id`). Historien von
Workflows, die vor dem 1.0.1-Update gestartet wurden, bleiben lesbar.

**Option (nicht ausgeliefert): Codec-Server.** Wer Payloads in UI oder CLI lesen muss, kann einen
kleinen HTTP-„Remote Codec“ betreiben, der `POST /decode` (und `/encode`) mit dem JSON-Body
`{"payloads": [{"metadata": {"<key>": "<base64>"}, "data": "<base64>"}]}` annimmt, mit
`core.temporal_codec.EncryptionCodec(API_SECRET_KEY).decode` entschlüsselt und dieselbe Form
zurückgibt. Die UI ruft ihn aus dem Browser des Operators auf (Einstellung „Data Encoder“ bzw.
Codec-Endpoint der UI, beim UI-Server über `TEMPORAL_CODEC_ENDPOINT` – für die eingesetzte
`temporalio/ui`-Version prüfen), die CLI mit `temporal workflow show -w <id> --codec-endpoint <url>`.
Sicherheitsfolgen: Wer `/decode` aufrufen kann, liest alle Payloads. Den Codec-Server daher nur im
internen Netz bzw. über denselben SSH-Tunnel erreichbar machen, mit Authentifizierung und auf den
UI-Origin beschränktem CORS, nie veröffentlichen; er braucht Schlüsselmaterial (`API_SECRET_KEY`
oder nur den abgeleiteten Codec-Schlüssel).
