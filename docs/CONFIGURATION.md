# eKI API – Konfigurationsreferenz

Vollständige Referenz aller Einstellungen aus `api/config.py` (`Settings`, 82 Felder)
plus der Variablen, die nur Docker Compose, die Infrastruktur-Container, Skripte oder
Tests lesen. Stand: Release 1.0.1. Bei Abweichungen gilt der Code; diese Tabelle bei
jeder Änderung an `Settings` mitpflegen.

**Neu bzw. geändert in 1.0.1:** `CALLBACK_URL_ALLOWED_HOSTS` und `TEMPORAL_PRIORITY_LEVELS`
(§3.4, §3.6), `METADATA_RETENTION_DAYS`, `METADATA_CLEANUP_ENABLED`, `METADATA_CLEANUP_CRON`
(§3.13); `KB_CLEANUP_CRON`/`METADATA_CLEANUP_CRON` werden beim Start streng geprüft, und
`KB_CLEANUP_ENABLED=false` pausiert einen bestehenden Schedule (§3.11); `API_SECRET_KEY` ist
zusätzlich Schlüsselmaterial der Temporal-Payload-Verschlüsselung (§3.5); `OLLAMA_BASE_URL` muss
auf eine externe Ollama-Instanz zeigen, weil der Compose-Stack kein Ollama mehr enthält (§3.8);
neue Temporal-Dynamic-Config für die Priorität (§2.4).

Vorlage mit allen betriebsrelevanten Werten: [`.env.example`](../.env.example).
Betriebsempfehlungen: [`OPERATIONS_GUIDE.md`](OPERATIONS_GUIDE.md) §3.

---

## 1. Wie die Konfiguration geladen wird

`Settings` (pydantic-settings) wird pro Prozess **einmal** gebaut (`get_settings()` ist
gecacht). Quellen, höchste Priorität zuerst:

1. `*_FILE`-Variablen (`DATABASE_URL_FILE`, `API_SECRET_KEY_FILE`, `EPRO_AUTH_TOKEN_FILE`,
   `EPRO_WEBHOOK_URL_FILE`, `MISTRAL_API_KEY_FILE`): Ist eine gesetzt, wird der Wert aus
   der Datei gelesen und überschreibt die Klartext-Variable. Fehlt die Datei oder ist sie
   leer, bricht der Start ab. Mount-Beispiel: [`secrets/README.md`](../secrets/README.md).
2. Umgebungsvariablen des Prozesses. In Docker sind das `env_file: .env.local` **und**
   die `environment:`-Einträge der Compose-Dateien – `environment:` gewinnt (siehe §2).
3. Datei `.env` im aktuellen Arbeitsverzeichnis. Relevant nur **außerhalb** von Docker
   (pytest, Skripte auf dem Host). `.env.local` wird von `Settings` nie gelesen.
4. Code-Default aus `api/config.py`.

Weitere Regeln:

- Variablennamen sind case-insensitiv (`case_sensitive=False`); unbekannte Variablen
  werden ignoriert.
- Listen (`CORS_ORIGINS`, `TRUSTED_PROXY_IPS`, `CALLBACK_URL_ALLOWED_HOSTS`) als kommagetrennter
  String oder JSON-Liste.
- Änderungen an `.env.local` werden erst nach Neuerzeugung der Container wirksam:
  `docker compose up -d --force-recreate api worker` (ein `docker compose restart` liest
  `env_file` **nicht** neu ein).
- Die API friert `LLM_PARALLEL_ENABLED`, `PDF_STRUCTURE_CONCURRENCY`,
  `RISK_ANALYSIS_CONCURRENCY`, `LLM_ACTIVITY_TIMEOUT_SECONDS`, `BUFFER_TTL_SECONDS` und
  `TEMPORAL_WORKFLOW_EXECUTION_TIMEOUT` beim Jobstart in die Workflow-Eingabe ein:
  neue Werte gelten nur für neue Jobs, und sie müssen im **API**-Container gesetzt sein.

### Start-Validator für Produktion

Bei `ENV=production` oder `ENV=prod` (`is_production`) verweigert `Settings` den Start, wenn

- `API_SECRET_KEY` kürzer als 32 Zeichen ist, einem Code-Default entspricht oder einen
  Platzhalter enthält (`change_me`, `change-me`, `changeme`, `replace-me`, `replace_me`,
  Groß-/Kleinschreibung egal – also auch die `CHANGE_ME_…`-Werte aus `.env.example`),
- `DATABASE_URL` `eki_password` oder einen dieser Platzhalter enthält,
- `DEBUG=true` ist,
- `LLM_PROVIDER=mistral_cloud` ohne `LLM_ALLOW_EXTERNAL_PROVIDERS=true` gesetzt ist.

Unabhängig von `ENV` wird `LLM_PROVIDER` gegen `local_mistral`, `ollama`, `mistral_cloud`
geprüft (Bindestrich-Varianten werden mit Hinweis abgelehnt). Ebenfalls immer geprüft werden
seit 1.0.1 `KB_CLEANUP_CRON` und `METADATA_CLEANUP_CRON` (5-Felder-Cron, §3.13): Ein ungültiger
Wert verhindert den Start von API und Worker (vorher entstand nur eine Warnung und kein Schedule).

---

## 2. Docker Compose: was Compose selbst liest

### 2.1 Variablen-Substitution (`${VAR}` in den Compose-Dateien)

Compose ersetzt `${VAR}` aus der **Shell-Umgebung** oder einer Datei `.env` im
Projektverzeichnis (alternativ `docker compose --env-file <datei> …`) – **nicht** aus
`.env.local`. Ein Eintrag nur in `.env.local` hat für diese Variablen keine Wirkung.

| Variable | Compose-Datei | Default | Wirkung |
|---|---|---|---|
| `EKI_IMAGE_TAG` | `docker-compose.prod.yml` | `latest` | Image-Tag für `eki-api`/`eki-worker` |
| `GHCR_OWNER` | `docker-compose.prod.yml` | `stefanmuellerai` | GHCR-Namespace der Images |
| `LLM_PROVIDER` | `docker-compose.prod.yml` | `local_mistral` | setzt `LLM_PROVIDER` für `api`/`worker` per `environment:` und **überschreibt** damit `.env.local` |
| `GRAFANA_ADMIN_USER` | `docker-compose.observability.yml` | `admin` | Grafana-Admin |
| `GRAFANA_ADMIN_PASSWORD` | `docker-compose.observability.yml` | `CHANGE_ME` | Grafana-Admin-Passwort – **ohne Setzen gilt wörtlich `CHANGE_ME`** |
| `OTEL_ENABLED` | `docker-compose.observability.yml` | `true` | überschreibt `OTEL_ENABLED` aus `.env.local` für `api`/`worker` |
| `POSTGRES_USER`, `POSTGRES_DB` | `docker-compose.yml` (Healthcheck) | `eki_user`, `eki_db` | nur `pg_isready` im Healthcheck |

Empfehlung: eine Datei `.env` im Projektverzeichnis (git-ignoriert) nur mit diesen
Variablen anlegen, z. B.

```bash
cat > .env <<'EOF'
GHCR_OWNER=<github-owner>
EKI_IMAGE_TAG=1.0.0
GRAFANA_ADMIN_PASSWORD=<starkes-passwort>
EOF
docker compose -f docker-compose.yml -f docker-compose.prod.yml config | grep -E 'image:|LLM_PROVIDER'   # prüfen
```

oder die Werte vor jedem Compose-Aufruf per `export` setzen. Hinweis: Dieselbe `.env`
liest `Settings` bei Skripten/Tests auf dem Host (Quelle 3 in §1) – dort also keine
abweichenden App-Werte hineinschreiben.

### 2.2 Werte, die die Overlays fest setzen

`environment:` schlägt `env_file`; diese Einträge in `.env.local` sind daher mit dem
jeweiligen Overlay wirkungslos:

| Overlay | Dienst | fest gesetzt |
|---|---|---|
| `docker-compose.prod.yml` | `api`, `worker` | `ENV=production`, `DEBUG=false`, `LOG_FORMAT=json`, `LLM_PROVIDER=${LLM_PROVIDER:-local_mistral}` |
| `docker-compose.observability.yml` | `api`, `worker` | `OTEL_ENABLED=${OTEL_ENABLED:-true}`, `OTEL_EXPORTER_OTLP_ENDPOINT=http://jaeger:4318` |
| `docker-compose.observability.yml` | `worker` | `PROMETHEUS_PORT=9090` |
| `docker-compose.ollama.yml` (optional, Entwicklung/Offline) | `api`, `worker` | `OLLAMA_BASE_URL=http://ollama:11434`, `LOCAL_MISTRAL_BASE_URL=http://ollama:11434` (zeigen auf den Ollama-Container des Overlays; ein Wert in `.env.local` ist dann wirkungslos) |
| `docker-compose.override.yml.example` | `api`, `worker` | `LLM_PROVIDER=local_mistral`, `LOCAL_MISTRAL_MODEL=mistral-small3.2` – beim Kopieren anpassen oder entfernen |

### 2.3 Variablen der Infrastruktur-Container und Images (nicht `Settings`)

| Variable | gelesen von | Hinweis |
|---|---|---|
| `OMP_THREAD_LIMIT` | Tesseract (OCR) in `api`/`worker` | in beiden Dockerfiles fest `1`; `tests/conftest.py` setzt es ebenfalls. Ohne Begrenzung konkurrieren die OpenMP-Threads unter Last, eine Seite braucht dann > 60 s statt ~1 s und wird nach `OCR_PAGE_TIMEOUT_SECONDS` übersprungen. Bei Installationen ohne diese Images (Bare Metal, eigene Images) selbst `OMP_THREAD_LIMIT=1` setzen |
| `POSTGRES_DB`, `POSTGRES_USER`, `POSTGRES_PASSWORD` | `postgres` (über `.env.local`) | nur beim **ersten** Initialisieren des Volumes `postgres_data`; danach Passwortwechsel per `ALTER USER` (`OPERATIONS_GUIDE.md` §5.9) |
| `POSTGRES_USER`, `POSTGRES_PWD` | `temporal` (auto-setup, über `.env.local`) | gleicher Nutzer/gleiches Passwort wie oben; legt die Datenbanken `temporal` und `temporal_visibility` an |
| `POSTGRES_INITDB_ARGS` | `postgres` | in `docker-compose.yml` fest `-A md5`: auch `psql` im Container braucht das Passwort |
| `DEFAULT_NAMESPACE` | `temporal` (auto-setup) | nicht gesetzt ⇒ `default`; nur relevant, wenn `TEMPORAL_NAMESPACE` geändert wird |
| `DEFAULT_NAMESPACE_RETENTION` | `temporal` (auto-setup) | nicht gesetzt ⇒ `24h`; wirkt nur beim **ersten** Anlegen des Namespace. Spätere Änderung mit der Temporal-CLI (`OPERATIONS_GUIDE.md` §5.11) |
| `DYNAMIC_CONFIG_FILE_PATH` | `temporal` | in `docker-compose.yml` fest `config/dynamicconfig/eki.yaml` (gemountet aus `docker/temporal/dynamicconfig/`, §2.4) |
| `OLLAMA_KEEP_ALIVE`, `OLLAMA_NUM_PARALLEL` | Ollama-Container des Overlays `docker-compose.ollama.yml` | dort `30m` bzw. `1`; für die externe Produktionsinstanz dieselben Werte empfohlen (`OPERATIONS_GUIDE.md` §4.11) |

### 2.4 Temporal-Dynamic-Config (`docker/temporal/dynamicconfig/eki.yaml`)

Die Datei wird in den `temporal`-Container gemountet (auch im Prod-Overlay, sie kommt also aus dem
Checkout, nicht aus einem Image). Temporal liest sie periodisch neu ein; `docker compose restart
temporal` wendet Änderungen sofort an.

| Schlüssel | Wert | Wirkung |
|---|---|---|
| `history.hostLevelCacheMaxSize` | `50000` | begrenzt den Mutable-State-Cache |
| `history.enableHostLevelEventsCache`, `history.eventsHostLevelCacheMaxSizeBytes` | `true`, 256 MB | Events-Cache mit Byte-Limit |
| `frontend.keepAliveMaxConnectionAge` | `5m` | begrenzt die Lebensdauer von gRPC-Verbindungen |
| `matching.useNewMatcher` | `true` (1.0.1) | prioritätsfähiger Matcher; in Temporal 1.29 standardmäßig aus – ohne ihn wird `priority` ignoriert |
| `matching.priorityLevels` | `10` (1.0.1) | Anzahl Prioritätsstufen (Temporal-Standard 5); muss `TEMPORAL_PRIORITY_LEVELS` entsprechen |

Bei einem extern betriebenen Temporal-Cluster müssen dessen Betreiber die beiden
`matching.*`-Schlüssel setzen.

---

## 3. Alle Einstellungen aus `api/config.py`

Spalte „Gelesen in“: Module, die den Wert verwenden (Stand 1.0.1, per `grep` ermittelt).
„ungenutzt (reserviert)“: das Feld existiert, wird aber nirgends gelesen – Setzen hat
keine Wirkung.

### 3.1 Umgebung & API-Prozess

| Variable | Typ | Default | Bedeutung | Gelesen in |
|---|---|---|---|---|
| `ENV` | str | `development` | `development` \| `stage` \| `production` (`prod`). `production`/`prod` aktiviert den Start-Validator | `api/config.py`, `api/main.py` (Log), `core/tracing.py` (`deployment.environment`), `llm/factory.py` |
| `DEBUG` | bool | `false` | `true`: `/docs`, `/redoc`, `/openapi.json` werden ausgeliefert und SQLAlchemy loggt alle SQL-Statements (`echo`). In Produktion verboten | `api/main.py`, `db/session.py` |
| `CORS_ORIGINS` | list[str] | `http://localhost:3000,http://localhost:8000` | erlaubte Browser-Origins | `api/main.py` |
| `METRICS_ENABLED` | bool | `true` | `/metrics` der API (API-Key) und Worker-Exporter auf `PROMETHEUS_PORT` | `api/main.py`, `worker/main.py` |
| `API_HOST` | str | `0.0.0.0` | ungenutzt (reserviert) – Bind-Adresse steht im Dockerfile-`CMD` bzw. Gunicorn-Kommando | – |
| `API_PORT` | int | `8000` | ungenutzt (reserviert) – Port steht im Dockerfile/Compose | – |
| `API_WORKERS` | int | `4` | ungenutzt (reserviert) – Prod nutzt 1 Gunicorn-Worker je Container, Skalierung über `--scale api=N` | – |
| `API_RELOAD` | bool | `false` | ungenutzt (reserviert) | – |

### 3.2 Datenbank

| Variable | Typ | Default | Bedeutung | Gelesen in |
|---|---|---|---|---|
| `DATABASE_URL` | PostgresDsn | `postgresql+asyncpg://eki_user:replace-me@localhost:5432/eki_db` | Metadaten-DB (asyncpg). Alembic ersetzt den Treiber intern durch `psycopg2` | `db/session.py`, `db/migrations/env.py`, `workflows/activities.py`, `workflows/maintenance.py`; `scripts/create_api_key.py` liest die Umgebungsvariable direkt |
| `DATABASE_URL_FILE` | str \| None | – | Datei mit vollständiger `DATABASE_URL` (Vorrang) | `api/config.py` |
| `DATABASE_POOL_SIZE` | int | `20` | Pool-Größe der API (Worker-Activities öffnen eigene Engines) | `db/session.py` |
| `DATABASE_MAX_OVERFLOW` | int | `10` | zusätzliche Verbindungen über den Pool hinaus (API) | `db/session.py` |

### 3.3 Redis & SecureBuffer

| Variable | Typ | Default | Bedeutung | Gelesen in |
|---|---|---|---|---|
| `REDIS_URL` | RedisDsn | `redis://localhost:6379/0` | SecureBuffer (verschlüsselte Inhalte), Rate-Limit-Zähler | `api/dependencies.py`, `workflows/activities.py`, `scripts/uat/run_acceptance_tests.py` (Fallback) |
| `REDIS_MAX_CONNECTIONS` | int | `50` | Verbindungs-Cap des Redis-Pools, den die API je Request öffnet | `api/dependencies.py` |
| `BUFFER_TTL_SECONDS` | int | `21600` | Lebensdauer von Skript-/Report-Inhalten in Redis (≤ 6 h laut Pflichtenheft); zugleich Pull-Abholfenster, danach Löschung + Dead Letter | `api/routers/security.py`, `workflows/activities.py` (Wert wird beim Jobstart eingefroren) |

### 3.4 Temporal & Worker

| Variable | Typ | Default | Bedeutung | Gelesen in |
|---|---|---|---|---|
| `TEMPORAL_HOST` | str | `localhost:7233` | Temporal-Frontend (`temporal:7233` in Compose) | `api/dependencies.py`, `api/routers/health.py`, `worker/main.py` |
| `TEMPORAL_NAMESPACE` | str | `default` | Namespace für API **und** Worker; muss existieren (auto-setup legt nur `default` an) | `api/dependencies.py`, `api/routers/health.py`, `worker/main.py` |
| `TEMPORAL_TASK_QUEUE` | str | `security-check` | Task-Queue für Prüf-Workflows und die Wartungs-Schedules (`eki-kb-cleanup`, `eki-metadata-cleanup`); eine Änderung zieht der Worker beim Start in den Schedules nach | `api/routers/security.py`, `worker/main.py`, `workflows/maintenance.py` |
| `TEMPORAL_WORKFLOW_EXECUTION_TIMEOUT` | int (s) | `36000` | Gesamtlaufzeit eines Prüf-Workflows (≥ 2 h Verarbeitung + 6 h Retry/Pull-TTL); geht zusammen mit `BUFFER_TTL_SECONDS` in die Schutzregel für verwaiste Jobs ein (§3.13) | `api/routers/security.py`, `workflows/maintenance.py` |
| `TEMPORAL_PRIORITY_LEVELS` | int 1–100 | `10` | Anzahl der Prioritätsstufen des Temporal-Servers (= Dynamic Config `matching.priorityLevels`, §2.4). Die API bildet das Request-Feld `priority` 1–10 auf Temporal-Schlüssel 1–N ab: `((p - 1) * N) // 10 + 1` (bei 10 identisch). Muss zur Server-Konfiguration passen | `api/routers/security.py` |
| `WORKER_MAX_CONCURRENT_WORKFLOW_TASKS` | int ≥ 1 | `10` | Temporal-Worker-Cap | `worker/main.py` |
| `WORKER_MAX_CONCURRENT_ACTIVITIES` | int ≥ 1 | `20` | Temporal-Worker-Cap | `worker/main.py` |

Die Temporal-Payloads (Workflow-/Activity-Ein- und -Ausgaben, Header, Fehlermeldungen) werden seit
1.0.1 immer verschlüsselt (AES-256-GCM, `core/temporal_codec.py`); es gibt keinen Schalter und
keine eigene Variable – der Schlüssel wird aus `API_SECRET_KEY` abgeleitet (§3.5).

### 3.5 Sicherheit & Rate-Limits

| Variable | Typ | Default | Bedeutung | Gelesen in |
|---|---|---|---|---|
| `API_SECRET_KEY` | str | `replace-this-secret-key-with-at-least-32-characters` | Master-Secret: Fernet-Schlüssel für SecureBuffer und KB-Originaltexte (SHA-256-Ableitung) und seit 1.0.1 Schlüssel des Temporal-Payload-Codecs (HKDF-SHA256, Label `eki-temporal-codec-v1`). **Muss in API und Worker identisch sein.** Wechsel ⇒ gepufferte Inhalte und KB-Originale nicht mehr entschlüsselbar, offene Workflows nicht mehr dekodierbar (vorher drainen, `OPERATIONS_GUIDE.md` §5.7) | `api/routers/security.py`, `api/routers/knowledge_base.py`, `workflows/activities.py`, `workflows/maintenance.py`; Codec (`core/temporal_codec.py`) über `api/dependencies.py`, `api/routers/health.py`, `worker/main.py` |
| `API_SECRET_KEY_FILE` | str \| None | – | Datei mit `API_SECRET_KEY` (Vorrang) | `api/config.py` |
| `API_TOKEN_EXPIRE_MINUTES` | int | `60` | ungenutzt (reserviert) – es gibt keine Tokens; API-Keys laufen über `api_keys.expires_at` ab | – |
| `RATE_LIMIT_ENABLED` | bool | `true` | Rate-Limiting an/aus (bei Redis-Ausfall wird durchgelassen) | `api/rate_limiting.py` |
| `RATE_LIMIT_PER_MINUTE` | int | `60` | Requests pro Minute je Quell-IP | `api/rate_limiting.py` |
| `RATE_LIMIT_PER_HOUR` | int | `1000` | Requests pro Stunde je präsentiertem API-Key | `api/rate_limiting.py` |
| `TRUST_PROXY_HEADERS` | bool | `false` | `X-Forwarded-For` auswerten – nur wenn der direkte Peer in `TRUSTED_PROXY_IPS` steht | `api/rate_limiting.py` |
| `TRUSTED_PROXY_IPS` | list[str] | leer | exakte Peer-IPs des Reverse-Proxys (kein CIDR) | `api/rate_limiting.py` |

### 3.6 ePro-Anbindung & Zustellung

| Variable | Typ | Default | Bedeutung | Gelesen in |
|---|---|---|---|---|
| `EPRO_BASE_URL` | str | `https://staging.epro.filmakademie.de/api` | Standard-Push-Ziel `EPRO_BASE_URL/eki/scl/set-risk-assessment/{project_id}`; ein `callback_url` im Request ersetzt es für diesen Job (seit 1.0.1) | `workflows/activities.py` |
| `CALLBACK_URL_ALLOWED_HOSTS` | list[str] (Komma oder JSON-Liste) | `epro.filmakademie.de,staging.epro.filmakademie.de` | Hosts, auf die ein `callback_url` zeigen darf: exakter Vergleich, Groß-/Kleinschreibung und abschließender Punkt egal, keine Wildcards/Subdomains, jeder Port. Leer ⇒ jedes `callback_url` wird mit 422 abgelehnt. Geprüft bei der Anfrage (API) **und** vor jedem Push (Worker, zusätzlich DNS: nur öffentliche Adressen) – API und Worker brauchen denselben Wert; ein entfernter Host lässt laufende Jobs mit Dead Letter `invalid_callback_url` enden | `api/config.py`, `core/models.py`, `core/callback_url.py`, `workflows/activities.py` |
| `EPRO_AUTH_TOKEN` | str | leer | optionaler Bearer-Token für Push und Webhook (ePro authentisiert derzeit per IP); wird auch an ein `callback_url` gesendet (deshalb die Host-Allow-List) | `workflows/activities.py` |
| `EPRO_AUTH_TOKEN_FILE` | str \| None | – | Datei mit `EPRO_AUTH_TOKEN` (Vorrang) | `api/config.py` |
| `EPRO_TIMEOUT` | int (s) | `30` | HTTP-Timeout je Push-/Webhook-Versuch | `workflows/activities.py` |
| `EPRO_WEBHOOK_URL` | str | leer | `security.delivery.failed`-Webhook; leer = aus | `workflows/activities.py` |
| `EPRO_WEBHOOK_URL_FILE` | str \| None | – | Datei mit `EPRO_WEBHOOK_URL` (Vorrang) | `api/config.py` |
| `DELIVERY_MODE` | str | `pull` | faktisch wirkungslos: Den Modus bestimmt das Request-Feld `delivery` (`pull`/`push`; fehlt es oder ist es leer, gilt `push`, wenn `callback_url` gesetzt ist, sonst `pull`); die Einstellung ist nur noch als Fallback im Code verankert | `api/routers/security.py` |

### 3.7 LLM-Provider

| Variable | Typ | Default | Bedeutung | Gelesen in |
|---|---|---|---|---|
| `LLM_PROVIDER` | str | `mistral_cloud` (!) | `local_mistral` (Produktion), `ollama`, `mistral_cloud` (nur Stage). Code-Default ist Cloud – daher in `.env.local` immer explizit setzen; `.env.example` setzt `local_mistral` | `llm/factory.py` (Provider für Worker-Activities, PDF-Parser und die KB-Endpoints der API), `api/main.py`, `worker/main.py` (`eki_build_info`) |
| `LLM_ALLOW_EXTERNAL_PROVIDERS` | bool | `false` | erlaubt `mistral_cloud` trotz `ENV=production` (Abnahmetest 9: nicht setzen) | `api/config.py`, `llm/factory.py` |
| `LOCAL_MISTRAL_MODEL` | str | `mistral-small3.2` | Ollama-Modell-Tag für `local_mistral` | `llm/factory.py` |
| `LOCAL_MISTRAL_BASE_URL` | str \| None | – | eigener Ollama-Endpoint für `local_mistral`; leer ⇒ `OLLAMA_BASE_URL` | `llm/factory.py` |
| `MISTRAL_API_KEY` | str | leer | API-Key für `mistral_cloud` | `llm/factory.py` |
| `MISTRAL_API_KEY_FILE` | str \| None | – | Datei mit `MISTRAL_API_KEY` (Vorrang) | `api/config.py` |
| `MISTRAL_MODEL` | str | `mistral-large-latest` | Modell für `mistral_cloud` | `llm/factory.py` |
| `MISTRAL_TIMEOUT` | int (s) | `120` | Timeout für `mistral_cloud` | `llm/factory.py` |

Mit `mistral_cloud` ist die Knowledge Base nicht nutzbar (der Provider hat kein `embed()`).

### 3.8 Ollama (gilt für `ollama` und `local_mistral`)

| Variable | Typ | Default | Bedeutung | Gelesen in |
|---|---|---|---|---|
| `OLLAMA_BASE_URL` | str | `http://ollama:11434` | Ollama-Endpoint für Inferenz und KB-Embeddings. Seit 1.0.1 enthält der Stack kein Ollama: **die URL der externen Instanz setzen** (`.env.example`: Platzhalter `http://ollama-host.invalid:11434`). Der Code-Default passt nur zum Overlay `docker-compose.ollama.yml`, das den Wert selbst setzt. Ohne erreichbare URL scheitern Jobs beim ersten LLM-Aufruf (`/ready` prüft Ollama nicht) | `llm/factory.py` |
| `OLLAMA_MODEL` | str | `mistral` | Modell **nur** für `LLM_PROVIDER=ollama` (`.env.example`: `mistral-small3.2`) | `llm/factory.py` |
| `OLLAMA_TIMEOUT` | int (s) | `300` | HTTP-Timeout je Ollama-Call | `llm/factory.py` |
| `OLLAMA_THINK` | bool | `false` | Thinking-Modus für gemma4/qwen3.x; von Mistral ignoriert | `llm/factory.py` |
| `OLLAMA_NUM_CTX` | int | `32768` | Kontextfenster in Tokens (16384 spart VRAM) | `llm/factory.py` |
| `OLLAMA_EMBEDDING_MODEL` | str | `mxbai-embed-large` | KB-Embedding-Modell, **muss 1024-dimensionale Vektoren liefern** (Spalte `vector(1024)`). Alternative `bge-m3`. Wechsel ⇒ KB leeren und neu einspielen (`OPERATIONS_GUIDE.md` §5.6) | `llm/factory.py` |
| `OLLAMA_EMBEDDING_MAX_CHARS` | int | `1000` | Eingabe für `embed()` wird auf diese Länge gekürzt; `mxbai-embed-large` ≤ 1000, `bge-m3` ca. 30000 | `llm/factory.py` |
| `OLLAMA_MAX_CONCURRENT_REQUESTS` | int 1–8 | `1` | prozessweiter Semaphore für Ollama-Calls (je API-/Worker-Prozess) | `llm/ollama.py` |
| `OLLAMA_MIN_INTERVAL_MS` | int ≥ 0 | `0` | Mindestabstand zwischen zwei Ollama-Calls (ms) | `llm/ollama.py` |

### 3.9 Großdokumente & Parallelisierung (M07)

| Variable | Typ | Default | Bedeutung | Gelesen in |
|---|---|---|---|---|
| `LLM_PARALLEL_ENABLED` | bool | `false` | Master-Flag für parallele LLM-Activities | `api/routers/security.py` (eingefroren) |
| `PDF_STRUCTURE_CONCURRENCY` | int 1–8 | `1` | parallele Strukturierungs-Activities je Workflow (nur mit Master-Flag) | `api/routers/security.py` (eingefroren) |
| `RISK_ANALYSIS_CONCURRENCY` | int 1–8 | `1` | parallele Risikoanalyse-Activities je Workflow (nur mit Master-Flag) | `api/routers/security.py` (eingefroren) |
| `LLM_ACTIVITY_TIMEOUT_SECONDS` | int ≥ 60 | `600` | `start_to_close_timeout` der LLM-Activities | `api/routers/security.py` (eingefroren) |
| `MAX_PDF_PAGES` | int ≥ 1 | `500` | Seitenlimit des PDF-Parsers | `parsers/pdf.py` |
| `MAX_PDF_SIZE_BYTES` | int ≥ 1 | `10485760` | Größenlimit des PDF-Parsers | `parsers/pdf.py` |
| `MAX_UPLOAD_SIZE_BYTES` | int ≥ 1 | `10485760` | Limit für Multipart-Uploads. JSON-Uploads sind unabhängig davon fest begrenzt: Base64-Text max. 10 MiB (`core/models.py`), also Dateien bis ca. 7,5 MiB | `api/routers/security.py` |

### 3.10 OCR-Fallback

| Variable | Typ | Default | Bedeutung | Gelesen in |
|---|---|---|---|---|
| `OCR_ENABLED` | bool | `true` | Tesseract für Seiten ohne Textebene | `parsers/pdf_ocr.py` |
| `OCR_LANGUAGES` | str | `deu+eng` | Tesseract-Sprachen (Pakete `tesseract-ocr-deu`/`-eng`) | `parsers/pdf_ocr.py` |
| `OCR_MAX_PAGES` | int | `50` | max. OCR-Seiten je Dokument | `parsers/pdf_ocr.py` |
| `OCR_DPI` | int | `300` | Render-Auflösung | `parsers/pdf_ocr.py` |
| `OCR_PAGE_TIMEOUT_SECONDS` | int | `60` | Tesseract-Timeout je Seite | `parsers/pdf_ocr.py` |

### 3.11 Knowledge Base

| Variable | Typ | Default | Bedeutung | Gelesen in |
|---|---|---|---|---|
| `KB_RETRIEVAL_ENABLED` | bool | `false` | KB-Kontext in der Risikoanalyse (erst nach fachlicher Freigabe der Inhalte) | `workflows/activities.py` |
| `KB_DEFAULT_TENANT_ID` | str (UUID) | `00000000-0000-0000-0000-000000000001` | Mandant aller KB-Dokumente | `api/routers/knowledge_base.py`, `workflows/activities.py` |
| `KB_TOP_K` | int | `3` | Chunks je Szene | `workflows/activities.py` |
| `KB_MAX_CHUNK_CHARS_IN_PROMPT` | int | `600` | max. Zeichen je Chunk im Prompt | `workflows/activities.py` |
| `KB_CLEANUP_ENABLED` | bool | `true` | Temporal-Schedule `eki-kb-cleanup`. Bei jedem Worker-Start: fehlt er, wird er angelegt; `false` **pausiert** einen bestehenden Schedule (bzw. legt keinen an), `true` setzt ihn fort (seit 1.0.1; vorher blieb ein bestehender Schedule unverändert) | `workflows/maintenance.py` |
| `KB_CLEANUP_CRON` | str | `0 3 * * *` | Cron (UTC, 5 Felder, Format §3.13) des Schedules; wird beim Start geprüft. Eine Änderung wirkt nach Neustart des Workers (Schedule wird aktualisiert; früher musste er dafür gelöscht werden). In `.env`-Dateien in Anführungszeichen setzen | `api/config.py`, `workflows/maintenance.py` |

### 3.12 Observability & Logging

| Variable | Typ | Default | Bedeutung | Gelesen in |
|---|---|---|---|---|
| `OTEL_ENABLED` | bool | `false` | OpenTelemetry-Tracing (Observability-Overlay setzt `true`) | `core/tracing.py` |
| `OTEL_SERVICE_NAME` | str | `eki-api` | Präfix des Service-Namens; Spans heißen `<name>-api` bzw. `<name>-worker` | `core/tracing.py` |
| `OTEL_EXPORTER_OTLP_ENDPOINT` | str | `http://localhost:4318` | OTLP/HTTP-Collector (z. B. `http://jaeger:4318`) | `core/tracing.py` |
| `PROMETHEUS_PORT` | int | `9090` | Port des Worker-Exporters (nur im Docker-Netz) | `worker/main.py` |
| `LOG_LEVEL` | str | `INFO` | Log-Level | `core/logging_config.py` |
| `LOG_FORMAT` | str | `json` | `json` oder `console`; Inhalte werden in beiden Formaten maskiert | `core/logging_config.py` |

### 3.13 Metadaten-Aufbewahrung (seit 1.0.1)

| Variable | Typ | Default | Bedeutung | Gelesen in |
|---|---|---|---|---|
| `METADATA_RETENTION_DAYS` | int ≥ 1 | `90` | Aufbewahrungsfrist in Tagen für `job_metadata` (Endzustände und verwaiste Jobs), `report_metadata`, `delivery_dead_letters` (auch unquittierte), nicht mehr nutzbare `api_keys` und `audit_logs`; Regeln: `DATENSCHUTZ.md` §4. Verwaiste Jobs werden nur gelöscht, wenn die Frist länger ist als `TEMPORAL_WORKFLOW_EXECUTION_TIMEOUT` + `BUFFER_TTL_SECONDS` (Standard 16 h, bei ≥ 1 Tag also immer). Nur der Worker liest den Wert | `workflows/maintenance.py` |
| `METADATA_CLEANUP_ENABLED` | bool | `true` | Temporal-Schedule `eki-metadata-cleanup`; `false` pausiert einen bestehenden bzw. legt keinen an, `true` setzt ihn beim nächsten Worker-Start fort | `workflows/maintenance.py` |
| `METADATA_CLEANUP_CRON` | str | `30 3 * * *` | Cron (UTC) des Metadaten-Cleanups; wird beim Start geprüft; eine Änderung wirkt nach Neustart des Workers. In `.env`-Dateien in Anführungszeichen setzen | `api/config.py`, `workflows/maintenance.py` |

**Cron-Format** (`KB_CLEANUP_CRON`, `METADATA_CLEANUP_CRON`): genau 5 Felder
`Minute Stunde Tag Monat Wochentag`, ausgewertet in **UTC**. Je Feld `*`, Zahlen, Bereiche `a-b`,
Schritte `*/n`, `a-b/n`, `a/n` und Listen mit Komma; Monatsnamen `JAN`–`DEC`, Wochentage
`SUN`–`SAT` (Groß-/Kleinschreibung egal). Wertebereiche: Minute 0–59, Stunde 0–23, Tag 1–31,
Monat 1–12, Wochentag 0–6 (0 = Sonntag; `7` wird abgelehnt). Nicht erlaubt: 6/7 Felder
(Sekunden, Jahr), Kürzel wie `@daily`/`@every`, `?`, `L`, `#`, `CRON_TZ=`. Leerraum wird
normalisiert. Sind Tag und Wochentag beide eingeschränkt, müssen bei Temporal **beide** passen
(UND; klassisches cron verknüpft sie mit ODER). Ein ungültiger Wert bricht den Start von API und
Worker mit einer Meldung ab, die den Variablennamen nennt.

Der Worker gleicht beide Schedules bei jedem Start mit diesen Einstellungen ab (Cron,
aktiviert/pausiert, Task-Queue, Zielworkflow); Änderungen in der Temporal UI überschreibt der
nächste Start. Betrieb: `OPERATIONS_GUIDE.md` §5.10.

---

## 4. Variablen der Skripte und Tests

Nicht Teil von `Settings`; nur für die genannten Werkzeuge.

| Variable | Werkzeug | Default | Bedeutung |
|---|---|---|---|
| `EKI_API_URL` | `scripts/seed_kb.py`, `scripts/smoke_prod.py`, `scripts/uat/run_acceptance_tests.py`, `tests/run_*.py` | `http://localhost:8000` | API-Basis-URL |
| `EKI_API_KEY` | dieselben | – | API-Key (Klartext `eki_…`); für `scripts/seed_kb.py` ein **Admin-Key** (`/v1/kb/*` seit 1.0.1 admin-only, sonst 403 mit Hinweis) |
| `EKI_ADMIN_KEY` | `scripts/uat/run_acceptance_tests.py` | – | Admin-Key für `/v1/ops/*` (AT4) |
| `MOCK_URL` | `scripts/uat/run_acceptance_tests.py` | – | Steuer-URL des ePro-Mocks (AT2/AT4) |
| `EKI_REDIS_URL` | `scripts/uat/run_acceptance_tests.py` | – | optionaler Redis-Zugang für den Buffer-Check |
| `EKI_LOG_CMD` | `scripts/uat/run_acceptance_tests.py` | – | Kommando, das die Logs liefert (AT7) |
| `EKI_WORKER_METRICS_URL` | `scripts/smoke_prod.py` | – | Worker-Exporter, z. B. `http://worker:9090` (nur im Docker-Netz erreichbar) |
| `TEST_POSTGRES_URL` | pytest (`tests/conftest.py`) | – | ganze Suite gegen echtes PostgreSQL; **Datenbank wird geleert** |
| `TEST_REDIS_URL` | pytest (`tests/test_e2e_pipeline.py`) | `redis://localhost:6379/4` | echtes Redis für die E2E-Tests; **DB wird geleert** (DB 0 wird verweigert) |
| `EKI_PRIVATE_FIXTURES_DIR` | `tests/run_pdf_bulk_test.py`, `tests/run_pdf_full_pipeline.py`, `tests/run_pdf_selected.py` | `tests/fixtures/pdf/private/` | Verzeichnis mit lokalen echten Drehbüchern (git- und docker-ignoriert); ohne PDFs dort nutzen die Runner die synthetischen Fixtures |
| `EKI_FULL_FIXTURE_SCAN` | pytest (`tests/test_edge_parsers_and_services.py`) | – (aus) | `1` = der Injection-Filter-Scan prüft alle Seiten jedes PDF-Fixtures statt der ersten 8 |

Details zu den Testvariablen: [`TESTING_GUIDE.md`](TESTING_GUIDE.md).
