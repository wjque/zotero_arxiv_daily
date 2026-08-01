"""Checkpoint-on-success arXiv candidate retrieval orchestration."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Protocol

from zotero_arxiv_daily.arxiv.client import category_query
from zotero_arxiv_daily.arxiv.models import ArxivCandidate, RetrievalCheckpoint, RetrievalResult
from zotero_arxiv_daily.arxiv.storage import ArxivStateStore


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
) -> RetrievalResult:
    """Fetch category pages, deduplicate revisions, then atomically advance the checkpoint."""

    if candidate_ceiling < 1 or page_size < 1:
        raise ValueError("candidate_ceiling and page_size must be positive")
    completed_at = now.astimezone(UTC)
    previous = store.checkpoint()
    start_at = (previous.completed_at - overlap) if previous else completed_at - timedelta(days=7)
    start_gmt = start_at.strftime("%Y%m%d%H%M")
    end_gmt = completed_at.strftime("%Y%m%d%H%M")
    store.begin(completed_at)
    collected: dict[str, ArxivCandidate] = {}
    requests = 0
    for category in categories:
        offset = 0
        while len(collected) < candidate_ceiling:
            page = client.query(
                category_query(category, start_gmt, end_gmt),
                offset,
                min(page_size, candidate_ceiling - len(collected)),
            )
            requests += 1
            for candidate in page:
                existing = collected.get(candidate.arxiv_id.canonical)
                if existing is None or candidate.updated > existing.updated:
                    collected[candidate.arxiv_id.canonical] = candidate
            if len(page) < page_size:
                break
            offset += len(page)
    candidates = tuple(
        sorted(
            collected.values(),
            key=lambda candidate: (candidate.published, candidate.arxiv_id.canonical),
            reverse=True,
        )
    )
    checkpoint = RetrievalCheckpoint(completed_at)
    store.commit(checkpoint, candidates)
    return RetrievalResult(candidates, checkpoint, requests)
