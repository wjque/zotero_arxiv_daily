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
from zotero_arxiv_daily.evidence.paper_sections import (
    PaperSectionClient,
    PaperSections,
    inspect_paper_sections,
)
from zotero_arxiv_daily.evidence.project_page import (
    ProjectPageClient,
    ProjectPageEvidence,
    inspect_project_pages,
)
from zotero_arxiv_daily.evidence.repository_materials import (
    MaterialGrade,
    RepositoryMaterials,
    RepositoryMaterialsClient,
)
from zotero_arxiv_daily.llm.batch import (
    DEFAULT_REQUEST_BYTE_LIMIT,
    DEFAULT_REQUEST_TOKEN_LIMIT,
    ProposalProvider,
    propose_bounded,
)
from zotero_arxiv_daily.llm.cache import ProposalCache
from zotero_arxiv_daily.llm.contracts import (
    Explanation,
    JudgeAssessment,
    ModelProposal,
    QualityDimension,
    parse_proposals,
)
from zotero_arxiv_daily.llm.preference_context import validate_preference_signals
from zotero_arxiv_daily.llm.refinement import (
    EXPLANATION_CONTRACT,
    JUDGE_CONTRACT,
    StructuredProvider,
    run_explanations,
    run_judgments,
)
from zotero_arxiv_daily.profile.models import RemoteProfile
from zotero_arxiv_daily.profile.quality import QualityReferenceProfile
from zotero_arxiv_daily.ranking.baseline import (
    BASELINE_VERSION,
    order_baseline,
    score_baseline,
    select_baseline,
)
from zotero_arxiv_daily.ranking.models import (
    RECOMMENDATION_RUN_MANIFEST_SCHEMA_VERSION,
    RECOMMENDATION_SET_SCHEMA_VERSION,
    RecommendationRecord,
    RecommendationRunManifest,
    RecommendationSet,
    ScoredCandidate,
)
from zotero_arxiv_daily.ranking.select import order_recommendations, pre_rank, select_diverse
from zotero_arxiv_daily.ranking.weights import (
    DEFAULT_WEIGHT_SET,
    FeatureGroup,
    NormalizedFeature,
    WeightSet,
)


def recommend(
    candidates: tuple[ArxivCandidate, ...],
    profile: RemoteProfile,
    proposals: tuple[ModelProposal, ...],
    now: datetime,
    *,
    author_bonus: float = 0.75,
    institution_bonus: float = 0.5,
    identity_bonus_cap: float = 1.0,
    weight_set: WeightSet = DEFAULT_WEIGHT_SET,
) -> tuple[RecommendationRecord, ...]:
    """Apply local policy after model validation; models cannot select URLs or state changes."""

    selected = select_diverse(
        pre_rank(
            candidates,
            profile,
            now,
            author_bonus=author_bonus,
            institution_bonus=institution_bonus,
            identity_bonus_cap=identity_bonus_cap,
            weight_set=weight_set,
        )
    )
    return order_recommendations(_proposal_records(selected, proposals))


def _proposal_records(
    selected: tuple[ScoredCandidate, ...], proposals: tuple[ModelProposal, ...]
) -> tuple[RecommendationRecord, ...]:
    by_id = {proposal.arxiv_id: proposal for proposal in proposals}
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
    completed_at: datetime | None = None,
    estimated_input_tokens: int = 0,
    estimated_output_tokens: int = 0,
    actual_input_tokens: int | None = None,
    actual_output_tokens: int | None = None,
    actual_cost_usd: float | None = None,
    provider_latency_seconds: float | None = None,
    judge_requests: int = 0,
    explanation_requests: int = 0,
    judge_cache_hits: int = 0,
    explanation_cache_hits: int = 0,
    retry_count: int = 0,
    weight_set_version: str = DEFAULT_WEIGHT_SET.version,
    preference_context_enabled: bool = False,
    quality_profile: QualityReferenceProfile | None = None,
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
        schema_version=RECOMMENDATION_RUN_MANIFEST_SCHEMA_VERSION,
        model=model,
        candidate_count=candidate_count,
        recommendation_count=len(records),
        model_requests=model_requests,
        cache_hits=cache_hits,
        estimated_tokens=estimated_tokens,
        estimated_cost_usd=estimated_cost_usd,
        duration_seconds=duration_seconds,
        generation_started_at=started_at,
        generation_completed_at=completion,
        profile_library_version=profile.source_library_version,
        estimated_input_tokens=estimated_input_tokens,
        estimated_output_tokens=estimated_output_tokens,
        actual_input_tokens=actual_input_tokens,
        actual_output_tokens=actual_output_tokens,
        judge_requests=judge_requests,
        explanation_requests=explanation_requests,
        judge_cache_hits=judge_cache_hits,
        explanation_cache_hits=explanation_cache_hits,
        retry_count=retry_count,
        weight_set_version=weight_set_version,
        actual_cost_usd=actual_cost_usd,
        provider_latency_seconds=provider_latency_seconds,
        preference_context_enabled=preference_context_enabled,
        quality_profile_version=quality_profile.version if quality_profile else None,
        quality_profile_criterion_count=quality_profile.criterion_count if quality_profile else 0,
        quality_profile_feedback_event_count=(
            quality_profile.explicit_feedback_event_count if quality_profile else 0
        ),
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
    pre_rank_limit: int = 60,
    estimate_cost: Callable[[int], float] | None = None,
    author_bonus: float = 0.75,
    institution_bonus: float = 0.5,
    identity_bonus_cap: float = 1.0,
    completed_at: datetime | None = None,
    weight_set: WeightSet = DEFAULT_WEIGHT_SET,
    batch_size: int = 40,
    max_requests: int = 2,
    max_request_tokens: int = DEFAULT_REQUEST_TOKEN_LIMIT,
    max_request_bytes: int = DEFAULT_REQUEST_BYTE_LIMIT,
    retries: int = 1,
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
        author_bonus=author_bonus,
        institution_bonus=institution_bonus,
        identity_bonus_cap=identity_bonus_cap,
        weight_set=weight_set,
    )[:pre_rank_limit]
    selected_candidates = tuple(item.candidate for item in ranked)
    cached, missing, cache_hits = _load_cached_proposals(
        selected_candidates, profile, cache, prompt_version, model
    )
    fresh, usage = propose_bounded(
        provider,
        [_model_candidate(item) for item in missing],
        batch_size=batch_size,
        max_requests=max_requests,
        max_tokens=max_request_tokens,
        max_request_bytes=max_request_bytes,
        retries=retries,
    )
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
        weight_set=weight_set,
    )
    duration_seconds = perf_counter() - started
    estimated_cost_usd = estimate_cost(usage.estimated_tokens) if estimate_cost else 0.0
    actual_tokens = (
        usage.actual_input_tokens + usage.actual_output_tokens
        if usage.actual_input_tokens is not None and usage.actual_output_tokens is not None
        else None
    )
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
        estimated_input_tokens=usage.estimated_tokens,
        estimated_output_tokens=0,
        actual_input_tokens=usage.actual_input_tokens,
        actual_output_tokens=usage.actual_output_tokens,
        actual_cost_usd=(
            estimate_cost(actual_tokens) if estimate_cost and actual_tokens is not None else None
        ),
        provider_latency_seconds=usage.latency_seconds,
        retry_count=usage.retry_count,
        weight_set_version=weight_set.version,
    )


def run_baseline_recommendation(
    candidates: tuple[ArxivCandidate, ...],
    profile: RemoteProfile,
    now: datetime,
    provider: ProposalProvider,
    cache: ProposalCache,
    *,
    prompt_version: str,
    model: str,
    excluded_ids: frozenset[str] = frozenset(),
    pre_rank_limit: int = 40,
    estimate_cost: Callable[[int], float] | None = None,
    author_bonus: float = 0.75,
    institution_bonus: float = 0.5,
    identity_bonus_cap: float = 1.0,
    completed_at: datetime | None = None,
    batch_size: int = 40,
    max_requests: int = 2,
    max_request_tokens: int = DEFAULT_REQUEST_TOKEN_LIMIT,
    max_request_bytes: int = DEFAULT_REQUEST_BYTE_LIMIT,
    retries: int = 1,
) -> tuple[RecommendationSet, RecommendationRunManifest]:
    """Run frozen v0.1.2 ranking through current safe provider and state boundaries."""

    if not 1 <= pre_rank_limit <= 80:
        raise ValueError("pre_rank_limit must be between 1 and 80")
    started = perf_counter()
    eligible = tuple(
        candidate for candidate in candidates if candidate.arxiv_id.canonical not in excluded_ids
    )
    ranked = score_baseline(
        eligible,
        profile,
        now,
        author_bonus=author_bonus,
        institution_bonus=institution_bonus,
        identity_bonus_cap=identity_bonus_cap,
    )[:pre_rank_limit]
    selected_candidates = tuple(item.candidate for item in ranked)
    cached, missing, cache_hits = _load_cached_proposals(
        selected_candidates, profile, cache, prompt_version, model
    )
    fresh, usage = propose_bounded(
        provider,
        [_model_candidate(item) for item in missing],
        batch_size=batch_size,
        max_requests=max_requests,
        max_tokens=max_request_tokens,
        max_request_bytes=max_request_bytes,
        retries=retries,
    )
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
    selected = select_baseline(
        score_baseline(
            selected_candidates,
            profile,
            now,
            author_bonus=author_bonus,
            institution_bonus=institution_bonus,
            identity_bonus_cap=identity_bonus_cap,
        )
    )
    records = order_baseline(_proposal_records(selected, cached + fresh))
    duration_seconds = perf_counter() - started
    estimated_cost_usd = estimate_cost(usage.estimated_tokens) if estimate_cost else 0.0
    actual_tokens = (
        usage.actual_input_tokens + usage.actual_output_tokens
        if usage.actual_input_tokens is not None and usage.actual_output_tokens is not None
        else None
    )
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
        estimated_input_tokens=usage.estimated_tokens,
        estimated_output_tokens=0,
        actual_input_tokens=usage.actual_input_tokens,
        actual_output_tokens=usage.actual_output_tokens,
        actual_cost_usd=(
            estimate_cost(actual_tokens) if estimate_cost and actual_tokens is not None else None
        ),
        provider_latency_seconds=usage.latency_seconds,
        retry_count=usage.retry_count,
        weight_set_version=BASELINE_VERSION,
    )


def run_refined_recommendation(
    candidates: tuple[ArxivCandidate, ...],
    profile: RemoteProfile,
    now: datetime,
    provider: StructuredProvider,
    cache: ProposalCache,
    *,
    model: str,
    output_language: str,
    excluded_ids: frozenset[str] = frozenset(),
    pre_rank_limit: int = 60,
    estimate_cost: Callable[[int], float] | None = None,
    author_bonus: float = 0.75,
    institution_bonus: float = 0.5,
    identity_bonus_cap: float = 1.0,
    weight_set: WeightSet = DEFAULT_WEIGHT_SET,
    project_page_client: ProjectPageClient | None = None,
    paper_section_client: PaperSectionClient | None = None,
    repository_materials_client: RepositoryMaterialsClient | None = None,
    quality_profile: QualityReferenceProfile | None = None,
    allow_preference_context: bool = False,
    completed_at: datetime | None = None,
    judge_batch_size: int = 20,
    explanation_batch_size: int = 10,
    max_request_tokens: int = DEFAULT_REQUEST_TOKEN_LIMIT,
    max_request_bytes: int = DEFAULT_REQUEST_BYTE_LIMIT,
    max_requests: int = 8,
    retries: int = 1,
) -> tuple[RecommendationSet, RecommendationRunManifest]:
    """Run judge-v3 then final-only explain-v2 without granting model control over selection."""

    if not 1 <= pre_rank_limit <= 80:
        raise ValueError("pre_rank_limit must be between 1 and 80")
    started = perf_counter()
    eligible = tuple(
        candidate for candidate in candidates if candidate.arxiv_id.canonical not in excluded_ids
    )
    preliminary = pre_rank(
        eligible,
        profile,
        now,
        author_bonus=author_bonus,
        institution_bonus=institution_bonus,
        identity_bonus_cap=identity_bonus_cap,
        weight_set=weight_set,
    )[:pre_rank_limit]
    preliminary_candidates = tuple(item.candidate for item in preliminary)
    project_pages = inspect_project_pages(preliminary_candidates, project_page_client, cache, now)
    coarse = pre_rank(
        preliminary_candidates,
        profile,
        now,
        author_bonus=author_bonus,
        institution_bonus=institution_bonus,
        identity_bonus_cap=identity_bonus_cap,
        weight_set=weight_set,
        extra_features={
            identifier: (_project_page_feature(value),)
            for identifier, value in project_pages.items()
        },
    )[:pre_rank_limit]
    shortlisted = tuple(item.candidate for item in coarse)
    if not shortlisted:
        return package_result(
            (),
            profile,
            now,
            model=model,
            candidate_count=0,
            model_requests=0,
            cache_hits=0,
            estimated_tokens=0,
            duration_seconds=perf_counter() - started,
            completed_at=completed_at or datetime.now(UTC),
            weight_set_version=weight_set.version,
        )
    paper_sections = inspect_paper_sections(shortlisted, paper_section_client, cache, now)
    shortlisted_ids = {candidate.arxiv_id.canonical for candidate in shortlisted}
    repository_materials = {
        identifier: (
            repository_materials_client.inspect(project_page)
            if repository_materials_client is not None
            else RepositoryMaterials(None, MaterialGrade.UNKNOWN, "not-inspected")
        )
        for identifier, project_page in project_pages.items()
        if identifier in shortlisted_ids
    }
    profile_digest = _profile_digest(profile)
    judge_records = tuple(
        _judge_record(
            candidate,
            paper_sections[candidate.arxiv_id.canonical],
            quality_profile,
        )
        for candidate in shortlisted
    )
    judge_keys = _layered_keys(
        cache,
        "judge",
        shortlisted,
        profile_digest,
        model,
        output_language,
        JUDGE_CONTRACT,
        evidence_snapshot=_records_digest(judge_records),
    )
    judgments, judge_usage = run_judgments(
        provider,
        cache,
        judge_records,
        cache_keys=judge_keys,
        allowed_evidence_fields=frozenset(
            {
                "title",
                "authors",
                "categories",
                "published",
                "summary",
                "method_evidence",
                "evaluation_evidence",
                "limitations_evidence",
                "quality_reference",
            }
        ),
        batch_size=judge_batch_size,
        max_request_tokens=max_request_tokens,
        max_request_bytes=max_request_bytes,
        max_requests=max_requests,
        retries=retries,
    )
    assessments = {assessment.arxiv_id: assessment for assessment in judgments}
    judged = pre_rank(
        shortlisted,
        profile,
        now,
        author_bonus=author_bonus,
        institution_bonus=institution_bonus,
        identity_bonus_cap=identity_bonus_cap,
        weight_set=weight_set,
        extra_features={
            identifier: _assessment_features(
                value, project_pages[identifier], repository_materials[identifier]
            )
            for identifier, value in assessments.items()
        },
    )
    selected = select_diverse(judged)
    if not selected:
        tokens = judge_usage.estimated_input_tokens + judge_usage.estimated_output_tokens
        actual_tokens = (
            judge_usage.actual_input_tokens + judge_usage.actual_output_tokens
            if judge_usage.actual_input_tokens is not None
            and judge_usage.actual_output_tokens is not None
            else None
        )
        return package_result(
            (),
            profile,
            now,
            model=model,
            candidate_count=len(shortlisted),
            model_requests=judge_usage.requests,
            cache_hits=judge_usage.cache_hits,
            estimated_tokens=tokens,
            estimated_cost_usd=estimate_cost(tokens) if estimate_cost else 0.0,
            duration_seconds=perf_counter() - started,
            completed_at=completed_at or datetime.now(UTC),
            estimated_input_tokens=judge_usage.estimated_input_tokens,
            estimated_output_tokens=judge_usage.estimated_output_tokens,
            actual_input_tokens=judge_usage.actual_input_tokens,
            actual_output_tokens=judge_usage.actual_output_tokens,
            actual_cost_usd=estimate_cost(actual_tokens)
            if estimate_cost and actual_tokens is not None
            else None,
            provider_latency_seconds=judge_usage.latency_seconds,
            judge_requests=judge_usage.requests,
            judge_cache_hits=judge_usage.cache_hits,
            retry_count=judge_usage.retry_count,
            weight_set_version=weight_set.version,
            preference_context_enabled=allow_preference_context,
            quality_profile=quality_profile,
        )
    explanation_records = tuple(
        _explanation_record(
            item, assessments[item.candidate.arxiv_id.canonical], allow_preference_context
        )
        for item in selected
    )
    selected_candidates = tuple(item.candidate for item in selected)
    explanation_keys = _layered_keys(
        cache,
        "explain",
        selected_candidates,
        profile_digest,
        model,
        output_language,
        f"{EXPLANATION_CONTRACT}:preference-context-v1"
        if allow_preference_context
        else f"{EXPLANATION_CONTRACT}:public-only",
        evidence_snapshot=_records_digest(explanation_records),
    )
    explanation_fields = {
        "title",
        "authors",
        "categories",
        "published",
        "summary",
        "quality_dimensions",
        "quality_uncertainty",
    }
    if allow_preference_context:
        explanation_fields.add("relevance_signals")
    explanations, explain_usage = run_explanations(
        provider,
        cache,
        explanation_records,
        cache_keys=explanation_keys,
        allowed_evidence_fields=frozenset(explanation_fields),
        batch_size=explanation_batch_size,
        max_request_tokens=max_request_tokens,
        max_request_bytes=max_request_bytes,
        max_requests=max_requests,
        retries=retries,
    )
    records = _refined_records(
        selected,
        assessments,
        {item.arxiv_id: item for item in explanations},
        paper_sections,
        repository_materials,
    )
    input_tokens = judge_usage.estimated_input_tokens + explain_usage.estimated_input_tokens
    output_tokens = judge_usage.estimated_output_tokens + explain_usage.estimated_output_tokens
    tokens = input_tokens + output_tokens
    actual_input_tokens = (
        judge_usage.actual_input_tokens + explain_usage.actual_input_tokens
        if judge_usage.actual_input_tokens is not None
        and explain_usage.actual_input_tokens is not None
        else None
    )
    actual_output_tokens = (
        judge_usage.actual_output_tokens + explain_usage.actual_output_tokens
        if judge_usage.actual_output_tokens is not None
        and explain_usage.actual_output_tokens is not None
        else None
    )
    actual_tokens = (
        actual_input_tokens + actual_output_tokens
        if actual_input_tokens is not None and actual_output_tokens is not None
        else None
    )
    provider_latency = (
        judge_usage.latency_seconds + explain_usage.latency_seconds
        if judge_usage.latency_seconds is not None and explain_usage.latency_seconds is not None
        else None
    )
    return package_result(
        records,
        profile,
        now,
        model=model,
        candidate_count=len(shortlisted),
        model_requests=judge_usage.requests + explain_usage.requests,
        cache_hits=judge_usage.cache_hits + explain_usage.cache_hits,
        estimated_tokens=tokens,
        estimated_cost_usd=estimate_cost(tokens) if estimate_cost else 0.0,
        duration_seconds=perf_counter() - started,
        completed_at=completed_at or datetime.now(UTC),
        estimated_input_tokens=input_tokens,
        estimated_output_tokens=output_tokens,
        actual_input_tokens=actual_input_tokens,
        actual_output_tokens=actual_output_tokens,
        actual_cost_usd=estimate_cost(actual_tokens)
        if estimate_cost and actual_tokens is not None
        else None,
        provider_latency_seconds=provider_latency,
        judge_requests=judge_usage.requests,
        explanation_requests=explain_usage.requests,
        judge_cache_hits=judge_usage.cache_hits,
        explanation_cache_hits=explain_usage.cache_hits,
        retry_count=judge_usage.retry_count + explain_usage.retry_count,
        weight_set_version=weight_set.version,
        preference_context_enabled=allow_preference_context,
        quality_profile=quality_profile,
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
    """Project public metadata without truncating complete paper text fields."""

    return {
        "arxiv_id": candidate.arxiv_id.canonical,
        "title": candidate.title,
        "authors": list(candidate.authors[:10]),
        "categories": list(candidate.categories[:10]),
        "published": candidate.published.isoformat(),
        "summary": candidate.summary,
    }


def _candidate_fingerprint(candidate: ArxivCandidate) -> str:
    value = {
        **_model_candidate(candidate),
        "revision": candidate.arxiv_id.revision,
        "updated": candidate.updated.isoformat(),
    }
    encoded = json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(encoded.encode("utf-8")).hexdigest()


def _judge_record(
    candidate: ArxivCandidate,
    sections: PaperSections,
    quality_profile: QualityReferenceProfile | None,
) -> dict[str, object]:
    """Keep quality assessment limited to bounded, explicitly labeled public evidence."""

    record = _model_candidate(candidate)
    for name, value in (
        ("method_evidence", sections.method),
        ("evaluation_evidence", sections.evaluation),
        ("limitations_evidence", sections.limitations),
    ):
        if value is not None:
            record[name] = value
    if quality_profile is not None:
        record["quality_reference"] = quality_profile.prompt_payload()
    return record


def _assessment_features(
    assessment: JudgeAssessment,
    project_page: ProjectPageEvidence,
    repository_materials: RepositoryMaterials,
) -> tuple[NormalizedFeature, ...]:
    dimensions = dict(assessment.dimensions)
    quality_dimensions = (
        QualityDimension.CONTRIBUTION_CLARITY,
        QualityDimension.NOVELTY,
        QualityDimension.INSIGHT_PLAUSIBILITY,
        QualityDimension.METHODOLOGICAL_EVIDENCE,
        QualityDimension.EMPIRICAL_EVIDENCE,
    )
    quality_values: list[float] = []
    for dimension in quality_dimensions:
        value = dimensions[dimension]
        if value is not None:
            quality_values.append(value)
    confidence = 1.0 - assessment.uncertainty
    quality = sum(quality_values) / len(quality_values) if quality_values else 0.0
    return (
        NormalizedFeature(
            "judge_quality",
            quality,
            bool(quality_values),
            confidence if quality_values else 0.0,
            JUDGE_CONTRACT,
            FeatureGroup.SCIENTIFIC_QUALITY,
        ),
        _project_page_feature(project_page),
        NormalizedFeature(
            "implementation_material",
            repository_materials.score,
            repository_materials.available,
            1.0 if repository_materials.available else 0.0,
            repository_materials.provenance,
            FeatureGroup.REPRODUCIBILITY,
        ),
    )


def _project_page_feature(project_page: ProjectPageEvidence) -> NormalizedFeature:
    known = project_page.url is not None and project_page.reachable is not None
    return NormalizedFeature(
        "accessible_project_page",
        1.0 if project_page.reachable is True else 0.0,
        known,
        1.0 if known else 0.0,
        "abstract-project-page-v1",
        FeatureGroup.REPRODUCIBILITY,
    )


def _explanation_record(
    item: ScoredCandidate, assessment: JudgeAssessment, allow_preference_context: bool
) -> dict[str, object]:
    record = _model_candidate(item.candidate)
    record["quality_dimensions"] = {
        dimension.value: value for dimension, value in assessment.dimensions
    }
    record["quality_uncertainty"] = assessment.uncertainty
    if allow_preference_context:
        record["relevance_signals"] = list(_relevance_signals(item))
    return record


def _relevance_signals(item: ScoredCandidate) -> tuple[str, ...]:
    components = dict(item.components)
    signals: list[str] = []
    if components.get("lexical", 0.0) > 0:
        signals.append("topic_overlap")
    if components.get("category", 0.0) >= 0.6:
        signals.append("category_overlap")
    if components.get("facet", 0.0) > 0:
        signals.append("preference_facet_overlap")
    if components.get("watched_author", 0.0) > 0:
        signals.append("watched_author")
    if components.get("watched_institution", 0.0) > 0:
        signals.append("watched_institution")
    return validate_preference_signals(tuple(signals[:4]))


def _refined_records(
    selected: tuple[ScoredCandidate, ...],
    assessments: Mapping[str, JudgeAssessment],
    explanations: Mapping[str, Explanation],
    sections: Mapping[str, PaperSections],
    materials: Mapping[str, RepositoryMaterials],
) -> tuple[RecommendationRecord, ...]:
    records: list[RecommendationRecord] = []
    for item in selected:
        identifier = item.candidate.arxiv_id.canonical
        assessment = assessments[identifier]
        explanation = explanations[identifier]
        paper_sections = sections[identifier]
        repository_materials = materials[identifier]
        components = dict(item.components)
        identities = tuple(
            name
            for name in ("watched_author", "watched_institution")
            if components.get(name, 0.0) > 0
        )
        records.append(
            RecommendationRecord(
                item.candidate,
                item.score,
                item.source,
                _quality_score(assessment),
                explanation.summary,
                explanation.reason,
                identities,
                explanation.limitation,
                assessment.uncertainty,
                assessment.evidence_fields,
                repository_materials.score if repository_materials.available else None,
                repository_materials.grade.value,
                (
                    "arxiv-metadata",
                    *(("ar5iv-sections-v1",) if paper_sections.available_fields else ()),
                    *((repository_materials.provenance,) if repository_materials.available else ()),
                ),
            )
        )
    return order_recommendations(tuple(records))


def _quality_score(assessment: JudgeAssessment) -> float:
    dimensions = {
        QualityDimension.CONTRIBUTION_CLARITY,
        QualityDimension.NOVELTY,
        QualityDimension.INSIGHT_PLAUSIBILITY,
        QualityDimension.METHODOLOGICAL_EVIDENCE,
        QualityDimension.EMPIRICAL_EVIDENCE,
    }
    values: list[float] = []
    for dimension, value in assessment.dimensions:
        if dimension in dimensions and value is not None:
            values.append(value)
    if not values:
        return 0.0
    confidence = 1.0 - assessment.uncertainty
    return (sum(values) / len(values)) * confidence


def _profile_digest(profile: RemoteProfile) -> str:
    """Hash local ranking inputs for local cache invalidation without serializing them to logs."""

    value = {
        "schema_version": profile.schema_version,
        "library_version": profile.source_library_version,
        "topics": profile.topics,
        "core_categories": profile.core_categories,
        "adjacent_categories": profile.adjacent_categories,
        "representative_terms": profile.representative_terms,
        "watched_authors": tuple(
            (identity.name, identity.aliases) for identity in profile.watched_authors
        ),
        "watched_institutions": tuple(
            (identity.name, identity.aliases) for identity in profile.watched_institutions
        ),
        "facets": tuple(
            (facet.kind, facet.value, facet.score, facet.confidence)
            for facet in profile.preference_facets
        ),
    }
    encoded = json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(encoded.encode("utf-8")).hexdigest()


def _layered_keys(
    cache: ProposalCache,
    layer: str,
    candidates: tuple[ArxivCandidate, ...],
    profile_digest: str,
    model: str,
    output_language: str,
    contract_version: str,
    *,
    evidence_snapshot: str = "no-public-evidence-v1",
) -> dict[str, str]:
    return {
        candidate.arxiv_id.canonical: cache.layered_key(
            layer=layer,
            arxiv_id=candidate.arxiv_id.canonical,
            candidate_fingerprint=_candidate_fingerprint(candidate),
            protected_profile_digest=profile_digest,
            evidence_snapshot=evidence_snapshot,
            contract_version=contract_version,
            model=model,
            output_language=output_language,
        )
        for candidate in candidates
    }


def _records_digest(records: tuple[dict[str, object], ...]) -> str:
    encoded = json.dumps(records, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(encoded.encode("utf-8")).hexdigest()
