# Secrets-Verzeichnis

Ablage für lokal erzeugte Geheimnisse. Alles hier außer dieser README ist
per `.gitignore` ausgeschlossen (`secrets/*.txt`, `*.key`, `*.pem`) – nie committen.

## 1. Standardweg: Secrets in `.env.local`

Die Compose-Dateien lesen Secrets aus `.env.local` (`env_file` von `api`, `worker`,
`postgres`, `temporal`). Kein Compose-File mountet dieses Verzeichnis in `api`/`worker`
(einzige Ausnahme: `prometheus_api_key.txt` im Observability-Overlay, §3).

```bash
cp .env.example .env.local
python3 scripts/generate_secrets.py        # schreibt secrets/db_password.txt,
                                           # secrets/api_secret_key.txt, secrets/database_url.txt
chmod 600 secrets/*.txt                    # setzt das Skript bereits, schadet nicht

# Werte in .env.local übernehmen. Das DB-Passwort MUSS in POSTGRES_PASSWORD,
# POSTGRES_PWD (Temporal) und DATABASE_URL identisch sein:
PW=$(cat secrets/db_password.txt); KEY=$(cat secrets/api_secret_key.txt)
sed -i.bak -e "s/CHANGE_ME_STRONG_PASSWORD_HERE/$PW/g" \
           -e "s/CHANGE_ME_256_BIT_SECRET_KEY_HERE/$KEY/" .env.local && rm .env.local.bak
grep -n CHANGE_ME .env.local              # danach nur noch Kommentarzeilen
```

Hinweise:

- `POSTGRES_PASSWORD` wirkt nur bei der **ersten** Initialisierung des Volumes
  `postgres_data`. Ein späterer Wechsel braucht zusätzlich `ALTER USER`
  (`docs/OPERATIONS_GUIDE.md` §5.9).
- In `ENV=production` lehnt der Start-Validator (`api/config.py`) Platzhalter wie
  `CHANGE_ME`/`replace-me` in `API_SECRET_KEY` und `DATABASE_URL` sowie Schlüssel
  unter 32 Zeichen ab. Echte Werte trotzdem immer selbst erzeugen.
- `API_SECRET_KEY` ist zugleich Schlüsselmaterial für SecureBuffer und KB-Originale:
  sicher aufbewahren (Passwort-Tresor) und nur nach Runbook rotieren.

## 2. Optional: `*_FILE`-Variablen (Docker-Secret-Muster)

`api/config.py` versteht `DATABASE_URL_FILE`, `API_SECRET_KEY_FILE`,
`EPRO_AUTH_TOKEN_FILE`, `EPRO_WEBHOOK_URL_FILE` und `MISTRAL_API_KEY_FILE`. Ist eine
davon gesetzt, wird der Wert aus der Datei gelesen und hat Vorrang vor der
Klartext-Variable. Voraussetzungen:

- Die Datei muss **im Container** existieren – den Mount musst Du selbst ergänzen.
  Ein gesetzter `*_FILE`-Pfad ohne (lesbare, nicht leere) Datei bricht den Start ab.
- Der Container läuft als Nutzer `eki` (UID 1000): die Dateien müssen für UID 1000
  lesbar sein.
- `postgres` und `temporal` brauchen das DB-Passwort weiterhin als
  `POSTGRES_PASSWORD`/`POSTGRES_PWD` in `.env.local`; `*_FILE` entlastet also vor allem
  `API_SECRET_KEY`.
- `scripts/create_api_key.py` liest nur `DATABASE_URL` (kein `_FILE`). Ohne
  `DATABASE_URL` im Container: `docker compose exec -e DATABASE_URL="$(cat secrets/database_url.txt)" api python scripts/create_api_key.py --insert`.

Beispiel `docker-compose.secrets.yml` im Projektverzeichnis (wird nicht automatisch
geladen, also bei **jedem** Compose-Aufruf zusätzlich angeben):

```yaml
services:
  api:
    volumes:
      - ./secrets/api_secret_key.txt:/run/secrets/api_secret_key:ro
      - ./secrets/database_url.txt:/run/secrets/database_url:ro
    environment:
      - API_SECRET_KEY_FILE=/run/secrets/api_secret_key
      - DATABASE_URL_FILE=/run/secrets/database_url
  worker:
    volumes:
      - ./secrets/api_secret_key.txt:/run/secrets/api_secret_key:ro
      - ./secrets/database_url.txt:/run/secrets/database_url:ro
    environment:
      - API_SECRET_KEY_FILE=/run/secrets/api_secret_key
      - DATABASE_URL_FILE=/run/secrets/database_url
```

```bash
sudo chown 1000 secrets/api_secret_key.txt secrets/database_url.txt   # UID des Containers
docker compose -f docker-compose.yml -f docker-compose.prod.yml -f docker-compose.secrets.yml config -q
docker compose -f docker-compose.yml -f docker-compose.prod.yml -f docker-compose.secrets.yml up -d --no-build
```

Die Reihenfolge ist wichtig: Die Datei muss **nach** `docker-compose.prod.yml` kommen,
weil das Prod-Overlay die Volumes von `api`/`worker` mit `!reset []` leert.

## 3. Observability (M09): Prometheus-Scraper-Key

Prometheus liest den API-Key-geschützten `/metrics`-Endpoint der API. Dafür einen
eigenen Key anlegen und hier ablegen – **bevor** das Observability-Overlay gestartet
wird. Fehlt die Datei beim `up`, legt Docker an ihrer Stelle ein leeres Verzeichnis an
(dann löschen und neu starten). Der Key braucht den laufenden Basis-Stack mit
migrierter Datenbank:

```bash
docker compose up -d && docker compose exec api alembic upgrade head
docker compose exec api python scripts/create_api_key.py --insert   # Key Name: prometheus-scraper
# ausgegebenen eki_...-Key ohne Zeilenumbruch speichern
printf '%s' 'eki_...' > secrets/prometheus_api_key.txt
sudo chown 65534 secrets/prometheus_api_key.txt && sudo chmod 600 secrets/prometheus_api_key.txt
docker compose -f docker-compose.yml -f docker-compose.observability.yml up -d
```

`docker-compose.observability.yml` mountet die Datei read-only als
`/etc/prometheus/api_key.txt` in den Prometheus-Container. Prometheus läuft dort als
Nutzer `nobody` (UID 65534); unter Linux muss die Datei für diese UID lesbar sein
(daher `chown 65534`), sonst schlägt der Scrape von `eki-api` fehl (Prometheus-UI →
Targets). Unter Docker Desktop (macOS/Windows) fällt das meist nicht auf, weil die
Dateifreigabe Besitzrechte anders abbildet.

## Dateien

| Datei | Inhalt | Erzeugt von |
|---|---|---|
| `db_password.txt` | PostgreSQL-Passwort | `scripts/generate_secrets.py` |
| `api_secret_key.txt` | `API_SECRET_KEY` | `scripts/generate_secrets.py` |
| `database_url.txt` | vollständige `DATABASE_URL` (für `DATABASE_URL_FILE`) | `scripts/generate_secrets.py` |
| `prometheus_api_key.txt` | API-Key des Prometheus-Scrapers | manuell (§3) |
