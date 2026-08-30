from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from zotero_arxiv_daily.arxiv.models import ArxivCandidate, ArxivId
from zotero_arxiv_daily.ranking.models import ScientificValueAssessment, ScoredCandidate
from zotero_arxiv_daily.ranking.outcome import (
    DEFAULT_WORTHWHILE_POLICY,
    FactorCalibration,
    WorthwhileEstimate,
    WorthwhilePolicy,
    estimate_worthwhile,
    unknown_estimate,
)
from zotero_arxiv_daily.ranking.select import (
    SelectionObjective,
    SelectionPolicy,
    select_diverse,
)
from zotero_arxiv_daily.ranking.weights import FeatureGroup, NormalizedFeature

_NOW = datetime(2026, 8, 30, tzinfo=UTC)
# Titles that share no tokens, so the diversity limit never masks an objective-ordering assertion.
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


def _candidate(identifier: str, title: str, author: str = "Ada") -> ArxivCandidate:
    return ArxivCandidate(
        ArxivId(identifier, 1),
        title,
        (author,),
        ("cs.LG",),
        _NOW - timedelta(days=1),
        _NOW,
        f"https://arxiv.org/abs/{identifier}",
        f"https://arxiv.org/pdf/{identifier}",
        "learning methods",
    )


def _feature(
    name: str, value: float, group: FeatureGroup, confidence: float = 1.0
) -> NormalizedFeature:
    return NormalizedFeature(name, value, True, confidence, "test-fixture", group)


def _scored(
    identifier: str,
    *,
    score: float,
    interest: float | None = None,
    recency: float | None = None,
    identity: float | None = None,
    quality: float | None = None,
    reproducibility: float | None = None,
    source: str = "core",
    title: str | None = None,
    author: str = "Ada",
) -> ScoredCandidate:
    features: list[NormalizedFeature] = []
    if interest is not None:
        features.append(_feature("lexical", interest, FeatureGroup.INTEREST))
    if recency is not None:
        features.append(_feature("recency", recency, FeatureGroup.RECENCY))
    if identity is not None:
        features.append(_feature("identity", identity, FeatureGroup.IDENTITY))
    if quality is not None:
        features.append(_feature("judged_quality", quality, FeatureGroup.SCIENTIFIC_QUALITY))
    if reproducibility is not None:
        features.append(_feature("implementation", reproducibility, FeatureGroup.REPRODUCIBILITY))
    return ScoredCandidate(
        _candidate(identifier, title or f"Paper {identifier}", author),
        score,
        (),
        source,
        tuple(features),
    )


# The two cases the milestone names as opposites: a paper that looks personally attractive but
# scientifically weak, and one that looks scientifically strong but personally unfamiliar.
_HIGH_INTEREST_LOW_VALUE = _scored(
    "2408.00001", score=0.9, interest=1.0, recency=0.5, quality=0.25, title="Familiar topic"
)
_LOW_INTEREST_HIGH_VALUE = _scored(
    "2408.00002",
    score=0.3,
    interest=0.05,
    recency=0.5,
    quality=0.95,
    reproducibility=0.95,
    title="Unfamiliar rigorous method",
    author="Grace",
)


def test_expected_worthwhile_prefers_post_reading_value_over_familiarity() -> None:
    estimates = estimate_worthwhile((_HIGH_INTEREST_LOW_VALUE, _LOW_INTEREST_HIGH_VALUE))

    familiar = estimates["2408.00001"]
    rigorous = estimates["2408.00002"]
    assert familiar.reading_likelihood > rigorous.reading_likelihood
    assert familiar.post_reading_value < rigorous.post_reading_value
    assert rigorous.expected_worthwhile > familiar.expected_worthwhile


def test_confident_interest_cannot_rescue_confident_low_post_reading_value() -> None:
    weak = _scored("2408.00010", score=0.9, interest=1.0, recency=1.0, identity=1.0, quality=0.05)
    strong = _scored("2408.00011", score=0.9, interest=1.0, recency=1.0, identity=1.0, quality=0.9)

    estimates = estimate_worthwhile((weak, strong))

    assert estimates["2408.00010"].reading_likelihood == estimates["2408.00011"].reading_likelihood
    assert estimates["2408.00010"].expected_worthwhile < estimates["2408.00011"].expected_worthwhile


def test_declared_reading_floor_keeps_an_unfamiliar_paper_competitive() -> None:
    unfamiliar = _scored("2408.00020", score=0.2, interest=0.0, recency=0.0, identity=0.0)

    estimate = estimate_worthwhile((unfamiliar,))["2408.00020"]

    assert estimate.reading_likelihood >= DEFAULT_WORTHWHILE_POLICY.reading.floor
    assert estimate.expected_worthwhile > 0


def test_missing_evidence_yields_the_declared_prior_rather_than_zero() -> None:
    only_interest = _scored("2408.00030", score=0.5, interest=0.8, recency=0.5)

    estimate = estimate_worthwhile((only_interest,))["2408.00030"]

    assert estimate.post_reading_value == DEFAULT_WORTHWHILE_POLICY.post_reading_value.prior
    assert estimate.post_reading_value_confidence == 0.0
    assert estimate.value_evidence_available is False
    assert estimate.expected_worthwhile > 0


def test_unknown_estimate_is_the_declared_prior_at_zero_confidence() -> None:
    estimate = unknown_estimate("2408.00040")

    assert estimate.reading_likelihood == DEFAULT_WORTHWHILE_POLICY.reading.prior
    assert estimate.post_reading_value == DEFAULT_WORTHWHILE_POLICY.post_reading_value.prior
    assert estimate.reading_likelihood_confidence == 0.0
    assert estimate.value_evidence_available is False
    assert estimate.expected_worthwhile > 0


def test_local_value_assessment_lowers_a_paper_without_group_quality_evidence() -> None:
    candidate = _scored("2408.00050", score=0.7, interest=0.9, recency=0.5)
    assessment = ScientificValueAssessment(0.1, 0.1, 1.0)

    without = estimate_worthwhile((candidate,))["2408.00050"]
    with_assessment = estimate_worthwhile((candidate,), {"2408.00050": assessment})["2408.00050"]

    assert with_assessment.value_evidence_available is True
    assert with_assessment.post_reading_value < without.post_reading_value
    assert with_assessment.expected_worthwhile < without.expected_worthwhile


def test_estimates_stay_bounded_and_multiply_their_two_factors() -> None:
    candidates = tuple(
        _scored(
            f"2408.001{index:02d}",
            score=0.5,
            interest=index / 10,
            recency=1 - index / 10,
            quality=index / 10,
            reproducibility=index / 10,
        )
        for index in range(11)
    )

    for estimate in estimate_worthwhile(candidates).values():
        assert 0 <= estimate.reading_likelihood <= 1
        assert 0 <= estimate.post_reading_value <= 1
        assert 0 <= estimate.expected_worthwhile <= 1
        assert estimate.expected_worthwhile == pytest.approx(
            estimate.reading_likelihood * estimate.post_reading_value
        )


def test_estimation_is_deterministic_across_repeated_runs() -> None:
    candidates = (_HIGH_INTEREST_LOW_VALUE, _LOW_INTEREST_HIGH_VALUE)

    assert estimate_worthwhile(candidates) == estimate_worthwhile(candidates)


def test_factor_calibration_rejects_a_prior_outside_its_declared_bounds() -> None:
    with pytest.raises(ValueError, match="inside its bounds"):
        FactorCalibration(0.2, 0.8, 0.9)
    with pytest.raises(ValueError, match="ordered normalized interval"):
        FactorCalibration(0.8, 0.2, 0.5)


def test_worthwhile_policy_requires_a_version() -> None:
    with pytest.raises(ValueError, match="requires a version"):
        WorthwhilePolicy("", FactorCalibration(0.2, 0.9, 0.35), FactorCalibration(0.1, 0.85, 0.3))


def test_worthwhile_estimate_rejects_values_outside_zero_and_one() -> None:
    with pytest.raises(ValueError, match="normalized"):
        WorthwhileEstimate("2408.00001", 1.5, 1.0, 0.5, 1.0, 0.75, True, "v1", "quality-first-v1")


def test_relevance_objective_remains_the_default_selection_behavior() -> None:
    scored = (_HIGH_INTEREST_LOW_VALUE, _LOW_INTEREST_HIGH_VALUE)
    estimates = estimate_worthwhile(scored)

    assert SelectionPolicy().objective is SelectionObjective.RELEVANCE
    assert select_diverse(scored) == select_diverse(scored, policy=SelectionPolicy())
    assert select_diverse(scored, estimates=estimates) == select_diverse(scored)


def test_worthwhile_objective_changes_which_single_paper_is_selected() -> None:
    scored = (_HIGH_INTEREST_LOW_VALUE, _LOW_INTEREST_HIGH_VALUE)
    estimates = estimate_worthwhile(scored)

    relevance = select_diverse(scored, policy=SelectionPolicy(target=1))
    worthwhile = select_diverse(
        scored,
        policy=SelectionPolicy(target=1, objective=SelectionObjective.EXPECTED_WORTHWHILE),
        estimates=estimates,
    )

    assert [item.candidate.arxiv_id.canonical for item in relevance] == ["2408.00001"]
    assert [item.candidate.arxiv_id.canonical for item in worthwhile] == ["2408.00002"]


def test_conflicting_signal_stays_ineligible_despite_a_maximal_estimate() -> None:
    conflicted = _scored(
        "2408.00060", score=0.95, interest=1.0, recency=1.0, quality=0.9, author="Grace"
    )
    ordinary = _scored("2408.00061", score=0.4, interest=0.5, recency=0.5, quality=0.6)
    policy = SelectionPolicy(objective=SelectionObjective.EXPECTED_WORTHWHILE)

    selected = select_diverse(
        (conflicted, ordinary),
        policy=policy,
        scientific_values={"2408.00060": ScientificValueAssessment(0.1, 0.1, 0.9)},
        estimates={
            "2408.00060": WorthwhileEstimate(
                "2408.00060", 1.0, 1.0, 1.0, 1.0, 1.0, True, "declared-prior-v1", "test"
            )
        },
    )

    assert [item.candidate.arxiv_id.canonical for item in selected] == ["2408.00061"]


def test_worthwhile_objective_breaks_ties_by_canonical_identifier() -> None:
    tied = tuple(
        _scored(
            f"2408.002{index:02d}",
            score=0.5,
            interest=0.5,
            recency=0.5,
            quality=0.5,
            title=f"On {_SUBJECTS[index]}",
            author=f"Author {index}",
        )
        for index in reversed(range(6))
    )
    policy = SelectionPolicy(objective=SelectionObjective.EXPECTED_WORTHWHILE)
    estimates = estimate_worthwhile(tied)

    orders = {
        tuple(
            item.candidate.arxiv_id.canonical
            for item in select_diverse(tied, policy=policy, estimates=estimates)
        )
        for _ in range(5)
    }

    assert orders == {tuple(sorted(item.candidate.arxiv_id.canonical for item in tied))}


def test_estimates_cannot_expand_a_batch_beyond_its_target() -> None:
    candidates = tuple(
        _scored(
            f"2408.003{index:02d}",
            score=0.5,
            interest=0.9,
            recency=0.9,
            quality=0.9,
            title=f"On {_SUBJECTS[index]}",
            author=f"Author {index}",
        )
        for index in range(12)
    )
    estimates = {
        item.candidate.arxiv_id.canonical: WorthwhileEstimate(
            item.candidate.arxiv_id.canonical,
            1.0,
            1.0,
            1.0,
            1.0,
            1.0,
            True,
            "declared-prior-v1",
            "test",
        )
        for item in candidates
    }
    policy = SelectionPolicy(target=3, objective=SelectionObjective.EXPECTED_WORTHWHILE)

    selected = select_diverse(candidates, policy=policy, estimates=estimates)

    assert len(selected) == 3


def test_unestimated_candidates_fall_back_to_the_prior_rather_than_zero() -> None:
    estimated = _scored(
        "2408.00070", score=0.5, interest=0.05, recency=0.0, quality=0.3, title="Weak but known"
    )
    unestimated = _scored(
        "2408.00071",
        score=0.5,
        interest=0.9,
        recency=0.9,
        quality=0.9,
        title="Absent estimate",
        author="Grace",
    )
    policy = SelectionPolicy(target=1, objective=SelectionObjective.EXPECTED_WORTHWHILE)

    selected = select_diverse(
        (estimated, unestimated),
        policy=policy,
        estimates={"2408.00070": estimate_worthwhile((estimated,))["2408.00070"]},
    )

    assert [item.candidate.arxiv_id.canonical for item in selected] == ["2408.00071"]
