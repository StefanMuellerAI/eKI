# Go-Live-Checkliste – eKI API v1.0.0

Abzuarbeiten nach abgenommenem UAT (`UAT_PROTOCOL_TEMPLATE.md` §5) und vor dem
produktiven Freischalten im eProjekt. Verantwortlichkeiten: **IT** = IT-Betrieb
Filmakademie, **eKI** = Auftragnehmer, **ePro** = eProjekt-Team, **SB** = Sicherheitsbeauftragter,
**DS** = Datenschutz der Filmakademie.

Ausführliche Anleitung: `docs/OPERATIONS_GUIDE.md`; alle Einstellungen: `docs/CONFIGURATION.md`.
`<Release-Tag>` steht im Folgenden für die Version des freigegebenen Images **ohne** führendes
`v` (z. B. `1.0.1`).

## A. Vorbedingungen

- [ ] UAT abgenommen; offene Punkte §4 des Protokolls haben Termin und Verantwortlichen (eKI/IT)
- [ ] Abnahmetests 2–7 wurden auf dem aktuellen Stand (Migrations-Head `b8d0f2a4c6e3`) ausgeführt;
      vor Migration `a7c9e1f3b5d2` scheiterte auf PostgreSQL die Zustellphase (`HANDOVER.md` §1.1) (eKI)
- [ ] Supportvertrag geschlossen (Pflichtenheft §14, spätestens mit M10) (IT/eKI)
- [ ] Release-Tag gesetzt und GHCR-Images vorhanden, Digest im Protokoll. Empfohlen: ein
      fixierter Patch-Release mit den Korrekturen vom 15.10.2026 statt der ursprünglichen
      `v1.0.0`-Images (eKI)
- [ ] Zielhardware: GPU ≥ 24 GB VRAM, NVIDIA Container Toolkit, Docker ≥ 24 mit
      **Docker Compose ≥ 2.24** (das Prod-Overlay nutzt `!reset`) (IT)
- [ ] Datenschutz-Entscheidungen aus `docs/DATENSCHUTZ.md` §8 getroffen, mindestens: echte
      Drehbücher in `tests/fixtures/pdf/` bzw. in den Images, Löschfrist für Metadaten,
      Retention der Temporal-Historie (DS/IT)

## B. Konfiguration & Secrets (IT)

- [ ] `.env.local` aus `.env.example` erstellt; `ENV=production`, `DEBUG=false`
- [ ] Echte Secrets erzeugt und eingetragen – der Validator lehnt nur Platzhalter
      (`change_me`, `replace-me` …) und Werte unter 32 Zeichen ab, keine sonstigen schwachen Werte:
      `python3 scripts/generate_secrets.py` schreibt `secrets/db_password.txt`,
      `secrets/api_secret_key.txt` und `secrets/database_url.txt`. Das DB-Passwort **identisch**
      in `POSTGRES_PASSWORD`, `POSTGRES_PWD` (Temporal) und `DATABASE_URL` übernehmen, den Schlüssel
      in `API_SECRET_KEY` (Details: `secrets/README.md`)
- [ ] `API_SECRET_KEY` zusätzlich sicher hinterlegt (Passwort-Manager): Er verschlüsselt Buffer
      und KB-Originaltexte; ein späterer Wechsel macht diese unlesbar
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
- [ ] `KB_RETRIEVAL_ENABLED` erst `true`, wenn SB die KB-Inhalte freigegeben hat
- [ ] Compose-Variablen in der Shell oder in einer `.env` im Projektverzeichnis gesetzt
  (Docker Compose liest sie **nicht** aus `.env.local`), zum Beispiel:

  ```bash
  export COMPOSE_FILE=docker-compose.yml:docker-compose.prod.yml
  export GHCR_OWNER=<github-owner> EKI_IMAGE_TAG=<Release-Tag>
  export GRAFANA_ADMIN_PASSWORD='<starkes Passwort>'   # sonst gilt wörtlich CHANGE_ME
  ```

  Mit `COMPOSE_FILE` gelten alle folgenden `docker compose …`-Befehle für den Prod-Stack.

## C. Deployment (IT/eKI)

Reihenfolge einhalten – der Prometheus-Key braucht eine laufende API, das
Observability-Overlay braucht die Key-Datei.

- [ ] Basis-Stack ohne Observability starten:
      `docker compose pull api worker && docker compose up -d --no-build`
- [ ] `docker compose exec api alembic upgrade head`, danach
      `docker compose exec api alembic current` → `b8d0f2a4c6e3 (head)`
      (Migrationskette: `OPERATIONS_GUIDE.md`, Abschnitt „Datenbank-Migrationen“)
- [ ] Modelle laden: `docker compose exec ollama ollama pull mistral-small3.2` und das gewählte
      Embedding-Modell (`ollama pull mxbai-embed-large` bzw. `ollama pull bge-m3`)
- [ ] API-Keys erzeugen (interaktiv: `user_id`, Name, Gültigkeit; Standard 365 Tage), Klartext
      sicher übergeben – technische `user_id`s verwenden (`DATENSCHUTZ.md` §6):
      `docker compose exec api python scripts/create_api_key.py --insert` (ePro-Service-Key),
      `… --insert --admin` (Ops-Key), `… --insert` (Prometheus-Scraper-Key)
- [ ] Prometheus-Key als `secrets/prometheus_api_key.txt` ablegen (ohne Zeilenumbruch, lesbar für
      den Container-Nutzer `nobody`, siehe `secrets/README.md`) – **bevor** das Overlay startet;
      fehlt die Datei, legt Docker an ihrer Stelle ein Verzeichnis an
- [ ] Observability-Overlay starten:
      `COMPOSE_FILE=docker-compose.yml:docker-compose.prod.yml:docker-compose.observability.yml docker compose up -d --no-build`
- [ ] Reverse-Proxy/TLS für `eki-api:8000` (Pflichtenheft §5 „durchgehend verschlüsselt (TLS)")
- [ ] Firewall: nur ePro → eKI:443; ausgehend nur ePro, GHCR, Ollama-Registry; `api.mistral.ai` blockiert
- [ ] Log-Rotation für die Container-Logs im Docker-Daemon konfiguriert (`DATENSCHUTZ.md` §3.4)

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
- [ ] Temporal-Schedule `eki-kb-cleanup` existiert:
      `docker compose logs worker | grep "KB cleanup schedule"` → `created` bzw. `already exists`
- [ ] Retention des Temporal-Namespaces geprüft (`DATENSCHUTZ.md` §3.3)
- [ ] Log-Stichprobe: JSON-Format, `request_id` vorhanden, keine Inhalte

## E. ePro-Integration (ePro)

- [ ] Service-Key im ePro hinterlegt; `delivery`-Modus (push/pull) festgelegt (Pflichtenheft §4.2)
- [ ] `Idempotency-Key`-Header wird ePro-seitig dedupliziert (Guide §4b)
- [ ] Jobs mit Status `failed`/`cancelled` und ein 410 beim Report-Abruf werden als
      „keine automatische Bewertung – manuelle Prüfung" angezeigt, nie als „kein Risiko" (Guide §3, §4)
- [ ] Polling-Timeout ≥ 2,5 h und Polling-Intervall passend zum Rate Limit (Guide §3, §8)
- [ ] Webhook-Endpunkt für `security.delivery.failed` bereit (optional)
- [ ] Erster produktiver Upload gemeinsam beobachtet (AT1 in Prod), Report im ePro sichtbar

## F. Betrieb (IT)

- [ ] Backup-Job `pg_dump` täglich eingerichtet (Metadaten und KB; Redis nicht sichern).
      Für ein Restore der KB wird derselbe `API_SECRET_KEY` benötigt
- [ ] Alert-Receiver (Mail/Teams) in `docker/observability/alertmanager.yml` konfiguriert
- [ ] Runbooks bekannt: `M10_FAILOVER_RUNBOOK.md`, Runbooks und Troubleshooting im `OPERATIONS_GUIDE.md`
- [ ] Key-Rotationsplan (jährlich, Ablauf nach 365 Tagen) und Ansprechpartner dokumentiert
- [ ] Brückenmeeting-Rhythmus (14 Tage) für die ersten 3 Monate bestätigt (Pflichtenheft §13)

## G. Fachliche Inbetriebnahme (SB)

- [ ] Echte KB-Dokumente (Leitfäden, Stunt-SOPs, Checklisten) in `config/kb_seed/real/` geliefert
      (git-ignoriert; nach dem Einspielen dort entfernen, da `config/` in die Images kopiert wird)
- [ ] KB eingespielt (vom Admin-Rechner, `EKI_API_URL`/`EKI_API_KEY` gesetzt, `pip install requests`):
      `python scripts/seed_kb.py --wipe-placeholders`, dann `--reseed`, dann `--status` geprüft
- [ ] Taxonomie/Maßnahmenkatalog (`config/taxonomy/*.yaml`) fachlich freigegeben
- [ ] `KB_RETRIEVAL_ENABLED=true` gesetzt, `docker compose up -d --force-recreate api worker`
      (ein `restart` liest `.env.local` nicht neu ein), Stichprobe eines Reports gesichtet

Go-Live freigegeben am: __________  durch: __________ (IT) / __________ (eKI) / __________ (ePro)
