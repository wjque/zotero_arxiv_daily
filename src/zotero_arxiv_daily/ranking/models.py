"""Inspectable local ranking values and final recommendation records."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime

from zotero_arxiv_daily.arxiv.models import ArxivCandidate
from zotero_arxiv_daily.core.time import require_aware_utc
from zotero_arxiv_daily.ranking.weights import NormalizedFeature

RECOMMENDATION_SET_SCHEMA_VERSION = 2
RECOMMENDATION_RUN_MANIFEST_SCHEMA_VERSION = 1


@dataclass(frozen=True, slots=True)
class ScoredCandidate:
    candidate: ArxivCandidate
    score: float
    components: tuple[tuple[str, float], ...]
    source: str
    feature_values: tuple[NormalizedFeature, ...] = ()
    weight_set_version: str = "quality-first-v1"


@dataclass(frozen=True, slots=True)
class ScientificValueAssessment:
    """Evidence-bounded values used only by deterministic local selection gates."""

    solution_advance: float | None
    technical_depth: float | None
    confidence: float

    def __post_init__(self) -> None:
        values = (self.solution_advance, self.technical_depth)
        if any(value is not None and not 0 <= value <= 1 for value in values):
            raise ValueError("scientific value scores must be normalized")
        if not 0 <= self.confidence <= 1:
            raise ValueError("scientific value confidence must be normalized")


@dataclass(frozen=True, slots=True)
class RecommendationRecord:
    candidate: ArxivCandidate
    score: float
    source: str
    quality: float
    summary: str
    reason: str
    identity_matches: tuple[str, ...] = ()
    limitation: str | None = None
    uncertainty: float | None = None
    quality_evidence_fields: tuple[str, ...] = ()
    reproducibility: float | None = None
    reproducibility_evidence: str = "unknown"
    evidence_provenance: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        if self.uncertainty is not None and not 0 <= self.uncertainty <= 1:
            raise ValueError("recommendation uncertainty must be between zero and one")
        if self.reproducibility is not None and not 0 <= self.reproducibility <= 1:
            raise ValueError("recommendation reproducibility must be between zero and one")
        allowed_reproducibility = {
            "unknown",
            "documentation_only",
            "implementation",
            "implementation_and_evaluation",
            "implementation_data_and_evaluation",
        }
        if self.reproducibility_evidence not in allowed_reproducibility:
            raise ValueError("recommendation reproducibility evidence is invalid")


@dataclass(frozen=True, slots=True)
class RecommendationSet:
    schema_version: int
    profile_version: int
    generation_started_at: datetime
    recommendations: tuple[RecommendationRecord, ...]
    generation_completed_at: datetime | None = None
    profile_snapshot_at: str | None = None

    def __post_init__(self) -> None:
        started = require_aware_utc(self.generation_started_at, "generation_started_at")
        if self.generation_completed_at is not None:
            completed = require_aware_utc(self.generation_completed_at, "generation_completed_at")
            if completed < started:
                raise ValueError("generation completion cannot precede its start")

    @property
    def generated_at(self) -> datetime:
        """Compatibility alias for callers that consumed the v1 start instant."""

        return self.generation_started_at


@dataclass(frozen=True, slots=True)
class RecommendationRunManifest:
    schema_version: int
    model: str
    candidate_count: int
    recommendation_count: int
    model_requests: int
    cache_hits: int
    estimated_tokens: int
    estimated_cost_usd: float
    duration_seconds: float
    generation_started_at: datetime | None = None
    generation_completed_at: datetime | None = None
    profile_library_version: int | None = None
    estimated_input_tokens: int = 0
    estimated_output_tokens: int = 0
    actual_input_tokens: int | None = None
    actual_output_tokens: int | None = None
    judge_requests: int = 0
    explanation_requests: int = 0
    judge_cache_hits: int = 0
    explanation_cache_hits: int = 0
    retry_count: int = 0
    weight_set_version: str = "quality-first-v1"
    candidate_pool_degraded: bool = False
    candidate_pool_degraded_reason: str | None = None
    candidate_pool_source_checkpoint: datetime | None = None
    actual_cost_usd: float | None = None
    provider_latency_seconds: float | None = None
    preference_context_enabled: bool = False
    quality_profile_version: str | None = None
    quality_profile_criterion_count: int = 0
    quality_profile_feedback_event_count: int = 0
    scientific_value_filtered_count: int = 0
