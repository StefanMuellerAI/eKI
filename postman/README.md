# eKI API - Postman Collection v0.5

Diese Postman Collection enthält alle API-Endpunkte der eKI API mit vollständiger LLM-Risikoanalyse.

## Import

1. Öffne Postman
2. Click auf **Import**
3. Wähle `eKI-API-v0.5.postman_collection.json`
4. Click **Import**

## Setup

### 1. Services starten

```bash
docker compose up -d

# Migrationen ausführen
docker compose exec api alembic upgrade head
```

### 2. API-Key erstellen

```bash
docker compose exec api python scripts/create_api_key.py --insert
```

### 3. Variablen setzen

In Postman, gehe zu **Variables** Tab und setze:

| Variable | Wert | Beschreibung |
|----------|------|--------------|
| `BASE_URL` | `http://localhost:8000` | API Base URL |
| `API_KEY` | `eki_your_key_here` | API Key von Schritt 2 |
| `API_KEY_USER2` | `eki_second_key` | Zweiter Key (anderer user_id) für IDOR-Tests |

`JOB_ID`, `REPORT_ID` und `SCRIPT_B64` werden automatisch durch Pre-Request und Test Scripts gesetzt.

## Collection-Struktur

### 1. Health & System
- **Health Check** - Liveness Probe (kein Auth)
- **Readiness Check** - DB, Redis, Temporal Status (Auth)
- **Root Endpoint** - API-Info (kein Auth)
- **Metrics** - Prometheus Metrics (Auth)

### 2. FDX Workflow (Async) - Echte LLM-Pipeline
- **Submit FDX Script** - 4-Szenen Stunt-Script (Base64/JSON)
- **Poll Job Status** - Warten bis completed
- **Fetch Report (One-Shot!)** - JSON + PDF Report
- **Verify One-Shot** - Zweiter Abruf = 410 Gone

### 3. PDF Workflow (Async) - Echte LLM-Pipeline
- **Submit PDF Script** - Multipart Datei-Upload
- **Poll PDF Job Status** - Warten bis completed
- **Fetch PDF Report (One-Shot!)** - JSON + PDF Report

### 4. Sync Check (Stub)
- **Sync FDX Check** - Sofortige Stub-Response (kein LLM)
- **Sync PDF Check** - Sofortige Stub-Response (kein LLM)

> **Hinweis:** Der synchrone Endpoint liefert aktuell eine M01-Stub-Response.
> Für echte LLM-basierte Risikoanalyse den **async Endpoint** verwenden (Sektion 2 und 3).

### 5. Security Tests
- **Missing Auth (401)** - Request ohne Authorization Header
- **Invalid API Key (401)** - Ungültiger API Key
- **Invalid Base64 (422)** - Ungültiges Base64 in script_content
- **SQL Injection (422)** - SQL Injection in project_id
- **SSRF Private IP (422)** - Callback URL mit privater IP
- **SSRF Non-Whitelisted Domain (422)** - Callback URL mit nicht-whitelisted Domain
- **IDOR Other User's Job (404)** - Zugriff auf fremden Job (benötigt API_KEY_USER2)
- **Idempotency (Same Key)** - Gleicher idempotency_key = gleicher Job

### 6. Documentation
- **Swagger UI** - Interaktive Docs (nur Development)
- **OpenAPI Spec** - OpenAPI 3.1 JSON

## Testing Workflows

### Happy Path (FDX)
1. **Submit FDX Script (Async)** -> 202 mit job_id (automatisch gespeichert)
2. **Poll Job Status** -> wiederholen bis status=completed
3. **Fetch Report** -> JSON mit Findings + PDF als Base64
4. **Verify One-Shot** -> 410 Gone bestätigt Löschung

### Happy Path (PDF)
1. **Submit PDF Script (Multipart)** -> PDF-Datei auswählen, 202
2. **Poll PDF Job Status** -> wiederholen (PDF dauert länger wegen LLM-Strukturierung)
3. **Fetch PDF Report** -> JSON + PDF

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
| IDOR Prevention | Fremder Job | 404 |
| One-Shot Reports | Zweiter Abruf | 410 |
| Idempotenz | Gleicher Key | Gleiche job_id |
| Rate Limiting | 60/min IP, 1000/h Key | 429 |

---

**Version:** 0.5.0
**Last Updated:** 2026-02-11
**Status:** Production Ready
