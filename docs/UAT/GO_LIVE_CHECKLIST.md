# Go-Live-Checkliste – eKI API v1.0.1

Abzuarbeiten nach abgenommenem UAT (`UAT_PROTOCOL_TEMPLATE.md` §5) und vor dem
produktiven Freischalten im eProjekt. Verantwortlichkeiten: **IT** = IT-Betrieb
Filmakademie, **eKI** = Auftragnehmer, **ePro** = eProjekt-Team, **SB** = Sicherheitsbeauftragter,
**DS** = Datenschutz der Filmakademie.

Ausführliche Anleitung: `docs/OPERATIONS_GUIDE.md`; alle Einstellungen: `docs/CONFIGURATION.md`.
`<Release-Tag>` steht im Folgenden für die Version des freigegebenen Images **ohne** führendes
`v` (z. B. `1.0.1`).

## A. Vorbedingungen

- [ ] UAT abgenommen; offene Punkte §4 des Protokolls haben Termin und Verantwortlichen (eKI/IT)
- [ ] Abnahmetests 2–7 wurden auf dem aktuellen Stand (Release 1.0.1, Migrations-Head
      `c4e6a8b0d2f1`) ausgeführt; vor Migration `a7c9e1f3b5d2` scheiterte auf PostgreSQL die
      Zustellphase (`HANDOVER.md` §1.1) (eKI)
- [ ] Supportvertrag geschlossen (Pflichtenheft §14, spätestens mit M10) (IT/eKI)
- [ ] Release-Tag `v1.0.1` gesetzt und GHCR-Images vorhanden, Digest im Protokoll; **nicht** die
      `v1.0.0`-Images verwenden (ohne die Korrekturen, noch mit echten Drehbüchern) (eKI)
- [ ] eKI-Host: Docker ≥ 24 mit **Docker Compose ≥ 2.24** (das Prod-Overlay nutzt `!reset`) (IT)
- [ ] Externe Ollama-Instanz bereit (`OPERATIONS_GUIDE.md` §4.11): GPU ≥ 24 GB VRAM, NVIDIA-Treiber
      bzw. Container Toolkit auf dem Ollama-Host, `OLLAMA_NUM_PARALLEL=1`, `OLLAMA_KEEP_ALIVE=30m`
      **auf dem Ollama-Host gesetzt** (nicht in `.env.local`; Kontrolle `journalctl -u ollama | grep
      "server config"`), keine weitere eKI-Instanz (Stage/Entwicklung) auf derselben Instanz,
      Port 11434 nur für die eKI-Hosts erreichbar; Ollama-Version dokumentiert
      (`curl -s http://<ollama-host>:11434/api/version`) und der Pin in `docker-compose.ollama.yml`
      (derzeit `0.33.3`) darauf abgeglichen (IT)
- [ ] Datenschutz-Punkte aus `docs/DATENSCHUTZ.md` §8 geklärt, mindestens: Retention der
      Temporal-Historie gesetzt, zeitliche Log-Aufbewahrung (zentrale Weiterleitung) entschieden,
      GHCR-Images ≤ 1.0.0 (enthalten echte Drehbücher) gelöscht bzw. Zugriff beschränkt (DS/IT)
- [ ] Bestehende Installation (Update von 1.0.0): Checkliste `OPERATIONS_GUIDE.md` §4.10
      „Upgrade 1.0.0 → 1.0.1“ abgearbeitet – insbesondere drainen und das Redis-Volume leeren
      (Redis 7.4 → 7.2), externe Ollama-URL, Temporal neu gestartet (Dynamic Config), Migration (IT/eKI)
- [ ] Fail-closed-Abstimmung mit ePro (zurückgestellt) terminiert; bis dahin gilt Abschnitt E (ePro/eKI)

## B. Konfiguration & Secrets (IT)

- [ ] `.env.local` aus `.env.example` erstellt; `ENV=production`, `DEBUG=false`
- [ ] Echte Secrets erzeugt und eingetragen – der Validator lehnt nur Platzhalter
      (`change_me`, `replace-me` …) und Werte unter 32 Zeichen ab, keine sonstigen schwachen Werte:
      `python3 scripts/generate_secrets.py` schreibt `secrets/db_password.txt`,
      `secrets/api_secret_key.txt` und `secrets/database_url.txt`. Das DB-Passwort **identisch**
      in `POSTGRES_PASSWORD`, `POSTGRES_PWD` (Temporal) und `DATABASE_URL` übernehmen, den Schlüssel
      in `API_SECRET_KEY` (Details: `secrets/README.md`)
- [ ] `API_SECRET_KEY` zusätzlich sicher hinterlegt (Passwort-Manager): Er verschlüsselt Buffer,
      KB-Originaltexte und die Temporal-Payloads; ein späterer Wechsel macht diese unlesbar. API und
      Worker nutzen denselben Wert
- [ ] `OLLAMA_BASE_URL=http://<ollama-host>:11434` (externe Instanz; ggf. zusätzlich
      `LOCAL_MISTRAL_BASE_URL`) – der Platzhalter aus `.env.example` löst nicht auf
- [ ] `LOCAL_MISTRAL_MODEL=mistral-small3.2`, **kein** `MISTRAL_API_KEY`,
      `LLM_ALLOW_EXTERNAL_PROVIDERS` nicht gesetzt. `LLM_PROVIDER` setzt das Prod-Overlay selbst auf
      `local_mistral`; ein Eintrag in `.env.local` wirkt dort **nicht** (nur Shell bzw. `.env`)
- [ ] Embedding-Modell entschieden, **bevor** die KB befüllt wird: Standard
      `OLLAMA_EMBEDDING_MODEL=mxbai-embed-large` mit `OLLAMA_EMBEDDING_MAX_CHARS=1000`; alternativ
      `bge-m3` mit `OLLAMA_EMBEDDING_MAX_CHARS=30000` (beide 1024 Dimensionen). Ein späterer Wechsel
      erfordert, die KB zu leeren und neu einzuspielen (Vektoren nicht vergleichbar)
- [ ] `EPRO_BASE_URL` = Prod-ePro, `EPRO_WEBHOOK_URL` (falls ePro den Webhook anbietet);
      prüfen, ob der feste Hosts-Eintrag `staging.epro.filmakademie.de:193.196.129.23`
      (`extra_hosts` in `docker-compose.yml`) in Produktion gewünscht ist
- [ ] `CORS_ORIGINS`, `TRUST_PROXY_HEADERS=true` + `TRUSTED_PROXY_IPS` (nur hinter Reverse-Proxy;
      sonst teilen sich alle Clients das Rate Limit der Proxy-IP)
- [ ] `TEMPORAL_WORKFLOW_EXECUTION_TIMEOUT=36000`, `BUFFER_TTL_SECONDS=21600`
- [ ] Dokument-Limit passend zur Ollama-Instanz: `MAX_CONCURRENT_DOCUMENTS=1` bei einer GPU mit
      `OLLAMA_NUM_PARALLEL=1`, `DOCUMENT_QUEUE_TIMEOUT_SECONDS=7200` (`OPERATIONS_GUIDE.md` §4.2, §5.12)
- [ ] `CALLBACK_URL_ALLOWED_HOSTS` mit ePro abgestimmt (Standard: `epro.filmakademie.de`,
      `staging.epro.filmakademie.de`), `TEMPORAL_PRIORITY_LEVELS=10` passend zu
      `matching.priorityLevels` der Dynamic Config
- [ ] Aufbewahrung: `METADATA_RETENTION_DAYS=90` (bzw. vom Datenschutz festgelegter Wert),
      `METADATA_CLEANUP_CRON`/`KB_CLEANUP_CRON` als 5-Felder-Cron in Anführungszeichen (DS/IT)
- [ ] `KB_RETRIEVAL_ENABLED` erst `true`, wenn SB die KB-Inhalte freigegeben hat
- [ ] Compose-Variablen in der Shell oder in einer `.env` im Projektverzeichnis gesetzt
  (Docker Compose liest sie **nicht** aus `.env.local`), zum Beispiel:

  ```bash
  export COMPOSE_FILE=docker-compose.yml:docker-compose.prod.yml
  export GHCR_OWNER=<github-owner, kleingeschrieben> EKI_IMAGE_TAG=<Release-Tag>
  export GRAFANA_ADMIN_PASSWORD='<starkes Passwort>'   # sonst gilt wörtlich CHANGE_ME
  ```

  Mit `COMPOSE_FILE` gelten alle folgenden `docker compose …`-Befehle für den Prod-Stack.

## C. Deployment (IT/eKI)

Reihenfolge einhalten – der Prometheus-Key braucht eine laufende API, das
Observability-Overlay braucht die Key-Datei.

- [ ] Basis-Stack ohne Observability starten:
      `docker compose pull api worker && docker compose up -d --no-build`
- [ ] `docker compose exec api alembic upgrade head`, danach
      `docker compose exec api alembic current` → `c4e6a8b0d2f1 (head)`
      (Migrationskette: `OPERATIONS_GUIDE.md`, Abschnitt „Datenbank-Migrationen“)
- [ ] Modelle auf der externen Ollama-Instanz laden (auf dem Ollama-Host):
      `ollama pull mistral-small3.2` und das gewählte Embedding-Modell
      (`ollama pull mxbai-embed-large` bzw. `ollama pull bge-m3`)
- [ ] Erreichbarkeit der Ollama-Instanz aus dem Worker geprüft (`/ready` prüft Ollama nicht):
      `docker compose exec worker python -c "import os,httpx;print(httpx.get((os.environ.get('LOCAL_MISTRAL_BASE_URL') or os.environ['OLLAMA_BASE_URL'])+'/api/tags',timeout=10).status_code)"` → `200`
- [ ] Temporal-Dynamic-Config aktiv (`matching.useNewMatcher: true`, `matching.priorityLevels: 10`;
      nach Änderung `docker compose restart temporal`); bei einem externen Temporal-Cluster dort gesetzt
- [ ] API-Keys erzeugen (interaktiv: `user_id`, Name, Gültigkeit; Standard 365 Tage), Klartext
      sicher übergeben – technische `user_id`s verwenden (`DATENSCHUTZ.md` §6):
      `docker compose exec api python scripts/create_api_key.py --insert` (ePro-Service-Key),
      `… --insert --admin` (Ops-/KB-Key; `/v1/kb/*` nur mit Admin-Key), `… --insert` (Prometheus-Scraper-Key)
- [ ] Prometheus-Key als `secrets/prometheus_api_key.txt` ablegen (ohne Zeilenumbruch, lesbar für
      den Container-Nutzer `nobody`, siehe `secrets/README.md`) – **bevor** das Overlay startet;
      fehlt die Datei, legt Docker an ihrer Stelle ein Verzeichnis an
- [ ] Observability-Overlay starten:
      `COMPOSE_FILE=docker-compose.yml:docker-compose.prod.yml:docker-compose.observability.yml docker compose up -d --no-build`
- [ ] Reverse-Proxy/TLS für `eki-api:8000` (Pflichtenheft §5 „durchgehend verschlüsselt (TLS)")
- [ ] Firewall: eingehend nur ePro → eKI:443; Port 8000 (HTTP, alle Interfaces) nur für den
      Reverse-Proxy – Docker umgeht ufw/firewalld, Regeln daher in der Kette `DOCKER-USER`; mit
      Overlay auch Grafana 3000 und Jaeger 16686 (ohne Anmeldung) beschränken. Ausgehend vom eKI-Host
      nur ePro, GitHub (Checkout), GHCR (bei privaten Paketen `docker login ghcr.io`), Docker Hub
      (PostgreSQL, Redis, Temporal, Observability – oder eigener Mirror) und die Ollama-Instanz
      (`<ollama-host>:11434`); die Ollama-Registry nur vom Ollama-Host; `api.mistral.ai` blockiert
- [ ] Container-Logs: Das Prod-Overlay rotiert nach Größe (20 MB × 5 je Container); ist eine
      zeitliche Frist gefordert, Weiterleitung an einen zentralen Log-Speicher eingerichtet
      (`DATENSCHUTZ.md` §3.4)

## D. Verifikation (eKI)

Host-Voraussetzung für die Skripte: Python ≥ 3.11 und `pip install httpx` (bzw. die
Dev-Abhängigkeiten, `docs/TESTING_GUIDE.md`).

- [ ] `curl -s localhost:8000/health` → `version` = Release-Version
- [ ] `docker compose ps`: nur `eki-api` veröffentlicht einen Host-Port (8000; mit Overlay
      zusätzlich Grafana 3000 und Jaeger 16686)
- [ ] `EKI_API_URL=http://localhost:8000 EKI_API_KEY=eki_… python scripts/smoke_prod.py` → `RESULT: PASSED`
- [ ] Paritätslauf im API-Container (Golden-Set ist nicht im Image enthalten):

  ```bash
  docker compose cp tests/parity/golden_scenes.yaml api:/tmp/golden_scenes.yaml
  docker compose exec api python scripts/run_parity.py --providers local_mistral \
      --golden /tmp/golden_scenes.yaml --out /tmp/parity
  docker compose cp api:/tmp/parity ./tests/reports/
  ```

  → alle Schwellen ✅ (Tabelle nach `M11_PARITY_REPORT.md` übernehmen)
- [ ] Grafana `eki-overview` zeigt API/Worker up; Alertmanager-Testalarm empfangen
- [ ] Temporal-Schedules `eki-kb-cleanup` und `eki-metadata-cleanup` existieren und sind aktiv:
      `docker compose logs worker | grep "Schedule eki-"` → `created`, `updated` bzw. `up to date`,
      `paused=False`; kein `schedule bootstrap failed`
- [ ] Payload-Verschlüsselung aktiv: `docker compose logs worker | grep "payload encryption active"`
      → Key-ID `eki-v1-…`, identisch mit der Key-ID der API (`OPERATIONS_GUIDE.md` §8.1)
- [ ] Retention des Temporal-Namespaces geprüft und gesetzt (`DATENSCHUTZ.md` §3.3,
      `OPERATIONS_GUIDE.md` §5.11)
- [ ] Log-Stichprobe: JSON-Format, `request_id` vorhanden, keine Inhalte

## E. ePro-Integration (ePro)

- [ ] Service-Key im ePro hinterlegt; `delivery`-Modus (push/pull) festgelegt (Pflichtenheft §4.2)
- [ ] `Idempotency-Key`-Header wird ePro-seitig dedupliziert (Guide §4b)
- [ ] Jobs mit Status `failed`/`cancelled` und ein 410 beim Report-Abruf werden als
      „keine automatische Bewertung – manuelle Prüfung" angezeigt, nie als „kein Risiko" (Guide §3, §4)
- [ ] Polling-Timeout ≥ 4,5 h (Warteschlange bis 2 h + Verarbeitung bis 2 h) und Polling-Intervall
      passend zum Rate Limit (Guide §3, §8); `pending` kann „wartet auf Verarbeitungsplatz“ bedeuten
- [ ] Webhook-Endpunkt für `security.delivery.failed` bereit (optional); Grund
      `invalid_callback_url` bekannt
- [ ] Falls ePro `callback_url` nutzt: Callback-Host steht in `CALLBACK_URL_ALLOWED_HOSTS`, löst
      öffentlich auf, hat ein gültiges Zertifikat und nimmt denselben Multipart-Vertrag an wie
      `set-risk-assessment` (Guide §4b); ePro nutzt nur `check:async` (der Sync-Endpunkt ist entfernt)
- [ ] Erster produktiver Upload gemeinsam beobachtet (AT1 in Prod), Report im ePro sichtbar

## F. Betrieb (IT)

- [ ] Backup-Job `pg_dump` täglich eingerichtet (`eki_db`, `temporal`, `temporal_visibility`;
      Redis nicht sichern). Für ein Restore der KB und der Temporal-Payloads wird derselbe
      `API_SECRET_KEY` benötigt; Aufbewahrungsfrist der Backups festgelegt
- [ ] Dead Letters werden innerhalb von 90 Tagen bearbeitet (danach löscht sie
      `eki-metadata-cleanup`, quittiert oder nicht; `OPERATIONS_GUIDE.md` §5.10)
- [ ] Support-Status von Redis 7.2 im Betriebskalender (halbjährlich prüfen, Ersatz Valkey bzw.
      Redis ≥ 8 einplanen; `OPERATIONS_GUIDE.md` §4.9)
- [ ] Alert-Receiver (Mail/Teams) in `docker/observability/alertmanager.yml` konfiguriert
- [ ] Runbooks bekannt: `M10_FAILOVER_RUNBOOK.md`, Runbooks und Troubleshooting im `OPERATIONS_GUIDE.md`
- [ ] Key-Rotationsplan (jährlich, Ablauf nach 365 Tagen) und Ansprechpartner dokumentiert
- [ ] Brückenmeeting-Rhythmus (14 Tage) für die ersten 3 Monate bestätigt (Pflichtenheft §13)

## G. Fachliche Inbetriebnahme (SB)

- [ ] Echte KB-Dokumente (Leitfäden, Stunt-SOPs, Checklisten) in `config/kb_seed/real/` geliefert
      (git-ignoriert und per `.dockerignore` vom Image-Build ausgeschlossen; nach dem Einspielen
      trotzdem dort entfernen)
- [ ] KB eingespielt (vom Admin-Rechner, `EKI_API_URL` gesetzt, `EKI_API_KEY` = **Admin-Key**,
      `pip install requests`): `python scripts/seed_kb.py --wipe-placeholders`, dann `--reseed`,
      dann `--status` geprüft
- [ ] Taxonomie/Maßnahmenkatalog (`config/taxonomy/*.yaml`) fachlich freigegeben
- [ ] `KB_RETRIEVAL_ENABLED=true` gesetzt, `docker compose up -d --force-recreate api worker`
      (ein `restart` liest `.env.local` nicht neu ein), Stichprobe eines Reports gesichtet

Go-Live freigegeben am: __________  durch: __________ (IT) / __________ (eKI) / __________ (ePro)
