from __future__ import annotations

import json

import pytest

from zotero_arxiv_daily.core.errors import SecurityError
from zotero_arxiv_daily.security.encryption import EncryptionEnvelope, decrypt_json, encrypt_json


def test_encryption_round_trip_is_authenticated_and_does_not_expose_plaintext() -> None:
    protected = {"title": "Sensitive paper title", "recommendations": ["2401.00001"]}
    envelope = encrypt_json(protected, "a sufficiently long test passphrase")

    assert "Sensitive paper title" not in envelope.to_json()
    assert decrypt_json(envelope, "a sufficiently long test passphrase") == protected


def test_wrong_passphrase_and_modified_ciphertext_fail_safely() -> None:
    envelope = encrypt_json({"title": "value"}, "a sufficiently long test passphrase")
    modified = json.loads(envelope.to_json())
    modified["ciphertext"] = modified["ciphertext"][:-2] + "AA"

    with pytest.raises(SecurityError):
        decrypt_json(envelope, "another sufficiently long passphrase")
    with pytest.raises(SecurityError):
        decrypt_json(
            EncryptionEnvelope.from_json(json.dumps(modified)),
            "a sufficiently long test passphrase",
        )
