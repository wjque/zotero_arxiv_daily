from __future__ import annotations

import json
from dataclasses import replace
from datetime import UTC, datetime, timedelta

import pytest

from zotero_arxiv_daily.arxiv.models import ArxivCandidate, ArxivId
from zotero_arxiv_daily.core.errors import ConfigurationError, ExternalServiceError
from zotero_arxiv_daily.llm.contracts import parse_proposals
from zotero_arxiv_daily.profile.build import project_serving_profile
from zotero_arxiv_daily.profile.models import (
    LocalInterestProfile,
    PreferenceFacet,
    ProtectedInterestPrototype,
    RemoteServingProfile,
    WatchedIdentity,
)
from zotero_arxiv_daily.profile.protection import protected_feature_digest
from zotero_arxiv_daily.ranking.models import RecommendationRecord, ScientificValueAssessment
from zotero_arxiv_daily.ranking.select import (
    order_recommendations,
    pre_rank,
    scientific_value_rejections,
    select_diverse,
)
from zotero_arxiv_daily.ranking.weights import DEFAULT_WEIGHT_SET, FeatureGroup, NormalizedFeature

_FEATURE_KEY = "test-profile-feature-key-0000000000000001"


def _candidate(identifier: str, category: str, title: str, age: int = 1) -> ArxivCandidate:
    now = datetime(2026, 8, 1, tzinfo=UTC)
    return ArxivCandidate(
        ArxivId(identifier, 1),
        title,
        ("Ada",),
        (category,),
        now - timedelta(days=age),
        now,
        f"https://arxiv.org/abs/{identifier}",
        f"https://arxiv.org/pdf/{identifier}",
        "learning methods",
    )


def _proposal_payload(
    identifier: object = "2401.00001", *, extra: dict[str, object] | None = None
) -> str:
    item: dict[str, object] = {
        "arxiv_id": identifier,
        "quality": 1,
        "summary": "A summary grounded in the supplied abstract.",
        "reason": "A concrete contribution connects to the profile topic.",
    }
    if extra:
        item.update(extra)
    return json.dumps([item])


def test_local_ranking_is_inspectable_and_allows_fewer_than_target() -> None:
    profile = RemoteServingProfile(1, 1, ("learning",), ("cs.LG",), (), ("learning",))
    scored = pre_rank(
        (_candidate("2401.00001", "cs.LG", "Learning"),), profile, datetime(2026, 8, 1, tzinfo=UTC)
    )

    assert scored[0].components[0][0] == "lexical"
    assert len(select_diverse(scored, minimum_score=99)) == 0


def test_model_output_cannot_introduce_unknown_fields_or_ids() -> None:
    with pytest.raises(ExternalServiceError, match="unsupported"):
        parse_proposals(
            _proposal_payload(extra={"url": "bad"}),
            frozenset({"2401.00001"}),
        )


def test_model_output_normalizes_an_allowed_arxiv_identifier() -> None:
    proposals = parse_proposals(
        _proposal_payload("arXiv:2401.00001v2"),
        frozenset({"2401.00001"}),
    )

    assert proposals[0].arxiv_id == "2401.00001"
    with pytest.raises(ExternalServiceError, match="was not requested"):
        parse_proposals(
            _proposal_payload("9999.99999"),
            frozenset({"2401.00001"}),
        )


def test_model_output_reports_safe_identifier_failure_categories() -> None:
    with pytest.raises(ExternalServiceError, match="must be a string"):
        parse_proposals(
            _proposal_payload(2401),
            frozenset({"2401.00001"}),
        )


def test_model_output_field_order_does_not_change_validation() -> None:
    proposals = parse_proposals(
        json.dumps(
            {
                "reason": "A concrete contribution connects to the profile topic.",
                "summary": "A summary grounded in the supplied abstract.",
                "quality": 0.8,
                "arxiv_id": "2401.00001",
            }
        )
        .replace("{", "[{", 1)
        .replace("}", "}]", 1),
        frozenset({"2401.00001"}),
    )

    assert proposals[0].quality == 0.8
    with pytest.raises(ExternalServiceError, match="malformed"):
        parse_proposals(
            _proposal_payload("not-an-id"),
            frozenset({"2401.00001"}),
        )


def test_feedback_is_not_a_v020_ranking_feature() -> None:
    profile = RemoteServingProfile(1, 1, ("learning",), ("cs.LG",), (), ())
    item = _candidate("2401.00001", "cs.LG", "Learning")

    scored = pre_rank((item,), profile, datetime(2026, 8, 1, tzinfo=UTC))

    assert (
        not {"feedback", "negative_feedback", "negative_feedback_penalty"}
        & dict(scored[0].components).keys()
    )


def test_watched_identity_matches_are_exact_inspectable_and_capped() -> None:
    profile = RemoteServingProfile(
        2,
        1,
        ("learning",),
        ("cs.LG",),
        (),
        (),
        watched_authors=(WatchedIdentity("Fei-Fei Li", ("Li Fei-Fei",)),),
        watched_institutions=(WatchedIdentity("MIT", ("Massachusetts Institute of Technology",)),),
    )
    base = _candidate("2401.00001", "cs.LG", "Learning")
    matching = ArxivCandidate(
        base.arxiv_id,
        base.title,
        ("FEI FEI LI",),
        base.categories,
        base.published,
        base.updated,
        base.abstract_url,
        base.pdf_url,
        base.summary,
        ("Massachusetts Institute of Technology",),
    )

    components = dict(
        pre_rank((matching,), profile, datetime(2026, 8, 1, tzinfo=UTC))[0].components
    )

    assert components["watched_author"] == 0.75
    assert components["watched_institution"] == 0.25
    assert components["watched_author"] + components["watched_institution"] == 1.0


def test_watched_author_substring_does_not_match() -> None:
    profile = RemoteServingProfile(
        2,
        1,
        (),
        ("cs.LG",),
        (),
        (),
        watched_authors=(WatchedIdentity("Yann LeCun"),),
    )
    base = _candidate("2401.00001", "cs.LG", "Learning")
    candidate = ArxivCandidate(
        base.arxiv_id,
        base.title,
        ("Yann LeCun Jr",),
        base.categories,
        base.published,
        base.updated,
        base.abstract_url,
        base.pdf_url,
        base.summary,
    )

    assert (
        dict(pre_rank((candidate,), profile, datetime(2026, 8, 1, tzinfo=UTC))[0].components)[
            "watched_author"
        ]
        == 0
    )


def test_protected_profile_matches_long_recent_and_anonymous_paper_interests() -> None:
    local = LocalInterestProfile(
        2,
        1,
        (("learning", 4.0), ("retrieval", 2.0)),
        (("retrieval", 3.0),),
        (("cs.LG", 1.0, "test"),),
        2,
        (PreferenceFacet("task", "retrieval", 0.8, 1.0, ("test",)),),
        (PreferenceFacet("task", "retrieval", 1.0, 1.0, ("test",)),),
    )
    base = project_serving_profile(local, _FEATURE_KEY)
    profile = replace(
        base,
        interest_prototypes=(
            ProtectedInterestPrototype(
                (protected_feature_digest("retrieval", _FEATURE_KEY, namespace="lexical"),)
            ),
        ),
    )
    matching = _candidate("2401.00001", "cs.LG", "Retrieval learning")
    unrelated = _candidate("2401.00002", "cs.LG", "Unrelated geometry")

    scored = pre_rank(
        (unrelated, matching),
        profile,
        datetime(2026, 8, 1, tzinfo=UTC),
        profile_feature_key=_FEATURE_KEY,
    )
    matching_components = dict(scored[0].components)

    assert scored[0].candidate.arxiv_id.canonical == "2401.00001"
    assert matching_components["long_term_lexical"] > 0
    assert matching_components["recent_lexical"] > 0
    assert matching_components["prototype"] == 1
    assert matching_components["recent_facet"] > 0
    assert "lexical" not in matching_components


def test_protected_profile_rejects_missing_or_mismatched_matching_key() -> None:
    local = LocalInterestProfile(2, 1, (("learning", 1.0),), (), (), 1)
    profile = project_serving_profile(local, _FEATURE_KEY)
    candidate = _candidate("2401.00001", "cs.LG", "Learning")

    with pytest.raises(ConfigurationError, match="32 UTF-8 bytes"):
        pre_rank((candidate,), profile, datetime(2026, 8, 1, tzinfo=UTC))
    with pytest.raises(ConfigurationError, match="does not match"):
        pre_rank(
            (candidate,),
            profile,
            datetime(2026, 8, 1, tzinfo=UTC),
            profile_feature_key="wrong-profile-feature-key-00000000000000001",
        )


def test_protected_watchlist_matches_exact_identity_without_exposing_its_name() -> None:
    local = LocalInterestProfile(2, 1, (("learning", 1.0),), (), (), 1)
    base = project_serving_profile(local, _FEATURE_KEY)
    profile = replace(
        base,
        watched_author_digests=(
            protected_feature_digest("yann lecun", _FEATURE_KEY, namespace="author"),
        ),
    )
    exact = replace(_candidate("2401.00001", "cs.LG", "Learning"), authors=("Yann LeCun",))
    substring = replace(_candidate("2401.00002", "cs.LG", "Learning"), authors=("Yann LeCun Jr",))

    exact_components = dict(
        pre_rank(
            (exact,),
            profile,
            datetime(2026, 8, 1, tzinfo=UTC),
            profile_feature_key=_FEATURE_KEY,
        )[0].components
    )
    substring_components = dict(
        pre_rank(
            (substring,),
            profile,
            datetime(2026, 8, 1, tzinfo=UTC),
            profile_feature_key=_FEATURE_KEY,
        )[0].components
    )

    assert exact_components["watched_author"] == 0.75
    assert substring_components["watched_author"] == 0
    assert "yann" not in json.dumps(profile, default=str).casefold()


def test_final_order_is_relevance_first_then_quality_and_stable_ties() -> None:
    first = RecommendationRecord(
        _candidate("2401.00003", "cs.LG", "Learning", age=3), 4.0, "core", 0.5, "x", "x"
    )
    second = RecommendationRecord(
        _candidate("2401.00002", "cs.LG", "Learning", age=2), 4.0, "core", 0.9, "x", "x"
    )
    third = RecommendationRecord(
        _candidate("2401.00001", "cs.LG", "Learning", age=1), 3.0, "core", 1.0, "x", "x"
    )
    fourth = RecommendationRecord(
        replace(
            _candidate("2401.00004", "cs.LG", "Learning", age=2),
            updated=datetime(2026, 8, 2, tzinfo=UTC),
        ),
        4.0,
        "core",
        0.9,
        "x",
        "x",
    )
    fifth = RecommendationRecord(
        replace(
            _candidate("2401.00005", "cs.LG", "Learning", age=2),
            updated=datetime(2026, 8, 2, tzinfo=UTC),
        ),
        4.0,
        "core",
        0.9,
        "x",
        "x",
    )

    ordered = order_recommendations((first, second, third, fifth, fourth))

    assert [record.candidate.arxiv_id.canonical for record in ordered] == [
        "2401.00004",
        "2401.00005",
        "2401.00002",
        "2401.00003",
        "2401.00001",
    ]


def test_normalized_ranker_preserves_core_adjacent_and_exploration_sources() -> None:
    profile = RemoteServingProfile(1, 1, ("learning",), ("cs.LG",), ("cs.AI",), ())
    scored = pre_rank(
        (
            _candidate("2401.00001", "cs.LG", "Core learning"),
            _candidate("2401.00002", "cs.AI", "Adjacent learning"),
            _candidate("2401.00003", "math.OC", "Exploration learning"),
        ),
        profile,
        datetime(2026, 8, 1, tzinfo=UTC),
    )

    assert {item.candidate.arxiv_id.canonical: item.source for item in scored} == {
        "2401.00001": "core",
        "2401.00002": "adjacent",
        "2401.00003": "exploration",
    }
    assert "scientific_quality_value" not in dict(scored[0].components)


def test_unknown_extra_evidence_is_excluded_instead_of_becoming_a_zero_score() -> None:
    profile = RemoteServingProfile(1, 1, ("learning",), ("cs.LG",), (), ())
    candidate = _candidate("2401.00001", "cs.LG", "Learning")

    baseline = pre_rank((candidate,), profile, datetime(2026, 8, 1, tzinfo=UTC))[0]
    unknown = pre_rank(
        (candidate,),
        profile,
        datetime(2026, 8, 1, tzinfo=UTC),
        extra_features={
            "2401.00001": (
                NormalizedFeature(
                    "judge_quality",
                    0.0,
                    False,
                    0.0,
                    "judge-v3",
                    FeatureGroup.SCIENTIFIC_QUALITY,
                ),
            )
        },
    )[0]

    assert unknown.score == baseline.score


def test_local_value_gates_reject_supported_incremental_work_but_preserve_unknowns() -> None:
    profile = RemoteServingProfile(1, 1, ("learning",), ("cs.LG",), (), ())
    candidates = tuple(
        _candidate(f"2401.{index:05d}", "cs.LG", f"Learning {title}")
        for index, title in enumerate(("alpha", "beta", "gamma", "delta"), start=1)
    )
    scored = pre_rank(candidates, profile, datetime(2026, 8, 1, tzinfo=UTC))

    assessments = {
        "2401.00001": ScientificValueAssessment(0.25, 0.8, 0.8),
        "2401.00002": ScientificValueAssessment(0.8, 0.25, 0.8),
        "2401.00003": ScientificValueAssessment(None, None, 0.8),
        "2401.00004": ScientificValueAssessment(0.25, 0.25, 0.4),
    }
    selected = select_diverse(scored, scientific_values=assessments)

    identifiers = {item.candidate.arxiv_id.canonical for item in selected}
    assert identifiers == {"2401.00003", "2401.00004"}
    assert scientific_value_rejections(scored, assessments) == frozenset(
        {"2401.00001", "2401.00002"}
    )


def test_quality_first_contributions_use_declared_available_group_weights() -> None:
    profile = RemoteServingProfile(1, 1, ("learning",), ("cs.LG",), (), ())
    candidate = _candidate("2401.00001", "cs.LG", "Learning")
    scored = pre_rank(
        (candidate,),
        profile,
        datetime(2026, 8, 1, tzinfo=UTC),
        extra_features={
            "2401.00001": (
                NormalizedFeature(
                    "judge_quality", 0.8, True, 1.0, "judge-v3", FeatureGroup.SCIENTIFIC_QUALITY
                ),
                NormalizedFeature(
                    "accessible_project_page",
                    1.0,
                    True,
                    1.0,
                    "project-page-v1",
                    FeatureGroup.REPRODUCIBILITY,
                ),
            )
        },
    )[0]
    components = dict(scored.components)
    available = (
        DEFAULT_WEIGHT_SET.interest
        + DEFAULT_WEIGHT_SET.recency
        + DEFAULT_WEIGHT_SET.scientific_quality
        + DEFAULT_WEIGHT_SET.reproducibility
    )

    assert components["scientific_quality_contribution"] == pytest.approx(0.35 * 0.8 / available)
    assert components["reproducibility_contribution"] == pytest.approx(0.10 / available)


def test_facet_matching_normalizes_hyphenated_and_spaced_labels() -> None:
    from zotero_arxiv_daily.profile.models import PreferenceFacet

    profile = RemoteServingProfile(
        4,
        1,
        ("learning",),
        ("cs.LG",),
        (),
        (),
        preference_facets=(
            PreferenceFacet("method", "reinforcement-learning", 1.0, 1.0, ("test",)),
        ),
    )
    candidate = _candidate("2401.00001", "cs.LG", "Reinforcement learning methods")

    scored = pre_rank((candidate,), profile, datetime(2026, 8, 1, tzinfo=UTC))[0]

    assert dict(scored.components)["facet"] > 0

    hyphenated_candidate = replace(candidate, title="Reinforcement-learning methods")
    hyphenated_score = pre_rank((hyphenated_candidate,), profile, datetime(2026, 8, 1, tzinfo=UTC))[
        0
    ]
    assert dict(hyphenated_score.components)["facet"] > 0
