"""Provider-neutral public evidence with explicit applicability and provenance."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum
from urllib.parse import urlparse

from zotero_arxiv_daily.core.time import require_aware_utc


class EvidenceAvailability(StrEnum):
    AVAILABLE = "available"
    UNKNOWN = "unknown"
    INAPPLICABLE = "inapplicable"


@dataclass(frozen=True, slots=True)
class EvidenceValue:
    availability: EvidenceAvailability
    confidence: float
    observed_at: datetime
    provider: str
    claim: bool | None = None

    def __post_init__(self) -> None:
        require_aware_utc(self.observed_at, "observed_at")
        if not self.provider or not 0 <= self.confidence <= 1:
            raise ValueError("evidence value is invalid")
        if self.claim is not None and self.availability is not EvidenceAvailability.AVAILABLE:
            raise ValueError("an evidence claim requires available evidence")


@dataclass(frozen=True, slots=True)
class RepositoryEvidence:
    repository_url: str | None
    association: EvidenceValue
    license_present: EvidenceValue
    archived: EvidenceValue
    releases: EvidenceValue
    documentation: EvidenceValue
    maintained: EvidenceValue

    def __post_init__(self) -> None:
        if self.repository_url is not None:
            parsed = urlparse(self.repository_url)
            if parsed.scheme != "https" or parsed.hostname not in {"github.com", "gitlab.com"}:
                raise ValueError("repository URL must be an allowlisted HTTPS host")
        if (
            self.association.availability is not EvidenceAvailability.AVAILABLE
            and self.repository_url
        ):
            raise ValueError("unverified repository association cannot expose a URL")


@dataclass(frozen=True, slots=True)
class CitationContextEvidence:
    """Bounded public context that remains a weak, non-ranking quality input."""

    citation_count: int | None
    reference_count: int | None
    open_access: EvidenceValue
    retracted: EvidenceValue

    def __post_init__(self) -> None:
        if self.citation_count is not None and self.citation_count < 0:
            raise ValueError("citation count must not be negative")
        if self.reference_count is not None and self.reference_count < 0:
            raise ValueError("reference count must not be negative")


@dataclass(frozen=True, slots=True)
class PublicPaperEvidence:
    canonical_paper_id: str
    schema_version: int
    observed_at: datetime
    expires_at: datetime
    repository: RepositoryEvidence | None = None
    context: CitationContextEvidence | None = None
    provider: str = "unspecified"
    adapter_version: str = "v1"

    def __post_init__(self) -> None:
        observed = require_aware_utc(self.observed_at, "observed_at")
        if require_aware_utc(self.expires_at, "expires_at") <= observed:
            raise ValueError("evidence expiry must follow observation")
        if not self.canonical_paper_id.strip():
            raise ValueError("canonical paper ID must not be empty")
        if not self.provider.strip() or not self.adapter_version.strip():
            raise ValueError("evidence provenance must not be empty")
