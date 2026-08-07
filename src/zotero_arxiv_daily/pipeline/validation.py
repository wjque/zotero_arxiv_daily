"""Metadata-only validation policy and privacy-safe protected manifests."""

from __future__ import annotations

import hashlib
import json
import os
import tempfile
from dataclasses import asdict, dataclass
from datetime import UTC, datetime, timedelta
from enum import StrEnum
from pathlib import Path

from zotero_arxiv_daily.arxiv.storage import ArxivStateStore
from zotero_arxiv_daily.core.errors import ApplicationError
from zotero_arxiv_daily.core.time import require_aware_utc

VALIDATION_MANIFEST_SCHEMA_VERSION = 1
_VALIDATION_INTERVAL = timedelta(hours=24)


class RunMode(StrEnum):
    PUBLICATION = "publication"
    METADATA_VALIDATION = "metadata-validation"


@dataclass(frozen=True, slots=True)
class DeploymentState:
    status: str
    deployed_at: datetime | None


@dataclass(frozen=True, slots=True)
class ValidationManifest:
    schema_version: int
    validation_id: str
    started_at: datetime
    completed_at: datetime
    previous_successful_deployment_at: datetime
    candidate_count: int
    candidate_pool_degraded: bool
    candidate_pool_degraded_reason: str | None
    model_requests: int = 0
    site_built: bool = False
    pages_uploaded: bool = False
    pages_deployed: bool = False
    publishable_state_changed: bool = False

    def __post_init__(self) -> None:
        started = require_aware_utc(self.started_at, "started_at")
        completed = require_aware_utc(self.completed_at, "completed_at")
        deployed = require_aware_utc(
            self.previous_successful_deployment_at, "previous_successful_deployment_at"
        )
        if completed < started or started - deployed >= _VALIDATION_INTERVAL:
            raise ValueError("validation manifest timing is invalid")
        if self.candidate_count < 0 or self.model_requests != 0:
            raise ValueError("validation manifest counts are invalid")
        if any(
            (
                self.site_built,
                self.pages_uploaded,
                self.pages_deployed,
                self.publishable_state_changed,
            )
        ):
            raise ValueError("metadata validation cannot report publication changes")


def validation_run_mode(now: datetime, receipt_path: Path) -> RunMode:
    """Choose validation only from a timestamped successful deployment receipt."""

    instant = require_aware_utc(now, "now")
    state = read_deployment_state(receipt_path)
    if state.deployed_at is None or state.status not in {"deployed", "reconciled"}:
        return RunMode.PUBLICATION
    elapsed = instant - state.deployed_at
    if timedelta(0) <= elapsed < _VALIDATION_INTERVAL:
        return RunMode.METADATA_VALIDATION
    return RunMode.PUBLICATION


def read_deployment_state(path: Path) -> DeploymentState:
    """Read current receipts while treating legacy receipts without time as no history."""

    if not path.exists():
        return DeploymentState("missing", None)
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
        if not isinstance(value, dict) or value.get("schema_version") != 1:
            raise ValueError
        status = value.get("status")
        if status not in {"prepared", "deployed", "reconciled"}:
            raise ValueError
        raw = value.get("deployed_at")
        if raw is None:
            return DeploymentState(str(status), None)
        if not isinstance(raw, str):
            raise ValueError
        deployed_at = datetime.fromisoformat(raw)
        return DeploymentState(str(status), require_aware_utc(deployed_at, "deployed_at"))
    except (OSError, TypeError, ValueError, json.JSONDecodeError) as error:
        raise ApplicationError("deployment receipt is invalid") from error


def record_metadata_validation(
    candidate_store: ArxivStateStore,
    receipt_path: Path,
    manifest_path: Path,
    history_path: Path,
    *,
    started_at: datetime,
    completed_at: datetime,
    workflow_run_id: str,
) -> ValidationManifest:
    """Record only privacy-safe metadata after retrieval; no publishable stores are accepted."""

    if validation_run_mode(started_at, receipt_path) is not RunMode.METADATA_VALIDATION:
        raise ApplicationError(
            "metadata validation requires a successful deployment under 24 hours"
        )
    deployed_at = read_deployment_state(receipt_path).deployed_at
    if deployed_at is None:
        raise AssertionError("validated deployment state lost its timestamp")
    degraded, reason, _ = candidate_store.retrieval_status()
    identity = hashlib.sha256(
        f"{workflow_run_id}|{started_at.astimezone(UTC).isoformat()}".encode()
    ).hexdigest()[:24]
    manifest = ValidationManifest(
        VALIDATION_MANIFEST_SCHEMA_VERSION,
        f"validation-{identity}",
        started_at.astimezone(UTC),
        completed_at.astimezone(UTC),
        deployed_at.astimezone(UTC),
        len(candidate_store.candidates()),
        degraded,
        reason,
    )
    payload = _manifest_payload(manifest)
    _write_atomic(manifest_path, payload)
    history = _read_history(history_path)
    existing = {str(item["validation_id"]): item for item in history}
    prior = existing.get(manifest.validation_id)
    if prior is not None and prior != payload:
        raise ApplicationError("validation manifest ID conflicts with protected history")
    existing[manifest.validation_id] = payload
    _write_atomic(
        history_path,
        {
            "schema_version": VALIDATION_MANIFEST_SCHEMA_VERSION,
            "manifests": sorted(existing.values(), key=lambda item: str(item["started_at"]))[-100:],
        },
    )
    return manifest


def _read_history(path: Path) -> list[dict[str, object]]:
    if not path.exists():
        return []
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
        if not isinstance(value, dict) or set(value) != {"schema_version", "manifests"}:
            raise ValueError
        raw = value["manifests"]
        if value["schema_version"] != VALIDATION_MANIFEST_SCHEMA_VERSION or not isinstance(
            raw, list
        ):
            raise ValueError
        if not all(
            isinstance(item, dict) and isinstance(item.get("validation_id"), str) for item in raw
        ):
            raise ValueError
        return raw
    except (OSError, TypeError, ValueError, json.JSONDecodeError) as error:
        raise ApplicationError("validation manifest history is invalid") from error


def _manifest_payload(manifest: ValidationManifest) -> dict[str, object]:
    return {
        **asdict(manifest),
        "started_at": manifest.started_at.isoformat(),
        "completed_at": manifest.completed_at.isoformat(),
        "previous_successful_deployment_at": manifest.previous_successful_deployment_at.isoformat(),
    }


def _write_atomic(path: Path, payload: dict[str, object]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary_name = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    temporary = Path(temporary_name)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as output:
            json.dump(payload, output, sort_keys=True, separators=(",", ":"))
            output.flush()
            os.fsync(output.fileno())
        os.replace(temporary, path)
        os.chmod(path, 0o600)
    finally:
        temporary.unlink(missing_ok=True)
