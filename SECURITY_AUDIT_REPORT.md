# 🔒 Security Audit Report - eKI API

**Audit Date:** 30. Januar 2026
**Auditor:** Security Review
**Scope:** Complete eKI API M01 Implementation
**Status:** ⚠️ **CRITICAL ISSUES FOUND**

---

## Executive Summary

Die eKI API weist mehrere **kritische Sicherheitslücken** auf, die vor einem Produktionseinsatz behoben werden müssen. Von 15 identifizierten Schwachstellen sind:

- 🔴 **5 CRITICAL** - Sofortiger Handlungsbedarf
- 🟠 **6 HIGH** - Hohe Priorität
- 🟡 **4 MEDIUM** - Mittlere Priorität

**Hauptrisiken:**
1. Keine echte Authentifizierung (jeder Token wird akzeptiert)
2. Fehlende Autorisierung (User A kann Daten von User B abrufen)
3. Hardcodierte Credentials in Konfiguration
4. SSRF-Anfälligkeit durch unvalidierte callback_url
5. Prompt Injection in LLM-Integration
6. Zu permissive CORS-Konfiguration

---

## 🔴 CRITICAL Vulnerabilities

### CRIT-01: Stub-Authentifizierung akzeptiert jeden Token

**File:** `api/dependencies.py:48-80`

**Problem:**
```python
# Stub: Accept any non-empty token for M01
if not token:
    raise HTTPException(...)
return token
```

Jeder nicht-leere Token wird akzeptiert. Ein Angreifer kann mit `Bearer anything` auf die API zugreifen.

**Impact:** Complete Authentication Bypass

**Fix:**
```python
import hmac
import hashlib
from datetime import datetime, timedelta
import jwt

async def verify_api_key(
    authorization: str | None = Header(None),
    settings: Settings = Depends(get_settings_dependency)
) -> dict[str, Any]:
    """Verify API key with JWT or HMAC validation."""
    if not authorization:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Missing authorization header",
            headers={"WWW-Authenticate": "Bearer"},
        )

    if not authorization.startswith("Bearer "):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid authorization header format",
            headers={"WWW-Authenticate": "Bearer"},
        )

    token = authorization[7:]

    try:
        # Option 1: JWT Validation
        payload = jwt.decode(
            token,
            settings.api_secret_key,
            algorithms=["HS256"]
        )

        # Check expiration
        if datetime.fromtimestamp(payload.get("exp", 0)) < datetime.utcnow():
            raise HTTPException(
                status_code=status.HTTP_401_UNAUTHORIZED,
                detail="Token expired"
            )

        return payload

    except jwt.InvalidTokenError:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid token"
        )
```

**Alternative: API Key Database:**
```python
async def verify_api_key(
    authorization: str | None = Header(None),
    db: AsyncSession = Depends(get_db)
) -> ApiKey:
    """Verify API key against database."""
    # ... extract token ...

    # Hash the token
    token_hash = hashlib.sha256(token.encode()).hexdigest()

    # Query database
    stmt = select(ApiKeyModel).where(
        ApiKeyModel.key_hash == token_hash,
        ApiKeyModel.is_active == True,
        ApiKeyModel.expires_at > datetime.utcnow()
    )
    api_key = await db.scalar(stmt)

    if not api_key:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid or expired API key"
        )

    # Update last_used_at
    api_key.last_used_at = datetime.utcnow()
    await db.commit()

    return api_key
```

---

### CRIT-02: Fehlende Autorisierung - IDOR Vulnerability

**Files:**
- `api/routers/security.py:128-156` (get_job_status)
- `api/routers/security.py:159-211` (get_report)

**Problem:**
```python
async def get_job_status(job_id: uuid.UUID) -> JobStatusResponse:
    # Keine Prüfung ob der User Zugriff auf diesen Job hat!
    return JobStatusResponse(...)
```

User A kann mit `GET /v1/security/jobs/{user_b_job_id}` auf Jobs von User B zugreifen.

**Impact:** Insecure Direct Object Reference (IDOR) - Zugriff auf fremde Daten

**Fix:**
```python
from fastapi import Depends
from sqlalchemy import select

async def get_job_status(
    job_id: uuid.UUID,
    api_key: ApiKey = Depends(verify_api_key),
    db: AsyncSession = Depends(get_db)
) -> JobStatusResponse:
    """Get job status with authorization check."""

    # Query job with ownership check
    stmt = select(JobMetadata).where(
        JobMetadata.job_id == job_id,
        JobMetadata.api_key_id == api_key.id  # Ownership check!
    )
    job = await db.scalar(stmt)

    if not job:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Job not found or access denied"
        )

    return JobStatusResponse(
        job_id=job.job_id,
        status=job.status,
        # ...
    )
```

**Same fix needed for `get_report()` endpoint!**

---

### CRIT-03: Hardcodierte Credentials in docker-compose.yml

**File:** `docker-compose.yml:7-9, 50-51, 113, 151`

**Problem:**
```yaml
environment:
  POSTGRES_PASSWORD: eki_password  # ❌ Hardcoded!
  DATABASE_URL: postgresql+asyncpg://eki_user:eki_password@postgres:5432/eki_db
```

Credentials sind im Git-Repository sichtbar.

**Impact:** Credential Exposure, Privilege Escalation

**Fix:**

1. **Docker Secrets verwenden:**
```yaml
# docker-compose.yml
services:
  postgres:
    image: pgvector/pgvector:pg16
    environment:
      POSTGRES_PASSWORD_FILE: /run/secrets/db_password
    secrets:
      - db_password

secrets:
  db_password:
    file: ./secrets/db_password.txt
```

2. **Environment-Files verwenden:**
```yaml
# docker-compose.yml
services:
  api:
    env_file:
      - .env.local  # NOT in Git!
```

3. **.gitignore erweitern:**
```gitignore
.env
.env.local
.env.*.local
secrets/
```

4. **Secrets in .env.local (nicht committen):**
```bash
# .env.local
DATABASE_URL=postgresql+asyncpg://eki_user:${DB_PASSWORD}@postgres:5432/eki_db
DB_PASSWORD=<starkes-generiertes-passwort>
API_SECRET_KEY=<256-bit-random-key>
MISTRAL_API_KEY=<actual-key>
```

---

### CRIT-04: SSRF via unvalidierte callback_url

**File:** `core/models.py:52-54`

**Problem:**
```python
callback_url: str | None = Field(
    None, description="Optional callback URL for async results"
)
# Keine Validierung der URL!
```

Angreifer kann interne Services scannen:
```bash
curl -X POST /v1/security/check:async \
  -d '{"callback_url": "http://internal-admin:8080/delete-all", ...}'
```

**Impact:** Server-Side Request Forgery (SSRF), Internal Network Scanning

**Fix:**
```python
from pydantic import HttpUrl, field_validator
import ipaddress
from urllib.parse import urlparse

class SecurityCheckRequest(BaseModel):
    # ... other fields ...

    callback_url: HttpUrl | None = Field(
        None, description="Optional callback URL for async results"
    )

    @field_validator("callback_url")
    @classmethod
    def validate_callback_url(cls, v: HttpUrl | None) -> HttpUrl | None:
        """Validate callback URL to prevent SSRF."""
        if v is None:
            return v

        parsed = urlparse(str(v))

        # Only allow HTTPS in production
        if parsed.scheme not in ["https", "http"]:
            raise ValueError("Callback URL must use HTTP or HTTPS")

        # Block private IP ranges
        try:
            host = parsed.hostname
            if host:
                ip = ipaddress.ip_address(host)
                if ip.is_private or ip.is_loopback or ip.is_link_local:
                    raise ValueError(
                        "Callback URL cannot point to private IP addresses"
                    )
        except ValueError:
            # Not an IP, it's a hostname - allow it
            pass

        # Whitelist of allowed domains (optional)
        allowed_domains = [
            "epro.filmakademie.de",
            "epro-stage.filmakademie.de"
        ]
        if parsed.hostname not in allowed_domains:
            raise ValueError(
                f"Callback URL must be from allowed domains: {allowed_domains}"
            )

        return v
```

---

### CRIT-05: DEBUG Mode in Production

**File:** `docker-compose.yml:112`

**Problem:**
```yaml
environment:
  - ENV=development
  - DEBUG=true  # ❌ Exposes stack traces!
```

Debug-Mode zeigt vollständige Stack Traces und interne Pfade.

**Impact:** Information Disclosure, Attack Surface Mapping

**Fix:**

1. **Separiere Environments:**
```yaml
# docker-compose.prod.yml
services:
  api:
    environment:
      - ENV=production
      - DEBUG=false
```

2. **Conditional Debug in Code:**
```python
# api/main.py
app = FastAPI(
    title="eKI API",
    version="0.1.0",
    docs_url="/docs" if settings.debug else None,  # Hide docs in prod
    redoc_url="/redoc" if settings.debug else None,
    debug=settings.debug,
)
```

3. **Error Handler anpassen:**
```python
@app.exception_handler(Exception)
async def general_exception_handler(request: Request, exc: Exception) -> JSONResponse:
    """Handle unexpected exceptions."""

    if settings.debug:
        # Development: Show details
        logger.error(f"Unexpected error: {exc}", exc_info=True)
        return JSONResponse(
            status_code=500,
            content={"error": str(exc), "traceback": traceback.format_exc()}
        )
    else:
        # Production: Hide details
        logger.error(f"Unexpected error: {exc}", exc_info=True)
        return JSONResponse(
            status_code=500,
            content={"error": "Internal server error", "request_id": str(uuid.uuid4())}
        )
```

---

## 🟠 HIGH Vulnerabilities

### HIGH-01: Keine Base64-Validierung für script_content

**File:** `core/models.py:59-66`

**Problem:**
```python
@field_validator("script_content")
@classmethod
def validate_script_content(cls, v: str) -> str:
    # Stub validation for M01
    if not v.strip():
        raise ValueError("Script content cannot be empty")
    return v
```

Keine echte Base64-Validierung. Könnte Binary Data, Code Injection enthalten.

**Fix:**
```python
import base64

@field_validator("script_content")
@classmethod
def validate_script_content(cls, v: str) -> str:
    """Validate base64 encoding and size."""
    if not v.strip():
        raise ValueError("Script content cannot be empty")

    try:
        # Validate Base64
        decoded = base64.b64decode(v, validate=True)

        # Check decoded size (10MB limit)
        if len(decoded) > 10_485_760:
            raise ValueError("Decoded script exceeds 10MB limit")

        # Optional: Check for null bytes (potential binary exploit)
        if b'\x00' in decoded[:100]:  # Check first 100 bytes
            raise ValueError("Script contains invalid characters")

        return v

    except Exception as e:
        raise ValueError(f"Invalid base64 encoding: {str(e)}")
```

---

### HIGH-02: Prompt Injection in LLM-Integration

**Files:** `llm/ollama.py:25-59`, `llm/mistral_cloud.py`

**Problem:**
```python
async def generate(self, prompt: str, system_prompt: str | None = None):
    payload = {
        "model": self.model,
        "prompt": prompt,  # ❌ Unvalidated user input!
        "system": system_prompt  # ❌ Could be overridden
    }
```

Angreifer kann mit speziellen Prompts:
- System-Prompt überschreiben
- Jailbreaks ausführen
- Interne Informationen extrahieren

**Impact:** Prompt Injection, Data Exfiltration, Model Manipulation

**Fix:**
```python
import re

class PromptSanitizer:
    """Sanitize prompts to prevent injection attacks."""

    DANGEROUS_PATTERNS = [
        r"ignore\s+previous\s+instructions",
        r"system\s*:",
        r"\\n\\nSystem:",
        r"\[INST\].*\[/INST\]",  # Mistral instruction format
        r"<\|im_start\|>",  # ChatML format
    ]

    @classmethod
    def sanitize(cls, text: str) -> str:
        """Remove dangerous patterns from prompt."""
        for pattern in cls.DANGEROUS_PATTERNS:
            text = re.sub(pattern, "", text, flags=re.IGNORECASE)

        # Limit length
        if len(text) > 10000:
            text = text[:10000]

        return text.strip()

async def generate(
    self,
    prompt: str,
    system_prompt: str | None = None,
    **kwargs
) -> str:
    """Generate with prompt sanitization."""

    # Sanitize inputs
    clean_prompt = PromptSanitizer.sanitize(prompt)

    # Lock system prompt (don't allow user to override)
    if system_prompt is None:
        system_prompt = "You are a helpful assistant for film safety analysis."

    payload = {
        "model": self.model,
        "prompt": clean_prompt,
        "system": system_prompt
    }
    # ...
```

---

### HIGH-03: Unvalidiertes metadata Dictionary

**File:** `core/models.py:55-57`

**Problem:**
```python
metadata: dict[str, Any] = Field(
    default_factory=dict, description="Additional metadata for audit trail"
)
# Keine Validierung! Könnte JSON Injection, NoSQL Injection enthalten
```

**Fix:**
```python
from typing import Union

MetadataValue = Union[str, int, float, bool, None]

metadata: dict[str, MetadataValue] = Field(
    default_factory=dict,
    description="Additional metadata (strings, numbers, booleans only)"
)

@field_validator("metadata")
@classmethod
def validate_metadata(cls, v: dict) -> dict:
    """Validate metadata values."""
    if len(v) > 50:
        raise ValueError("Too many metadata fields (max 50)")

    for key, value in v.items():
        # Key validation
        if not re.match(r'^[a-zA-Z0-9_-]{1,50}$', key):
            raise ValueError(f"Invalid metadata key: {key}")

        # Value validation
        if isinstance(value, str) and len(value) > 1000:
            raise ValueError(f"Metadata value too long for key: {key}")

        if value is not None and not isinstance(value, (str, int, float, bool)):
            raise ValueError(f"Invalid metadata value type for key: {key}")

    return v
```

---

### HIGH-04: Zu permissive CORS-Konfiguration

**File:** `api/main.py:54-60`

**Problem:**
```python
app.add_middleware(
    CORSMiddleware,
    allow_origins=settings.cors_origins,
    allow_credentials=True,  # ❌ Dangerous with wildcard!
    allow_methods=["*"],     # ❌ Too permissive!
    allow_headers=["*"],     # ❌ Too permissive!
)
```

**Impact:** CSRF, Cross-Origin attacks

**Fix:**
```python
app.add_middleware(
    CORSMiddleware,
    allow_origins=settings.cors_origins,  # Must be explicit list!
    allow_credentials=True,
    allow_methods=["GET", "POST", "PUT", "DELETE"],  # Explicit methods
    allow_headers=[
        "Authorization",
        "Content-Type",
        "X-Request-ID",
        "X-Actor-User-Id",
        "X-Actor-Project-Id",
    ],
    max_age=600,
)

# In config.py - validate CORS origins
@field_validator("cors_origins", mode="before")
@classmethod
def validate_cors_origins(cls, v: Any) -> list[str]:
    """Validate CORS origins."""
    if isinstance(v, str):
        origins = [origin.strip() for origin in v.split(",")]
    else:
        origins = v

    # Never allow wildcards in production
    if "*" in origins:
        raise ValueError("Wildcard CORS origin not allowed")

    # Validate each origin
    for origin in origins:
        if not origin.startswith(("http://", "https://")):
            raise ValueError(f"Invalid CORS origin: {origin}")

    return origins
```

---

### HIGH-05: Keine Rate Limiting Implementation

**Files:** Alle API Endpoints

**Problem:**
Keine Rate Limiting trotz Redis-Integration.

**Impact:** DDoS, Resource Exhaustion, Credential Stuffing

**Fix:**
```python
# dependencies.py
from slowapi import Limiter
from slowapi.util import get_remote_address
import redis.asyncio as aioredis

limiter = Limiter(key_func=get_remote_address)

async def rate_limit_dependency(
    request: Request,
    redis: aioredis.Redis = Depends(get_redis)
):
    """Rate limit based on IP and API key."""

    # Get identifier (IP or API key)
    api_key = request.headers.get("Authorization", "").replace("Bearer ", "")
    identifier = api_key if api_key else get_remote_address(request)

    # Check rate limit
    key = f"rate_limit:{identifier}"
    current = await redis.incr(key)

    if current == 1:
        await redis.expire(key, 60)  # 1 minute window

    if current > 60:  # 60 requests per minute
        raise HTTPException(
            status_code=status.HTTP_429_TOO_MANY_REQUESTS,
            detail="Rate limit exceeded. Try again later.",
            headers={"Retry-After": "60"}
        )

# In router
@router.post(
    "/check",
    dependencies=[
        Depends(verify_api_key),
        Depends(rate_limit_dependency)
    ]
)
async def security_check_sync(...):
    # ...
```

---

### HIGH-06: SQL Injection Risk via project_id

**File:** `core/models.py:51`

**Problem:**
```python
project_id: str = Field(..., description="eProjekt project ID", min_length=1)
# Nur min_length, keine Format-Validierung!
```

Wenn project_id in Raw SQL verwendet wird → SQL Injection.

**Fix:**
```python
import re

project_id: str = Field(
    ...,
    description="eProjekt project ID",
    min_length=1,
    max_length=100,
    pattern=r'^[a-zA-Z0-9_-]+$'  # Alphanumeric + underscore/hyphen only
)

@field_validator("project_id")
@classmethod
def validate_project_id(cls, v: str) -> str:
    """Validate project ID format."""
    if not re.match(r'^[a-zA-Z0-9_-]{1,100}$', v):
        raise ValueError(
            "project_id must contain only alphanumeric characters, "
            "hyphens, and underscores"
        )
    return v
```

---

## 🟡 MEDIUM Vulnerabilities

### MED-01: Information Disclosure in Error Messages

**File:** `api/main.py:64-75`

**Problem:**
```python
details=[ErrorDetail(message=str(v)) for v in exc.details.values()],
```

Exception details können sensitive Informationen leaken.

**Fix:**
```python
@app.exception_handler(EKIException)
async def eki_exception_handler(request: Request, exc: EKIException) -> JSONResponse:
    """Handle custom EKI exceptions with sanitized details."""

    # Log full error internally
    logger.error(
        f"EKI Exception: {exc.message}",
        extra={
            "details": exc.details,
            "request_id": request.headers.get("X-Request-ID")
        }
    )

    # Return sanitized error to client
    sanitized_details = []
    for key, value in exc.details.items():
        # Don't expose internal paths, keys, etc.
        if not key.startswith("_internal"):
            sanitized_details.append(ErrorDetail(
                field=key,
                message=str(value)[:200]  # Truncate long messages
            ))

    return JSONResponse(
        status_code=status.HTTP_400_BAD_REQUEST,
        content=ErrorResponse(
            error=exc.__class__.__name__,
            message=exc.message,
            details=sanitized_details,
            request_id=request.headers.get("X-Request-ID"),
        ).model_dump(),
    )
```

---

### MED-02: Fehlender CSRF-Schutz

**Files:** Alle POST/PUT/DELETE Endpoints

**Problem:**
Keine CSRF-Tokens für State-Changing Operations.

**Fix:**
```python
from fastapi_csrf_protect import CsrfProtect
from pydantic import BaseModel

class CsrfSettings(BaseModel):
    secret_key: str = "your-secret-key"

@CsrfProtect.load_config
def get_csrf_config():
    return CsrfSettings()

# In dependencies.py
async def verify_csrf(
    request: Request,
    csrf_protect: CsrfProtect = Depends()
):
    """Verify CSRF token for state-changing operations."""
    if request.method in ["POST", "PUT", "DELETE", "PATCH"]:
        await csrf_protect.validate_csrf(request)

# In router
@router.post(
    "/check",
    dependencies=[
        Depends(verify_api_key),
        Depends(verify_csrf)  # Add CSRF protection
    ]
)
```

---

### MED-03: Fehlende Input Sanitization für Logs

**Files:** Alle Logging-Statements

**Problem:**
```python
logger.info(f"Processing project: {request.project_id}")
# project_id könnte newlines, ANSI codes enthalten → Log Injection
```

**Fix:**
```python
def sanitize_for_logging(value: str) -> str:
    """Sanitize string for safe logging."""
    # Remove newlines and control characters
    return re.sub(r'[\x00-\x1f\x7f-\x9f]', '', str(value))

logger.info(
    "Processing project",
    extra={
        "project_id": sanitize_for_logging(request.project_id),
        "user_id": sanitize_for_logging(actor_info.get("user_id"))
    }
)
```

---

### MED-04: Schwache Passworter in Beispiel-Configs

**Files:** `.env.example`, `docker-compose.yml`

**Problem:**
```
POSTGRES_PASSWORD: eki_password  # Trivial password
```

**Fix:**
```bash
# .env.example
# Generate strong passwords:
# python -c "import secrets; print(secrets.token_urlsafe(32))"

DATABASE_URL=postgresql+asyncpg://eki_user:CHANGE_ME_STRONG_PASSWORD@postgres:5432/eki_db
API_SECRET_KEY=CHANGE_ME_MINIMUM_32_CHARS_RANDOM
```

**Add to README:**
```markdown
## Security Setup

Generate secure passwords:
```bash
# Database password
python -c "import secrets; print(secrets.token_urlsafe(32))"

# API secret key (256-bit)
python -c "import secrets; print(secrets.token_hex(32))"
```
```

---

## 📋 Security Checklist für Produktion

### Authentication & Authorization
- [ ] Implement proper JWT or API key authentication
- [ ] Add authorization checks (RBAC/ABAC)
- [ ] Implement API key rotation mechanism
- [ ] Add API key expiration
- [ ] Log all authentication attempts

### Input Validation
- [ ] Validate all user inputs (Base64, URLs, IDs)
- [ ] Implement strict type checking
- [ ] Add length limits to all string fields
- [ ] Sanitize metadata dictionaries
- [ ] Validate file uploads (wenn implementiert)

### Network Security
- [ ] Configure strict CORS policies
- [ ] Implement rate limiting (per IP, per API key)
- [ ] Add WAF (Web Application Firewall)
- [ ] Use HTTPS only (TLS 1.3+)
- [ ] Implement request size limits

### Secrets Management
- [ ] Remove all hardcoded credentials
- [ ] Use Docker Secrets or Vault
- [ ] Implement secret rotation
- [ ] Encrypt sensitive environment variables
- [ ] Add .env.local to .gitignore

### LLM Security
- [ ] Implement prompt sanitization
- [ ] Add output validation
- [ ] Rate limit LLM requests
- [ ] Monitor for prompt injection attempts
- [ ] Implement prompt logging (for audit)

### Docker & Infrastructure
- [ ] Disable debug mode in production
- [ ] Use read-only filesystems where possible
- [ ] Run containers as non-root user ✅ (Already done)
- [ ] Implement resource limits (CPU, memory)
- [ ] Use Docker content trust

### Monitoring & Logging
- [ ] Implement structured logging (JSON)
- [ ] Add security event logging
- [ ] Set up intrusion detection
- [ ] Monitor for anomalies
- [ ] Implement log retention policy
- [ ] Add alerting for security events

### Vulnerability Management
- [ ] Run dependency scan (safety, bandit)
- [ ] Implement automated security scanning in CI/CD
- [ ] Regular penetration testing
- [ ] Subscribe to security advisories
- [ ] Implement vulnerability disclosure program

---

## 🛠️ Immediate Action Items

### Priority 1 (Must Fix Before Production)
1. **Implement proper authentication** (CRIT-01)
2. **Add authorization checks** (CRIT-02)
3. **Remove hardcoded credentials** (CRIT-03)
4. **Fix SSRF vulnerability** (CRIT-04)
5. **Disable debug in production** (CRIT-05)

### Priority 2 (High Risk)
6. Implement Base64 validation (HIGH-01)
7. Add prompt injection protection (HIGH-02)
8. Fix CORS configuration (HIGH-04)
9. Implement rate limiting (HIGH-05)

### Priority 3 (Hardening)
10. Add CSRF protection (MED-02)
11. Sanitize log inputs (MED-03)
12. Update example passwords (MED-04)

---

## 📊 Risk Score

**Current Security Posture:** 3.2/10 ⚠️

**After Fixes:** Estimated 8.5/10 ✅

**Recommendation:** **DO NOT deploy to production** until Priority 1 and 2 items are fixed.

---

## Appendix: Tools für Security Testing

```bash
# 1. Dependency Scan
safety check
bandit -r api core services workflows

# 2. SAST (Static Analysis)
semgrep --config=auto .

# 3. Container Scan
trivy image eki-api:latest

# 4. API Security Testing
zap-cli quick-scan http://localhost:8000

# 5. Load Testing (for DoS protection)
locust -f locustfile.py --host=http://localhost:8000
```

---

**Report End**
**Next Review:** Nach Implementation der Fixes
