"""Bounded exploration aimed at uncertain, potentially worthwhile papers.

Category difference is not exploration. An off-category paper the ranker is already confident
about teaches nothing, and a paper no evidence exists for is noise rather than a calculated risk.
What earns a slot is a paper whose declared objective estimate could still move a long way: real
post-reading-value evidence exists, the optimistic end of its interval is genuinely promising, and
the interval is still wide.

Every constant here is declared and reviewed. Exploration spends a bounded amount of expected
worthwhile reads, records what it spent and why everything else was refused, and declines rather
than forcing an item when nothing safe is available.
"""

from __future__ import annotations

from collections.abc import Collection, Mapping, Sequence
from dataclasses import dataclass
from enum import StrEnum
from hashlib import blake2b

from zotero_arxiv_daily.ranking.models import ScoredCandidate
from zotero_arxiv_daily.ranking.outcome import (
    DEFAULT_WORTHWHILE_POLICY,
    FactorCalibration,
    WorthwhileEstimate,
    WorthwhilePolicy,
    unknown_estimate,
)


class ExplorationOutcome(StrEnum):
    """Declared vocabulary for why a candidate did or did not take an exploration slot."""

    SELECTED = "selected"
    RECENTLY_PUBLISHED = "recently_published"
    POLICY_VERSION_MISMATCH = "policy_version_mismatch"
    NO_VALUE_EVIDENCE = "no_value_evidence"
    EVIDENCE_CONFIDENCE_BELOW_MINIMUM = "evidence_confidence_below_minimum"
    POTENTIAL_BELOW_MINIMUM = "potential_below_minimum"
    UNCERTAINTY_BELOW_MINIMUM = "uncertainty_below_minimum"
    COST_EXCEEDS_RISK_BUDGET = "cost_exceeds_risk_budget"
    BUDGET_EXHAUSTED = "budget_exhausted"


@dataclass(frozen=True, slots=True)
class ExplorationPolicy:
    """A declared risk budget; exploration is a constraint on cost, not a score bonus."""

    version: str
    # At most one paper per batch, so a bad exploration day costs one slot out of the target.
    budget: int = 1
    # Expected worthwhile reads the batch may give up in total. At the default target of twenty
    # and typical estimates that is a few percent of the batch's expected worthwhile reads.
    risk_budget: float = 0.20
    # The optimistic end of the interval must be genuinely promising, not merely unmeasured.
    minimum_potential: float = 0.35
    # The interval must also still be wide, or there is nothing left to learn from the slot.
    minimum_uncertainty: float = 0.25
    # Some post-reading-value evidence must already exist. A paper nothing is known about has a
    # maximal interval for the wrong reason, and reading it resolves no declared question.
    minimum_evidence_confidence: float = 0.25
    # Candidates whose potential agrees to two decimals are treated as equivalent and rotated by
    # the seed, so a fixed seed reproduces a batch exactly while different days need not repeat
    # the same exploration.
    seed: str = ""

    def __post_init__(self) -> None:
        if not self.version:
            raise ValueError("exploration policy requires a version")
        if self.budget < 0:
            raise ValueError("exploration budget cannot be negative")
        thresholds = (
            self.risk_budget,
            self.minimum_potential,
            self.minimum_uncertainty,
            self.minimum_evidence_confidence,
        )
        if any(not 0 <= value <= 1 for value in thresholds):
            raise ValueError("exploration policy thresholds must be normalized")


DEFAULT_EXPLORATION_POLICY = ExplorationPolicy("bounded-uncertainty-v1")


@dataclass(frozen=True, slots=True)
class ExplorationAssessment:
    """One admitted candidate's exploration case, with every bound kept inspectable."""

    arxiv_id: str
    expected_worthwhile: float
    conservative_worthwhile: float
    potential_worthwhile: float
    uncertainty: float
    expected_cost: float
    outcome: ExplorationOutcome

    def __post_init__(self) -> None:
        if not self.arxiv_id:
            raise ValueError("exploration assessment identity is invalid")
        values = (
            self.expected_worthwhile,
            self.conservative_worthwhile,
            self.potential_worthwhile,
            self.uncertainty,
            self.expected_cost,
        )
        if any(not 0 <= value <= 1 for value in values):
            raise ValueError("exploration assessment values must be normalized")
        if not (
            self.conservative_worthwhile <= self.expected_worthwhile <= self.potential_worthwhile
        ):
            raise ValueError("exploration assessment interval must contain its expected value")


@dataclass(frozen=True, slots=True)
class ExplorationDecision:
    """What exploration admitted, what it spent, and why every other candidate was refused."""

    policy_version: str
    worthwhile_policy_version: str
    seed: str
    budget: int
    risk_budget: float
    spent: float
    considered_count: int
    eligible_count: int
    assessments: tuple[ExplorationAssessment, ...] = ()
    declined_reasons: tuple[ExplorationOutcome, ...] = ()

    def __post_init__(self) -> None:
        if len(self.assessments) > self.budget:
            raise ValueError("exploration decision exceeds its declared budget")
        if self.spent > self.risk_budget:
            raise ValueError("exploration decision exceeds its declared risk budget")

    @property
    def selected(self) -> tuple[str, ...]:
        """Canonical identifiers admitted into the reserved slots, in the order they were paid."""

        return tuple(item.arxiv_id for item in self.assessments)


def choose_exploration(
    qualified: Sequence[ScoredCandidate],
    estimates: Mapping[str, WorthwhileEstimate] | None = None,
    *,
    policy: ExplorationPolicy = DEFAULT_EXPLORATION_POLICY,
    worthwhile_policy: WorthwhilePolicy = DEFAULT_WORTHWHILE_POLICY,
    excluded_ids: Collection[str] = (),
) -> ExplorationDecision:
    """Admit at most ``policy.budget`` uncertain candidates within the declared risk budget.

    ``qualified`` must already have passed every selection gate, so exploration can only ever
    reach a paper the batch was allowed to contain. Nothing is forced: when no candidate satisfies
    the declared requirements the decision is empty and the slot returns to ordinary selection.
    """

    available = estimates or {}
    excluded = frozenset(excluded_ids)
    pool = [
        (
            item.candidate.arxiv_id.canonical,
            available.get(item.candidate.arxiv_id.canonical)
            or unknown_estimate(item.candidate.arxiv_id.canonical, policy=worthwhile_policy),
        )
        for item in qualified
    ]
    best_expected = max((estimate.expected_worthwhile for _, estimate in pool), default=0.0)
    cases: list[_Case] = []
    declined: set[ExplorationOutcome] = set()
    for canonical, estimate in pool:
        case = _case(canonical, estimate, best_expected, worthwhile_policy)
        refusal = _refusal(case, estimate, policy, worthwhile_policy, excluded)
        if refusal is None:
            cases.append(case)
        else:
            declined.add(refusal)
    eligible_count = len(cases)
    cases.sort(key=lambda case: (-round(case.potential, 2), _rotation(policy.seed, case.arxiv_id)))
    admitted: list[ExplorationAssessment] = []
    spent = 0.0
    for case in cases:
        if len(admitted) >= policy.budget:
            declined.add(ExplorationOutcome.BUDGET_EXHAUSTED)
            break
        if spent + case.cost > policy.risk_budget:
            declined.add(ExplorationOutcome.COST_EXCEEDS_RISK_BUDGET)
            continue
        spent += case.cost
        admitted.append(
            ExplorationAssessment(
                case.arxiv_id,
                case.expected,
                case.conservative,
                case.potential,
                case.uncertainty,
                case.cost,
                ExplorationOutcome.SELECTED,
            )
        )
    return ExplorationDecision(
        policy.version,
        worthwhile_policy.version,
        policy.seed,
        policy.budget,
        policy.risk_budget,
        spent,
        len(pool),
        eligible_count,
        tuple(admitted),
        tuple(sorted(declined)),
    )


@dataclass(frozen=True, slots=True)
class _Case:
    """Exploration arithmetic for one candidate, before the budget walk decides its outcome."""

    arxiv_id: str
    expected: float
    conservative: float
    potential: float
    cost: float

    @property
    def uncertainty(self) -> float:
        return self.potential - self.conservative


def _case(
    canonical: str,
    estimate: WorthwhileEstimate,
    best_expected: float,
    worthwhile_policy: WorthwhilePolicy,
) -> _Case:
    reading = _band(
        estimate.reading_likelihood,
        estimate.reading_likelihood_confidence,
        worthwhile_policy.reading,
    )
    value = _band(
        estimate.post_reading_value,
        estimate.post_reading_value_confidence,
        worthwhile_policy.post_reading_value,
    )
    return _Case(
        canonical,
        estimate.expected_worthwhile,
        reading[0] * value[0],
        reading[1] * value[1],
        # The displaced candidate is the marginal one, never the pool maximum, so this over-states
        # the true cost and the risk budget therefore binds conservatively.
        max(best_expected - estimate.expected_worthwhile, 0.0),
    )


def _band(value: float, confidence: float, calibration: FactorCalibration) -> tuple[float, float]:
    """Widen a calibrated factor by whatever evidence has not yet resolved.

    Full confidence collapses the interval onto the estimate and no confidence opens it to the
    whole declared interval, so the result is bounded by the reviewed calibration. The bounds are
    taken against the estimate itself, which keeps the interval containing a value that was
    produced outside the declared calibration rather than silently inverting it.
    """

    slack = 1.0 - confidence
    return (
        min(value, value - slack * (value - calibration.floor)),
        max(value, value + slack * (calibration.ceiling - value)),
    )


def _refusal(
    case: _Case,
    estimate: WorthwhileEstimate,
    policy: ExplorationPolicy,
    worthwhile_policy: WorthwhilePolicy,
    excluded: frozenset[str],
) -> ExplorationOutcome | None:
    """Return why this candidate cannot take an exploration slot, or None when it can."""

    if case.arxiv_id in excluded:
        return ExplorationOutcome.RECENTLY_PUBLISHED
    if estimate.policy_version != worthwhile_policy.version:
        return ExplorationOutcome.POLICY_VERSION_MISMATCH
    if not estimate.value_evidence_available:
        return ExplorationOutcome.NO_VALUE_EVIDENCE
    if estimate.post_reading_value_confidence < policy.minimum_evidence_confidence:
        return ExplorationOutcome.EVIDENCE_CONFIDENCE_BELOW_MINIMUM
    if case.potential < policy.minimum_potential:
        return ExplorationOutcome.POTENTIAL_BELOW_MINIMUM
    if case.uncertainty < policy.minimum_uncertainty:
        return ExplorationOutcome.UNCERTAINTY_BELOW_MINIMUM
    return None


def _rotation(seed: str, arxiv_id: str) -> str:
    """Deterministic per-seed ordering salt; identical inputs always produce identical batches."""

    return blake2b(f"{seed}\x00{arxiv_id}".encode(), digest_size=8).hexdigest()
