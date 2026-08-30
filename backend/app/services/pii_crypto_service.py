"""Versioned AEAD envelope for Contact PII (Workstream B1).

The service never provides a plaintext fallback. Keys are supplied by an
operator/KMS key ring and may be rotated by changing the active version while
retaining previous versions for decryption of existing rows.
"""
from __future__ import annotations

import base64
import hashlib
import hmac
import os
import struct
from dataclasses import dataclass
from typing import Mapping

from cryptography.hazmat.primitives.ciphers.aead import AESGCM

_MAGIC = b"jbpi1"
_NONCE_BYTES = 12
_HEADER_BYTES = len(_MAGIC) + 2
_MAX_KEY_VERSION = 65_535


class PiiCryptoError(RuntimeError):
    """Raised for missing keys, malformed envelopes or authentication errors."""


def _derive_key(material: str | bytes) -> bytes:
    if isinstance(material, str):
        material = material.encode("utf-8")
    if not material:
        raise PiiCryptoError("PII key material is empty")
    return hashlib.sha256(material).digest()


def pii_digest(value: str) -> str:
    """Deterministic equality digest; this is not used as an encryption key."""
    if value is None:
        raise ValueError("PII digest requires a value")
    return hashlib.sha256(str(value).strip().encode("utf-8")).hexdigest()


@dataclass(frozen=True)
class PiiCiphertext:
    value: bytes
    key_version: int
    digest: str


class PiiCryptoService:
    def __init__(self, keyring: Mapping[int, str | bytes] | None = None, *, active_key_version: int = 1, aad_domain: str = "jobbridge:pii:v1"):
        self._keyring = {int(version): material for version, material in (keyring or {}).items() if material}
        self.active_key_version = int(active_key_version)
        self.aad_domain = str(aad_domain)
        if not 1 <= self.active_key_version <= _MAX_KEY_VERSION:
            raise ValueError("active_key_version must be between 1 and 65535")

    @classmethod
    def from_settings(cls, settings_obj=None) -> "PiiCryptoService":
        """Build a service from ``PII_KEYRING_REF``/settings without logging keys."""
        settings_obj = settings_obj or _load_settings()
        raw = getattr(settings_obj, "pii_keyring_ref", None) or os.getenv("PII_KEYRING_REF", "")
        ring: dict[int, str] = {}
        for item in str(raw).split(","):
            if ":" not in item:
                continue
            version, material = item.split(":", 1)
            try:
                parsed = int(version.strip())
            except ValueError:
                continue
            if material.strip():
                ring[parsed] = material.strip()
        active = int(getattr(settings_obj, "pii_active_key_version", None) or os.getenv("PII_ACTIVE_KEY_VERSION", "1"))
        standalone = getattr(settings_obj, "pii_key_material", None) or os.getenv("PII_KEY_MATERIAL", "")
        if standalone and active not in ring:
            ring[active] = standalone
        return cls(ring, active_key_version=active)

    def _key(self, version: int) -> bytes:
        try:
            return _derive_key(self._keyring[int(version)])
        except (KeyError, TypeError, ValueError) as exc:
            raise PiiCryptoError(f"PII key version {version} is unavailable") from exc

    def _aad(self, field: str, entity_type: str, entity_id: str, version: int) -> bytes:
        if not field or not entity_type or not entity_id:
            raise ValueError("PII AAD requires field, entity_type and entity_id")
        return f"{self.aad_domain}|{entity_type}|{entity_id}|{field}|{version}".encode("utf-8")

    def encrypt(self, plaintext: str, *, field: str, entity_type: str, entity_id: str, key_version: int | None = None) -> PiiCiphertext:
        if plaintext is None:
            raise ValueError("PII plaintext cannot be None")
        version = int(key_version or self.active_key_version)
        if not 1 <= version <= _MAX_KEY_VERSION:
            raise ValueError("key_version must be between 1 and 65535")
        nonce = os.urandom(_NONCE_BYTES)
        sealed = AESGCM(self._key(version)).encrypt(nonce, str(plaintext).encode("utf-8"), self._aad(field, entity_type, str(entity_id), version))
        envelope = base64.urlsafe_b64encode(_MAGIC + struct.pack(">H", version) + nonce + sealed)
        return PiiCiphertext(envelope, version, pii_digest(str(plaintext)))

    def decrypt(self, ciphertext: bytes | str, *, field: str, entity_type: str, entity_id: str) -> str:
        raw = ciphertext.encode("ascii") if isinstance(ciphertext, str) else bytes(ciphertext)
        try:
            blob = base64.urlsafe_b64decode(raw)
            if not blob.startswith(_MAGIC) or len(blob) <= _HEADER_BYTES + _NONCE_BYTES + 16:
                raise ValueError
            version = struct.unpack(">H", blob[len(_MAGIC):_HEADER_BYTES])[0]
            nonce = blob[_HEADER_BYTES:_HEADER_BYTES + _NONCE_BYTES]
            sealed = blob[_HEADER_BYTES + _NONCE_BYTES:]
            value = AESGCM(self._key(version)).decrypt(nonce, sealed, self._aad(field, entity_type, str(entity_id), version))
            return value.decode("utf-8")
        except PiiCryptoError:
            raise
        except Exception as exc:  # InvalidTag, bad base64, invalid UTF-8
            raise PiiCryptoError("PII ciphertext authentication failed") from exc

    def rotate(self, ciphertext: bytes | str, *, field: str, entity_type: str, entity_id: str) -> PiiCiphertext:
        plaintext = self.decrypt(ciphertext, field=field, entity_type=entity_type, entity_id=entity_id)
        try:
            return self.encrypt(plaintext, field=field, entity_type=entity_type, entity_id=entity_id)
        finally:
            # Drop the only local reference as soon as rotation is complete.
            plaintext = ""


def _load_settings():
    try:
        from app.config import settings
        return settings
    except Exception:
        return object()


__all__ = ["PiiCiphertext", "PiiCryptoError", "PiiCryptoService", "pii_digest"]
