"""Encrypted transient data store backed by Redis.

All sensitive data (script content, parsed scenes, reports) is AES-encrypted
in Redis with a configurable TTL.  Temporal activities exchange only opaque
reference keys -- never raw screenplay content.
"""

import hashlib
import json
import logging
from typing import Any
from uuid import uuid4

import redis.asyncio as aioredis
from cryptography.fernet import Fernet, InvalidToken

from core.exceptions import NotFoundException

logger = logging.getLogger(__name__)

# Key prefix to namespace buffer entries in Redis
_KEY_PREFIX = "eki:buf:"


def _derive_fernet_key(secret: str) -> bytes:
    """Derive a URL-safe base64 Fernet key from an arbitrary secret string.

    Uses SHA-256 to produce exactly 32 bytes, then base64-encodes them as
    required by ``cryptography.fernet.Fernet``.
    """
    import base64

    digest = hashlib.sha256(secret.encode("utf-8")).digest()
    return base64.urlsafe_b64encode(digest)


class SecureBuffer:
    """AES-encrypted transient data store backed by Redis.

    Every ``store`` call encrypts the payload with Fernet (AES-128-CBC +
    HMAC-SHA256), writes it to Redis with a TTL, and returns an opaque
    reference key.  ``retrieve`` decrypts the blob.  ``delete`` removes
    keys explicitly -- Redis TTL serves as a safety net.
    """

    def __init__(self, redis_client: aioredis.Redis, secret_key: str, default_ttl: int = 21600):
        self._redis = redis_client
        self._fernet = Fernet(_derive_fernet_key(secret_key))
        self._default_ttl = default_ttl  # 6 h

    async def store(self, data: dict[str, Any], ttl_seconds: int | None = None) -> str:
        """Encrypt *data* and store it in Redis.  Returns the reference key."""
        ttl = ttl_seconds if ttl_seconds is not None else self._default_ttl
        ref_key = f"{_KEY_PREFIX}{uuid4()}"
        plaintext = json.dumps(data, default=str).encode("utf-8")
        encrypted = self._fernet.encrypt(plaintext)
        await self._redis.setex(ref_key, ttl, encrypted)
        logger.debug("SecureBuffer: stored %s (ttl=%ds)", ref_key, ttl)
        return ref_key

    async def retrieve(self, ref_key: str) -> dict[str, Any]:
        """Retrieve and decrypt data for *ref_key*.

        Raises ``NotFoundException`` when the key has expired or was deleted.
        """
        encrypted = await self._redis.get(ref_key)
        if encrypted is None:
            raise NotFoundException(
                "Buffer key expired or not found",
                details={"ref_key": ref_key},
            )
        try:
            plaintext = self._fernet.decrypt(
                encrypted if isinstance(encrypted, bytes) else encrypted.encode("utf-8")
            )
        except InvalidToken as exc:
            raise NotFoundException(
                "Buffer decryption failed (key rotated or corrupted)",
                details={"ref_key": ref_key},
            ) from exc
        return json.loads(plaintext)

    async def delete(self, *ref_keys: str) -> int:
        """Explicitly delete one or more buffer entries.  Returns count deleted."""
        if not ref_keys:
            return 0
        count: int = await self._redis.delete(*ref_keys)
        logger.debug("SecureBuffer: deleted %d keys", count)
        return count
