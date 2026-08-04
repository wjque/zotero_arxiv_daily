"""Checkpoint-on-success arXiv candidate retrieval orchestration."""

from __future__ import annotations

import math
from datetime import UTC, datetime, timedelta
from typing import Protocol

from zotero_arxiv_daily.arxiv.client import category_query
from zotero_arxiv_daily.arxiv.models import ArxivCandidate, RetrievalCheckpoint, RetrievalResult
from zotero_arxiv_daily.arxiv.storage import ArxivStateStore
from zotero_arxiv_daily.core.errors import ExternalServiceError


class CandidateClient(Protocol):
    def query(self, search_query: str, start: int, maximum: int) -> tuple[ArxivCandidate, ...]: ...


def retrieve(
    client: CandidateClient,
    store: ArxivStateStore,
    categories: tuple[str, ...],
    now: datetime,
    *,
    candidate_ceiling: int = 1000,
    page_size: int = 100,
    overlap: timedelta = timedelta(hours=12),
    stale_pool_max_age: timedelta = timedelta(hours=72),
) -> RetrievalResult:
    """Fetch category pages, deduplicate revisions, then atomically advance the checkpoint."""

    if candidate_ceiling < 1 or page_size < 1 or stale_pool_max_age < timedelta(0):
        raise ValueError("candidate_ceiling and page_size must be positive")
    if not categories:
        raise ValueError("at least one retrieval category is required")
    completed_at = now.astimezone(UTC)
    previous = store.checkpoint()
    previous_candidates = store.candidates()
    start_at = (
        previous.completed_at - overlap
        if previous is not None and previous_candidates
        else completed_at - timedelta(days=7)
    )
    start_gmt = start_at.strftime("%Y%m%d%H%M")
    end_gmt = completed_at.strftime("%Y%m%d%H%M")
    store.begin(completed_at)
    collected: dict[str, ArxivCandidate] = {}
    requests = 0
    per_category_limit = max(1, math.ceil(candidate_ceiling / len(categories)))
    for category in categories:
        offset = 0
        category_count = 0
        try:
            while len(collected) < candidate_ceiling and category_count < per_category_limit:
                maximum = min(
                    page_size,
                    per_category_limit - category_count,
                    candidate_ceiling - len(collected),
                )
                page = client.query(category_query(category, start_gmt, end_gmt), offset, maximum)
                requests += 1
                category_count += len(page)
                for candidate in page:
                    existing = collected.get(candidate.arxiv_id.canonical)
                    if existing is None or candidate.updated > existing.updated:
                        collected[candidate.arxiv_id.canonical] = candidate
                if len(page) < maximum:
                    break
                offset += len(page)
        except ExternalServiceError as error:
            if (
                previous is not None
                and previous_candidates
                and completed_at - previous.completed_at <= stale_pool_max_age
            ):
                reason = _degraded_reason(error)
                store.mark_degraded(reason, previous)
                return RetrievalResult(
                    previous_candidates,
                    previous,
                    requests,
                    True,
                    reason,
                )
            raise
    candidates = tuple(
        sorted(
            collected.values(),
            key=lambda candidate: (candidate.published, candidate.arxiv_id.canonical),
            reverse=True,
        )
    )
    checkpoint = RetrievalCheckpoint(completed_at)
    store.commit(checkpoint, candidates)
    return RetrievalResult(store.candidates(), checkpoint, requests)


def _degraded_reason(error: ExternalServiceError) -> str:
    """Keep a bounded diagnostic without copying provider responses into runtime state."""

    message = " ".join(str(error).split())
    return (message or error.__class__.__name__)[:160]
