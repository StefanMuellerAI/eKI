"""Regressions that only show up on the real PostgreSQL dialect.

The default suite runs on SQLite, which neither enforces ``VARCHAR(n)`` nor
attaches time zones.  These tests pass on SQLite too, but they only
*discriminate* when the suite runs with ``TEST_POSTGRES_URL`` (CI does).
"""

from __future__ import annotations

import hashlib
import os
import secrets
import time
from datetime import UTC, datetime, timedelta
from uuid import uuid4

import pytest
from fastapi import status

from core.db_models import ApiKeyModel


@pytest.fixture
def local_tz():
    """Switch the process-local time zone; asyncpg reads naive datetimes as local time."""
    if not hasattr(time, "tzset"):
        pytest.skip("time.tzset() not available on this platform")
    previous = os.environ.get("TZ")

    def _set(tz: str) -> None:
        os.environ["TZ"] = tz
        time.tzset()

    yield _set

    if previous is None:
        os.environ.pop("TZ", None)
    else:
        os.environ["TZ"] = previous
    time.tzset()


async def _add_key(db_session, expires_in: timedelta) -> str:
    api_key = f"eki_{secrets.token_hex(32)}"
    now = datetime.now(UTC)
    db_session.add(
        ApiKeyModel(
            id=uuid4(),
            user_id=f"tz-user-{uuid4().hex[:8]}",
            key_hash=hashlib.sha256(api_key.encode()).hexdigest(),
            name="TZ regression key",
            is_active=True,
            created_at=now - timedelta(days=1),
            expires_at=now + expires_in,
            usage_count=0,
        )
    )
    await db_session.commit()
    return api_key


class TestApiKeyExpiryIsTimeZoneIndependent:
    """``verify_api_key`` used ``datetime.utcnow()`` (naive).  asyncpg encodes a
    naive value for a ``timestamptz`` column as *local* time, so on a host with
    TZ=Europe/Berlin an expired key stayed valid for two more hours (and on a
    UTC-5 host a valid key was rejected five hours early)."""

    # POSIX TZ strings: "EKI-5" means local = UTC+5, "EKI+5" means local = UTC-5.
    @pytest.mark.parametrize(
        ("tz", "expires_in", "expected"),
        [
            ("EKI-5", timedelta(minutes=-30), status.HTTP_401_UNAUTHORIZED),
            ("EKI+5", timedelta(minutes=30), status.HTTP_404_NOT_FOUND),
        ],
        ids=["expired-key-on-utc-plus-5-host", "valid-key-on-utc-minus-5-host"],
    )
    @pytest.mark.asyncio
    async def test_expiry_check_uses_aware_utc(
        self, client, db_session, local_tz, tz, expires_in, expected
    ):
        api_key = await _add_key(db_session, expires_in)
        local_tz(tz)

        # 404 = authenticated but the job does not exist; 401 = key rejected.
        response = client.get(
            f"/v1/security/jobs/{uuid4()}",
            headers={"Authorization": f"Bearer {api_key}"},
        )

        assert response.status_code == expected
