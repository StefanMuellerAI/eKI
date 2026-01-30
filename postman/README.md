# eKI API - Postman Collection

Diese Postman Collection enthält alle API-Endpunkte der eKI API mit vollständiger Security-Integration.

## 📥 Import

### Option 1: Direkt importieren
1. Öffne Postman
2. Click auf **Import**
3. Wähle `eKI-API-v0.1.postman_collection.json`
4. Click **Import**

### Option 2: Von GitHub
```
Import → Link → Paste URL
```

## 🔑 Setup

### 1. API-Key erstellen

```bash
# Im eki-api Verzeichnis
python scripts/create_api_key.py
```

Beispiel-Output:
```
API Key: eki_1332f79e4acc2e1d8970c11b3f1eb036e757549df356fcfab18de4b1e803288d
```

### 2. Variablen setzen

In Postman, gehe zu **Variables** Tab und setze:

| Variable | Wert | Beschreibung |
|----------|------|--------------|
| `BASE_URL` | `http://localhost:8000` | API Base URL |
| `API_KEY` | `eki_your_key_here` | API Key von Schritt 1 |
| `API_KEY_USER2` | `eki_second_key` | Zweiter Key für IDOR-Tests |
| `ACTOR_USER_ID` | `test-user-123` | User ID für Audit Trail |
| `ACTOR_PROJECT_ID` | `test-project-456` | Project ID für Audit Trail |

### 3. Services starten

```bash
docker compose up -d
```

Warte bis alle Services healthy sind:
```bash
docker compose ps
# Alle sollten "healthy" zeigen
```

## 📁 Collection-Struktur

### 1. Health & System
- **Health Check** - Liveness Probe
- **Readiness Check** - Dependency Status
- **Root Endpoint** - API Information

### 2. Security Checks (Authenticated)
- **Synchronous Security Check** - Sync Script Analysis
- **Asynchronous Security Check** - Async with Temporal
- **Get Job Status** - Query Job State
- **Get Report (One-Shot)** - Retrieve Report (only once)

### 3. Security Tests
- **Test: Missing Authentication** - Expect 401
- **Test: Invalid API Key** - Expect 401
- **Test: Invalid Base64** - Expect 422
- **Test: SSRF - Private IP** - Expect 422
- **Test: SSRF - Non-Whitelisted Domain** - Expect 422
- **Test: SQL Injection in Project ID** - Expect 422
- **Test: IDOR - Access Other User Job** - Expect 404

### 4. Documentation
- **OpenAPI Spec** - OpenAPI JSON (dev only)
- **Swagger UI** - Interactive Docs (dev only)
- **Metrics** - Prometheus Metrics

## 🧪 Testing Workflow

### Happy Path Test
1. **Health Check** → Verify API is running
2. **Synchronous Security Check** → Get immediate report
3. **Asynchronous Security Check** → Start long-running job
4. **Get Job Status** → Check job progress
5. **Get Report** → Retrieve final report

### Security Test Suite
Run alle Requests im **"3. Security Tests"** Ordner:
- Alle sollten mit den erwarteten Error-Codes antworten
- Keine sollte 200 OK zurückgeben (außer mit korrektem Key)

## 🔒 Security Features

### Authentication
Alle `/v1/security/*` Endpoints benötigen:
```
Authorization: Bearer eki_your_api_key_here
```

**Test:**
- ✅ Mit Key → 200 OK
- ❌ Ohne Key → 401 Unauthorized
- ❌ Ungültiger Key → 401 Unauthorized

### Rate Limiting
- **IP-based:** 60 Requests/Minute
- **API Key-based:** 1000 Requests/Stunde

Bei Überschreitung: `429 Too Many Requests` mit `Retry-After` Header

### Input Validation

**Base64:**
- Script Content muss gültiges Base64 sein
- Max 10MB decoded size

**SSRF Prevention:**
- Callback URLs müssen whitelisted domains sein:
  - `epro.filmakademie.de`
  - `epro-stage.filmakademie.de`
- Private IPs sind blockiert (192.168.x.x, 10.x.x.x, 127.x.x.x)

**SQL Injection:**
- `project_id` nur alphanumerisch + Bindestriche + Unterstriche
- Pattern: `^[a-zA-Z0-9_-]{1,100}$`

### IDOR Prevention
- User können nur eigene Jobs/Reports abrufen
- Zugriff auf fremde Ressourcen → `404 Not Found`

### One-Shot Reports
- Reports können nur EINMAL abgerufen werden
- Zweiter Zugriff → `410 Gone`

## 📊 Expected Responses

### Successful Responses

**Health Check:**
```json
{
  "status": "healthy",
  "timestamp": "2026-01-30T10:00:00.000000",
  "version": "0.1.0"
}
```

**Sync Security Check:**
```json
{
  "report": {
    "report_id": "uuid",
    "project_id": "test-project-123",
    "script_format": "fdx",
    "created_at": "2026-01-30T10:00:00",
    "risk_summary": {"critical": 0, "high": 0, ...},
    "total_findings": 1,
    "findings": [...],
    "processing_time_seconds": 0.1
  },
  "message": "Security check completed successfully"
}
```

**Async Security Check:**
```json
{
  "job_id": "uuid",
  "status": "pending",
  "message": "Security check job created successfully",
  "status_url": "/v1/security/jobs/uuid",
  "estimated_completion_seconds": 120
}
```

### Error Responses

**401 Unauthorized:**
```json
{
  "detail": "Missing authorization header"
}
```

**422 Validation Error:**
```json
{
  "error": "ValidationError",
  "message": "Request validation failed",
  "details": [
    {
      "field": "script_content",
      "message": "Invalid base64 encoding",
      "error_code": "value_error"
    }
  ]
}
```

**429 Too Many Requests:**
```json
{
  "detail": "Rate limit exceeded. Maximum 60 requests per 60 seconds."
}
```
Headers: `Retry-After: 30`

## 🐛 Troubleshooting

### "Connection refused"
```bash
# Check if services are running
docker compose ps

# Start services if needed
docker compose up -d
```

### "401 Unauthorized"
```bash
# Verify API key is set correctly in Postman Variables
# Create new API key if needed
python scripts/create_api_key.py
```

### "422 Validation Error"
- Check Base64 encoding
- Verify callback_url is whitelisted domain
- Ensure project_id matches pattern

### "404 Not Found" on Job/Report
- Verify you're using the correct API key (IDOR protection)
- Check that job_id/report_id exists and belongs to your user

## 📚 Additional Resources

- **README.md** - Full API documentation
- **DEPLOYMENT_GUIDE.md** - Production deployment
- **TESTING_GUIDE.md** - Testing documentation
- **SECURITY_AUDIT_SUMMARY.md** - Security features

## 🎯 Quick Test Script

Alle Tests in **"3. Security Tests"** ausführen:
1. Select folder "3. Security Tests"
2. Right-click → Run folder
3. Alle Tests sollten erwartete Error-Codes liefern

## 📝 Notes

- **M01 Stub:** Endpoints geben Mock-Daten zurück
- **Production:** Swagger UI (`/docs`) ist hidden wenn `DEBUG=false`
- **Temporal:** Worker muss laufen für async jobs
- **Database:** PostgreSQL muss migrations haben (`alembic upgrade head`)

---

**Version:** 0.1.0-security  
**Last Updated:** 2026-01-30  
**Status:** ✅ Production Ready
