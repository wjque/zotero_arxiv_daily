from __future__ import annotations

from collections.abc import Callable
from datetime import UTC, datetime

import pytest

from zotero_arxiv_daily.arxiv.discovery import (
    DiscoveryFacet,
    DiscoveryPolicy,
    DiscoveryQuery,
    bridge_candidate_matches,
    plan_discovery_queries,
)
from zotero_arxiv_daily.arxiv.models import ArxivCandidate, ArxivId


def _candidate(title: str, summary: str) -> ArxivCandidate:
    now = datetime(2026, 8, 20, tzinfo=UTC)
    return ArxivCandidate(
        ArxivId("2608.00001", 1),
        title,
        (),
        ("cs.IR",),
        now,
        now,
        "https://arxiv.org/abs/2608.00001",
        "https://arxiv.org/pdf/2608.00001",
        summary,
    )


def test_query_plan_preserves_released_path_then_adds_stable_bounded_bridges() -> None:
    facets = (
        DiscoveryFacet("method", "optimization", 0.7, 0.8),
        DiscoveryFacet("task", "retrieval", 1.0, 0.9),
    )

    first = plan_discovery_queries(("cs.LG",), facets)
    second = plan_discovery_queries(("cs.LG",), tuple(reversed(facets)))

    assert first == second
    assert [query.category for query in first] == [
        "cs.LG",
        "cs.AI",
        "cs.NE",
        "stat.ML",
        "cs.IR",
        "cs.DB",
        "math.OC",
        "cs.DS",
    ]
    assert [query.required_facets for query in first[-4:]] == [
        ("retrieval",),
        ("retrieval",),
        ("optimization",),
        ("optimization",),
    ]


def test_empty_or_weak_profile_never_expands_to_unbounded_search() -> None:
    assert plan_discovery_queries((), ()) == ()
    queries = plan_discovery_queries(
        ("cs.LG",),
        (DiscoveryFacet("task", "retrieval", 0.4, 0.4),),
    )

    assert len(queries) == 4
    assert all(not query.is_bridge for query in queries)


def test_policy_caps_core_adjacent_bridge_and_facet_inputs() -> None:
    policy = DiscoveryPolicy(
        maximum_core_queries=1,
        maximum_adjacent_queries=1,
        maximum_bridge_queries=1,
        maximum_facets=1,
    )
    queries = plan_discovery_queries(
        ("cs.LG", "cs.CL"),
        (
            DiscoveryFacet("task", "retrieval", 1.0, 1.0),
            DiscoveryFacet("method", "optimization", 0.9, 1.0),
        ),
        policy=policy,
    )

    assert [query.category for query in queries] == ["cs.LG", "cs.AI", "cs.IR"]


def test_duplicate_long_and_recent_facets_do_not_consume_distinct_facet_budget() -> None:
    queries = plan_discovery_queries(
        ("cs.LG",),
        (
            DiscoveryFacet("task", "retrieval", 0.8, 0.8),
            DiscoveryFacet("task", "retrieval", 1.0, 1.0),
            DiscoveryFacet("method", "optimization", 0.9, 1.0),
        ),
        policy=DiscoveryPolicy(maximum_bridge_queries=3, maximum_facets=2),
    )

    assert [query.category for query in queries[-3:]] == ["cs.IR", "cs.DB", "math.OC"]
    assert [query.required_facets for query in queries[-3:]] == [
        ("retrieval",),
        ("retrieval",),
        ("optimization",),
    ]


def test_bridge_matching_requires_declared_facet_in_public_candidate_text() -> None:
    query = DiscoveryQuery("cs.IR", ("retrieval", "machine-learning"))

    assert bridge_candidate_matches(
        _candidate("Dense retrieval for agents", "A public evaluation."), query
    )
    assert bridge_candidate_matches(
        _candidate("Cross-domain model", "A machine-learning benchmark."), query
    )
    assert not bridge_candidate_matches(
        _candidate("Storage layout", "A database systems benchmark."), query
    )
    assert not bridge_candidate_matches(
        _candidate("Storage layout", "A benchmark."), DiscoveryQuery("cs.IR", ("ai",))
    )


@pytest.mark.parametrize(
    "query",
    (
        lambda: DiscoveryQuery("cs.LG OR all:private"),
        lambda: DiscoveryQuery("cs.IR", ("private note",)),
    ),
)
def test_query_model_rejects_search_injection_and_unbounded_facet_text(
    query: Callable[[], object],
) -> None:
    with pytest.raises(ValueError):
        query()
