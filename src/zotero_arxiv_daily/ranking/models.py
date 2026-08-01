"""Inspectable local ranking values and final recommendation records."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime

from zotero_arxiv_daily.arxiv.models import ArxivCandidate


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
    summary: str
    reason: str


@dataclass(frozen=True, slots=True)
class RecommendationSet:
    schema_version: int
    profile_version: int
    generated_at: datetime
    recommendations: tuple[RecommendationRecord, ...]


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
