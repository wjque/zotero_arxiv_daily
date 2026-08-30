from __future__ import annotations

from dataclasses import replace
from datetime import UTC, datetime, timedelta

import pytest

from zotero_arxiv_daily.arxiv.models import ArxivCandidate, ArxivId
from zotero_arxiv_daily.ranking.exploration import (
    DEFAULT_EXPLORATION_POLICY,
    ExplorationAssessment,
    ExplorationDecision,
    ExplorationOutcome,
    ExplorationPolicy,
    choose_exploration,
)
from zotero_arxiv_daily.ranking.models import ScientificValueAssessment, ScoredCandidate
from zotero_arxiv_daily.ranking.outcome import (
    DEFAULT_WORTHWHILE_POLICY,
    WorthwhileEstimate,
    estimate_worthwhile,
)
from zotero_arxiv_daily.ranking.select import (
    SelectionPolicy,
    qualified_candidates,
    select_diverse,
)
from zotero_arxiv_daily.ranking.weights import FeatureGroup, NormalizedFeature

_NOW = datetime(2026, 8, 30, tzinfo=UTC)
# Titles that share no tokens, so the diversity limit never masks an exploration assertion.
_SUBJECTS = (
    "graphs",
    "kernels",
    "optimizers",
    "transformers",
    "diffusion",
    "retrieval",
    "planning",
    "robotics",
    "compilers",
    "verification",
    "topology",
    "sampling",
)


def _scored(
    identifier: str,
    *,
    index: int,
    score: float = 0.5,
    quality: float = 0.6,
    source: str = "core",
) -> ScoredCandidate:
    """A candidate that clears every eligibility gate, so only exploration rules can refuse it."""

    candidate = ArxivCandidate(
        ArxivId(identifier, 1),
        f"On {_SUBJECTS[index]}",
        (f"Author {index}",),
        ("cs.LG",),
        _NOW - timedelta(days=1),
        _NOW,
        f"https://arxiv.org/abs/{identifier}",
        f"https://arxiv.org/pdf/{identifier}",
        "learning methods",
    )
    features = (
        NormalizedFeature(
            "judged_quality", quality, True, 1.0, "test-fixture", FeatureGroup.SCIENTIFIC_QUALITY
        ),
    )
    return ScoredCandidate(candidate, score, (), source, features)


def _estimate(
    identifier: str,
    *,
    reading: float,
    reading_confidence: float,
    value: float,
    value_confidence: float,
    evidence: bool = True,
    policy_version: str | None = None,
) -> WorthwhileEstimate:
    return WorthwhileEstimate(
        identifier,
        reading,
        reading_confidence,
        value,
        value_confidence,
        reading * value,
        evidence,
        policy_version or DEFAULT_WORTHWHILE_POLICY.version,
        "test-fixture",
    )


def _uncertain(identifier: str) -> WorthwhileEstimate:
    """Real value evidence, a promising optimistic end, and a still-wide interval."""

    return _estimate(
        identifier, reading=0.50, reading_confidence=0.60, value=0.55, value_confidence=0.40
    )


def _confident(identifier: str) -> WorthwhileEstimate:
    """A better paper the ranker has already settled, so its slot teaches nothing."""

    return _estimate(
        identifier, reading=0.65, reading_confidence=0.95, value=0.62, value_confidence=0.90
    )


def _unknown(identifier: str) -> WorthwhileEstimate:
    """No post-reading-value evidence at all: a maximal interval for the wrong reason."""

    return _estimate(
        identifier,
        reading=0.35,
        reading_confidence=0.0,
        value=0.30,
        value_confidence=0.0,
        evidence=False,
    )


def test_an_uncertain_promising_paper_wins_the_slot_over_settled_and_unknown_papers() -> None:
    pool = (
        _scored("2408.00001", index=0),
        _scored("2408.00002", index=1),
        _scored("2408.00003", index=2),
    )
    estimates = {
        "2408.00001": _uncertain("2408.00001"),
        "2408.00002": _confident("2408.00002"),
        "2408.00003": _unknown("2408.00003"),
    }

    decision = choose_exploration(pool, estimates)

    assert decision.selected == ("2408.00001",)
    assert decision.eligible_count == 1
    assert set(decision.declined_reasons) == {
        ExplorationOutcome.UNCERTAINTY_BELOW_MINIMUM,
        ExplorationOutcome.NO_VALUE_EVIDENCE,
    }


def test_category_difference_alone_does_not_earn_an_exploration_slot() -> None:
    off_category = _scored("2408.00001", index=0, source="exploration")
    on_category = _scored("2408.00002", index=1, source="core")
    estimates = {"2408.00001": _confident("2408.00001"), "2408.00002": _uncertain("2408.00002")}

    decision = choose_exploration((off_category, on_category), estimates)

    assert decision.selected == ("2408.00002",)


def test_a_paper_without_value_evidence_is_never_a_calculated_risk() -> None:
    pool = (_scored("2408.00003", index=0),)

    decision = choose_exploration(pool, {"2408.00003": _unknown("2408.00003")})

    assert decision.selected == ()
    assert decision.declined_reasons == (ExplorationOutcome.NO_VALUE_EVIDENCE,)


def test_an_unestimated_candidate_falls_back_to_the_prior_and_is_refused() -> None:
    decision = choose_exploration((_scored("2408.00004", index=0),))

    assert decision.selected == ()
    assert decision.considered_count == 1
    assert decision.declined_reasons == (ExplorationOutcome.NO_VALUE_EVIDENCE,)


def test_evidence_below_the_declared_confidence_minimum_is_refused() -> None:
    pool = (_scored("2408.00005", index=0),)
    barely_measured = _estimate(
        "2408.00005", reading=0.50, reading_confidence=0.60, value=0.55, value_confidence=0.10
    )

    decision = choose_exploration(pool, {"2408.00005": barely_measured})

    assert decision.selected == ()
    assert decision.declined_reasons == (ExplorationOutcome.EVIDENCE_CONFIDENCE_BELOW_MINIMUM,)


def test_an_estimate_from_another_objective_policy_version_is_refused() -> None:
    pool = (_scored("2408.00006", index=0),)
    foreign = _estimate(
        "2408.00006",
        reading=0.50,
        reading_confidence=0.60,
        value=0.55,
        value_confidence=0.40,
        policy_version="declared-prior-v99",
    )

    decision = choose_exploration(pool, {"2408.00006": foreign})

    assert decision.selected == ()
    assert decision.declined_reasons == (ExplorationOutcome.POLICY_VERSION_MISMATCH,)


def test_a_recently_published_paper_cannot_take_the_exploration_slot() -> None:
    pool = (_scored("2408.00001", index=0),)
    estimates = {"2408.00001": _uncertain("2408.00001")}

    decision = choose_exploration(pool, estimates, excluded_ids=frozenset({"2408.00001"}))

    assert decision.selected == ()
    assert decision.declined_reasons == (ExplorationOutcome.RECENTLY_PUBLISHED,)


def test_exploration_never_reaches_a_candidate_rejected_by_a_confident_value_gate() -> None:
    rejected = _scored("2408.00001", index=0)
    ordinary = _scored("2408.00002", index=1)
    values = {"2408.00001": ScientificValueAssessment(0.1, 0.1, 0.9)}
    estimates = {"2408.00001": _uncertain("2408.00001"), "2408.00002": _confident("2408.00002")}

    pool = qualified_candidates((rejected, ordinary), values)
    decision = choose_exploration(pool, estimates)
    selected = select_diverse((rejected, ordinary), scientific_values=values, exploration=decision)

    assert decision.selected == ()
    assert [item.candidate.arxiv_id.canonical for item in selected] == ["2408.00002"]


def test_exploration_costing_more_than_the_risk_budget_is_declined() -> None:
    pool = (_scored("2408.00001", index=0), _scored("2408.00002", index=1))
    estimates = {"2408.00001": _uncertain("2408.00001"), "2408.00002": _confident("2408.00002")}
    frugal = replace(DEFAULT_EXPLORATION_POLICY, risk_budget=0.05)

    affordable = choose_exploration(pool, estimates)
    refused = choose_exploration(pool, estimates, policy=frugal)

    assert affordable.selected == ("2408.00001",)
    assert affordable.spent == pytest.approx(affordable.assessments[0].expected_cost)
    assert affordable.spent <= DEFAULT_EXPLORATION_POLICY.risk_budget
    assert refused.selected == ()
    assert refused.eligible_count == 1
    assert ExplorationOutcome.COST_EXCEEDS_RISK_BUDGET in refused.declined_reasons


def test_no_safe_candidate_fills_the_slot_with_the_best_eligible_ordinary_candidate() -> None:
    best = _scored("2408.00010", index=0, score=0.9)
    ineligible = _scored("2408.00011", index=1, score=0.05)
    runner_up = _scored("2408.00012", index=2, score=0.7)
    trailing = _scored("2408.00013", index=3, score=0.6)
    pool = (best, ineligible, runner_up, trailing)
    estimates = {
        item.candidate.arxiv_id.canonical: _confident(item.candidate.arxiv_id.canonical)
        for item in pool
    }
    policy = SelectionPolicy(target=2)

    decision = choose_exploration(qualified_candidates(pool, policy=policy), estimates)
    selected = select_diverse(pool, policy=policy, exploration=decision)

    assert decision.selected == ()
    assert [item.candidate.arxiv_id.canonical for item in selected] == ["2408.00010", "2408.00012"]
    assert selected == select_diverse(pool, policy=policy)


def test_the_default_batch_admits_at_most_one_exploration_item() -> None:
    uncertain = tuple(
        _scored(f"2408.0000{index}", index=index, source="exploration") for index in range(1, 4)
    )
    settled = tuple(_scored(f"2408.0001{index}", index=index + 4) for index in range(5))
    scored = uncertain + settled
    estimates = {
        item.candidate.arxiv_id.canonical: (
            _uncertain(item.candidate.arxiv_id.canonical)
            if item.source == "exploration"
            else _confident(item.candidate.arxiv_id.canonical)
        )
        for item in scored
    }

    decision = choose_exploration(qualified_candidates(scored), estimates)
    selected = select_diverse(scored, policy=SelectionPolicy(target=5), exploration=decision)

    assert decision.budget == 1
    assert len(decision.selected) == 1
    assert decision.eligible_count == 3
    assert ExplorationOutcome.BUDGET_EXHAUSTED in decision.declined_reasons
    assert len(selected) == 5
    assert [item.source for item in selected].count("exploration") == 1
    assert decision.selected[0] in [item.candidate.arxiv_id.canonical for item in selected]


def test_exploration_records_the_evidence_behind_its_selection() -> None:
    pool = (_scored("2408.00001", index=0), _scored("2408.00002", index=1))
    estimates = {"2408.00001": _uncertain("2408.00001"), "2408.00002": _confident("2408.00002")}

    decision = choose_exploration(
        pool, estimates, policy=replace(DEFAULT_EXPLORATION_POLICY, seed="2026-08-30")
    )
    evidence = decision.assessments[0]

    assert decision.policy_version == "bounded-uncertainty-v1"
    assert decision.worthwhile_policy_version == DEFAULT_WORTHWHILE_POLICY.version
    assert decision.seed == "2026-08-30"
    assert decision.considered_count == 2
    assert evidence.arxiv_id == "2408.00001"
    assert evidence.outcome is ExplorationOutcome.SELECTED
    assert evidence.expected_worthwhile == pytest.approx(0.275)
    assert evidence.potential_worthwhile > DEFAULT_EXPLORATION_POLICY.minimum_potential
    assert evidence.uncertainty == pytest.approx(
        evidence.potential_worthwhile - evidence.conservative_worthwhile
    )
    assert evidence.expected_cost == pytest.approx(0.403 - 0.275)


def test_repeated_runs_are_deterministic_for_a_fixed_seed() -> None:
    pool = tuple(_scored(f"2408.0000{index}", index=index) for index in range(1, 5))
    estimates = {
        item.candidate.arxiv_id.canonical: _uncertain(item.candidate.arxiv_id.canonical)
        for item in pool
    }
    policy = replace(DEFAULT_EXPLORATION_POLICY, seed="2026-08-30")

    decisions = {choose_exploration(pool, estimates, policy=policy) for _ in range(5)}
    batches = {
        select_diverse(
            pool,
            policy=SelectionPolicy(target=2),
            exploration=choose_exploration(pool, estimates, policy=policy),
        )
        for _ in range(5)
    }

    assert len(decisions) == 1
    assert len(batches) == 1


def test_a_different_seed_can_rotate_among_equivalent_candidates() -> None:
    pool = (_scored("2408.00001", index=0), _scored("2408.00002", index=1))
    estimates = {
        "2408.00001": _uncertain("2408.00001"),
        "2408.00002": _uncertain("2408.00002"),
    }

    picks = {
        choose_exploration(
            pool, estimates, policy=replace(DEFAULT_EXPLORATION_POLICY, seed=str(seed))
        ).selected
        for seed in range(4)
    }

    assert picks == {("2408.00001",), ("2408.00002",)}


def test_exploration_cannot_expand_a_batch_beyond_its_target() -> None:
    pool = tuple(_scored(f"2408.0000{index}", index=index) for index in range(1, 6))
    estimates = {
        item.candidate.arxiv_id.canonical: _uncertain(item.candidate.arxiv_id.canonical)
        for item in pool
    }
    generous = replace(DEFAULT_EXPLORATION_POLICY, budget=4, risk_budget=1.0)

    decision = choose_exploration(pool, estimates, policy=generous)
    selected = select_diverse(pool, policy=SelectionPolicy(target=2), exploration=decision)

    assert len(decision.selected) == 4
    assert len(selected) == 2


def test_a_pick_outside_the_qualified_pool_is_dropped_rather_than_forced() -> None:
    pool = tuple(
        _scored(f"2408.0001{index}", index=index, score=0.9 - index / 10) for index in range(3)
    )
    stale = ExplorationDecision(
        "bounded-uncertainty-v1",
        DEFAULT_WORTHWHILE_POLICY.version,
        "",
        1,
        0.2,
        0.1,
        3,
        1,
        (
            ExplorationAssessment(
                "2408.99999", 0.3, 0.1, 0.5, 0.4, 0.1, ExplorationOutcome.SELECTED
            ),
        ),
    )
    policy = SelectionPolicy(target=2)

    selected = select_diverse(pool, policy=policy, exploration=stale)

    assert "2408.99999" not in [item.candidate.arxiv_id.canonical for item in selected]
    assert selected == select_diverse(pool, policy=policy)


def test_admitted_intervals_contain_their_expected_estimate_and_stay_bounded() -> None:
    pool = tuple(_scored(f"2408.000{index:02d}", index=index) for index in range(11))
    estimates = {
        item.candidate.arxiv_id.canonical: _estimate(
            item.candidate.arxiv_id.canonical,
            reading=0.3 + index / 20,
            reading_confidence=index / 10,
            value=0.9 - index / 20,
            value_confidence=1 - index / 20,
        )
        for index, item in enumerate(pool)
    }
    generous = replace(DEFAULT_EXPLORATION_POLICY, budget=11, risk_budget=1.0)

    decision = choose_exploration(pool, estimates, policy=generous)

    assert decision.assessments
    for evidence in decision.assessments:
        assert 0 <= evidence.conservative_worthwhile <= evidence.expected_worthwhile
        assert evidence.expected_worthwhile <= evidence.potential_worthwhile <= 1
        assert evidence.uncertainty >= generous.minimum_uncertainty


def _measured(identifier: str, *, index: int, quality: float, confidence: float) -> ScoredCandidate:
    """Equal reading evidence, so only how settled the value evidence is can differ."""

    return ScoredCandidate(
        _scored(identifier, index=index).candidate,
        0.5,
        (),
        "core",
        (
            NormalizedFeature("lexical", 0.6, True, 1.0, "test-fixture", FeatureGroup.INTEREST),
            NormalizedFeature("recency", 0.5, True, 1.0, "test-fixture", FeatureGroup.RECENCY),
            NormalizedFeature(
                "judged_quality",
                quality,
                True,
                confidence,
                "test-fixture",
                FeatureGroup.SCIENTIFIC_QUALITY,
            ),
        ),
    )


def test_declared_objective_estimates_compose_with_exploration() -> None:
    unsettled = _measured("2408.00001", index=0, quality=0.7, confidence=0.5)
    settled = _measured("2408.00002", index=1, quality=0.8, confidence=1.0)
    pool = (unsettled, settled)

    estimates = estimate_worthwhile(pool)
    decision = choose_exploration(pool, estimates)

    assert estimates["2408.00002"].expected_worthwhile > estimates["2408.00001"].expected_worthwhile
    assert decision.selected == ("2408.00001",)
    assert decision.declined_reasons == (ExplorationOutcome.UNCERTAINTY_BELOW_MINIMUM,)


def test_qualified_candidates_bounds_everything_selection_may_admit() -> None:
    below_minimum = _scored("2408.00001", index=0, score=0.05)
    weak_quality = _scored("2408.00002", index=1, quality=0.1)
    rejected = _scored("2408.00003", index=2)
    ordinary = _scored("2408.00004", index=3)
    values = {"2408.00003": ScientificValueAssessment(0.1, 0.1, 0.9)}
    scored = (below_minimum, weak_quality, rejected, ordinary)

    qualified = qualified_candidates(scored, values)

    assert [item.candidate.arxiv_id.canonical for item in qualified] == ["2408.00004"]
    assert set(select_diverse(scored, scientific_values=values)) <= set(qualified)


def test_exploration_policy_rejects_invalid_declared_limits() -> None:
    with pytest.raises(ValueError, match="requires a version"):
        ExplorationPolicy("")
    with pytest.raises(ValueError, match="cannot be negative"):
        ExplorationPolicy("test", budget=-1)
    with pytest.raises(ValueError, match="thresholds must be normalized"):
        ExplorationPolicy("test", risk_budget=1.5)


def test_an_exploration_decision_cannot_exceed_its_declared_budgets() -> None:
    assessment = ExplorationAssessment(
        "2408.00001", 0.3, 0.1, 0.5, 0.4, 0.1, ExplorationOutcome.SELECTED
    )

    with pytest.raises(ValueError, match="exceeds its declared budget"):
        ExplorationDecision("p", "w", "", 0, 0.2, 0.0, 1, 1, (assessment,))
    with pytest.raises(ValueError, match="exceeds its declared risk budget"):
        ExplorationDecision("p", "w", "", 1, 0.05, 0.1, 1, 1, (assessment,))


def test_an_exploration_assessment_interval_must_contain_its_expected_value() -> None:
    with pytest.raises(ValueError, match="must contain its expected value"):
        ExplorationAssessment("2408.00001", 0.9, 0.1, 0.5, 0.4, 0.1, ExplorationOutcome.SELECTED)
