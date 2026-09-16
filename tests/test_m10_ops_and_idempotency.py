"""M10 -- /v1/ops/* endpoints, user-scoped idempotency, X-One-Shot header."""

from __future__ import annotations

import base64
from datetime import datetime
from unittest.mock import AsyncMock, patch
from uuid import uuid4

import pytest
from fastapi import status
from sqlalchemy.exc import IntegrityError

from core.db_models import DeliveryDeadLetter, JobMetadata, ReportMetadata
from tests.conftest import MINIMAL_PDF_BYTES


def _job(user_id: str, **kw) -> JobMetadata:
    defaults = {
        "job_id": uuid4(),
        "project_id": "p1",
        "script_format": "fdx",
        "status": "completed",
        "user_id": user_id,
        "priority": 5,
        "created_at": datetime.utcnow(),
        "updated_at": datetime.utcnow(),
        "delivery_mode": "push",
        "delivery_status": "delivered",
        "delivery_attempts": 2,
        "delivery_last_status_code": 201,
    }
    defaults.update(kw)
    return JobMetadata(**defaults)


def _dead_letter(job_id, **kw) -> DeliveryDeadLetter:
    defaults = {
        "id": uuid4(),
        "job_id": job_id,
        "report_id": uuid4(),
        "project_id": "p1",
        "user_id": "u1",
        "delivery_mode": "push",
        "reason": "hard_4xx",
        "attempts": 1,
        "last_status_code": 422,
        "webhook_sent": False,
        "created_at": datetime.utcnow(),
    }
    defaults.update(kw)
    return DeliveryDeadLetter(**defaults)


class TestOpsAuthorization:
    def test_ops_requires_admin_flag(self, client, auth_headers):
        response = client.get("/v1/ops/jobs", headers=auth_headers)
        assert response.status_code == status.HTTP_403_FORBIDDEN
        assert "admin" in response.json()["message"].lower()

    def test_ops_requires_auth(self, client):
        assert client.get("/v1/ops/dead-letters").status_code == status.HTTP_401_UNAUTHORIZED


class TestOpsJobs:
    @pytest.mark.asyncio
    async def test_list_jobs_is_content_free_and_filterable(
        self, client, db_session, admin_headers
    ):
        db_session.add(_job("u1", status="completed", delivery_status="delivered"))
        db_session.add(_job("u2", status="failed", delivery_status="dead_lettered"))
        await db_session.commit()

        response = client.get("/v1/ops/jobs", headers=admin_headers)
        assert response.status_code == status.HTTP_200_OK
        body = response.json()
        assert body["total"] >= 2
        for item in body["items"]:
            assert set(item) >= {"job_id", "status", "delivery_status", "delivery_attempts"}
            for forbidden in ("findings", "report", "script_content"):
                assert forbidden not in item

        response = client.get(
            "/v1/ops/jobs", params={"delivery_status": "dead_lettered"}, headers=admin_headers
        )
        assert response.status_code == status.HTTP_200_OK
        assert all(i["delivery_status"] == "dead_lettered" for i in response.json()["items"])
        assert response.json()["total"] >= 1

    def test_invalid_delivery_status_is_422(self, client, admin_headers):
        response = client.get(
            "/v1/ops/jobs", params={"delivery_status": "bogus"}, headers=admin_headers
        )
        assert response.status_code == status.HTTP_422_UNPROCESSABLE_ENTITY

    @pytest.mark.asyncio
    async def test_summary_counts(self, client, db_session, admin_headers):
        db_session.add(_job("u1", status="running", delivery_status="pending"))
        await db_session.commit()
        response = client.get("/v1/ops/summary", headers=admin_headers)
        assert response.status_code == status.HTTP_200_OK
        body = response.json()
        assert body["jobs_by_status"].get("running", 0) >= 1
        assert "dead_letters_unacknowledged" in body


class TestDeadLetters:
    @pytest.mark.asyncio
    async def test_list_get_acknowledge_flow(self, client, db_session, admin_headers):
        job = _job("u1", status="failed", delivery_status="dead_lettered")
        db_session.add(job)
        dl = _dead_letter(job.job_id)
        db_session.add(dl)
        await db_session.commit()

        listing = client.get(
            "/v1/ops/dead-letters", params={"acknowledged": "false"}, headers=admin_headers
        )
        assert listing.status_code == status.HTTP_200_OK
        ids = [i["id"] for i in listing.json()["items"]]
        assert str(dl.id) in ids
        assert listing.json()["unacknowledged"] >= 1

        single = client.get(f"/v1/ops/dead-letters/{dl.id}", headers=admin_headers)
        assert single.status_code == status.HTTP_200_OK
        assert single.json()["reason"] == "hard_4xx"

        ack = client.post(
            f"/v1/ops/dead-letters/{dl.id}:acknowledge",
            json={"note": "ePro re-triggered the check"},
            headers=admin_headers,
        )
        assert ack.status_code == status.HTTP_200_OK
        assert ack.json()["acknowledged_by"] == "ops-admin"
        assert ack.json()["note"] == "ePro re-triggered the check"

        again = client.post(f"/v1/ops/dead-letters/{dl.id}:acknowledge", headers=admin_headers)
        assert again.status_code == status.HTTP_409_CONFLICT

        only_acked = client.get(
            "/v1/ops/dead-letters", params={"acknowledged": "true"}, headers=admin_headers
        )
        assert str(dl.id) in [i["id"] for i in only_acked.json()["items"]]

    def test_unknown_dead_letter_is_404(self, client, admin_headers):
        response = client.get(f"/v1/ops/dead-letters/{uuid4()}", headers=admin_headers)
        assert response.status_code == status.HTTP_404_NOT_FOUND


class TestUserScopedIdempotency:
    def _payload(self, key: str) -> dict:
        return {
            "script_content": base64.b64encode(MINIMAL_PDF_BYTES).decode(),
            "script_format": "pdf",
            "project_id": "idem-project",
            "idempotency_key": key,
        }

    @pytest.mark.asyncio
    async def test_same_user_same_key_returns_same_job(self, client, auth_headers, mock_temporal):
        key = f"k-{uuid4().hex}"
        first = client.post(
            "/v1/security/check:async", json=self._payload(key), headers=auth_headers
        )
        second = client.post(
            "/v1/security/check:async", json=self._payload(key), headers=auth_headers
        )
        assert first.status_code == second.status_code == status.HTTP_202_ACCEPTED
        assert first.json()["job_id"] == second.json()["job_id"]
        assert "idempotency" in second.json()["message"].lower()
        assert len(mock_temporal.started) == 1

    @pytest.mark.asyncio
    async def test_different_users_same_key_get_different_jobs(
        self, client, auth_headers, auth_headers_user2, mock_temporal
    ):
        key = f"k-{uuid4().hex}"
        a = client.post("/v1/security/check:async", json=self._payload(key), headers=auth_headers)
        b = client.post(
            "/v1/security/check:async", json=self._payload(key), headers=auth_headers_user2
        )
        assert a.status_code == b.status_code == status.HTTP_202_ACCEPTED
        assert a.json()["job_id"] != b.json()["job_id"]
        assert len(mock_temporal.started) == 2

    @pytest.mark.asyncio
    async def test_lost_race_returns_existing_job(
        self, client, db_session, auth_headers, mock_temporal
    ):
        """Simulate two concurrent POSTs: the second commit hits the unique constraint."""
        key = f"k-{uuid4().hex}"
        winner = _job(
            "test-user-123",
            status="pending",
            delivery_status="pending",
            delivery_attempts=0,
            idempotency_key=key,
            project_id="idem-project",
            script_format="pdf",
        )

        real_commit = db_session.commit
        state = {"raised": False}

        async def racing_commit():
            pending_job = any(isinstance(o, JobMetadata) for o in db_session.new)
            if pending_job and not state["raised"]:
                state["raised"] = True
                # The "other" request wins the race between our SELECT and COMMIT.
                db_session.expunge_all()
                db_session.add(winner)
                await real_commit()
                raise IntegrityError("INSERT job_metadata", {}, Exception("unique violation"))
            await real_commit()

        with patch.object(db_session, "commit", new=AsyncMock(side_effect=racing_commit)):
            response = client.post(
                "/v1/security/check:async", json=self._payload(key), headers=auth_headers
            )

        assert response.status_code == status.HTTP_202_ACCEPTED
        assert response.json()["job_id"] == str(winner.job_id)
        assert len(mock_temporal.started) == 0

    @pytest.mark.asyncio
    async def test_workflow_start_failure_cleans_buffer(
        self, client, auth_headers, mock_temporal, mock_redis
    ):
        mock_temporal.start_workflow = AsyncMock(side_effect=RuntimeError("temporal down"))
        response = client.post(
            "/v1/security/check:async",
            json={
                "script_content": base64.b64encode(MINIMAL_PDF_BYTES).decode(),
                "script_format": "pdf",
                "project_id": "p",
            },
            headers=auth_headers,
        )
        assert response.status_code == status.HTTP_503_SERVICE_UNAVAILABLE
        assert not [k for k in mock_redis._store if str(k).startswith("eki:buf")]


class TestOneShotHeaderAndDeliveryBookkeeping:
    @pytest.mark.asyncio
    async def test_get_report_sets_one_shot_header_and_marks_delivered(
        self, client, db_session, auth_headers
    ):
        report_id = uuid4()
        job = _job(
            "test-user-123",
            status="completed",
            delivery_mode="pull",
            delivery_status="pending",
            delivery_attempts=0,
            report_id=report_id,
        )
        db_session.add(job)
        db_session.add(
            ReportMetadata(
                report_id=report_id,
                job_id=job.job_id,
                project_id="p1",
                user_id="test-user-123",
                script_format="fdx",
                created_at=datetime.utcnow(),
                is_retrieved=False,
                total_findings=0,
                processing_time_seconds=1.0,
                delivery_mode="pull",
            )
        )
        await db_session.commit()

        response = client.get(f"/v1/security/reports/{report_id}", headers=auth_headers)
        assert response.status_code == status.HTTP_200_OK
        assert response.headers.get("X-One-Shot") == "true"

        await db_session.refresh(job)
        assert job.delivery_status == "delivered"
        assert job.delivered_at is not None

        status_response = client.get(f"/v1/security/jobs/{job.job_id}", headers=auth_headers)
        meta = status_response.json()["metadata"]
        assert meta["delivery_status"] == "delivered"
        assert meta["delivered_at"] is not None
