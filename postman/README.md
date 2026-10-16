# eKI API - Postman Collection v1.0

Diese Postman Collection enthält alle API-Endpunkte der eKI API v1.0.1: Security-Checks (FDX/PDF, Push/Pull), Knowledge Base (M06), Operations/Dead Letters (M10) und Schnellnachweise für die Abnahmetests (M12).

## Import

1. Öffne Postman
2. Click auf **Import**
3. Wähle `eKI-API-v1.0.postman_collection.json` **und** `eKI-API.postman_environment.json`
4. Click **Import**, dann oben rechts das Environment „eKI API - local" auswählen

## Setup

### 1. Services starten

Lokaler Stack gemäß Schnellstart im `README.md` (`.env.local` aus `.env.example`):

```bash
docker compose up -d

# Migrationen ausführen (Head: b8d0f2a4c6e3)
docker compose exec api alembic upgrade head

# LLM-Modell laden – ohne erreichbares Modell enden asynchrone Jobs mit "failed"
# (die Risikoanalyse ist fail-closed)
docker compose exec ollama ollama pull mistral-small3.2
```

**Push-Tests nur mit ePro-Mock:** Ohne `callback_url` geht der Push an `EPRO_BASE_URL` aus der
eKI-Konfiguration (`.env.local`). In der Vorlage zeigt `EPRO_BASE_URL` auf das **echte Stage-ePro**.
Vor „Submit FDX Script (Push Delivery)" deshalb `EPRO_BASE_URL`/`EPRO_WEBHOOK_URL` auf den Mock
umstellen und die Container neu erzeugen (`docs/UAT/UAT_TESTPLAN.md` §2.3).

**`callback_url` (seit 1.0.1):** Ein `callback_url` im Request ist das Push-Ziel dieses Jobs (statt
`EPRO_BASE_URL/...`); ohne `delivery` gilt dann `push`, `delivery=pull` + `callback_url` → 422. Es muss
HTTPS sein, auf einem Host aus `CALLBACK_URL_ALLOWED_HOSTS` liegen (Standard: `epro.filmakademie.de`,
`staging.epro.filmakademie.de`) und öffentlich auflösen – auf den lokalen HTTP-Mock kann es daher
**nicht** zeigen. Die Collection enthält deshalb nur die Validierungs-Tests (Ordner 5), keinen echten
Callback-Push.

### 2. API-Key erstellen

```bash
docker compose exec api python scripts/create_api_key.py --insert          # API_KEY
docker compose exec api python scripts/create_api_key.py --insert          # API_KEY_USER2 (andere user_id)
docker compose exec api python scripts/create_api_key.py --insert --admin  # ADMIN_API_KEY
```

### 3. Variablen setzen

In Postman, gehe zu **Variables** Tab und setze:

| Variable | Wert | Beschreibung |
|----------|------|--------------|
| `BASE_URL` | `http://localhost:8000` | API Base URL |
| `API_KEY` | `eki_your_key_here` | API Key von Schritt 2 |
| `API_KEY_USER2` | `eki_second_key` | Zweiter Key (anderer user_id) für IDOR-Tests |
| `ADMIN_API_KEY` | `eki_admin_key` | Key mit `is_admin=true` (`--admin`) für Ordner 9 |

`JOB_ID`, `REPORT_ID`, `SCRIPT_B64`, `DOC_ID`, `DEAD_LETTER_ID`, `UAT_*` werden automatisch durch Pre-Request und Test Scripts gesetzt. Die Environment-Datei enthält alle Schlüssel mit Platzhaltern (`eki_REPLACE_ME`).

## Collection-Struktur

### 1. Health & System
- **Health Check** - Liveness Probe (kein Auth)
- **Readiness Check** - DB, Redis, Temporal Status (Auth)
- **Root Endpoint** - API-Info (kein Auth)
- **Metrics** - Prometheus Metrics (Auth)

### 2. PDF Workflow (Async) - Synthetic Fixtures (Primär)
Ausschließlich synthetische Test-Drehbücher aus `tests/fixtures/pdf/` (echte Drehbücher sind nicht
mehr im Repository; eigene Dateien nur mit geklärten Rechten, `docs/DATENSCHUTZ.md`):
- **Submit: Einfaches Drehbuch** - `simple_screenplay.pdf`
- **Submit: Deutsches Drehbuch** - `german_screenplay.pdf`
- **Submit: Mehrere Szenen** - `multi_scene.pdf`
- **Submit: Eigene PDF** - Freie Dateiauswahl (z. B. `large_120_pages.pdf`, `scanned_screenplay.pdf` für OCR)
- **Poll Job Status** - Warten bis completed
- **Fetch Report (One-Shot!)** - JSON + PDF Report
- **Verify One-Shot** - Zweiter Abruf = 410 Gone

### 3. FDX Workflow (Async)
- **Submit FDX Script** - 4-Szenen Stunt-Script (inline Base64/JSON)
- **Submit FDX Script (Push Delivery)** - Push an `EPRO_BASE_URL/eki/scl/set-risk-assessment/{project_id}`
  aus der eKI-Konfiguration (ohne `callback_url`). Lokal nur mit ePro-Mock ausführen (siehe Setup)
- **Submit FDX Script (Priority 1 = highest)** - `priority: 1` (1 = höchste … 10 = niedrigste, Standard 5);
  wird als Temporal-Task-Queue-Priorität gesetzt: wartende Jobs höherer Priorität werden zuerst abgearbeitet
- **Poll Job Status** - Warten bis completed
- **Fetch Report (One-Shot!)** - JSON + PDF Report

### 4. (entfallen)
Der synchrone M01-Stub `POST /v1/security/check` wurde in 1.0.1 entfernt (Antwort 404); der frühere
Ordner „4. Sync Check (Stub)" entfällt. Einziger Prüf-Endpunkt ist `POST /v1/security/check:async`.

### 5. Security Tests
- **Missing Auth (401)** - Request ohne Authorization Header
- **Invalid API Key (401)** - Ungültiger API Key
- **Invalid Base64 (422)** - Ungültiges Base64 in script_content
- **SQL Injection (422)** - SQL Injection in project_id
- **SSRF Private IP (422)** - Callback URL mit privater IP (und ohne HTTPS)
- **SSRF Non-Whitelisted Domain (422)** - Callback URL mit Host außerhalb von `CALLBACK_URL_ALLOWED_HOSTS`
- **callback_url with delivery=pull (422)** - Widerspruch: `callback_url` ist ein Push-Ziel
- **Removed sync endpoint (404)** - `POST /v1/security/check` existiert nicht mehr
- **IDOR Other User's Job (404)** - Zugriff auf fremden Job (benötigt API_KEY_USER2)
- **Idempotency (Same Key)** - Gleicher idempotency_key = gleicher Job

### 6. Automated PDF Flow (Collection Runner)
- **Step 1: Submit PDF Script** - PDF aus Fixtures hochladen, setzt JOB_ID
- **Step 2: Poll Until Complete** - Pollt in Schleife (5-30s Backoff, max 60 Polls ≈ 27 min – für
  Großdokumente zu kurz, dann manuell weiter pollen)
- **Step 3: Fetch Report** - Holt Report ab, validiert Findings + PDF

> **Nutzung:** Einmalig PDF-Datei in Step 1 auswählen, dann Rechtsklick auf Ordner → "Run folder" → "Run".

### 7. Documentation
- **Swagger UI** - Interaktive Docs (nur bei `DEBUG=true`)
- **ReDoc** - Alternative Docs im ReDoc-Stil (nur bei `DEBUG=true`)
- **OpenAPI Spec** - von FastAPI generiertes OpenAPI-JSON (nur bei `DEBUG=true`); verbindlich ist
  `openapi/eki-api-v1.0.yaml`

### 8. Knowledge Base (M06, Admin-Key)
Alle Requests nutzen `ADMIN_API_KEY` – seit 1.0.1 verlangen alle `/v1/kb/*`-Endpunkte einen Key mit
`is_admin=true` (`scripts/create_api_key.py --insert --admin`).
- **Upload KB document (Markdown)** - Multipart Upload (PDF/MD/TXT), liefert `doc_id`
- **List KB documents** - Alle KB-Dokumente, optional gefiltert nach `tag`
- **List KB documents filtered by tag** - z.B. `?tag=placeholder` für Seed-Inhalte
- **Get KB document metadata** - Einzelmetadaten (kein Originaltext)
- **Delete a KB document** - Einzel-Delete, Chunks cascaden
- **Wipe placeholders (bulk delete by tag)** - `?tag=placeholder` löscht nur Seed-Daten
- **Test: Non-admin key is 403** - Normaler Key (`API_KEY`) wird abgewiesen

> **Feature-Flag:** Die KB-Endpunkte sind immer verfügbar (nur mit Admin-Key). Ob KB-Treffer in die
> Risikoanalyse einfließen, steuert `KB_RETRIEVAL_ENABLED` in `.env.local`
> (Default `false`). Uploads brauchen ein Ollama-Embedding-Modell; mit
> `LLM_PROVIDER=mistral_cloud` schlagen sie fehl.

### 9. Operations (M10, Admin-Key)
- **Summary** - Jobs nach Status/Zustellstatus, offene Dead Letters
- **List Jobs** - Job-Übersicht ohne Inhalte, Filter `status`/`delivery_status`/`project_id`
- **List Dead Letters (open)** - setzt `DEAD_LETTER_ID`
- **Get / Acknowledge Dead Letter** - Einzelansicht, Quittierung (409 bei Doppelquittung)
- **Test: Non-admin key is 403** - Normaler Key wird abgewiesen

### 10. Abnahmetests (M12)
- **AT5 Idempotenz** - zwei Aufrufe mit gleichem Key liefern dieselbe `job_id`
- **AT3 Job-Status** - `metadata.delivery_status` / `delivery_attempts` vorhanden
- **AT7 Fehlerformat** - `ErrorResponse` (`error`, `message`, `details`, `request_id`, `timestamp`) + `X-Request-ID`
  (`request_id` ist immer gesetzt und gleich dem Header)

Die Fetch-Report-Requests prüfen zusätzlich den Header `X-One-Shot: true`.
Vollständiger Testplan: `docs/UAT/UAT_TESTPLAN.md`; die Abnahmetests 2–7 automatisiert
`scripts/uat/run_acceptance_tests.py` (Test 6 nur mit `--with-large-document`).

> **Reihenfolge der Ordner:** In der Collection-Datei stehen die Ordner in der Reihenfolge
> 1, 2, 3, 5, **8**, 6, 7, 9, 10 (Ordner 4 entfällt seit 1.0.1); Postman zeigt sie so an. Maßgeblich
> ist die Nummer im Ordnernamen.

## Testing Workflows

### Happy Path (PDF - Empfohlen)
1. In Sektion 2 eine **Submit**-Anfrage wählen (z.B. "Einfaches Drehbuch (simple_screenplay.pdf)")
2. Senden -> 202 mit job_id (automatisch gespeichert)
3. **Poll Job Status** -> wiederholen bis status=completed
4. **Fetch Report** -> JSON mit Findings + PDF als Base64
5. **Verify One-Shot** -> 410 Gone bestätigt Löschung

### Happy Path (FDX)
1. **Submit FDX Script (Async)** in Sektion 3 -> 202 mit job_id
2. **Poll Job Status** -> wiederholen bis status=completed
3. **Fetch Report** -> JSON + PDF

### Automated PDF Flow (Vollautomatisch)
1. In **"6. Automated PDF Flow"** → Step 1 öffnen und PDF auswählen
2. Rechtsklick auf den Ordner → **Run folder**
3. Delay auf **0ms** lassen (Wartezeiten sind im Script eingebaut)
4. **Run** klicken -- der Flow macht alles automatisch:
   - Schickt PDF ab → pollt in Schleife → holt Report ab
   - Konsole zeigt Fortschritt, Findings und PDF-Größe

### Security Test Suite
1. Select Ordner **"5. Security Tests"**
2. Rechtsklick -> **Run folder**
3. Alle Tests sollten die erwarteten Error-Codes liefern

## Sicherheitsfeatures

| Feature | Test | Erwartung |
|---------|------|-----------|
| Auth erforderlich | Missing Auth | 401 |
| Key-Validierung | Invalid API Key | 401 |
| Input-Validierung | Invalid Base64 | 422 |
| SQL Injection Schutz | SQL in project_id | 422 |
| SSRF Prevention | Private IP / Non-Whitelisted | 422 |
| Push-Ziel eindeutig | callback_url + delivery=pull | 422 |
| KB nur für Admins | KB mit normalem Key | 403 |
| Sync-Stub entfernt | POST /v1/security/check | 404 |
| IDOR Prevention | Fremder Job | 404 |
| One-Shot Reports | Zweiter Abruf | 410 |
| Idempotenz | Gleicher Key | Gleiche job_id |
| Rate Limiting | 60/min je Quell-IP, 1000/h je Key | 429 |

---

**Version:** 1.0.1
**Last Updated:** 2026-10-16
**Status:** Release (M12 UAT-Paket)
