"""Safe recommendation orchestration from local scores and validated model proposals."""

from __future__ import annotations

import hashlib
import json
from collections.abc import Callable, Mapping
from dataclasses import asdict
from datetime import UTC, datetime
from time import perf_counter

from zotero_arxiv_daily.arxiv.models import ArxivCandidate
from zotero_arxiv_daily.core.errors import ExternalServiceError
from zotero_arxiv_daily.llm.batch import ProposalProvider, propose_bounded
from zotero_arxiv_daily.llm.cache import ProposalCache
from zotero_arxiv_daily.llm.contracts import ModelProposal, parse_proposals
from zotero_arxiv_daily.profile.models import RemoteProfile
from zotero_arxiv_daily.ranking.models import (
    RECOMMENDATION_RUN_MANIFEST_SCHEMA_VERSION,
    RECOMMENDATION_SET_SCHEMA_VERSION,
    RecommendationRecord,
    RecommendationRunManifest,
    RecommendationSet,
)
from zotero_arxiv_daily.ranking.select import order_recommendations, pre_rank, select_diverse


def recommend(
    candidates: tuple[ArxivCandidate, ...],
    profile: RemoteProfile,
    proposals: tuple[ModelProposal, ...],
    now: datetime,
    *,
    author_bonus: float = 0.75,
    institution_bonus: float = 0.5,
    identity_bonus_cap: float = 1.0,
    feedback_adjustments: Mapping[str, float] | None = None,
) -> tuple[RecommendationRecord, ...]:
    """Apply local policy after model validation; models cannot select URLs or state changes."""

    by_id = {proposal.arxiv_id: proposal for proposal in proposals}
    selected = select_diverse(
        pre_rank(
            candidates,
            profile,
            now,
            feedback_adjustments,
            author_bonus=author_bonus,
            institution_bonus=institution_bonus,
            identity_bonus_cap=identity_bonus_cap,
        )
    )
    records: list[RecommendationRecord] = []
    for item in selected:
        proposal = by_id.get(item.candidate.arxiv_id.canonical)
        if proposal is None or proposal.quality < 0.5:
            continue
        components = dict(item.components)
        identity_matches = tuple(
            name
            for name, component in (
                ("watched_author", components["watched_author"]),
                ("watched_institution", components["watched_institution"]),
            )
            if component > 0
        )
        records.append(
            RecommendationRecord(
                item.candidate,
                item.score,
                item.source,
                proposal.quality,
                proposal.summary,
                proposal.reason,
                identity_matches,
            )
        )
    return order_recommendations(tuple(records))


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
    completed_at: datetime | None = None,
) -> tuple[RecommendationSet, RecommendationRunManifest]:
    """Create versioned recommendation data and non-sensitive operational metadata."""

    completion = (completed_at or now).astimezone(UTC)
    started_at = now.astimezone(UTC)
    result = RecommendationSet(
        RECOMMENDATION_SET_SCHEMA_VERSION,
        profile.source_library_version,
        started_at,
        records,
        completion,
        profile.source_library_synced_at,
    )
    manifest = RecommendationRunManifest(
        RECOMMENDATION_RUN_MANIFEST_SCHEMA_VERSION,
        model,
        candidate_count,
        len(records),
        model_requests,
        cache_hits,
        estimated_tokens,
        estimated_cost_usd,
        duration_seconds,
        started_at,
        completion,
        profile.source_library_version,
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
    author_bonus: float = 0.75,
    institution_bonus: float = 0.5,
    identity_bonus_cap: float = 1.0,
    completed_at: datetime | None = None,
) -> tuple[RecommendationSet, RecommendationRunManifest]:
    """Run bounded, cached model work while retaining final policy locally."""

    if not 1 <= pre_rank_limit <= 80:
        raise ValueError("pre_rank_limit must be between 1 and 80")
    started = perf_counter()
    eligible = tuple(
        candidate for candidate in candidates if candidate.arxiv_id.canonical not in excluded_ids
    )
    ranked = pre_rank(
        eligible,
        profile,
        now,
        feedback_adjustments,
        author_bonus=author_bonus,
        institution_bonus=institution_bonus,
        identity_bonus_cap=identity_bonus_cap,
    )[:pre_rank_limit]
    selected_candidates = tuple(item.candidate for item in ranked)
    cached, missing, cache_hits = _load_cached_proposals(
        selected_candidates, profile, cache, prompt_version, model
    )
    fresh, usage = propose_bounded(provider, [_model_candidate(item) for item in missing])
    candidates_by_id = {candidate.arxiv_id.canonical: candidate for candidate in missing}
    for proposal in fresh:
        candidate = candidates_by_id[proposal.arxiv_id]
        cache.put(
            cache.key(
                proposal.arxiv_id,
                profile.source_library_version,
                prompt_version,
                model,
                _candidate_fingerprint(candidate),
            ),
            json.dumps(asdict(proposal), ensure_ascii=False, separators=(",", ":")),
        )
    records = recommend(
        selected_candidates,
        profile,
        cached + fresh,
        now,
        author_bonus=author_bonus,
        institution_bonus=institution_bonus,
        identity_bonus_cap=identity_bonus_cap,
        feedback_adjustments=feedback_adjustments,
    )
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
        completed_at=completed_at or datetime.now(UTC),
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
            candidate.arxiv_id.canonical,
            profile.source_library_version,
            prompt_version,
            model,
            _candidate_fingerprint(candidate),
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


def _candidate_fingerprint(candidate: ArxivCandidate) -> str:
    value = {
        **_model_candidate(candidate),
        "revision": candidate.arxiv_id.revision,
        "updated": candidate.updated.isoformat(),
    }
    encoded = json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(encoded.encode("utf-8")).hexdigest()
