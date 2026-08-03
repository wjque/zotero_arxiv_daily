"""Atomic local TTL cache for validated optional public evidence."""

from __future__ import annotations

import json
import os
import tempfile
from dataclasses import asdict
from datetime import datetime
from pathlib import Path

from zotero_arxiv_daily.core.errors import ExternalServiceError
from zotero_arxiv_daily.core.time import require_aware_utc
from zotero_arxiv_daily.evidence.models import (
    EvidenceAvailability,
    EvidenceValue,
    PublicPaperEvidence,
    RepositoryEvidence,
)


class EvidenceCache:
    """Keep bounded public facts local and discard expired entries deterministically."""

    def __init__(self, path: Path) -> None:
        self.path = path

    def get(self, paper_id: str, now: datetime) -> PublicPaperEvidence | None:
        value = self._read().get(paper_id)
        if not isinstance(value, dict):
            return None
        evidence = _decode(value)
        return evidence if evidence.expires_at > require_aware_utc(now, "now") else None

    def put(self, evidence: PublicPaperEvidence) -> None:
        values = self._read()
        values[evidence.canonical_paper_id] = _encode(evidence)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        descriptor, temporary_name = tempfile.mkstemp(
            prefix=f".{self.path.name}.", dir=self.path.parent
        )
        temporary = Path(temporary_name)
        try:
            with os.fdopen(descriptor, "w", encoding="utf-8") as output:
                json.dump(values, output, sort_keys=True, separators=(",", ":"))
                output.flush()
                os.fsync(output.fileno())
            os.replace(temporary, self.path)
            os.chmod(self.path, 0o600)
        finally:
            temporary.unlink(missing_ok=True)

    def _read(self) -> dict[str, object]:
        if not self.path.exists():
            return {}
        try:
            value = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as error:
            raise ExternalServiceError("evidence cache is unreadable") from error
        if not isinstance(value, dict):
            raise ExternalServiceError("evidence cache root is invalid")
        return value


def _encode(evidence: PublicPaperEvidence) -> dict[str, object]:
    payload = asdict(evidence)
    payload["observed_at"] = evidence.observed_at.isoformat()
    payload["expires_at"] = evidence.expires_at.isoformat()
    if evidence.repository:
        for field, value in enumerate(_repository_values(evidence.repository)):
            encoded = payload["repository"]
            if isinstance(encoded, dict):
                key = (
                    "association",
                    "license_present",
                    "archived",
                    "releases",
                    "documentation",
                    "maintained",
                )[field]
                nested = encoded[key]
                if isinstance(nested, dict):
                    nested["availability"] = value.availability.value
                    nested["observed_at"] = value.observed_at.isoformat()
    return payload


def _decode(value: dict[str, object]) -> PublicPaperEvidence:
    try:
        repository_value = value.get("repository")
        repository = _repository(repository_value) if isinstance(repository_value, dict) else None
        return PublicPaperEvidence(
            str(value["canonical_paper_id"]),
            int(str(value["schema_version"])),
            datetime.fromisoformat(str(value["observed_at"])),
            datetime.fromisoformat(str(value["expires_at"])),
            repository,
        )
    except (KeyError, TypeError, ValueError) as error:
        raise ExternalServiceError("cached evidence is invalid") from error


def _repository(value: dict[str, object]) -> RepositoryEvidence:
    return RepositoryEvidence(
        str(value["repository_url"]) if value["repository_url"] is not None else None,
        *(
            _evidence_value(value[key])
            for key in (
                "association",
                "license_present",
                "archived",
                "releases",
                "documentation",
                "maintained",
            )
        ),
    )


def _evidence_value(value: object) -> EvidenceValue:
    if not isinstance(value, dict):
        raise ValueError
    return EvidenceValue(
        EvidenceAvailability(str(value["availability"])),
        float(value["confidence"]),
        datetime.fromisoformat(str(value["observed_at"])),
        str(value["provider"]),
    )


def _repository_values(value: RepositoryEvidence) -> tuple[EvidenceValue, ...]:
    return (
        value.association,
        value.license_present,
        value.archived,
        value.releases,
        value.documentation,
        value.maintained,
    )
