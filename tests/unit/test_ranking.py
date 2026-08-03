from __future__ import annotations

from dataclasses import replace
from datetime import UTC, datetime, timedelta

import pytest

from zotero_arxiv_daily.arxiv.models import ArxivCandidate, ArxivId
from zotero_arxiv_daily.core.errors import ExternalServiceError
from zotero_arxiv_daily.llm.contracts import parse_proposals
from zotero_arxiv_daily.profile.models import RemoteProfile, WatchedIdentity
from zotero_arxiv_daily.ranking.models import RecommendationRecord
from zotero_arxiv_daily.ranking.select import order_recommendations, pre_rank, select_diverse


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


def test_local_ranking_is_inspectable_and_allows_fewer_than_target() -> None:
    profile = RemoteProfile(1, 1, ("learning",), ("cs.LG",), (), ("learning",))
    scored = pre_rank(
        (_candidate("2401.00001", "cs.LG", "Learning"),), profile, datetime(2026, 8, 1, tzinfo=UTC)
    )

    assert scored[0].components[0][0] == "lexical"
    assert len(select_diverse(scored, minimum_score=99)) == 0


def test_model_output_cannot_introduce_unknown_fields_or_ids() -> None:
    with pytest.raises(ExternalServiceError, match="unsupported"):
        parse_proposals(
            '[{"arxiv_id":"2401.00001","quality":1,"summary":"x","reason":"x","url":"bad"}]',
            frozenset({"2401.00001"}),
        )


def test_model_output_normalizes_an_allowed_arxiv_identifier() -> None:
    proposals = parse_proposals(
        '[{"arxiv_id":"arXiv:2401.00001v2","quality":1,"summary":"x","reason":"x"}]',
        frozenset({"2401.00001"}),
    )

    assert proposals[0].arxiv_id == "2401.00001"
    with pytest.raises(ExternalServiceError, match="was not requested"):
        parse_proposals(
            '[{"arxiv_id":"9999.99999","quality":1,"summary":"x","reason":"x"}]',
            frozenset({"2401.00001"}),
        )


def test_model_output_reports_safe_identifier_failure_categories() -> None:
    with pytest.raises(ExternalServiceError, match="must be a string"):
        parse_proposals(
            '[{"arxiv_id":2401,"quality":1,"summary":"x","reason":"x"}]',
            frozenset({"2401.00001"}),
        )


def test_model_output_field_order_does_not_change_validation() -> None:
    proposals = parse_proposals(
        '[{"reason":"x","summary":"x","quality":0.8,"arxiv_id":"2401.00001"}]',
        frozenset({"2401.00001"}),
    )

    assert proposals[0].quality == 0.8
    with pytest.raises(ExternalServiceError, match="malformed"):
        parse_proposals(
            '[{"arxiv_id":"not-an-id","quality":1,"summary":"x","reason":"x"}]',
            frozenset({"2401.00001"}),
        )


def test_feedback_adjustment_is_visible_in_local_score_components() -> None:
    profile = RemoteProfile(1, 1, ("learning",), ("cs.LG",), (), ())
    item = _candidate("2401.00001", "cs.LG", "Learning")

    scored = pre_rank((item,), profile, datetime(2026, 8, 1, tzinfo=UTC), {"2401.00001": -0.5})

    assert dict(scored[0].components)["feedback"] == 0
    assert dict(scored[0].components)["negative_feedback"] == 0.5


def test_watched_identity_matches_are_exact_inspectable_and_capped() -> None:
    profile = RemoteProfile(
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
    profile = RemoteProfile(
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
