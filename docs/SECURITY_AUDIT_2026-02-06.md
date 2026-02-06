# Security Audit Report - eKI API

- Audit date: 2026-02-06
- Scope: API code, auth, rate limiting, input validation, deployment config, helper scripts
- Auditor mode: static code review + targeted test execution

## Executive Summary

Overall risk level: **HIGH**

- High: 3
- Medium: 3
- Low: 2

The API already has meaningful controls (hashed API keys, ownership checks, SSRF domain allowlist), but deployment and abuse-resistance gaps still create exploitable attack paths.

## Findings

### 1) [HIGH] Rate-limit bypass via untrusted `X-Forwarded-For`

- Evidence: `/Users/davinci_kollektiv/Documents/eKI/eki-api/api/rate_limiting.py:28`
- Evidence: `/Users/davinci_kollektiv/Documents/eKI/eki-api/api/rate_limiting.py:29`
- Evidence: `/Users/davinci_kollektiv/Documents/eKI/eki-api/api/rate_limiting.py:35`
- Evidence: `/Users/davinci_kollektiv/Documents/eKI/eki-api/api/rate_limiting.py:87`

**Issue**
Client IP is taken directly from `X-Forwarded-For`, which is user-controlled unless a trusted reverse proxy sanitizes it. Attackers can rotate fake IPs to evade the per-IP limiter. Limits are also hardcoded (60/min, 1000/hour) instead of using config values.

**Impact**
Bypass of anti-abuse controls and easier request flooding.

**Recommendation**
- Only trust forwarded headers behind a verified proxy chain.
- Otherwise use `request.client.host`.
- Use `settings.rate_limit_per_minute` and `settings.rate_limit_per_hour` instead of literals.

### 2) [HIGH] `/metrics` is publicly exposed without auth/network guard

- Evidence: `/Users/davinci_kollektiv/Documents/eKI/eki-api/api/main.py:130`
- Evidence: `/Users/davinci_kollektiv/Documents/eKI/eki-api/api/main.py:131`

**Issue**
Prometheus metrics are mounted on `/metrics` with no authentication or IP restriction.

**Impact**
Information disclosure (service internals, endpoint behavior), plus potential scraping abuse.

**Recommendation**
- Expose `/metrics` only on internal network.
- Or add auth/mTLS/reverse-proxy ACL.
- Optionally disable endpoint by default in production.

### 3) [HIGH] Insecure deployment defaults (hardcoded credentials + exposed infra ports)

- Evidence: `/Users/davinci_kollektiv/Documents/eKI/eki-api/api/config.py:32`
- Evidence: `/Users/davinci_kollektiv/Documents/eKI/eki-api/api/config.py:56`
- Evidence: `/Users/davinci_kollektiv/Documents/eKI/eki-api/docker-compose.yml:9`
- Evidence: `/Users/davinci_kollektiv/Documents/eKI/eki-api/docker-compose.yml:11`
- Evidence: `/Users/davinci_kollektiv/Documents/eKI/eki-api/docker-compose.yml:27`
- Evidence: `/Users/davinci_kollektiv/Documents/eKI/eki-api/docker-compose.yml:43`
- Evidence: `/Users/davinci_kollektiv/Documents/eKI/eki-api/docker-compose.yml:68`
- Evidence: `/Users/davinci_kollektiv/Documents/eKI/eki-api/docker-compose.yml:92`
- Evidence: `/Users/davinci_kollektiv/Documents/eKI/eki-api/docker-compose.prod.yml:2`

**Issue**
Repository defaults include predictable DB password / secret placeholders, and base Compose publishes PostgreSQL/Redis/Temporal/UI/Ollama ports to host. Production overlay adds secrets but does not remove inherited port exposure from base file.

**Impact**
Elevated risk of infrastructure compromise and credential misuse in misconfigured deployments.

**Recommendation**
- Remove hardcoded secrets from defaults.
- Keep only API port public by default; bind infra services to internal network.
- Maintain a production-only compose baseline rather than inheriting dev port mappings.

### 4) [MEDIUM] Missing rate limiting on report/job lookup endpoints

- Evidence: `/Users/davinci_kollektiv/Documents/eKI/eki-api/api/routers/security.py:132`
- Evidence: `/Users/davinci_kollektiv/Documents/eKI/eki-api/api/routers/security.py:179`
- Reference (protected endpoints): `/Users/davinci_kollektiv/Documents/eKI/eki-api/api/routers/security.py:35`
- Reference (protected endpoints): `/Users/davinci_kollektiv/Documents/eKI/eki-api/api/routers/security.py:97`

**Issue**
`GET /v1/security/jobs/{job_id}` and `GET /v1/security/reports/{report_id}` require auth but do not apply `rate_limit_combined`.

**Impact**
Authenticated callers can hammer metadata endpoints and drive DB load.

**Recommendation**
Apply the same limiter dependency to GET lookup endpoints (or an endpoint-specific stricter profile).

### 5) [MEDIUM] One-shot retrieval is vulnerable to race condition

- Evidence: `/Users/davinci_kollektiv/Documents/eKI/eki-api/api/routers/security.py:214`
- Evidence: `/Users/davinci_kollektiv/Documents/eKI/eki-api/api/routers/security.py:222`
- Evidence: `/Users/davinci_kollektiv/Documents/eKI/eki-api/api/routers/security.py:224`

**Issue**
`is_retrieved` is checked and then updated in separate steps without row lock/atomic conditional update.

**Impact**
Concurrent requests can both pass check and retrieve the same report, violating one-shot semantics.

**Recommendation**
Use atomic update (e.g., `UPDATE ... SET is_retrieved=true ... WHERE report_id=? AND is_retrieved=false RETURNING ...`) or row-level lock (`SELECT ... FOR UPDATE`).

### 6) [MEDIUM] SQL injection risk in API-key helper script

- Evidence: `/Users/davinci_kollektiv/Documents/eKI/eki-api/scripts/create_api_key.py:21`
- Evidence: `/Users/davinci_kollektiv/Documents/eKI/eki-api/scripts/create_api_key.py:61`
- Evidence: `/Users/davinci_kollektiv/Documents/eKI/eki-api/scripts/create_api_key.py:73`
- Evidence: `/Users/davinci_kollektiv/Documents/eKI/eki-api/scripts/create_api_key.py:76`

**Issue**
User-provided fields are interpolated directly into generated SQL text.

**Impact**
If copied/executed with untrusted input, script can produce injected SQL.

**Recommendation**
Replace raw SQL string generation with parameterized DB insert via SQLAlchemy.

### 7) [LOW] Prompt-injection detection is fail-open

- Evidence: `/Users/davinci_kollektiv/Documents/eKI/eki-api/core/prompt_sanitizer.py:96`
- Evidence: `/Users/davinci_kollektiv/Documents/eKI/eki-api/core/prompt_sanitizer.py:119`
- Evidence: `/Users/davinci_kollektiv/Documents/eKI/eki-api/llm/mistral_cloud.py:39`
- Evidence: `/Users/davinci_kollektiv/Documents/eKI/eki-api/llm/ollama.py:36`

**Issue**
Dangerous patterns are detected but accepted (`raise_on_unsafe=False`).

**Impact**
When LLM features are enabled, malicious prompts are still forwarded.

**Recommendation**
Switch to fail-closed for untrusted inputs (`raise_on_unsafe=True`) and add policy-based handling/logging.

### 8) [LOW] Readiness endpoint leaks dependency status anonymously

- Evidence: `/Users/davinci_kollektiv/Documents/eKI/eki-api/api/routers/health.py:37`
- Evidence: `/Users/davinci_kollektiv/Documents/eKI/eki-api/api/routers/health.py:59`

**Issue**
`/ready` is unauthenticated and returns service-level dependency state.

**Impact**
Aids reconnaissance for attackers.

**Recommendation**
Keep `/health` public; restrict `/ready` to internal network or authenticated operators.

## Positive Controls Observed

- API keys are stored as SHA-256 hashes (not plaintext): `/Users/davinci_kollektiv/Documents/eKI/eki-api/api/dependencies.py:82`
- Key status/expiry is validated: `/Users/davinci_kollektiv/Documents/eKI/eki-api/api/dependencies.py:88`
- Ownership checks prevent classic IDOR on jobs/reports: `/Users/davinci_kollektiv/Documents/eKI/eki-api/api/routers/security.py:153`, `/Users/davinci_kollektiv/Documents/eKI/eki-api/api/routers/security.py:201`
- Callback URL validation enforces domain allowlist for SSRF control: `/Users/davinci_kollektiv/Documents/eKI/eki-api/core/models.py:126`

## Validation Notes

- A targeted test run could not be completed in this environment due to missing dependency:
  - `ModuleNotFoundError: No module named 'typing_extensions'` while loading pytest.
  - Command: `pytest -q tests/test_security.py`

## Recommended Remediation Order

1. Fix deployment exposure and secret defaults (Finding 3).
2. Fix rate-limit trust model and apply limits consistently (Findings 1, 4).
3. Protect `/metrics` and `/ready` with network/auth controls (Findings 2, 8).
4. Make one-shot retrieval atomic (Finding 5).
5. Hardening tasks for tooling and future LLM path (Findings 6, 7).
