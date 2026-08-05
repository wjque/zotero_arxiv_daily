"""Deterministic offline splits, metrics, snapshots, and comparison gates."""

from __future__ import annotations

import hashlib
import json
import math
import os
import re
import tempfile
from dataclasses import asdict
from datetime import datetime, timedelta
from pathlib import Path

from zotero_arxiv_daily.arxiv.ids import parse_arxiv_id
from zotero_arxiv_daily.core.errors import ApplicationError, ConfigurationError
from zotero_arxiv_daily.core.time import require_aware_utc
from zotero_arxiv_daily.evaluation.models import (
    CURATED_CORPUS_SCHEMA_VERSION,
    EVALUATION_SNAPSHOT_SCHEMA_VERSION,
    ComparisonReport,
    CorpusLabel,
    CorpusSnapshot,
    EvaluationSnapshot,
    EvaluationSplit,
    RankedPaper,
    RankingMetrics,
    ResolvedJudgment,
)

_ARXIV_DOI = re.compile(
    r"^10\.48550/arxiv\.(?P<identifier>\d{4}\.\d{4,5}(?:v\d+)?)$", re.IGNORECASE
)


def make_evaluation_snapshot(
    corpus: CorpusSnapshot,
    *,
    created_at: datetime,
    anchor_paper_ids: tuple[str, ...] = (),
    rolling_days: int = 90,
    temporal_holdout_ratio: float = 0.2,
) -> EvaluationSnapshot:
    """Freeze stable, rolling, and temporal identity splits from a corpus snapshot."""

    created = require_aware_utc(created_at, "created_at")
    if rolling_days < 1 or not 0 < temporal_holdout_ratio < 1:
        raise ValueError("rolling_days and temporal_holdout_ratio are outside safe bounds")
    labels = corpus.labels
    identities = {label.paper_id for label in labels}
    missing_anchor = set(anchor_paper_ids) - identities
    if missing_anchor:
        raise ValueError("stable anchor contains paper IDs absent from the corpus snapshot")
    ordered_by_time = tuple(sorted(labels, key=lambda label: (label.resolved_at, label.paper_id)))
    temporal_count = math.ceil(len(ordered_by_time) * temporal_holdout_ratio)
    temporal_holdout = ordered_by_time[-temporal_count:] if ordered_by_time else ()
    rolling_start = corpus.cutoff_at - timedelta(days=rolling_days)
    rolling = tuple(label for label in labels if label.resolved_at >= rolling_start)
    splits = (
        EvaluationSplit("stable-anchor", tuple(sorted(anchor_paper_ids))),
        EvaluationSplit("rolling", tuple(sorted(label.paper_id for label in rolling))),
        EvaluationSplit("temporal-holdout", tuple(label.paper_id for label in temporal_holdout)),
        EvaluationSplit(
            "temporal-train",
            tuple(label.paper_id for label in ordered_by_time[:-temporal_count])
            if temporal_count
            else (),
        ),
        EvaluationSplit(
            "pairwise",
            tuple(
                sorted(
                    {
                        paper_id
                        for label in corpus.pairwise
                        for paper_id in (label.paper_id, label.compared_paper_id)
                        if paper_id is not None
                    }
                )
            ),
        ),
    )
    seed = {
        "schema_version": EVALUATION_SNAPSHOT_SCHEMA_VERSION,
        "corpus_revision": corpus.revision,
        "corpus_digest": corpus.digest,
        "cutoff_at": corpus.cutoff_at.isoformat(),
        "splits": [(split.name, split.paper_ids) for split in splits],
    }
    snapshot_id = (
        "eval-"
        + hashlib.sha256(
            json.dumps(seed, sort_keys=True, separators=(",", ":")).encode("utf-8")
        ).hexdigest()[:24]
    )
    return EvaluationSnapshot(
        EVALUATION_SNAPSHOT_SCHEMA_VERSION,
        snapshot_id,
        corpus.revision,
        corpus.digest,
        corpus.cutoff_at,
        splits,
        tuple((label.paper_id, label.label) for label in corpus.labels if label.label is not None),
        tuple(
            (label.paper_id, label.compared_paper_id)
            for label in corpus.pairwise
            if label.compared_paper_id is not None
        ),
        len(labels),
        sum(label.label is CorpusLabel.NEGATIVE for label in labels),
        len(corpus.pairwise),
        corpus.conflict_count,
        created,
    )


def frozen_corpus(snapshot: EvaluationSnapshot) -> CorpusSnapshot:
    """Rebuild resolved judgments needed to replay an immutable evaluation snapshot."""

    labels = tuple(
        ResolvedJudgment(
            paper_id,
            label,
            snapshot.cutoff_at,
            f"{snapshot.snapshot_id}:label:{paper_id}",
            "evaluation-snapshot",
        )
        for paper_id, label in snapshot.labels
    )
    pairwise = tuple(
        ResolvedJudgment(
            winner,
            CorpusLabel.POSITIVE,
            snapshot.cutoff_at,
            f"{snapshot.snapshot_id}:pairwise:{winner}:{other}",
            "evaluation-snapshot",
            compared_paper_id=other,
        )
        for winner, other in snapshot.pairwise_preferences
    )
    return CorpusSnapshot(
        CURATED_CORPUS_SCHEMA_VERSION,
        snapshot.corpus_revision,
        snapshot.corpus_digest,
        snapshot.cutoff_at,
        labels,
        pairwise,
        snapshot.conflict_count,
    )


def evaluate_snapshot_ranking(
    ranked: tuple[RankedPaper, ...], snapshot: EvaluationSnapshot, split_name: str, *, k: int = 20
) -> RankingMetrics:
    """Evaluate a ranking against labels frozen inside a named immutable split."""

    matches = tuple(split for split in snapshot.splits if split.name == split_name)
    if len(matches) != 1:
        raise ValueError("evaluation snapshot does not contain the requested split")
    return evaluate_ranking(ranked, frozen_corpus(snapshot), matches[0].paper_ids, k=k)


class EvaluationSnapshotStore:
    """Persist immutable local evaluation snapshots as write-once JSON files."""

    def __init__(self, directory: Path) -> None:
        self.directory = directory

    def write(self, snapshot: EvaluationSnapshot) -> Path:
        """Write a snapshot once; the same ID may only be written with identical content."""

        self.directory.mkdir(parents=True, exist_ok=True)
        os.chmod(self.directory, 0o700)
        path = self.directory / f"{snapshot.snapshot_id}.json"
        encoded = _snapshot_payload(snapshot)
        serialized = json.dumps(encoded, sort_keys=True, separators=(",", ":"))
        if path.exists():
            try:
                existing = json.loads(path.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError) as error:
                raise ApplicationError("stored evaluation snapshot is unreadable") from error
            if json.dumps(existing, sort_keys=True, separators=(",", ":")) != serialized:
                raise ApplicationError(
                    "evaluation snapshot ID already exists with different content"
                )
            return path
        descriptor, temporary_name = tempfile.mkstemp(prefix=f".{path.name}.", dir=self.directory)
        temporary = Path(temporary_name)
        try:
            with os.fdopen(descriptor, "w", encoding="utf-8") as output:
                output.write(serialized)
                output.flush()
                os.fsync(output.fileno())
            os.replace(temporary, path)
            os.chmod(path, 0o600)
        finally:
            temporary.unlink(missing_ok=True)
        return path

    def read(self, snapshot_id: str) -> EvaluationSnapshot:
        """Read one immutable local snapshot for an offline replay."""

        path = self.directory / f"{snapshot_id}.json"
        try:
            value = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as error:
            raise ApplicationError("stored evaluation snapshot is unreadable") from error
        try:
            return _snapshot_from_payload(value)
        except (KeyError, TypeError, ValueError) as error:
            raise ApplicationError("stored evaluation snapshot is invalid") from error


def evaluate_ranking(
    ranked: tuple[RankedPaper, ...],
    corpus: CorpusSnapshot,
    paper_ids: tuple[str, ...],
    *,
    k: int = 20,
) -> RankingMetrics:
    """Calculate core offline metrics only over explicitly labeled or paired papers."""

    if k < 1:
        raise ValueError("k must be positive")
    selected_ids = frozenset(_canonical_identity(value) for value in paper_ids)
    labels = {
        _canonical_identity(label.paper_id): label.label
        for label in corpus.labels
        if _canonical_identity(label.paper_id) in selected_ids and label.label is not None
    }
    positives = {paper_id for paper_id, label in labels.items() if label is CorpusLabel.POSITIVE}
    negatives = {paper_id for paper_id, label in labels.items() if label is CorpusLabel.NEGATIVE}
    ordered = tuple(sorted(ranked, key=lambda item: (-item.score, item.paper_id)))
    top_k = ordered[:k]
    labeled_top = tuple(
        (item, label) for item in top_k if (label := _label_for(item, labels)) is not None
    )
    positive_top = sum(label is CorpusLabel.POSITIVE for _, label in labeled_top)
    negative_top = sum(label is CorpusLabel.NEGATIVE for _, label in labeled_top)
    recall = positive_top / len(positives) if positives else None
    precision = positive_top / len(labeled_top) if labeled_top else None
    negative_rate = negative_top / len(labeled_top) if labeled_top else None
    ndcg = _ndcg(top_k, labels, len(positives), k)
    diversity = _intra_list_diversity(top_k)
    brier = _brier_score(ordered, labels)
    pairwise_accuracy = _pairwise_accuracy(ordered, corpus, selected_ids)
    insufficient = _insufficiency_reason(len(labels), len(positives), len(corpus.pairwise))
    ranked_identifiers = _ranked_identifiers(ordered)
    candidate_overlap = len(set(labels) & ranked_identifiers)
    candidate_positive_labels = len(positives & ranked_identifiers)
    candidate_negative_labels = len(negatives & ranked_identifiers)
    candidate_recall = (
        positive_top / candidate_positive_labels if candidate_positive_labels else None
    )
    return RankingMetrics(
        len(labels),
        len(positives),
        len(negatives),
        recall,
        precision,
        ndcg,
        negative_rate,
        len({item.source for item in top_k if item.source}),
        len({item.category for item in top_k if item.category}),
        diversity,
        brier,
        pairwise_accuracy,
        insufficient is not None or len(labels) < 5 or candidate_overlap < 5,
        insufficient,
        candidate_overlap,
        candidate_positive_labels,
        candidate_negative_labels,
        candidate_recall,
    )


def compare_rankings(
    *,
    baseline_name: str,
    candidate_name: str,
    snapshot: EvaluationSnapshot,
    baseline: RankingMetrics,
    candidate: RankingMetrics,
) -> ComparisonReport:
    """Report metric deltas and conservative eligibility for later manual approval."""

    ndcg_delta = _delta(candidate.ndcg_at_k, baseline.ndcg_at_k)
    recall_delta = _delta(candidate.candidate_recall_at_k, baseline.candidate_recall_at_k)
    negative_rate_delta = _delta(candidate.negative_rate_at_k, baseline.negative_rate_at_k)
    reasons: list[str] = []
    warnings: list[str] = []
    if candidate.insufficiency_reason:
        warnings.append(candidate.insufficiency_reason)
    overlap = min(baseline.candidate_overlap, candidate.candidate_overlap)
    if overlap == 0:
        warnings.append("candidate-label overlap is zero for at least one ranking")
    elif overlap < 5:
        warnings.append("few overlapping labels; metric uncertainty is high")
    if overlap < 5:
        warnings.append("sparse independent-label sample; metric uncertainty is high")
    if baseline.candidate_positive_labels == 0 or candidate.candidate_positive_labels == 0:
        warnings.append(
            "no positive labels overlap the candidate pool; Recall@K is not interpretable"
        )
    if ndcg_delta is None:
        warnings.append("NDCG is unavailable")
    elif ndcg_delta < 0.05:
        reasons.append("NDCG improvement is below the 0.05 release target")
    if negative_rate_delta is not None and negative_rate_delta > 0:
        reasons.append("negative-label rate increased")
    # Warnings prevent automatic tuning; explicit canary review may still proceed.
    eligible = not reasons and not warnings
    return ComparisonReport(
        baseline_name,
        candidate_name,
        snapshot.snapshot_id,
        baseline,
        candidate,
        ndcg_delta,
        recall_delta,
        negative_rate_delta,
        eligible,
        tuple(reasons),
        tuple(warnings),
    )


def _snapshot_payload(snapshot: EvaluationSnapshot) -> dict[str, object]:
    return {
        **asdict(snapshot),
        "cutoff_at": snapshot.cutoff_at.isoformat(),
        "created_at": snapshot.created_at.isoformat(),
    }


def _snapshot_from_payload(value: object) -> EvaluationSnapshot:
    if not isinstance(value, dict):
        raise ValueError
    required = {
        "schema_version",
        "snapshot_id",
        "corpus_revision",
        "corpus_digest",
        "cutoff_at",
        "splits",
        "labels",
        "pairwise_preferences",
        "label_count",
        "negative_count",
        "pairwise_count",
        "conflict_count",
        "created_at",
    }
    if set(value) != required:
        raise ValueError
    raw_splits = value["splits"]
    raw_labels = value["labels"]
    raw_pairwise = value["pairwise_preferences"]
    if (
        not isinstance(raw_splits, list)
        or not isinstance(raw_labels, list)
        or not isinstance(raw_pairwise, list)
    ):
        raise ValueError
    splits = tuple(
        EvaluationSplit(str(split["name"]), tuple(str(item) for item in split["paper_ids"]))
        for split in raw_splits
        if isinstance(split, dict)
        and set(split) == {"name", "paper_ids"}
        and isinstance(split["paper_ids"], list)
    )
    labels = tuple(
        (str(entry[0]), CorpusLabel(str(entry[1])))
        for entry in raw_labels
        if isinstance(entry, list) and len(entry) == 2
    )
    pairwise = tuple(
        (str(entry[0]), str(entry[1]))
        for entry in raw_pairwise
        if isinstance(entry, list) and len(entry) == 2
    )
    if (
        len(splits) != len(raw_splits)
        or len(labels) != len(raw_labels)
        or len(pairwise) != len(raw_pairwise)
    ):
        raise ValueError
    return EvaluationSnapshot(
        int(value["schema_version"]),
        str(value["snapshot_id"]),
        int(value["corpus_revision"]),
        str(value["corpus_digest"]),
        datetime.fromisoformat(str(value["cutoff_at"])),
        splits,
        labels,
        pairwise,
        int(value["label_count"]),
        int(value["negative_count"]),
        int(value["pairwise_count"]),
        int(value["conflict_count"]),
        datetime.fromisoformat(str(value["created_at"])),
    )


def _ndcg(
    ranked: tuple[RankedPaper, ...], labels: dict[str, CorpusLabel], positives: int, k: int
) -> float | None:
    if not positives:
        return None
    dcg = sum(
        (1.0 / math.log2(index + 2))
        for index, item in enumerate(ranked[:k])
        if _label_for(item, labels) is CorpusLabel.POSITIVE
    )
    ideal = sum(1.0 / math.log2(index + 2) for index in range(min(positives, k)))
    return dcg / ideal if ideal else None


def _intra_list_diversity(ranked: tuple[RankedPaper, ...]) -> float | None:
    if len(ranked) < 2:
        return None
    similarities: list[float] = []
    for index, left in enumerate(ranked):
        left_facets = frozenset(left.facets)
        for right in ranked[index + 1 :]:
            right_facets = frozenset(right.facets)
            union = left_facets | right_facets
            similarities.append(len(left_facets & right_facets) / len(union) if union else 0.0)
    return 1.0 - sum(similarities) / len(similarities)


def _brier_score(ranked: tuple[RankedPaper, ...], labels: dict[str, CorpusLabel]) -> float | None:
    values: list[float] = []
    if not ranked:
        return None
    minimum = min(item.score for item in ranked)
    maximum = max(item.score for item in ranked)
    for item in ranked:
        label = _label_for(item, labels)
        if label is None:
            continue
        probability = 0.5 if maximum == minimum else (item.score - minimum) / (maximum - minimum)
        outcome = 1.0 if label is CorpusLabel.POSITIVE else 0.0
        values.append((probability - outcome) ** 2)
    return sum(values) / len(values) if values else None


def _pairwise_accuracy(
    ranked: tuple[RankedPaper, ...], corpus: CorpusSnapshot, paper_ids: frozenset[str]
) -> float | None:
    scores: dict[str, float] = {}
    for item in ranked:
        for identifier in (item.paper_id, *item.identifiers):
            canonical = _canonical_identity(identifier)
            if canonical in scores and scores[canonical] != item.score:
                raise ValueError("ranked paper identifiers map to conflicting scores")
            scores[canonical] = item.score
    outcomes: list[bool] = []
    for pair in corpus.pairwise:
        if (
            _canonical_identity(pair.paper_id) not in paper_ids
            or not pair.compared_paper_id
            or _canonical_identity(pair.compared_paper_id) not in paper_ids
        ):
            continue
        winner = _canonical_identity(pair.paper_id)
        other = _canonical_identity(pair.compared_paper_id)
        if winner in scores and other in scores:
            outcomes.append(scores[winner] > scores[other])
    return sum(outcomes) / len(outcomes) if outcomes else None


def _ranked_identifiers(ranked: tuple[RankedPaper, ...]) -> frozenset[str]:
    return frozenset(
        _canonical_identity(identifier)
        for item in ranked
        for identifier in (item.paper_id, *item.identifiers)
    )


def _label_for(item: RankedPaper, labels: dict[str, CorpusLabel]) -> CorpusLabel | None:
    matched = {
        labels[_canonical_identity(identifier)]
        for identifier in (item.paper_id, *item.identifiers)
        if _canonical_identity(identifier) in labels
    }
    if len(matched) > 1:
        raise ValueError("ranked paper identifiers map to conflicting corpus labels")
    return next(iter(matched), None)


def _canonical_identity(value: str) -> str:
    """Normalize only exact arXiv/DOI aliases; never infer identity from content."""

    normalized = value.strip().casefold()
    if normalized.startswith("arxiv:"):
        try:
            return f"arxiv:{parse_arxiv_id(normalized[6:]).canonical}"
        except ConfigurationError:
            return normalized
    doi = normalized.removeprefix("doi:")
    match = _ARXIV_DOI.fullmatch(doi)
    if match is not None:
        try:
            return f"arxiv:{parse_arxiv_id(match.group('identifier')).canonical}"
        except ConfigurationError:
            return normalized
    return normalized


def _insufficiency_reason(label_count: int, positive_count: int, pairwise_count: int) -> str | None:
    if label_count == 0 and pairwise_count == 0:
        return "no eligible labels or pairwise judgments"
    if positive_count == 0:
        return "no positive labels available for recall or NDCG"
    return None


def _delta(candidate: float | None, baseline: float | None) -> float | None:
    return candidate - baseline if candidate is not None and baseline is not None else None
