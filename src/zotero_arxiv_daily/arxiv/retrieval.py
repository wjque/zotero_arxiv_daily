"""Checkpoint-on-success arXiv candidate retrieval orchestration."""

from __future__ import annotations

import math
from datetime import UTC, datetime, timedelta
from typing import Protocol

from zotero_arxiv_daily.arxiv.client import category_query
from zotero_arxiv_daily.arxiv.discovery import DiscoveryQuery, bridge_candidate_matches
from zotero_arxiv_daily.arxiv.models import ArxivCandidate, RetrievalCheckpoint, RetrievalResult
from zotero_arxiv_daily.arxiv.storage import ArxivStateStore
from zotero_arxiv_daily.core.errors import ExternalServiceError

MAX_RETRIEVAL_CANDIDATES = 1000
MAX_BRIDGE_CANDIDATES = 200
MAX_RETRIEVAL_QUERIES = 16
MAX_RETRIEVAL_REQUESTS = 32
MAX_PAGE_SIZE = 100


class CandidateClient(Protocol):
    def query(self, search_query: str, start: int, maximum: int) -> tuple[ArxivCandidate, ...]: ...


def retrieve(
    client: CandidateClient,
    store: ArxivStateStore,
    queries: tuple[DiscoveryQuery, ...],
    now: datetime,
    *,
    candidate_ceiling: int = MAX_RETRIEVAL_CANDIDATES,
    bridge_candidate_ceiling: int = MAX_BRIDGE_CANDIDATES,
    query_ceiling: int = MAX_RETRIEVAL_QUERIES,
    request_ceiling: int = MAX_RETRIEVAL_REQUESTS,
    page_size: int = MAX_PAGE_SIZE,
    overlap: timedelta = timedelta(hours=12),
    stale_pool_max_age: timedelta = timedelta(hours=72),
) -> RetrievalResult:
    """Fetch category pages, deduplicate revisions, then atomically advance the checkpoint."""

    if (
        not 1 <= candidate_ceiling <= MAX_RETRIEVAL_CANDIDATES
        or not 0 <= bridge_candidate_ceiling <= MAX_BRIDGE_CANDIDATES
        or not 1 <= page_size <= MAX_PAGE_SIZE
        or not 1 <= query_ceiling <= MAX_RETRIEVAL_QUERIES
        or not 1 <= request_ceiling <= MAX_RETRIEVAL_REQUESTS
        or not timedelta(0) <= overlap <= timedelta(days=1)
        or not timedelta(0) <= stale_pool_max_age <= timedelta(days=7)
    ):
        raise ValueError("retrieval limits exceed the supported boundary")
    if not queries:
        raise ValueError("at least one retrieval query is required")
    if len(queries) > query_ceiling or len({query.category for query in queries}) != len(queries):
        raise ValueError("retrieval queries exceed the deterministic boundary")
    bridge_queries = tuple(query for query in queries if query.is_bridge)
    category_queries = tuple(query for query in queries if not query.is_bridge)
    if not category_queries:
        raise ValueError("at least one category query is required")
    if bridge_queries and bridge_candidate_ceiling >= candidate_ceiling:
        raise ValueError("bridge candidate ceiling must leave capacity for category results")
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
    bridge_ids: set[str] = set()
    requests = 0
    bridge_budget = bridge_candidate_ceiling if bridge_queries else 0
    category_budget = candidate_ceiling - bridge_budget
    groups = (
        (category_queries, category_budget),
        (bridge_queries, bridge_budget),
    )
    try:
        for group_queries, group_budget in groups:
            if not group_queries or group_budget == 0 or requests >= request_ceiling:
                continue
            per_query_limit = max(1, math.ceil(group_budget / len(group_queries)))
            group_ids: set[str] = set()
            for query in group_queries:
                offset = 0
                query_count = 0
                while (
                    len(collected) < candidate_ceiling
                    and len(group_ids) < group_budget
                    and query_count < per_query_limit
                    and requests < request_ceiling
                ):
                    maximum = min(
                        page_size,
                        per_query_limit - query_count,
                        group_budget - len(group_ids),
                    )
                    requests += 1
                    page = client.query(
                        category_query(query.category, start_gmt, end_gmt), offset, maximum
                    )
                    if len(page) > maximum:
                        raise ExternalServiceError("arXiv returned more candidates than requested")
                    query_count += len(page)
                    for candidate in page:
                        if not bridge_candidate_matches(candidate, query):
                            continue
                        canonical = candidate.arxiv_id.canonical
                        existing = collected.get(canonical)
                        if existing is None:
                            group_ids.add(canonical)
                            if query.is_bridge:
                                bridge_ids.add(canonical)
                        if existing is None or candidate.updated > existing.updated:
                            collected[canonical] = candidate
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
                len(queries),
                len(bridge_queries),
                0,
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
    return RetrievalResult(
        store.candidates(),
        checkpoint,
        requests,
        planned_query_count=len(queries),
        bridge_query_count=len(bridge_queries),
        bridge_candidate_count=len(bridge_ids),
    )


def _degraded_reason(error: ExternalServiceError) -> str:
    """Keep a bounded diagnostic without copying provider responses into runtime state."""

    message = " ".join(str(error).split())
    return (message or error.__class__.__name__)[:160]
