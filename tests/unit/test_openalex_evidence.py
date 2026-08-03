from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path

import pytest

from zotero_arxiv_daily.arxiv.models import ArxivCandidate, ArxivId
from zotero_arxiv_daily.core.errors import ExternalServiceError
from zotero_arxiv_daily.evidence.models import EvidenceAvailability
from zotero_arxiv_daily.evidence.openalex import (
    OpenAlexClient,
    OpenAlexEvidenceEnricher,
    OpenAlexNotFoundError,
    OpenAlexRateLimitError,
)
from zotero_arxiv_daily.evidence.storage import EvidenceCache

_NOW = datetime(2026, 8, 3, tzinfo=UTC)
_PAYLOAD = b"""{
  "doi":"https://doi.org/10.1000/example",
  "cited_by_count":12,
  "referenced_works_count":4,
  "is_retracted":false,
  "open_access":{"is_oa":true}
}"""


class Transport:
    def __init__(self, responses: list[bytes | Exception]) -> None:
        self.responses = responses
        self.urls: list[str] = []

    def get(self, url: str, timeout_seconds: float) -> bytes:
        self.urls.append(url)
        response = self.responses.pop(0)
        if isinstance(response, Exception):
            raise response
        return response


def _candidate(
    identifier: str = "2401.00001", doi: str | None = "10.1000/example"
) -> ArxivCandidate:
    return ArxivCandidate(
        ArxivId(identifier, 1),
        "Public candidate",
        (),
        ("cs.LG",),
        _NOW,
        _NOW,
        f"https://arxiv.org/abs/{identifier}",
        f"https://arxiv.org/pdf/{identifier}",
        "Public abstract",
        doi=doi,
    )


def test_openalex_fetches_minimal_fields_and_validates_exact_doi() -> None:
    transport = Transport([_PAYLOAD])

    evidence = OpenAlexClient(transport, retries=0).fetch(_candidate(), _NOW)

    assert transport.urls == [
        "https://api.openalex.org/works/doi:10.1000/example?"
        "select=doi%2Ccited_by_count%2Creferenced_works_count%2Cis_retracted%2Copen_access"
    ]
    assert evidence.canonical_paper_id == "arxiv:2401.00001"
    assert evidence.context is not None
    assert evidence.context.citation_count == 12
    assert evidence.context.open_access.claim is True
    assert evidence.context.retracted.claim is False


def test_openalex_missing_doi_is_unknown_without_a_network_request() -> None:
    transport = Transport([])

    evidence = OpenAlexClient(transport, retries=0).fetch(_candidate(doi=None), _NOW)

    assert transport.urls == []
    assert evidence.context is not None
    assert evidence.context.open_access.availability is EvidenceAvailability.UNKNOWN


def test_openalex_not_found_degrades_to_unknown_context() -> None:
    evidence = OpenAlexClient(Transport([OpenAlexNotFoundError("missing")]), retries=0).fetch(
        _candidate(), _NOW
    )

    assert evidence.context is not None
    assert evidence.context.retracted.availability is EvidenceAvailability.UNKNOWN


def test_openalex_rejects_mismatched_identity() -> None:
    payload = _PAYLOAD.replace(b"10.1000/example", b"10.1000/other")

    with pytest.raises(ExternalServiceError, match="mismatched DOI"):
        OpenAlexClient(Transport([payload]), retries=0).fetch(_candidate(), _NOW)


def test_openalex_rejects_malformed_provider_data() -> None:
    with pytest.raises(ExternalServiceError, match="malformed JSON"):
        OpenAlexClient(Transport([b"not-json"]), retries=0).fetch(_candidate(), _NOW)


def test_openalex_retries_short_rate_limit_delay() -> None:
    sleeps: list[float] = []
    client = OpenAlexClient(
        Transport([OpenAlexRateLimitError(2.0), _PAYLOAD]),
        sleep=sleeps.append,
        minimum_interval_seconds=0,
    )

    evidence = client.fetch(_candidate(), _NOW)

    assert evidence.context is not None
    assert sleeps == [2.0]


def test_enricher_uses_provider_versioned_cache_and_degrades_an_outage(tmp_path: Path) -> None:
    transport = Transport([_PAYLOAD, ExternalServiceError("outage")])
    enricher = OpenAlexEvidenceEnricher(
        OpenAlexClient(transport, retries=0), EvidenceCache(tmp_path / "evidence.json")
    )

    first = enricher.enrich((_candidate(), _candidate("2401.00002")), _NOW, limit=2)
    cached = enricher.enrich((_candidate(),), _NOW, limit=1)

    assert len(transport.urls) == 2
    assert first[0].context is not None and first[0].context.citation_count == 12
    assert first[1].context is not None
    assert first[1].context.open_access.availability is EvidenceAvailability.UNKNOWN
    assert cached == (first[0],)
