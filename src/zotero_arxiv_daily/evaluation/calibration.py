"""Deterministic local shadow evaluation and feature-group ablation reports."""

from __future__ import annotations

import json
import os
import re
import tempfile
from dataclasses import asdict, dataclass
from datetime import datetime
from pathlib import Path

from zotero_arxiv_daily.arxiv.models import ArxivCandidate
from zotero_arxiv_daily.evaluation.models import (
    ComparisonReport,
    EvaluationSnapshot,
    RankedPaper,
    RankingMetrics,
)
from zotero_arxiv_daily.evaluation.offline import compare_rankings, evaluate_snapshot_ranking
from zotero_arxiv_daily.profile.models import RemoteProfile
from zotero_arxiv_daily.ranking.baseline import BASELINE_VERSION, score_baseline
from zotero_arxiv_daily.ranking.select import pre_rank
from zotero_arxiv_daily.ranking.weights import FeatureGroup, WeightSet

SHADOW_REPORT_SCHEMA_VERSION = 1
_WORDS = re.compile(r"[a-z][a-z0-9-]{2,}")


@dataclass(frozen=True, slots=True)
class FeatureAblation:
    """The metric change caused by removing one normalized feature group."""

    group: FeatureGroup
    metrics_at_20: RankingMetrics
    ndcg_delta_from_candidate: float | None
    recall_delta_from_candidate: float | None


@dataclass(frozen=True, slots=True)
class ShadowReport:
    """A non-mutating comparison of the frozen baseline and a candidate weight set."""

    schema_version: int
    snapshot_id: str
    split: str
    baseline_version: str
    weight_set_version: str
    baseline_at_20: RankingMetrics
    candidate_at_20: RankingMetrics
    baseline_at_60: RankingMetrics
    candidate_at_60: RankingMetrics
    comparison: ComparisonReport
    ablations: tuple[FeatureAblation, ...]
    eligible_for_activation: bool
    reasons: tuple[str, ...]
    warnings: tuple[str, ...] = ()


def run_shadow_evaluation(
    candidates: tuple[ArxivCandidate, ...],
    profile: RemoteProfile,
    snapshot: EvaluationSnapshot,
    now: datetime,
    *,
    split: str = "temporal-holdout",
    weight_set: WeightSet,
) -> ShadowReport:
    """Evaluate scoring only; this function cannot modify runtime or production state."""

    baseline_ranked = _ranked(score_baseline(candidates, profile, now))
    candidate_ranked = _ranked(pre_rank(candidates, profile, now, weight_set=weight_set))
    baseline_at_20 = evaluate_snapshot_ranking(baseline_ranked, snapshot, split, k=20)
    candidate_at_20 = evaluate_snapshot_ranking(candidate_ranked, snapshot, split, k=20)
    baseline_at_60 = evaluate_snapshot_ranking(baseline_ranked, snapshot, split, k=60)
    candidate_at_60 = evaluate_snapshot_ranking(candidate_ranked, snapshot, split, k=60)
    comparison = compare_rankings(
        baseline_name=BASELINE_VERSION,
        candidate_name=weight_set.version,
        snapshot=snapshot,
        baseline=baseline_at_20,
        candidate=candidate_at_20,
    )
    ablations = tuple(
        _ablation(
            group,
            candidates,
            profile,
            snapshot,
            now,
            split,
            weight_set,
            candidate_at_20,
        )
        for group, value in weight_set.group_weights.items()
        if value > 0
    )
    warnings = [*comparison.warnings, *comparison.reasons]
    if candidate_at_20.provisional:
        warnings.append("sparse evaluation result is provisional")
    _append_coarse_recall_observations(warnings, baseline_at_60, candidate_at_60)
    _append_diversity_observations(warnings, baseline_at_20, candidate_at_20)
    return ShadowReport(
        SHADOW_REPORT_SCHEMA_VERSION,
        snapshot.snapshot_id,
        split,
        BASELINE_VERSION,
        weight_set.version,
        baseline_at_20,
        candidate_at_20,
        baseline_at_60,
        candidate_at_60,
        comparison,
        ablations,
        # v0.2.0 records these provisional metrics without using them as an activation gate.
        True,
        (),
        tuple(warnings),
    )


def write_shadow_report(report: ShadowReport, path: Path) -> None:
    """Atomically persist only aggregate local evaluation output with owner-only permissions."""

    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary_name = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    temporary = Path(temporary_name)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as output:
            json.dump(asdict(report), output, default=str, sort_keys=True, separators=(",", ":"))
            output.flush()
            os.fsync(output.fileno())
        os.replace(temporary, path)
        os.chmod(path, 0o600)
    finally:
        temporary.unlink(missing_ok=True)


def _ranked(scored: tuple[object, ...]) -> tuple[RankedPaper, ...]:
    from zotero_arxiv_daily.ranking.models import ScoredCandidate

    values: list[RankedPaper] = []
    for item in scored:
        if not isinstance(item, ScoredCandidate):
            raise AssertionError("ranking adapter returned an invalid scored candidate")
        values.append(
            RankedPaper(
                f"arxiv:{item.candidate.arxiv_id.canonical}",
                item.score,
                item.source,
                item.candidate.categories[0] if item.candidate.categories else None,
                tuple(_WORDS.findall(item.candidate.title.casefold()))[:12],
                (f"doi:{item.candidate.doi}",) if item.candidate.doi else (),
            )
        )
    return tuple(values)


def _ablation(
    group: FeatureGroup,
    candidates: tuple[ArxivCandidate, ...],
    profile: RemoteProfile,
    snapshot: EvaluationSnapshot,
    now: datetime,
    split: str,
    weight_set: WeightSet,
    candidate_metrics: RankingMetrics,
) -> FeatureAblation:
    ranked = _ranked(
        pre_rank(
            candidates,
            profile,
            now,
            weight_set=weight_set.without(group),
        )
    )
    metrics = evaluate_snapshot_ranking(ranked, snapshot, split, k=20)
    return FeatureAblation(
        group,
        metrics,
        _delta(metrics.ndcg_at_k, candidate_metrics.ndcg_at_k),
        _delta(metrics.recall_at_k, candidate_metrics.recall_at_k),
    )


def _append_coarse_recall_observations(
    warnings: list[str], baseline: RankingMetrics, candidate: RankingMetrics
) -> None:
    if candidate.candidate_recall_at_k is None or baseline.candidate_recall_at_k is None:
        warnings.append("coarse candidate-conditional Recall@60 is unavailable")
        return
    if candidate.candidate_recall_at_k < baseline.candidate_recall_at_k:
        warnings.append("coarse Recall@60 regressed from the frozen baseline")
    if candidate.candidate_recall_at_k < 0.95:
        warnings.append("coarse Recall@60 is below the 95% labeled-relevant target")


def _append_diversity_observations(
    warnings: list[str], baseline: RankingMetrics, candidate: RankingMetrics
) -> None:
    if (
        baseline.intra_list_diversity_at_k is not None
        and candidate.intra_list_diversity_at_k is not None
        and candidate.intra_list_diversity_at_k < baseline.intra_list_diversity_at_k
    ):
        warnings.append("intra-list diversity regressed")
    if candidate.source_coverage_at_k < baseline.source_coverage_at_k:
        warnings.append("source coverage regressed")


def _delta(value: float | None, reference: float | None) -> float | None:
    return value - reference if value is not None and reference is not None else None
