"""Safe recommendation orchestration from local scores and validated model proposals."""

from __future__ import annotations

import json
from collections.abc import Callable
from dataclasses import asdict
from datetime import datetime
from time import perf_counter

from zotero_arxiv_daily.arxiv.models import ArxivCandidate
from zotero_arxiv_daily.core.errors import ExternalServiceError
from zotero_arxiv_daily.llm.batch import ProposalProvider, propose_bounded
from zotero_arxiv_daily.llm.cache import ProposalCache
from zotero_arxiv_daily.llm.contracts import ModelProposal, parse_proposals
from zotero_arxiv_daily.profile.models import RemoteProfile
from zotero_arxiv_daily.ranking.models import (
    RecommendationRecord,
    RecommendationRunManifest,
    RecommendationSet,
)
from zotero_arxiv_daily.ranking.select import pre_rank, select_diverse


def recommend(
    candidates: tuple[ArxivCandidate, ...],
    profile: RemoteProfile,
    proposals: tuple[ModelProposal, ...],
    now: datetime,
) -> tuple[RecommendationRecord, ...]:
    """Apply local policy after model validation; models cannot select URLs or state changes."""

    by_id = {proposal.arxiv_id: proposal for proposal in proposals}
    selected = select_diverse(pre_rank(candidates, profile, now))
    records: list[RecommendationRecord] = []
    for item in selected:
        proposal = by_id.get(item.candidate.arxiv_id.canonical)
        if proposal is None or proposal.quality < 0.5:
            continue
        records.append(
            RecommendationRecord(
                item.candidate,
                item.score,
                item.source,
                proposal.quality,
                proposal.summary,
                proposal.reason,
            )
        )
    return tuple(records)


def package_result(
    records: tuple[RecommendationRecord, ...],
    profile: RemoteProfile,
    now: datetime,
    *,
    model: str,
    candidate_count: int,
    model_requests: int,
    cache_hits: int,
    estimated_tokens: int,
    estimated_cost_usd: float = 0.0,
    duration_seconds: float = 0.0,
) -> tuple[RecommendationSet, RecommendationRunManifest]:
    """Create versioned recommendation data and non-sensitive operational metadata."""

    result = RecommendationSet(1, profile.source_library_version, now, records)
    manifest = RecommendationRunManifest(
        1,
        model,
        candidate_count,
        len(records),
        model_requests,
        cache_hits,
        estimated_tokens,
        estimated_cost_usd,
        duration_seconds,
    )
    return result, manifest


def run_recommendation(
    candidates: tuple[ArxivCandidate, ...],
    profile: RemoteProfile,
    now: datetime,
    provider: ProposalProvider,
    cache: ProposalCache,
    *,
    prompt_version: str,
    model: str,
    excluded_ids: frozenset[str] = frozenset(),
    feedback_adjustments: dict[str, float] | None = None,
    pre_rank_limit: int = 60,
    estimate_cost: Callable[[int], float] | None = None,
) -> tuple[RecommendationSet, RecommendationRunManifest]:
    """Run bounded, cached model work while retaining final policy locally."""

    if not 1 <= pre_rank_limit <= 80:
        raise ValueError("pre_rank_limit must be between 1 and 80")
    started = perf_counter()
    eligible = tuple(
        candidate for candidate in candidates if candidate.arxiv_id.canonical not in excluded_ids
    )
    ranked = pre_rank(eligible, profile, now, feedback_adjustments)[:pre_rank_limit]
    selected_candidates = tuple(item.candidate for item in ranked)
    cached, missing, cache_hits = _load_cached_proposals(
        selected_candidates, profile, cache, prompt_version, model
    )
    fresh, usage = propose_bounded(provider, [_model_candidate(item) for item in missing])
    for proposal in fresh:
        cache.put(
            cache.key(proposal.arxiv_id, profile.source_library_version, prompt_version, model),
            json.dumps(asdict(proposal), ensure_ascii=False, separators=(",", ":")),
        )
    records = recommend(selected_candidates, profile, cached + fresh, now)
    duration_seconds = perf_counter() - started
    estimated_cost_usd = estimate_cost(usage.estimated_tokens) if estimate_cost else 0.0
    return package_result(
        records,
        profile,
        now,
        model=model,
        candidate_count=len(selected_candidates),
        model_requests=usage.requests,
        cache_hits=cache_hits,
        estimated_tokens=usage.estimated_tokens,
        estimated_cost_usd=estimated_cost_usd,
        duration_seconds=duration_seconds,
    )


def _load_cached_proposals(
    candidates: tuple[ArxivCandidate, ...],
    profile: RemoteProfile,
    cache: ProposalCache,
    prompt_version: str,
    model: str,
) -> tuple[tuple[ModelProposal, ...], tuple[ArxivCandidate, ...], int]:
    cached: list[ModelProposal] = []
    missing: list[ArxivCandidate] = []
    for candidate in candidates:
        key = cache.key(
            candidate.arxiv_id.canonical, profile.source_library_version, prompt_version, model
        )
        payload = cache.get(key)
        if payload is None:
            missing.append(candidate)
            continue
        try:
            cached.extend(parse_cached_proposal(payload, candidate.arxiv_id.canonical))
        except ExternalServiceError:
            missing.append(candidate)
    return tuple(cached), tuple(missing), len(cached)


def parse_cached_proposal(payload: str, arxiv_id: str) -> tuple[ModelProposal, ...]:
    """Revalidate cached data rather than treating a local cache as trusted."""

    return parse_proposals(f"[{payload}]", frozenset({arxiv_id}))


def _model_candidate(candidate: ArxivCandidate) -> dict[str, object]:
    return {
        "arxiv_id": candidate.arxiv_id.canonical,
        "title": candidate.title[:500],
        "authors": list(candidate.authors[:10]),
        "categories": list(candidate.categories[:10]),
        "published": candidate.published.isoformat(),
        "summary": candidate.summary[:400],
    }
