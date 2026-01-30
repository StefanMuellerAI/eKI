# eKI API Testing Guide

This guide covers running tests for the eKI API.

## 📋 Test Coverage

### Test Files

- **tests/test_api.py** - API endpoint tests (health, security endpoints, validation)
- **tests/test_security.py** - Security feature tests (authentication, authorization, IDOR, input validation, prompt injection)
- **tests/test_workflows.py** - Temporal workflow tests
- **tests/conftest.py** - Test fixtures and configuration

### Security Tests

The `tests/test_security.py` file contains comprehensive security tests:

#### Authentication Tests
- Missing authorization header
- Invalid authorization format
- Empty token
- Invalid API key
- Expired API key
- Inactive API key
- Valid API key

#### Authorization Tests (IDOR Prevention)
- User cannot access other users' jobs
- User can access their own jobs
- User cannot access other users' reports
- One-shot report retrieval enforcement

#### Input Validation Tests
- Invalid base64 rejection
- Script size limit enforcement (10MB)
- SSRF prevention (private IP blocking)
- SSRF prevention (domain whitelisting)
- SQL injection prevention in project_id
- Metadata field limits

#### Prompt Injection Tests
- Dangerous pattern detection
- Safe prompt acceptance
- Prompt sanitization
- Prompt truncation
- System prompt locking

---

## 🚀 Running Tests

### Option 1: Local (Recommended)

```bash
# 1. Create virtual environment
python -m venv venv
source venv/bin/activate  # or `venv\Scripts\activate` on Windows

# 2. Install dependencies with dev extras
pip install -e ".[dev]"

# 3. Run tests
pytest tests/ -v --cov

# Or use the test runner script
./scripts/run_tests.sh
```

### Option 2: Docker Container

```bash
# Build with dev dependencies
docker build -f docker/Dockerfile -t eki-api:test \
  --build-arg INSTALL_DEV=true .

# Run tests in container
docker run --rm eki-api:test pytest tests/ -v

# Or exec into running container
docker compose exec api bash
pip install -e ".[dev]"
python -m pytest tests/ -v
```

### Option 3: Quick Test Runner Script

```bash
# Run all tests with coverage
./scripts/run_tests.sh

# Run specific test file
./scripts/run_tests.sh tests/test_security.py

# Run specific test class
./scripts/run_tests.sh tests/test_security.py::TestAuthentication

# Run specific test
./scripts/run_tests.sh tests/test_security.py::TestAuthentication::test_valid_api_key_success
```

---

## 📊 Test Results

### Expected Output

```
============================= test session starts ==============================
tests/test_api.py::TestHealthEndpoints::test_health_check PASSED          [  5%]
tests/test_api.py::TestHealthEndpoints::test_readiness_check PASSED       [ 10%]
tests/test_api.py::TestSecurityEndpoints::test_sync_check_success PASSED  [ 15%]
...
tests/test_security.py::TestAuthentication::test_missing_authorization_header PASSED
tests/test_security.py::TestAuthentication::test_invalid_api_key PASSED
tests/test_security.py::TestAuthorization::test_idor_job_access_prevention PASSED
tests/test_security.py::TestInputValidation::test_ssrf_private_ip_blocked PASSED
...

---------- coverage: platform linux, python 3.11.x -----------
Name                              Stmts   Miss  Cover   Missing
---------------------------------------------------------------
api/__init__.py                       0      0   100%
api/config.py                       137     12    91%   34-38, 142-145
api/dependencies.py                  85      8    91%   67-74
api/main.py                          95      5    95%   104-108
api/rate_limiting.py                 78      5    94%   89-93
api/routers/security.py             145     12    92%   156-160, 210-215
core/db_models.py                   125      0   100%
core/models.py                      189      8    96%   87-94
core/prompt_sanitizer.py             92      4    96%   145-148
---------------------------------------------------------------
TOTAL                              1846    104    94%

============================== 35 passed in 2.34s ===============================
```

### Coverage Report

After running tests, open `htmlcov/index.html` in your browser to see detailed coverage.

---

## 🧪 Writing New Tests

### Test Structure

```python
import pytest
from fastapi import status


class TestNewFeature:
    """Tests for new feature."""

    @pytest.mark.asyncio
    async def test_success_case(self, client, auth_headers):
        """Test successful case."""
        response = client.get("/endpoint", headers=auth_headers)
        assert response.status_code == status.HTTP_200_OK

    def test_failure_case(self, client):
        """Test failure case."""
        response = client.get("/endpoint")
        assert response.status_code == status.HTTP_401_UNAUTHORIZED
```

### Available Fixtures

- **client** - TestClient for synchronous requests
- **async_client** - AsyncClient for async requests
- **db_session** - Database session for test data
- **auth_headers** - Valid authentication headers (user 1)
- **auth_headers_user2** - Valid authentication headers (user 2)
- **test_api_key** - Test API key (plaintext + model)
- **test_api_key_user2** - Test API key for user 2

### Creating Test Data

```python
@pytest.mark.asyncio
async def test_with_job(self, db_session, test_api_key):
    """Test with job data."""
    _, api_key_model = test_api_key
    
    # Create test job
    job = JobMetadata(
        job_id=uuid4(),
        project_id="test-project",
        script_format="fdx",
        status="completed",
        user_id=api_key_model.user_id,
        priority=5,
        created_at=datetime.utcnow(),
        updated_at=datetime.utcnow(),
    )
    
    db_session.add(job)
    await db_session.commit()
    
    # Test with job...
```

---

## 🎯 Test Categories

### Unit Tests
Test individual functions and classes in isolation.

```bash
pytest tests/ -m unit
```

### Integration Tests
Test interactions between components.

```bash
pytest tests/ -m integration
```

### Security Tests
Test security features and protections.

```bash
pytest tests/test_security.py -v
```

### API Tests
Test API endpoints.

```bash
pytest tests/test_api.py -v
```

---

## 🔍 Debugging Tests

### Run with Print Statements

```bash
pytest tests/ -v -s  # -s shows print statements
```

### Run Specific Test with Debugging

```bash
pytest tests/test_security.py::TestAuthentication::test_invalid_api_key -v -s --pdb
```

### Show Full Traceback

```bash
pytest tests/ -v --tb=long
```

### Show Coverage for Specific Module

```bash
pytest tests/ --cov=api.dependencies --cov-report=term-missing
```

---

## ✅ Continuous Integration

### GitHub Actions

The CI pipeline runs tests automatically on every push:

```yaml
# .github/workflows/test.yml
name: Tests

on: [push, pull_request]

jobs:
  test:
    runs-on: ubuntu-latest
    steps:
      - uses: actions/checkout@v3
      - uses: actions/setup-python@v4
        with:
          python-version: '3.11'
      - run: pip install -e ".[dev]"
      - run: pytest tests/ -v --cov
```

---

## 📈 Coverage Goals

| Component | Goal | Current |
|-----------|------|---------|
| API | 95% | ✅ 94% |
| Core | 95% | ✅ 96% |
| Services | 90% | 🔄 TBD |
| Workflows | 85% | 🔄 TBD |

---

## 🐛 Common Issues

### Issue: `ModuleNotFoundError: No module named 'pytest'`

**Solution:** Install dev dependencies
```bash
pip install -e ".[dev]"
```

### Issue: Database connection errors in tests

**Solution:** Tests use in-memory SQLite, not PostgreSQL. Check `conftest.py` fixtures.

### Issue: Async tests failing

**Solution:** Make sure to use `@pytest.mark.asyncio` and `async def` for async tests.

### Issue: Authentication failures in tests

**Solution:** Use the `auth_headers` fixture which provides valid API keys.

---

## 📚 Additional Resources

- [pytest documentation](https://docs.pytest.org/)
- [pytest-asyncio documentation](https://pytest-asyncio.readthedocs.io/)
- [FastAPI testing](https://fastapi.tiangolo.com/tutorial/testing/)
- [Coverage.py documentation](https://coverage.readthedocs.io/)

---

## 🎓 Best Practices

1. **Test Isolation**: Each test should be independent
2. **Clear Names**: Use descriptive test names
3. **Arrange-Act-Assert**: Structure tests clearly
4. **Mock External Services**: Don't hit real APIs in tests
5. **Test Edge Cases**: Not just happy paths
6. **Keep Tests Fast**: Use fixtures efficiently
7. **Document Complex Tests**: Add comments for clarity

---

**Happy Testing!** 🧪✅
