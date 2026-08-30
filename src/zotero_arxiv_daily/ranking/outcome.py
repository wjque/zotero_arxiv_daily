"""Declared, bounded estimation of the expected number of worthwhile reads.

The v0.3.0 product metric is the number of papers explicitly marked worthwhile *after reading*.
That decomposes into two separately reviewable factors::

    expected_worthwhile = P(read | shown) x P(worthwhile | read)

Keeping them separate is the point: a blended relevance score cannot say whether a paper was
chosen because it looks personally interesting or because it looks scientifically valuable, and
those two failures need different corrections.

Every calibration constant here is *declared and reviewed*, never fitted to the sparse, biased,
and self-selected feedback the system collects. Offline evaluation may propose new constants; only
an explicit operator action may adopt them.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass

from zotero_arxiv_daily.ranking.models import ScientificValueAssessment, ScoredCandidate
from zotero_arxiv_daily.ranking.weights import (
    DEFAULT_WEIGHT_SET,
    FeatureGroup,
    GroupAggregate,
    WeightSet,
    aggregate_groups,
)

READING_GROUPS = (FeatureGroup.INTEREST, FeatureGroup.RECENCY, FeatureGroup.IDENTITY)
POST_READING_VALUE_GROUPS = (FeatureGroup.SCIENTIFIC_QUALITY, FeatureGroup.REPRODUCIBILITY)


@dataclass(frozen=True, slots=True)
class FactorCalibration:
    """An affine map with shrinkage toward a declared prior.

    ``floor`` and ``ceiling`` bound what any amount of evidence may claim, and ``prior`` is what is
    claimed when there is no evidence at all. Because ``floor <= prior <= ceiling`` the result is
    always inside ``[floor, ceiling]``, and it is monotone in the raw signal, so a stronger signal
    can never lower an estimate.
    """

    floor: float
    ceiling: float
    prior: float

    def __post_init__(self) -> None:
        if not 0 <= self.floor <= self.ceiling <= 1:
            raise ValueError("factor calibration bounds must be an ordered normalized interval")
        if not self.floor <= self.prior <= self.ceiling:
            raise ValueError("factor calibration prior must lie inside its bounds")

    def calibrate(self, raw: float, confidence: float) -> float:
        """Map a raw signal into the declared interval, then shrink it by evidence confidence."""

        if not 0 <= raw <= 1 or not 0 <= confidence <= 1:
            raise ValueError("factor calibration inputs must be normalized")
        mapped = self.floor + (self.ceiling - self.floor) * raw
        return self.prior + (mapped - self.prior) * confidence


@dataclass(frozen=True, slots=True)
class WorthwhilePolicy:
    """A versioned declared objective policy; no field is learned from collected feedback."""

    version: str
    reading: FactorCalibration
    post_reading_value: FactorCalibration
    assessment_weight: float = 0.35

    def __post_init__(self) -> None:
        if not self.version:
            raise ValueError("worthwhile policy requires a version")
        if not 0 <= self.assessment_weight <= 1:
            raise ValueError("worthwhile policy assessment weight must be normalized")


DEFAULT_WORTHWHILE_POLICY = WorthwhilePolicy(
    "declared-prior-v1",
    # A shown paper is already in front of the reader, so the reading floor stays clearly above
    # zero. That floor is what keeps a low-interest high-value paper competitive under a product
    # objective instead of collapsing it to an expected value of zero.
    FactorCalibration(0.20, 0.90, 0.35),
    # Public metadata can suggest post-reading value but never settles it, so the ceiling stays
    # below one and the floor stays above zero.
    FactorCalibration(0.10, 0.85, 0.30),
)


@dataclass(frozen=True, slots=True)
class WorthwhileEstimate:
    """One candidate's declared objective estimate with both factors kept inspectable."""

    arxiv_id: str
    reading_likelihood: float
    reading_likelihood_confidence: float
    post_reading_value: float
    post_reading_value_confidence: float
    expected_worthwhile: float
    value_evidence_available: bool
    policy_version: str
    provenance: str

    def __post_init__(self) -> None:
        if not self.arxiv_id or not self.policy_version or not self.provenance:
            raise ValueError("worthwhile estimate identity is invalid")
        values = (
            self.reading_likelihood,
            self.reading_likelihood_confidence,
            self.post_reading_value,
            self.post_reading_value_confidence,
            self.expected_worthwhile,
        )
        if any(not 0 <= value <= 1 for value in values):
            raise ValueError("worthwhile estimate values must be normalized")


def unknown_estimate(
    arxiv_id: str,
    *,
    policy: WorthwhilePolicy = DEFAULT_WORTHWHILE_POLICY,
    provenance: str = "unknown",
) -> WorthwhileEstimate:
    """Return the declared no-evidence estimate.

    A candidate the estimator never saw is unknown, not bad. It receives both declared priors at
    zero confidence so that absent evidence can never be read as a negative outcome claim.
    """

    reading = policy.reading.prior
    value = policy.post_reading_value.prior
    return WorthwhileEstimate(
        arxiv_id,
        reading,
        0.0,
        value,
        0.0,
        reading * value,
        False,
        policy.version,
        provenance,
    )


def estimate_worthwhile(
    scored: Sequence[ScoredCandidate],
    scientific_values: Mapping[str, ScientificValueAssessment] | None = None,
    *,
    policy: WorthwhilePolicy = DEFAULT_WORTHWHILE_POLICY,
    weight_set: WeightSet = DEFAULT_WEIGHT_SET,
) -> dict[str, WorthwhileEstimate]:
    """Estimate expected worthwhile reads from local features already computed by pre-ranking.

    Group importance reuses the reviewed ranking weight set rather than introducing a second set
    of undeclared constants, so the objective stays traceable to the active weight version.
    """

    assessments = scientific_values or {}
    weights = weight_set.group_weights
    estimates: dict[str, WorthwhileEstimate] = {}
    for item in scored:
        arxiv_id = item.candidate.arxiv_id.canonical
        aggregates = aggregate_groups(item.feature_values)
        reading_evidence = _group_evidence(aggregates, weights, READING_GROUPS)
        value_evidence = _group_evidence(aggregates, weights, POST_READING_VALUE_GROUPS)
        assessment_evidence = _assessment_evidence(assessments.get(arxiv_id), policy)
        reading_raw, reading_confidence = _blend(
            reading_evidence, sum(weights[group] for group in READING_GROUPS)
        )
        # An assessment that was never produced is not missing evidence held against the paper, so
        # it enters the coverage denominator only when it actually supplied a value.
        value_declared = sum(weights[group] for group in POST_READING_VALUE_GROUPS) + sum(
            weight for weight, _, _ in assessment_evidence
        )
        value_raw, value_confidence = _blend(
            [*value_evidence, *assessment_evidence], value_declared
        )
        reading = policy.reading.calibrate(reading_raw, reading_confidence)
        value = policy.post_reading_value.calibrate(value_raw, value_confidence)
        estimates[arxiv_id] = WorthwhileEstimate(
            arxiv_id,
            reading,
            reading_confidence,
            value,
            value_confidence,
            reading * value,
            bool(value_evidence or assessment_evidence),
            policy.version,
            item.weight_set_version,
        )
    return estimates


def _group_evidence(
    aggregates: Mapping[FeatureGroup, GroupAggregate],
    weights: Mapping[FeatureGroup, float],
    groups: Sequence[FeatureGroup],
) -> list[tuple[float, float, float]]:
    return [
        (weights[group], aggregates[group].value, aggregates[group].confidence)
        for group in groups
        if group in aggregates and weights[group] > 0
    ]


def _assessment_evidence(
    assessment: ScientificValueAssessment | None, policy: WorthwhilePolicy
) -> list[tuple[float, float, float]]:
    if assessment is None or policy.assessment_weight <= 0:
        return []
    dimensions = [
        value
        for value in (assessment.solution_advance, assessment.technical_depth)
        if value is not None
    ]
    if not dimensions:
        return []
    mean = sum(dimensions) / len(dimensions)
    return [(policy.assessment_weight, mean, assessment.confidence)]


def _blend(
    evidence: Sequence[tuple[float, float, float]], declared_weight: float
) -> tuple[float, float]:
    """Reduce weighted evidence to a value and a coverage-discounted confidence.

    Absent evidence lowers confidence, never the value. That is what keeps a paper with no
    available evidence at its declared prior instead of at a confident zero.
    """

    present = sum(weight for weight, _, _ in evidence)
    if present <= 0 or declared_weight <= 0:
        return 0.0, 0.0
    value = sum(weight * item for weight, item, _ in evidence) / present
    confidence = sum(weight * item for weight, _, item in evidence) / present
    return min(value, 1.0), min(confidence * present / declared_weight, 1.0)
