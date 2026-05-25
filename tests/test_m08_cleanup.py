"""M08 -- cleanup_buffer_activity Tests.

Stellt sicher, dass nach einem endgueltigen Push-Fail oder dem Ablauf
des 6h-Retry-Fensters der Report aus dem SecureBuffer geloescht wird
(Pflichtenheft Abnahmetest 2 und 4).
"""

from unittest.mock import AsyncMock, patch

import pytest

from workflows.activities import cleanup_buffer_activity


def _mock_buffer(*, delete_returns: int = 1, delete_raises: Exception | None = None):
    buf = AsyncMock()
    if delete_raises is not None:
        buf.delete = AsyncMock(side_effect=delete_raises)
    else:
        buf.delete = AsyncMock(return_value=delete_returns)
    return buf


@pytest.mark.asyncio
class TestCleanupBufferActivity:
    async def test_deletes_single_key(self):
        buf = _mock_buffer(delete_returns=1)
        with patch("workflows.activities._get_buffer", return_value=buf):
            result = await cleanup_buffer_activity({"ref_keys": ["eki:buf:abc"]})
        assert result == {"deleted": 1}
        buf.delete.assert_awaited_once_with("eki:buf:abc")

    async def test_deletes_multiple_keys(self):
        buf = _mock_buffer(delete_returns=3)
        with patch("workflows.activities._get_buffer", return_value=buf):
            result = await cleanup_buffer_activity({
                "ref_keys": ["eki:buf:a", "eki:buf:b", "eki:buf:c"],
            })
        assert result == {"deleted": 3}
        buf.delete.assert_awaited_once_with("eki:buf:a", "eki:buf:b", "eki:buf:c")

    async def test_accepts_str_single_key_input(self):
        buf = _mock_buffer(delete_returns=1)
        with patch("workflows.activities._get_buffer", return_value=buf):
            result = await cleanup_buffer_activity({"ref_keys": "eki:buf:single"})
        assert result["deleted"] == 1
        buf.delete.assert_awaited_once_with("eki:buf:single")

    async def test_empty_input_is_safe(self):
        buf = _mock_buffer()
        with patch("workflows.activities._get_buffer", return_value=buf):
            result = await cleanup_buffer_activity({"ref_keys": []})
        assert result == {"deleted": 0, "reason": "no_keys"}
        buf.delete.assert_not_awaited()

    async def test_missing_input_key_is_safe(self):
        buf = _mock_buffer()
        with patch("workflows.activities._get_buffer", return_value=buf):
            result = await cleanup_buffer_activity({})
        assert result == {"deleted": 0, "reason": "no_keys"}
        buf.delete.assert_not_awaited()

    async def test_empty_strings_are_skipped(self):
        buf = _mock_buffer()
        with patch("workflows.activities._get_buffer", return_value=buf):
            result = await cleanup_buffer_activity({"ref_keys": ["", None, ""]})
        assert result == {"deleted": 0, "reason": "no_keys"}
        buf.delete.assert_not_awaited()

    async def test_redis_error_is_swallowed_and_logged(self):
        """Redis-Fehler darf den Workflow-Failure-Branch nicht abbrechen.
        Der TTL (6h) wirkt als Sicherheitsnetz."""
        buf = _mock_buffer(delete_raises=ConnectionError("redis down"))
        with patch("workflows.activities._get_buffer", return_value=buf):
            result = await cleanup_buffer_activity({"ref_keys": ["eki:buf:x"]})
        assert result["deleted"] == 0
        assert result["reason"] == "delete_error"
