"""Encrypted private workflow-state bundles."""

from __future__ import annotations

import json
import os
import tempfile
from pathlib import Path
from typing import Any

from zotero_arxiv_daily.core.errors import SecurityError
from zotero_arxiv_daily.security.encryption import EncryptionEnvelope, decrypt_json, encrypt_json

STATE_BUNDLE_SCHEMA_VERSION = 1
STATE_BUNDLE_FILENAME = "state.enc.json"
REQUIRED_STATE_FILES = (
    "arxiv-state.json",
    "feedback-state.json",
    "recommendation-history.json",
)
OPTIONAL_STATE_FILES = (
    "proposal-cache.json",
    "ranking-weights.json",
    "deployment-receipt.json",
    "pending-publishable-recommendations.json",
    "pending-recommendation-history.json",
    "run-manifest.json",
    "run-manifest-history.json",
    "efficiency-baseline-manifest.json",
    "efficiency-report.json",
    "quality-profile.json",
    "validation-manifest.json",
    "validation-manifest-history.json",
)
ALLOWED_STATE_FILES = frozenset((*REQUIRED_STATE_FILES, *OPTIONAL_STATE_FILES))


def encrypt_state_directory(input_directory: Path, output_path: Path, passphrase: str) -> None:
    """Encrypt validated state files atomically into one owner-readable envelope."""

    files: dict[str, Any] = {}
    for name in sorted(ALLOWED_STATE_FILES):
        path = input_directory / name
        if path.exists():
            files[name] = _read_json(path)
    _validate_files(files, require_required=True)
    payload = {
        "schema_version": STATE_BUNDLE_SCHEMA_VERSION,
        "files": files,
    }
    _atomic_write(output_path, encrypt_json(payload, passphrase).to_json())


def decrypt_state_bundle(
    input_path: Path, output_directory: Path, passphrase: str
) -> tuple[str, ...]:
    """Decrypt and validate a state bundle before writing any file."""

    try:
        envelope = EncryptionEnvelope.from_json(input_path.read_text(encoding="utf-8"))
        value = decrypt_json(envelope, passphrase)
    except (OSError, UnicodeError, SecurityError) as error:
        raise SecurityError("encrypted workflow state cannot be decrypted") from error
    if not isinstance(value, dict) or set(value) != {"schema_version", "files"}:
        raise SecurityError("encrypted workflow state has an invalid payload")
    if value["schema_version"] != STATE_BUNDLE_SCHEMA_VERSION:
        raise SecurityError("encrypted workflow state schema is unsupported")
    files = value["files"]
    if not isinstance(files, dict):
        raise SecurityError("encrypted workflow state files are invalid")
    _validate_files(files, require_required=True)
    output_directory.mkdir(parents=True, exist_ok=True)
    written: list[str] = []
    try:
        for name in sorted(files):
            path = output_directory / name
            _atomic_write(path, json.dumps(files[name], ensure_ascii=False, separators=(",", ":")))
            os.chmod(path, 0o600)
            written.append(name)
    except OSError as error:
        raise SecurityError("decrypted workflow state cannot be written") from error
    return tuple(written)


def _read_json(path: Path) -> Any:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as error:
        raise SecurityError(f"workflow state file is invalid: {path.name}") from error


def _validate_files(files: dict[str, Any], *, require_required: bool) -> None:
    unknown = set(files).difference(ALLOWED_STATE_FILES)
    if unknown:
        raise SecurityError("encrypted workflow state contains unsupported files")
    if require_required and set(REQUIRED_STATE_FILES).difference(files):
        raise SecurityError("encrypted workflow state is missing required files")


def _atomic_write(path: Path, content: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary_name = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    temporary = Path(temporary_name)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as output:
            output.write(content)
            output.flush()
            os.fsync(output.fileno())
        os.replace(temporary, path)
        os.chmod(path, 0o600)
    finally:
        temporary.unlink(missing_ok=True)
