from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from zotero_arxiv_daily.arxiv.models import ArxivCandidate, ArxivId
from zotero_arxiv_daily.core.errors import ExternalServiceError
from zotero_arxiv_daily.llm.contracts import parse_proposals
from zotero_arxiv_daily.profile.models import RemoteProfile
from zotero_arxiv_daily.ranking.select import pre_rank, select_diverse


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
    with pytest.raises(ExternalServiceError, match="outside"):
        parse_proposals(
            '[{"arxiv_id":"9999.99999","quality":1,"summary":"x","reason":"x"}]',
            frozenset({"2401.00001"}),
        )


def test_feedback_adjustment_is_visible_in_local_score_components() -> None:
    profile = RemoteProfile(1, 1, ("learning",), ("cs.LG",), (), ())
    item = _candidate("2401.00001", "cs.LG", "Learning")

    scored = pre_rank((item,), profile, datetime(2026, 8, 1, tzinfo=UTC), {"2401.00001": -0.5})

    assert dict(scored[0].components)["feedback"] == -0.5
