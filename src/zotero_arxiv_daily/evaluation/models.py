"""Typed local-only contracts for curated judgments and offline evaluation."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum

from zotero_arxiv_daily.core.time import require_aware_utc

CURATED_CORPUS_SCHEMA_VERSION = 1
EVALUATION_SNAPSHOT_SCHEMA_VERSION = 1


class JudgmentKind(StrEnum):
    """The explicit decision represented by a corpus event."""

    LABEL = "label"
    PAIRWISE = "pairwise"
    UNLABEL = "unlabel"


class CorpusLabel(StrEnum):
    """A relevance label that is never inferred from missing feedback."""

    POSITIVE = "positive"
    NEGATIVE = "negative"


@dataclass(frozen=True, slots=True)
class CorpusEvent:
    """An append-only local judgment or correction event."""

    event_id: str
    kind: JudgmentKind
    paper_id: str
    occurred_at: datetime
    source: str
    label: CorpusLabel | None = None
    compared_paper_id: str | None = None
    reason_codes: tuple[str, ...] = ()
    applicable_dimensions: tuple[str, ...] = ()
    private_rationale: str | None = None
    supersedes_event_id: str | None = None
    source_item_key: str | None = None

    def __post_init__(self) -> None:
        require_aware_utc(self.occurred_at, "occurred_at")
        if not self.event_id.strip() or not self.paper_id.strip() or not self.source.strip():
            raise ValueError("event_id, paper_id, and source must not be empty")
        if len(self.reason_codes) > 16 or len(self.applicable_dimensions) > 16:
            raise ValueError("corpus event has too many categorical values")
        if any(not value.strip() or len(value) > 80 for value in self.reason_codes):
            raise ValueError("corpus event contains an invalid reason code")
        if any(not value.strip() or len(value) > 80 for value in self.applicable_dimensions):
            raise ValueError("corpus event contains an invalid applicability dimension")
        if self.private_rationale is not None and len(self.private_rationale) > 4_000:
            raise ValueError("private rationale exceeds the local storage limit")
        if self.kind is JudgmentKind.LABEL and self.label is None:
            raise ValueError("label events require a label")
        if self.kind is JudgmentKind.PAIRWISE and not self.compared_paper_id:
            raise ValueError("pairwise events require a compared paper")
        if self.kind is JudgmentKind.UNLABEL and (
            self.label is not None or self.compared_paper_id is not None
        ):
            raise ValueError("unlabel events cannot carry a label or comparison")


@dataclass(frozen=True, slots=True)
class ResolvedJudgment:
    """The latest effective label or pairwise decision at a cutoff instant."""

    paper_id: str
    label: CorpusLabel | None
    resolved_at: datetime
    event_id: str
    source: str
    reason_codes: tuple[str, ...] = ()
    compared_paper_id: str | None = None


@dataclass(frozen=True, slots=True)
class CorpusSnapshot:
    """A deterministic view of the evolving ledger at one cutoff."""

    schema_version: int
    revision: int
    digest: str
    cutoff_at: datetime
    labels: tuple[ResolvedJudgment, ...]
    pairwise: tuple[ResolvedJudgment, ...]
    conflict_count: int

    def __post_init__(self) -> None:
        require_aware_utc(self.cutoff_at, "cutoff_at")


@dataclass(frozen=True, slots=True)
class EvaluationSplit:
    """Named immutable paper identities used for one evaluation purpose."""

    name: str
    paper_ids: tuple[str, ...]

    def __post_init__(self) -> None:
        if not self.name.strip() or len(set(self.paper_ids)) != len(self.paper_ids):
            raise ValueError("evaluation split must have a name and unique paper IDs")


@dataclass(frozen=True, slots=True)
class EvaluationSnapshot:
    """Persisted evaluation input, independent from future corpus edits."""

    schema_version: int
    snapshot_id: str
    corpus_revision: int
    corpus_digest: str
    cutoff_at: datetime
    splits: tuple[EvaluationSplit, ...]
    labels: tuple[tuple[str, CorpusLabel], ...]
    pairwise_preferences: tuple[tuple[str, str], ...]
    label_count: int
    negative_count: int
    pairwise_count: int
    conflict_count: int
    created_at: datetime

    def __post_init__(self) -> None:
        require_aware_utc(self.cutoff_at, "cutoff_at")
        require_aware_utc(self.created_at, "created_at")
        if not self.snapshot_id.strip() or len({split.name for split in self.splits}) != len(
            self.splits
        ):
            raise ValueError("evaluation snapshot must have an ID and uniquely named splits")
        if len({paper_id for paper_id, _ in self.labels}) != len(self.labels):
            raise ValueError("evaluation snapshot must have unique resolved labels")
        if self.label_count != len(self.labels) or self.pairwise_count != len(
            self.pairwise_preferences
        ):
            raise ValueError("evaluation snapshot counts must match frozen judgments")


@dataclass(frozen=True, slots=True)
class RankedPaper:
    """A non-sensitive scored paper projection for an offline evaluation run."""

    paper_id: str
    score: float
    source: str | None = None
    category: str | None = None
    facets: tuple[str, ...] = ()


@dataclass(frozen=True, slots=True)
class RankingMetrics:
    """Core metric values, with unavailable values represented by ``None``."""

    evaluated_labels: int
    positive_labels: int
    negative_labels: int
    recall_at_k: float | None
    precision_at_k: float | None
    ndcg_at_k: float | None
    negative_rate_at_k: float | None
    source_coverage_at_k: int
    category_coverage_at_k: int
    intra_list_diversity_at_k: float | None
    brier_score: float | None
    pairwise_accuracy: float | None
    provisional: bool
    insufficiency_reason: str | None


@dataclass(frozen=True, slots=True)
class ComparisonReport:
    """A deterministic baseline/candidate comparison without production side effects."""

    baseline_name: str
    candidate_name: str
    snapshot_id: str
    baseline: RankingMetrics
    candidate: RankingMetrics
    ndcg_delta: float | None
    recall_delta: float | None
    negative_rate_delta: float | None
    eligible_for_tuning: bool
    reasons: tuple[str, ...]
