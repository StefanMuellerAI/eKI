"""Tests for API endpoints."""

import base64

import pytest
from fastapi import status


class TestHealthEndpoints:
    """Tests for health check endpoints."""

    def test_health_check(self, client):
        """Test health check endpoint."""
        response = client.get("/health")
        assert response.status_code == status.HTTP_200_OK
        data = response.json()
        assert data["status"] == "healthy"
        assert "timestamp" in data
        assert data["version"] == "0.1.0"

    def test_readiness_check(self, client):
        """Test readiness check endpoint."""
        response = client.get("/ready")
        # May be 200 or 503 depending on mock services
        assert response.status_code in [status.HTTP_200_OK, status.HTTP_503_SERVICE_UNAVAILABLE]
        data = response.json()
        assert "status" in data
        assert "services" in data
        assert "timestamp" in data


class TestSecurityEndpoints:
    """Tests for security check endpoints."""

    @pytest.mark.asyncio
    async def test_sync_check_success(self, client, auth_headers):
        """Test synchronous security check with valid data."""
        script_content = base64.b64encode(b"Test script content").decode()

        payload = {
            "script_content": script_content,
            "script_format": "fdx",
            "project_id": "test-project-123",
        }

        response = client.post("/v1/security/check", json=payload, headers=auth_headers)
        assert response.status_code == status.HTTP_200_OK
        data = response.json()
        assert "report" in data
        assert "message" in data
        assert data["report"]["project_id"] == "test-project-123"

    def test_sync_check_missing_auth(self, client):
        """Test synchronous security check without authentication."""
        script_content = base64.b64encode(b"Test script content").decode()

        payload = {
            "script_content": script_content,
            "script_format": "fdx",
            "project_id": "test-project-123",
        }

        response = client.post("/v1/security/check", json=payload)
        assert response.status_code == status.HTTP_401_UNAUTHORIZED

    @pytest.mark.asyncio
    async def test_sync_check_invalid_format(self, client, auth_headers):
        """Test synchronous security check with invalid format."""
        script_content = base64.b64encode(b"Test script content").decode()

        payload = {
            "script_content": script_content,
            "script_format": "invalid",
            "project_id": "test-project-123",
        }

        response = client.post("/v1/security/check", json=payload, headers=auth_headers)
        assert response.status_code == status.HTTP_422_UNPROCESSABLE_ENTITY

    @pytest.mark.asyncio
    async def test_async_check_success(self, client, auth_headers):
        """Test asynchronous security check with valid data."""
        script_content = base64.b64encode(b"Large test script content").decode()

        payload = {
            "script_content": script_content,
            "script_format": "pdf",
            "project_id": "test-project-456",
            "priority": 3,
        }

        response = client.post("/v1/security/check:async", json=payload, headers=auth_headers)
        assert response.status_code == status.HTTP_202_ACCEPTED
        data = response.json()
        assert "job_id" in data
        assert data["status"] == "pending"
        assert "status_url" in data

    @pytest.mark.asyncio
    async def test_get_job_status(self, client, auth_headers):
        """Test job status query."""
        job_id = "123e4567-e89b-12d3-a456-426614174000"

        response = client.get(f"/v1/security/jobs/{job_id}", headers=auth_headers)
        # Job doesn't exist in test DB, should return 404
        assert response.status_code == status.HTTP_404_NOT_FOUND

    @pytest.mark.asyncio
    async def test_get_job_status_invalid_uuid(self, client, auth_headers):
        """Test job status query with invalid UUID."""
        response = client.get("/v1/security/jobs/invalid-uuid", headers=auth_headers)
        assert response.status_code == status.HTTP_422_UNPROCESSABLE_ENTITY

    @pytest.mark.asyncio
    async def test_get_report(self, client, auth_headers):
        """Test report retrieval."""
        report_id = "123e4567-e89b-12d3-a456-426614174000"

        response = client.get(f"/v1/security/reports/{report_id}", headers=auth_headers)
        # Report doesn't exist in test DB, should return 404
        assert response.status_code == status.HTTP_404_NOT_FOUND

    @pytest.mark.asyncio
    async def test_get_report_invalid_uuid(self, client, auth_headers):
        """Test report retrieval with invalid UUID."""
        response = client.get("/v1/security/reports/invalid-uuid", headers=auth_headers)
        assert response.status_code == status.HTTP_422_UNPROCESSABLE_ENTITY


class TestRootEndpoint:
    """Tests for root endpoint."""

    def test_root(self, client):
        """Test root endpoint."""
        response = client.get("/")
        assert response.status_code == status.HTTP_200_OK
        data = response.json()
        assert "message" in data
        assert "docs" in data


class TestValidation:
    """Tests for request validation."""

    @pytest.mark.asyncio
    async def test_empty_script_content(self, client, auth_headers):
        """Test validation with empty script content."""
        payload = {
            "script_content": "",
            "script_format": "fdx",
            "project_id": "test-project-123",
        }

        response = client.post("/v1/security/check", json=payload, headers=auth_headers)
        assert response.status_code == status.HTTP_422_UNPROCESSABLE_ENTITY

    @pytest.mark.asyncio
    async def test_missing_required_field(self, client, auth_headers):
        """Test validation with missing required field."""
        payload = {
            "script_format": "fdx",
            "project_id": "test-project-123",
        }

        response = client.post("/v1/security/check", json=payload, headers=auth_headers)
        assert response.status_code == status.HTTP_422_UNPROCESSABLE_ENTITY

    @pytest.mark.asyncio
    async def test_invalid_priority(self, client, auth_headers):
        """Test validation with invalid priority value."""
        script_content = base64.b64encode(b"Test script content").decode()

        payload = {
            "script_content": script_content,
            "script_format": "fdx",
            "project_id": "test-project-123",
            "priority": 15,  # Out of range (1-10)
        }

        response = client.post("/v1/security/check:async", json=payload, headers=auth_headers)
        assert response.status_code == status.HTTP_422_UNPROCESSABLE_ENTITY
