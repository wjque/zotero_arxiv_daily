"""Inspectable local ranking values and final recommendation records."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime

from zotero_arxiv_daily.arxiv.models import ArxivCandidate
from zotero_arxiv_daily.core.time import require_aware_utc


@dataclass(frozen=True, slots=True)
class ScoredCandidate:
    candidate: ArxivCandidate
    score: float
    components: tuple[tuple[str, float], ...]
    source: str


@dataclass(frozen=True, slots=True)
class RecommendationRecord:
    candidate: ArxivCandidate
    score: float
    source: str
    quality: float
    summary: str
    reason: str
    identity_matches: tuple[str, ...] = ()


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
