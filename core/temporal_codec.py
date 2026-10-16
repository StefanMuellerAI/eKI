"""Encryption of Temporal payloads at rest (Decision 10a, release 1.0.1).

Without a codec the Temporal server stores every workflow/activity input and
result, signal, header and failure message in plaintext in its history
(PostgreSQL database ``temporal``, shown verbatim in the Temporal UI). For
eKI that meant ``project_id``/``user_id``/``script_id``, client ``metadata``,
the PDF title, buffer reference keys and error texts.

:class:`EncryptionCodec` is a Temporal :class:`~temporalio.converter.PayloadCodec`
that seals every payload with AES-256-GCM before it leaves the process:

* The serialized original ``Payload`` (metadata + data) becomes the
  plaintext; the new payload carries ``encoding: binary/encrypted`` and
  ``encryption-key-id`` (a fingerprint of the key, never the key itself) as
  metadata and ``nonce (12 bytes) || ciphertext || tag`` as data. The
  encoding and key id are bound to the ciphertext as associated data.
* The key is derived from ``API_SECRET_KEY`` with HKDF-SHA256 and the
  dedicated label ``eki-temporal-codec-v1`` -- the same master secret as the
  SecureBuffer and the KB originals, but an independent key (domain
  separation, like ``eki-kb-doc-v1`` in ``services/knowledge_base.py``).
* ``decode`` passes payloads WITHOUT ``binary/encrypted`` through unchanged:
  histories of workflows started before 1.0.1 are plaintext and must keep
  replaying on the new worker.
* ``decode`` fails loudly with :class:`PayloadDecryptionError` (key ids only,
  never content) when the key id differs (other/rotated ``API_SECRET_KEY``)
  or the ciphertext/metadata was tampered with.

:func:`build_data_converter` combines the codec with
``DefaultFailureConverterWithEncodedAttributes`` (``encode_common_attributes=True``):
failure messages and stack traces are moved into an encoded attribute and
encrypted too. :func:`temporal_client_options` is what every payload-carrying
``Client.connect`` uses (``api/dependencies.py``, ``worker/main.py``,
``api/routers/health.py``): API and worker must run with the identical
converter, i.e. the same ``API_SECRET_KEY``. Headers (``eki-request-id``,
OpenTelemetry context) are encoded as well (``HeaderCodecBehavior.CODEC``).

Not encrypted (Temporal needs them in clear): workflow/run/activity ids
(workflow id = ``job_id``), workflow and activity type names, task queue,
timer ids, timestamps, retry/timeout settings and ``ApplicationError`` type
names (exception class names).
"""

from __future__ import annotations

import hashlib
import hmac
import os
from collections.abc import Sequence
from typing import Any

from cryptography.exceptions import InvalidTag
from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.ciphers.aead import AESGCM
from cryptography.hazmat.primitives.kdf.hkdf import HKDF
from google.protobuf.message import DecodeError
from temporalio.api.common.v1 import Payload
from temporalio.common import HeaderCodecBehavior
from temporalio.converter import (
    DataConverter,
    DefaultFailureConverterWithEncodedAttributes,
    PayloadCodec,
)

#: HKDF ``info`` label; changing it changes the key (bump to ``-v2`` only together
#: with a decode path for ``-v1`` payloads).
CODEC_KEY_LABEL = b"eki-temporal-codec-v1"
#: ``encoding`` metadata value of an encrypted payload (Temporal convention).
ENCRYPTED_ENCODING = b"binary/encrypted"
METADATA_ENCODING = "encoding"
METADATA_KEY_ID = "encryption-key-id"
#: Headers (request id, trace context) go through the codec as well.
HEADER_CODEC_BEHAVIOR = HeaderCodecBehavior.CODEC

_KEY_ID_PREFIX = "eki-v1-"
_KEY_ID_MESSAGE = b"eki-temporal-codec-key-id"
_NONCE_BYTES = 12
_TAG_BYTES = 16
_MAX_REPORTED_KEY_ID = 64


class PayloadDecryptionError(Exception):
    """An encrypted Temporal payload could not be decrypted.

    The message names key ids and the failure kind only -- never payload
    content or key material.
    """


def derive_codec_key(secret: str) -> bytes:
    """Derive the 256-bit AES key for Temporal payloads from ``API_SECRET_KEY``."""
    if not secret:
        raise ValueError("API_SECRET_KEY is empty; cannot derive the Temporal codec key")
    return HKDF(
        algorithm=hashes.SHA256(),
        length=32,
        salt=None,
        info=CODEC_KEY_LABEL,
    ).derive(secret.encode("utf-8"))


def codec_key_id(key: bytes) -> str:
    """Non-secret fingerprint of *key*, stored with every encrypted payload.

    An HMAC of a fixed message under the key: equal keys give equal ids,
    and the id reveals nothing about the key.
    """
    digest = hmac.new(key, _KEY_ID_MESSAGE, hashlib.sha256).hexdigest()
    return f"{_KEY_ID_PREFIX}{digest[:16]}"


class EncryptionCodec(PayloadCodec):
    """AES-256-GCM payload codec keyed by ``API_SECRET_KEY`` (see module docstring)."""

    def __init__(self, secret_key: str) -> None:
        key = derive_codec_key(secret_key)
        self._aesgcm = AESGCM(key)
        self._key_id = codec_key_id(key)
        self._aad = ENCRYPTED_ENCODING + b"|" + self._key_id.encode("ascii")

    @property
    def key_id(self) -> str:
        return self._key_id

    def __repr__(self) -> str:  # never show key material in logs/tracebacks
        return f"EncryptionCodec(key_id={self._key_id!r})"

    async def encode(self, payloads: Sequence[Payload]) -> list[Payload]:
        return [self._encrypt(payload) for payload in payloads]

    async def decode(self, payloads: Sequence[Payload]) -> list[Payload]:
        return [self._decrypt(payload) for payload in payloads]

    @staticmethod
    def is_encrypted(payload: Payload) -> bool:
        return payload.metadata.get(METADATA_ENCODING) == ENCRYPTED_ENCODING

    def _encrypt(self, payload: Payload) -> Payload:
        if self.is_encrypted(payload):
            # Already sealed (e.g. a raw payload handed back to the SDK):
            # wrapping it twice would make one decode yield ciphertext.
            return payload
        nonce = os.urandom(_NONCE_BYTES)
        sealed = self._aesgcm.encrypt(nonce, payload.SerializeToString(), self._aad)
        return Payload(
            metadata={
                METADATA_ENCODING: ENCRYPTED_ENCODING,
                METADATA_KEY_ID: self._key_id.encode("ascii"),
            },
            data=nonce + sealed,
        )

    def _decrypt(self, payload: Payload) -> Payload:
        if not self.is_encrypted(payload):
            # Plaintext from before 1.0.1 (or a payload that never went
            # through a codec): pass through so old histories still replay.
            return payload
        key_id = payload.metadata.get(METADATA_KEY_ID, b"").decode("utf-8", "replace")
        if key_id != self._key_id:
            raise PayloadDecryptionError(
                "Temporal payload was encrypted with key id "
                f"{key_id[:_MAX_REPORTED_KEY_ID]!r}, but this process derives key id "
                f"{self._key_id!r} from API_SECRET_KEY. API and worker must use the same "
                "API_SECRET_KEY; workflows started under a previous key cannot be decoded "
                "(drain open workflows before rotating the key)."
            )
        data = payload.data
        if len(data) < _NONCE_BYTES + _TAG_BYTES:
            raise PayloadDecryptionError(
                f"Encrypted Temporal payload is truncated ({len(data)} bytes, key id {key_id!r})"
            )
        try:
            plaintext = self._aesgcm.decrypt(data[:_NONCE_BYTES], data[_NONCE_BYTES:], self._aad)
        except InvalidTag:
            raise PayloadDecryptionError(
                "Encrypted Temporal payload failed authentication (ciphertext or metadata "
                f"modified, key id {key_id!r})"
            ) from None
        try:
            return Payload.FromString(plaintext)
        except DecodeError:
            raise PayloadDecryptionError(
                f"Decrypted Temporal payload is not a valid Payload message (key id {key_id!r})"
            ) from None


def build_data_converter(secret_key: str) -> DataConverter:
    """Default JSON payload converter + :class:`EncryptionCodec` + encoded failure attributes."""
    return DataConverter(
        payload_codec=EncryptionCodec(secret_key),
        failure_converter_class=DefaultFailureConverterWithEncodedAttributes,
    )


def temporal_client_options(secret_key: str) -> dict[str, Any]:
    """Keyword arguments for ``Client.connect``/``Client(...)``/``Replayer(...)``.

    The single place that decides how eKI payloads are encoded, so the API
    (``api/dependencies.py``), the worker (``worker/main.py``) and replay tests
    cannot drift apart.
    """
    return {
        "data_converter": build_data_converter(secret_key),
        "header_codec_behavior": HEADER_CODEC_BEHAVIOR,
    }


__all__ = [
    "CODEC_KEY_LABEL",
    "ENCRYPTED_ENCODING",
    "HEADER_CODEC_BEHAVIOR",
    "METADATA_KEY_ID",
    "EncryptionCodec",
    "PayloadDecryptionError",
    "build_data_converter",
    "codec_key_id",
    "derive_codec_key",
    "temporal_client_options",
]
