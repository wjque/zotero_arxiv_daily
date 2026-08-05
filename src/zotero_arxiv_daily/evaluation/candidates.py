"""Evaluation-only exact-identity candidate hydration from public arXiv metadata."""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Protocol

from zotero_arxiv_daily.arxiv.ids import parse_arxiv_id
from zotero_arxiv_daily.arxiv.models import ArxivCandidate
from zotero_arxiv_daily.core.errors import ConfigurationError

_ARXIV_DOI = re.compile(
    r"^10\.48550/arxiv\.(?P<identifier>\d{4}\.\d{4,5}(?:v\d+)?)$", re.IGNORECASE
)


class CandidateClient(Protocol):
    def query(self, search_query: str, start: int, maximum: int) -> tuple[ArxivCandidate, ...]: ...


@dataclass(frozen=True, slots=True)
class HydratedCandidates:
    """Exact matches returned for a frozen evaluation snapshot."""

    candidates: tuple[ArxivCandidate, ...]
    requested_ids: tuple[str, ...]
    unresolved_ids: tuple[str, ...]
    request_count: int


def hydrate_labeled_candidates(
    client: CandidateClient,
    paper_ids: tuple[str, ...],
    *,
    batch_size: int = 20,
) -> HydratedCandidates:
    """Fetch only exact arXiv identities; never infer identity from title or authors."""

    if not 1 <= batch_size <= 50:
        raise ValueError("batch_size must be between 1 and 50")
    requested_values: list[str] = []
    unsupported: list[str] = []
    for value in paper_ids:
        try:
            identifier = _arxiv_id(value)
        except ConfigurationError:
            identifier = None
        if identifier is not None:
            requested_values.append(identifier)
        else:
            unsupported.append(value)
    requested = tuple(dict.fromkeys(requested_values))
    found: dict[str, ArxivCandidate] = {}
    requests = 0
    for offset in range(0, len(requested), batch_size):
        batch = requested[offset : offset + batch_size]
        query = " OR ".join(f"id:{identifier}" for identifier in batch)
        for candidate in client.query(query, 0, len(batch)):
            identifier = candidate.arxiv_id.canonical
            if identifier in batch:
                previous = found.get(identifier)
                if previous is None or candidate.updated > previous.updated:
                    found[identifier] = candidate
        requests += 1
    unresolved = tuple(unsupported) + tuple(
        identifier for identifier in requested if identifier not in found
    )
    return HydratedCandidates(tuple(found.values()), requested, unresolved, requests)


def _arxiv_id(value: str) -> str | None:
    normalized = value.strip().casefold()
    if normalized.startswith("arxiv:"):
        return parse_arxiv_id(normalized[6:]).canonical
    if normalized.startswith("doi:"):
        normalized = normalized[4:]
    match = _ARXIV_DOI.fullmatch(normalized)
    if match is not None:
        return parse_arxiv_id(match.group("identifier")).canonical
    return None
