"""Offline comparison of the declared worthwhile objective against realized explicit outcomes.

This module is the only bridge between collected feedback and the objective, and it runs strictly
offline. ``ranking`` never imports ``feedback``, so feedback stays out of the ranking path: it can
inform a human review of the declared policy, but it cannot silently become a ranking input.

Nothing here activates anything. The report records what was predicted, what was explicitly
observed, and how much of the batch was never labeled at all - and it keeps those three separate,
because an unlabeled impression is unknown, not a failure.
"""

from __future__ import annotations

import json
import os
import tempfile
from collections.abc import Mapping, Sequence
from dataclasses import asdict, dataclass
from pathlib import Path

from zotero_arxiv_daily.feedback.ledger import BatchOutcomeMetrics, PositionOutcomeRate
from zotero_arxiv_daily.ranking.outcome import (
    DEFAULT_WORTHWHILE_POLICY,
    FactorCalibration,
    WorthwhileEstimate,
    WorthwhilePolicy,
)

WORTHWHILE_REPORT_SCHEMA_VERSION = 1

# A batch with fewer explicit post-reading labels than this is reported as provisional, and the
# whole report refuses to propose a calibration below the total threshold. Both are deliberately
# conservative: the cost of a premature policy change is far higher than a delayed one.
_MINIMUM_BATCH_OUTCOMES = 5
_MINIMUM_TOTAL_OUTCOMES = 30
_MINIMUM_COVERAGE = 0.2


@dataclass(frozen=True, slots=True)
class BatchObjectiveComparison:
    """One published batch's predicted objective against its explicitly labeled outcomes."""

    batch_id: str
    impression_count: int
    reading_completion_count: int
    post_reading_outcome_count: int
    worthwhile_read_count: int
    not_worthwhile_read_count: int
    unlabeled_impression_count: int
    post_reading_outcome_coverage: float | None
    worthwhile_given_explicit_outcome: float | None
    predicted_worthwhile_reads: float | None
    predicted_minus_realized: float | None
    provisional: bool


@dataclass(frozen=True, slots=True)
class ProposedCalibration:
    """A candidate policy derived from observation and recorded for review only.

    Emitting this changes no behavior. Adopting it is an explicit operator action under V030-M6,
    which is what keeps the objective declared rather than fitted to biased feedback.
    """

    basis_policy_version: str
    observed_reading_rate: float | None
    observed_worthwhile_rate: float | None
    proposed_reading_prior: float | None
    proposed_post_reading_value_prior: float | None
    labeled_outcome_count: int
    sufficient: bool


@dataclass(frozen=True, slots=True)
class WorthwhileReport:
    """Aggregate-only objective observation; it carries counts and rates, never paper identities."""

    schema_version: int
    policy_version: str
    batch_count: int
    impression_count: int
    reading_completion_count: int
    post_reading_outcome_count: int
    worthwhile_read_count: int
    not_worthwhile_read_count: int
    unlabeled_impression_count: int
    post_reading_outcome_coverage: float | None
    worthwhile_given_explicit_outcome: float | None
    predicted_worthwhile_reads: float | None
    batches: tuple[BatchObjectiveComparison, ...]
    positions: tuple[PositionOutcomeRate, ...]
    proposed_calibration: ProposedCalibration
    eligible_for_activation: bool
    reasons: tuple[str, ...]
    warnings: tuple[str, ...] = ()


def run_worthwhile_evaluation(
    batches: Sequence[BatchOutcomeMetrics],
    positions: Sequence[PositionOutcomeRate] = (),
    *,
    policy: WorthwhilePolicy = DEFAULT_WORTHWHILE_POLICY,
    predictions: Mapping[str, Sequence[WorthwhileEstimate]] | None = None,
) -> WorthwhileReport:
    """Compare declared objective predictions with realized explicit outcomes, without side effects.

    Batch attribution is the ledger's, so an outcome submitted days later is already credited to
    the impression batch that produced it. Nothing in this function revisits an earlier report or
    treats the interval before a delayed submission as a negative outcome.
    """

    predicted = predictions or {}
    comparisons = tuple(_compare(batch, predicted.get(batch.batch_id)) for batch in batches)
    impressions = sum(batch.impression_count for batch in batches)
    reads = sum(batch.reading_completion_count for batch in batches)
    labeled = sum(batch.post_reading_outcome_count for batch in batches)
    worthwhile = sum(batch.worthwhile_read_count for batch in batches)
    not_worthwhile = sum(batch.not_worthwhile_read_count for batch in batches)
    predicted_total = _total_prediction(comparisons)
    reading_rate = reads / impressions if impressions else None
    worthwhile_rate = worthwhile / labeled if labeled else None
    calibration = _propose(policy, reading_rate, worthwhile_rate, labeled)
    coverage = labeled / reads if reads else None
    return WorthwhileReport(
        WORTHWHILE_REPORT_SCHEMA_VERSION,
        policy.version,
        len(batches),
        impressions,
        reads,
        labeled,
        worthwhile,
        not_worthwhile,
        impressions - labeled,
        coverage,
        worthwhile_rate,
        predicted_total,
        comparisons,
        tuple(positions),
        calibration,
        # Activation is V030-M6's decision and requires an explicit operator action.
        False,
        ("objective activation requires an explicit operator decision under V030-M6",),
        _warnings(comparisons, coverage, labeled, predicted_total),
    )


def write_worthwhile_report(report: WorthwhileReport, path: Path) -> None:
    """Atomically persist only aggregate objective observation with owner-only permissions."""

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


def _compare(
    batch: BatchOutcomeMetrics, estimates: Sequence[WorthwhileEstimate] | None
) -> BatchObjectiveComparison:
    predicted = (
        sum(estimate.expected_worthwhile for estimate in estimates)
        if estimates is not None
        else None
    )
    return BatchObjectiveComparison(
        batch.batch_id,
        batch.impression_count,
        batch.reading_completion_count,
        batch.post_reading_outcome_count,
        batch.worthwhile_read_count,
        batch.not_worthwhile_read_count,
        # Unlabeled impressions are reported as unknown. They are never folded into the
        # not-worthwhile count, so silence cannot be read as a negative judgement.
        batch.impression_count - batch.post_reading_outcome_count,
        batch.post_reading_outcome_coverage,
        batch.worthwhile_given_explicit_outcome,
        predicted,
        predicted - batch.worthwhile_read_count if predicted is not None else None,
        batch.post_reading_outcome_count < _MINIMUM_BATCH_OUTCOMES,
    )


def _total_prediction(comparisons: Sequence[BatchObjectiveComparison]) -> float | None:
    values = [
        item.predicted_worthwhile_reads
        for item in comparisons
        if item.predicted_worthwhile_reads is not None
    ]
    return sum(values) if values else None


def _propose(
    policy: WorthwhilePolicy,
    reading_rate: float | None,
    worthwhile_rate: float | None,
    labeled: int,
) -> ProposedCalibration:
    sufficient = labeled >= _MINIMUM_TOTAL_OUTCOMES
    reading_prior = None
    value_prior = None
    if sufficient and reading_rate is not None:
        # The observed rate counts only reported reads, so it is a lower bound on P(read | shown).
        # Proposing it directly would bake reporting silence into the policy as pessimism.
        reading_prior = _clamp(max(reading_rate, policy.reading.floor), policy.reading)
    if sufficient and worthwhile_rate is not None:
        value_prior = _clamp(worthwhile_rate, policy.post_reading_value)
    return ProposedCalibration(
        policy.version,
        reading_rate,
        worthwhile_rate,
        reading_prior,
        value_prior,
        labeled,
        sufficient,
    )


def _clamp(value: float, calibration: FactorCalibration) -> float:
    return min(max(value, calibration.floor), calibration.ceiling)


def _warnings(
    comparisons: Sequence[BatchObjectiveComparison],
    coverage: float | None,
    labeled: int,
    predicted: float | None,
) -> tuple[str, ...]:
    warnings: list[str] = []
    if predicted is None:
        warnings.append("no batch prediction was supplied; realized outcomes are reported alone")
    elif any(item.predicted_worthwhile_reads is None for item in comparisons):
        # The predicted total sums only the batches that carry a prediction, while every realized
        # count covers all of them. Saying so keeps a history that predates prediction persistence
        # from being read as an unflattering comparison.
        warnings.append("predicted totals cover only some batches; totals are not comparable")
    if labeled < _MINIMUM_TOTAL_OUTCOMES:
        warnings.append("labeled post-reading outcomes are insufficient to propose a calibration")
    if coverage is None or coverage < _MINIMUM_COVERAGE:
        warnings.append("post-reading outcome coverage is low; most reads remain unlabeled")
    if any(item.provisional for item in comparisons):
        warnings.append("at least one batch is provisional on its explicit outcome count")
    warnings.append("unreported impressions remain unknown and are not counted as not worthwhile")
    warnings.append("explicit post-reading outcomes are self-selected and may be biased")
    return tuple(warnings)
