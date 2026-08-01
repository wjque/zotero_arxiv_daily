"""Versioned PBKDF2/AES-GCM envelopes compatible with browser Web Crypto."""

from __future__ import annotations

import base64
import binascii
import json
import os
from dataclasses import dataclass
from typing import Any

from cryptography.exceptions import InvalidTag
from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.ciphers.aead import AESGCM
from cryptography.hazmat.primitives.kdf.pbkdf2 import PBKDF2HMAC

from zotero_arxiv_daily.core.errors import SecurityError

ENCRYPTION_SCHEMA_VERSION = 1
_SALT_BYTES = 16
_NONCE_BYTES = 12
_ITERATIONS = 310_000


@dataclass(frozen=True, slots=True)
class EncryptionEnvelope:
    schema_version: int
    algorithm: str
    kdf: str
    iterations: int
    salt: str
    nonce: str
    ciphertext: str

    def to_json(self) -> str:
        return json.dumps(
            {
                "schema_version": self.schema_version,
                "algorithm": self.algorithm,
                "kdf": self.kdf,
                "iterations": self.iterations,
                "salt": self.salt,
                "nonce": self.nonce,
                "ciphertext": self.ciphertext,
            },
            separators=(",", ":"),
        )

    @classmethod
    def from_json(cls, payload: str) -> EncryptionEnvelope:
        try:
            value = json.loads(payload)
            if not isinstance(value, dict) or set(value) != {
                "schema_version",
                "algorithm",
                "kdf",
                "iterations",
                "salt",
                "nonce",
                "ciphertext",
            }:
                raise ValueError
            envelope = cls(**value)
            envelope._validate()
            return envelope
        except (TypeError, ValueError, binascii.Error, json.JSONDecodeError) as error:
            raise SecurityError("encrypted recommendation envelope is invalid") from error

    def _validate(self) -> None:
        if (
            self.schema_version != ENCRYPTION_SCHEMA_VERSION
            or self.algorithm != "AES-GCM"
            or self.kdf != "PBKDF2-SHA256"
            or not isinstance(self.iterations, int)
            or self.iterations < _ITERATIONS
        ):
            raise ValueError
        if len(_decode(self.salt)) != _SALT_BYTES or len(_decode(self.nonce)) != _NONCE_BYTES:
            raise ValueError
        if len(_decode(self.ciphertext)) < 16:
            raise ValueError


def encrypt_json(value: Any, passphrase: str) -> EncryptionEnvelope:
    """Encrypt compact JSON with a user-supplied, non-embedded passphrase."""

    key = _derive_key(passphrase, salt := os.urandom(_SALT_BYTES))
    nonce = os.urandom(_NONCE_BYTES)
    plaintext = json.dumps(value, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
    ciphertext = AESGCM(key).encrypt(nonce, plaintext, None)
    return EncryptionEnvelope(
        ENCRYPTION_SCHEMA_VERSION,
        "AES-GCM",
        "PBKDF2-SHA256",
        _ITERATIONS,
        _encode(salt),
        _encode(nonce),
        _encode(ciphertext),
    )


def decrypt_json(envelope: EncryptionEnvelope, passphrase: str) -> Any:
    """Authenticate then decode an envelope; no unauthenticated plaintext is returned."""

    envelope._validate()
    try:
        plaintext = AESGCM(_derive_key(passphrase, _decode(envelope.salt))).decrypt(
            _decode(envelope.nonce), _decode(envelope.ciphertext), None
        )
        return json.loads(plaintext)
    except (InvalidTag, UnicodeDecodeError, json.JSONDecodeError, ValueError) as error:
        raise SecurityError("unable to decrypt recommendation data") from error


def _derive_key(passphrase: str, salt: bytes) -> bytes:
    if len(passphrase) < 16:
        raise SecurityError("Pages passphrase must contain at least 16 characters")
    return PBKDF2HMAC(
        algorithm=hashes.SHA256(), length=32, salt=salt, iterations=_ITERATIONS
    ).derive(passphrase.encode("utf-8"))


def _encode(value: bytes) -> str:
    return base64.b64encode(value).decode("ascii")


def _decode(value: str) -> bytes:
    if not isinstance(value, str):
        raise ValueError
    return base64.b64decode(value.encode("ascii"), validate=True)
