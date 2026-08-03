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
    CitationContextEvidence,
    EvidenceAvailability,
    EvidenceValue,
    PublicPaperEvidence,
    RepositoryEvidence,
)


class EvidenceCache:
    """Keep bounded public facts local and discard expired entries deterministically."""

    def __init__(self, path: Path) -> None:
        self.path = path

    def get(
        self,
        paper_id: str,
        now: datetime,
        *,
        provider: str | None = None,
        adapter_version: str | None = None,
    ) -> PublicPaperEvidence | None:
        """Read a still-valid provider/version-specific evidence snapshot when available."""

        values = self._read()
        if provider is not None and adapter_version is not None:
            value = values.get(_cache_key(paper_id, provider, adapter_version))
            return _unexpired(value, now)
        legacy_value = values.get(paper_id)
        if legacy_value is not None:
            return _unexpired(legacy_value, now)
        matching = [
            evidence
            for value in values.values()
            if (evidence := _unexpired(value, now)) is not None
            and evidence.canonical_paper_id == paper_id
        ]
        return matching[0] if len(matching) == 1 else None

    def put(self, evidence: PublicPaperEvidence) -> None:
        values = self._read()
        values[
            _cache_key(evidence.canonical_paper_id, evidence.provider, evidence.adapter_version)
        ] = _encode(evidence)
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
                    _encode_evidence_value(nested, value)
    if evidence.context:
        encoded_context = payload["context"]
        if isinstance(encoded_context, dict):
            for key, value in (
                ("open_access", evidence.context.open_access),
                ("retracted", evidence.context.retracted),
            ):
                nested = encoded_context[key]
                if isinstance(nested, dict):
                    _encode_evidence_value(nested, value)
    return payload


def _decode(value: dict[str, object]) -> PublicPaperEvidence:
    try:
        repository_value = value.get("repository")
        repository = _repository(repository_value) if isinstance(repository_value, dict) else None
        context_value = value.get("context")
        context = _context(context_value) if isinstance(context_value, dict) else None
        return PublicPaperEvidence(
            str(value["canonical_paper_id"]),
            int(str(value["schema_version"])),
            datetime.fromisoformat(str(value["observed_at"])),
            datetime.fromisoformat(str(value["expires_at"])),
            repository,
            context,
            str(value.get("provider", "unspecified")),
            str(value.get("adapter_version", "v1")),
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
    claim = value.get("claim")
    if claim is not None and not isinstance(claim, bool):
        raise ValueError
    return EvidenceValue(
        EvidenceAvailability(str(value["availability"])),
        float(value["confidence"]),
        datetime.fromisoformat(str(value["observed_at"])),
        str(value["provider"]),
        claim,
    )


def _context(value: dict[str, object]) -> CitationContextEvidence:
    citation_count = value.get("citation_count")
    reference_count = value.get("reference_count")
    if citation_count is not None and (
        not isinstance(citation_count, int) or isinstance(citation_count, bool)
    ):
        raise ValueError
    if reference_count is not None and (
        not isinstance(reference_count, int) or isinstance(reference_count, bool)
    ):
        raise ValueError
    return CitationContextEvidence(
        citation_count,
        reference_count,
        _evidence_value(value["open_access"]),
        _evidence_value(value["retracted"]),
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


def _encode_evidence_value(target: dict[str, object], value: EvidenceValue) -> None:
    target["availability"] = value.availability.value
    target["observed_at"] = value.observed_at.isoformat()


def _cache_key(paper_id: str, provider: str, adapter_version: str) -> str:
    return f"{provider}:{adapter_version}:{paper_id}"


def _unexpired(value: object, now: datetime) -> PublicPaperEvidence | None:
    if not isinstance(value, dict):
        return None
    evidence = _decode(value)
    return evidence if evidence.expires_at > require_aware_utc(now, "now") else None
