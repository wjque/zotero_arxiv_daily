from __future__ import annotations

import json
from collections.abc import Sequence
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from zotero_arxiv_daily.arxiv.client import ArxivClient, TransientArxivError
from zotero_arxiv_daily.arxiv.discovery import (
    DiscoveryFacet,
    DiscoveryQuery,
    category_queries,
    plan_discovery_queries,
)
from zotero_arxiv_daily.arxiv.models import ArxivCandidate, ArxivId, RetrievalCheckpoint
from zotero_arxiv_daily.arxiv.retrieval import retrieve
from zotero_arxiv_daily.arxiv.storage import ArxivStateStore
from zotero_arxiv_daily.core.errors import ExternalServiceError

_FEED = b"""<feed xmlns="http://www.w3.org/2005/Atom"><entry><id>http://arxiv.org/abs/2401.01234v1</id><updated>2026-08-01T00:00:00Z</updated><published>2026-07-31T00:00:00Z</published><title>Paper</title><summary>Summary</summary><author><name>Ada</name></author><category term="cs.LG"/></entry></feed>"""


class Transport:
    def __init__(self, payloads: list[bytes | Exception]) -> None:
        self.payloads = payloads
        self.urls: list[str] = []

    def get(self, url: str, timeout_seconds: float) -> bytes:
        self.urls.append(url)
        value = self.payloads.pop(0)
        if isinstance(value, Exception):
            raise value
        return value


def test_client_serializes_requests_with_three_second_interval() -> None:
    clock = [0.0]
    sleeps: list[float] = []
    transport = Transport([_FEED, _FEED])
    client = ArxivClient(transport, lambda: clock[0], sleeps.append, retries=0)

    client.query("cat:cs.LG", 0, 1)
    client.query("cat:cs.LG", 1, 1)

    assert sleeps == [3.0]
    assert len(transport.urls) == 2


def test_client_retries_only_transient_transport_failures() -> None:
    sleeps: list[float] = []
    client = ArxivClient(Transport([TransientArxivError("429"), _FEED]), sleep=sleeps.append)

    assert len(client.query("cat:cs.LG", 0, 1)) == 1
    assert sleeps[0] == 1.0
    assert sleeps[1] == pytest.approx(3.0, rel=0.01)


class CandidateClient:
    def __init__(self, pages: Sequence[tuple[ArxivCandidate, ...] | Exception]) -> None:
        self.pages = list(pages)
        self.search_queries: list[str] = []
        self.maximums: list[int] = []

    def query(self, search_query: str, start: int, maximum: int) -> tuple[ArxivCandidate, ...]:
        self.search_queries.append(search_query)
        self.maximums.append(maximum)
        value = self.pages.pop(0)
        if isinstance(value, Exception):
            raise value
        return value


def _candidate(revision: int, updated: datetime, identifier: str = "2401.01234") -> ArxivCandidate:
    return ArxivCandidate(
        ArxivId(identifier, revision),
        "Paper",
        (),
        ("cs.LG",),
        updated,
        updated,
        f"https://arxiv.org/abs/{identifier}",
        f"https://arxiv.org/pdf/{identifier}",
        "summary",
    )


def test_retrieval_deduplicates_revisions_and_commits_only_after_success(tmp_path: Path) -> None:
    store = ArxivStateStore(tmp_path / "arxiv-state.json")
    now = datetime(2026, 8, 1, tzinfo=UTC)
    older = _candidate(1, now - timedelta(days=1))
    newer = _candidate(2, now)

    result = retrieve(
        CandidateClient([(older, newer)]),
        store,
        category_queries(("cs.LG",)),
        now,
        page_size=100,
    )

    assert len(result.candidates) == 1
    assert result.candidates[0].arxiv_id.revision == 2
    assert store.checkpoint() == result.checkpoint


def test_failed_retrieval_preserves_the_previous_checkpoint(tmp_path: Path) -> None:
    store = ArxivStateStore(tmp_path / "arxiv-state.json")
    previous = datetime(2026, 7, 31, tzinfo=UTC)
    store.commit(RetrievalCheckpoint(previous), ())

    with pytest.raises(ExternalServiceError):
        retrieve(
            CandidateClient([ExternalServiceError("timeout")]),
            store,
            category_queries(("cs.LG",)),
            previous + timedelta(days=1),
        )

    checkpoint = store.checkpoint()
    assert checkpoint is not None
    assert checkpoint.completed_at == previous
    assert store.in_progress_at() == previous + timedelta(days=1)


def test_retrieval_paginates_until_an_short_page(tmp_path: Path) -> None:
    store = ArxivStateStore(tmp_path / "arxiv-state.json")
    now = datetime(2026, 8, 1, tzinfo=UTC)
    first = _candidate(1, now)
    second = ArxivCandidate(
        ArxivId("2401.01235", 1),
        "Two",
        (),
        ("cs.LG",),
        now,
        now,
        "https://arxiv.org/abs/2401.01235",
        "https://arxiv.org/pdf/2401.01235",
        "summary",
    )

    result = retrieve(
        CandidateClient([(first, second), ()]),
        store,
        category_queries(("cs.LG",)),
        now,
        page_size=2,
    )

    assert result.request_count == 2
    assert len(result.candidates) == 2


def test_empty_increment_returns_retained_historical_candidate_pool(tmp_path: Path) -> None:
    store = ArxivStateStore(tmp_path / "arxiv-state.json")
    previous = datetime(2026, 8, 1, tzinfo=UTC)
    historical = _candidate(1, previous)
    store.commit(RetrievalCheckpoint(previous), (historical,))

    result = retrieve(
        CandidateClient([()]),
        store,
        category_queries(("cs.LG",)),
        previous + timedelta(days=1),
    )

    assert result.candidates == (historical,)
    assert store.candidates() == (historical,)


def test_empty_legacy_pool_triggers_bounded_seven_day_backfill(tmp_path: Path) -> None:
    store = ArxivStateStore(tmp_path / "arxiv-state.json")
    previous = datetime(2026, 8, 1, tzinfo=UTC)
    store.commit(RetrievalCheckpoint(previous), ())
    now = previous + timedelta(days=1)
    client = CandidateClient([(_candidate(1, previous),)])

    result = retrieve(client, store, category_queries(("cs.LG",)), now)

    assert "submittedDate:[202607260000 TO 202608020000]" in client.search_queries[0]
    assert len(result.candidates) == 1


def test_candidate_pool_keeps_newest_revision_and_prunes_expired_entries(
    tmp_path: Path,
) -> None:
    store = ArxivStateStore(tmp_path / "arxiv-state.json")
    started = datetime(2026, 7, 1, tzinfo=UTC)
    original = _candidate(1, started)
    revised = _candidate(2, started + timedelta(days=1))
    store.commit(RetrievalCheckpoint(started), (original,))
    store.commit(RetrievalCheckpoint(started + timedelta(days=1)), (revised,))

    assert store.candidates()[0].arxiv_id.revision == 2

    store.commit(RetrievalCheckpoint(started + timedelta(days=32)), ())
    assert store.candidates() == ()


def test_candidate_pool_is_bounded_to_newest_thousand_entries(tmp_path: Path) -> None:
    store = ArxivStateStore(tmp_path / "arxiv-state.json")
    now = datetime(2026, 8, 1, tzinfo=UTC)
    candidates = tuple(
        _candidate(1, now + timedelta(seconds=index), f"2401.{index:05d}") for index in range(1001)
    )

    store.commit(RetrievalCheckpoint(now + timedelta(seconds=1001)), candidates)

    retained = store.candidates()
    assert len(retained) == 1000
    assert retained[0].arxiv_id.canonical == "2401.01000"
    assert all(item.arxiv_id.canonical != "2401.00000" for item in retained)


def test_candidate_state_v3_reader_leaves_unknown_doi_unset(tmp_path: Path) -> None:
    path = tmp_path / "arxiv-state.json"
    path.write_text(
        json.dumps(
            {
                "schema_version": 3,
                "candidates": [
                    {
                        "arxiv_id": "2401.01234",
                        "revision": 1,
                        "title": "Legacy public paper",
                        "authors": [],
                        "categories": ["cs.LG"],
                        "published": "2026-08-01T00:00:00+00:00",
                        "updated": "2026-08-01T00:00:00+00:00",
                        "summary": "Legacy summary",
                        "affiliations": [],
                    }
                ],
            }
        ),
        encoding="utf-8",
    )

    assert ArxivStateStore(path).candidates()[0].doi is None


def test_transient_outage_uses_recent_pool_without_advancing_checkpoint(tmp_path: Path) -> None:
    store = ArxivStateStore(tmp_path / "arxiv-state.json")
    previous = datetime(2026, 8, 1, tzinfo=UTC)
    candidate = _candidate(1, previous)
    store.commit(RetrievalCheckpoint(previous), (candidate,))

    result = retrieve(
        CandidateClient([ExternalServiceError("timeout")]),
        store,
        category_queries(("cs.LG",)),
        previous + timedelta(days=1),
    )

    assert result.degraded is True
    assert result.candidates == (candidate,)
    assert store.checkpoint() == RetrievalCheckpoint(previous)
    assert store.retrieval_status()[0] is True
    assert result.degraded_reason == "timeout"
    assert store.retrieval_status()[1] == "timeout"


def test_controlled_discovery_recalls_declared_bridge_paper_without_remote_facets(
    tmp_path: Path,
) -> None:
    now = datetime(2026, 8, 20, tzinfo=UTC)
    core = ("cs.LG",)
    baseline_queries = category_queries(("cs.LG", "cs.AI", "cs.NE", "stat.ML"))
    baseline_client = CandidateClient([()] * len(baseline_queries))
    baseline = retrieve(
        baseline_client,
        ArxivStateStore(tmp_path / "baseline.json"),
        baseline_queries,
        now,
    )
    bridge = ArxivCandidate(
        ArxivId("2608.00001", 1),
        "Reliable dense retrieval for long-horizon agents",
        ("Ada",),
        ("cs.IR",),
        now,
        now,
        "https://arxiv.org/abs/2608.00001",
        "https://arxiv.org/pdf/2608.00001",
        "A retrieval benchmark for agent error propagation.",
    )
    unrelated = ArxivCandidate(
        ArxivId("2608.00002", 1),
        "Database storage layout",
        ("Turing",),
        ("cs.IR",),
        now,
        now,
        "https://arxiv.org/abs/2608.00002",
        "https://arxiv.org/pdf/2608.00002",
        "A public systems paper without the declared task.",
    )
    queries = plan_discovery_queries(core, (DiscoveryFacet("task", "retrieval", 1.0, 1.0),))
    pages = [(bridge, unrelated) if query.category == "cs.IR" else () for query in queries]
    client = CandidateClient(pages)

    expanded = retrieve(client, ArxivStateStore(tmp_path / "expanded.json"), queries, now)

    assert baseline.candidates == ()
    assert [candidate.arxiv_id.canonical for candidate in expanded.candidates] == ["2608.00001"]
    assert expanded.bridge_candidate_count == 1
    assert expanded.bridge_query_count == 2
    assert all("retrieval" not in search_query for search_query in client.search_queries)
    assert all("retrieval" not in search_query for search_query in baseline_client.search_queries)


def test_partial_bridge_failure_falls_back_to_previous_usable_pool(tmp_path: Path) -> None:
    store = ArxivStateStore(tmp_path / "arxiv-state.json")
    previous_at = datetime(2026, 8, 19, tzinfo=UTC)
    previous = _candidate(1, previous_at, "2608.00003")
    fresh = _candidate(1, previous_at + timedelta(days=1), "2608.00004")
    store.commit(RetrievalCheckpoint(previous_at), (previous,))
    queries = (
        DiscoveryQuery("cs.LG"),
        DiscoveryQuery("cs.IR", ("retrieval",)),
    )

    result = retrieve(
        CandidateClient([(fresh,), ExternalServiceError("bridge timeout")]),
        store,
        queries,
        previous_at + timedelta(days=1),
    )

    assert result.degraded is True
    assert result.candidates == (previous,)
    assert result.request_count == 2
    assert store.checkpoint() == RetrievalCheckpoint(previous_at)
    assert store.candidates() == (previous,)


def test_duplicate_across_category_and_bridge_queries_keeps_the_newest_revision(
    tmp_path: Path,
) -> None:
    now = datetime(2026, 8, 20, tzinfo=UTC)
    older = _candidate(1, now - timedelta(hours=1), "2608.00005")
    newer_source = _candidate(2, now, "2608.00005")
    newer = ArxivCandidate(
        newer_source.arxiv_id,
        "Retrieval across scientific collections",
        newer_source.authors,
        ("cs.IR",),
        newer_source.published,
        newer_source.updated,
        newer_source.abstract_url,
        newer_source.pdf_url,
        "A retrieval evaluation.",
    )

    result = retrieve(
        CandidateClient([(older,), (newer,)]),
        ArxivStateStore(tmp_path / "deduplicated.json"),
        (
            DiscoveryQuery("cs.LG"),
            DiscoveryQuery("cs.IR", ("retrieval",)),
        ),
        now,
    )

    assert len(result.candidates) == 1
    assert result.candidates[0].arxiv_id.revision == 2
    assert result.bridge_candidate_count == 0


def test_candidate_order_is_stable_for_equal_timestamps(tmp_path: Path) -> None:
    now = datetime(2026, 8, 20, tzinfo=UTC)
    lower = _candidate(1, now, "2608.00040")
    higher = _candidate(1, now, "2608.00041")

    first = retrieve(
        CandidateClient([(lower, higher)]),
        ArxivStateStore(tmp_path / "first.json"),
        category_queries(("cs.LG",)),
        now,
    )
    second = retrieve(
        CandidateClient([(higher, lower)]),
        ArxivStateStore(tmp_path / "second.json"),
        category_queries(("cs.LG",)),
        now,
    )

    expected = ["2608.00041", "2608.00040"]
    assert [candidate.arxiv_id.canonical for candidate in first.candidates] == expected
    assert [candidate.arxiv_id.canonical for candidate in second.candidates] == expected


def test_empty_and_excessive_query_plans_fail_before_network_or_state_mutation(
    tmp_path: Path,
) -> None:
    client = CandidateClient([])
    store = ArxivStateStore(tmp_path / "arxiv-state.json")
    now = datetime(2026, 8, 20, tzinfo=UTC)

    with pytest.raises(ValueError, match="at least one retrieval query"):
        retrieve(client, store, (), now)
    with pytest.raises(ValueError, match="deterministic boundary"):
        retrieve(
            client,
            store,
            tuple(DiscoveryQuery(f"cs.X{index}") for index in range(17)),
            now,
        )

    assert client.search_queries == []
    assert not store.path.exists()


def test_bridge_candidate_and_request_budgets_are_hard_limits(tmp_path: Path) -> None:
    now = datetime(2026, 8, 20, tzinfo=UTC)
    queries = (
        DiscoveryQuery("cs.LG"),
        DiscoveryQuery("cs.IR", ("retrieval",)),
        DiscoveryQuery("cs.DB", ("retrieval",)),
        DiscoveryQuery("cs.RO", ("reinforcement-learning",)),
        DiscoveryQuery("cs.SE", ("machine-learning",)),
    )
    base_page = tuple(_candidate(1, now, f"2608.{index:05d}") for index in range(3))
    bridge_pages = [
        (_candidate(1, now, "2608.00010"),),
        (_candidate(1, now, "2608.00011"),),
    ]
    pages: list[tuple[ArxivCandidate, ...]] = [
        base_page,
        *bridge_pages,
    ]
    pages[1:] = [
        tuple(
            ArxivCandidate(
                candidate.arxiv_id,
                "Retrieval benchmark",
                candidate.authors,
                ("cs.IR",),
                candidate.published,
                candidate.updated,
                candidate.abstract_url,
                candidate.pdf_url,
                "retrieval evaluation",
            )
            for candidate in page
        )
        for page in pages[1:]
    ]
    client = CandidateClient(pages)

    result = retrieve(
        client,
        ArxivStateStore(tmp_path / "bounded.json"),
        queries,
        now,
        candidate_ceiling=5,
        bridge_candidate_ceiling=2,
        page_size=100,
    )

    assert len(result.candidates) == 5
    assert result.bridge_candidate_count == 2
    assert result.request_count == 3
    assert client.maximums == [3, 1, 1]


def test_provider_cannot_exceed_requested_page_size(tmp_path: Path) -> None:
    now = datetime(2026, 8, 20, tzinfo=UTC)
    client = CandidateClient(
        [
            (
                _candidate(1, now, "2608.00020"),
                _candidate(1, now, "2608.00021"),
            )
        ]
    )

    with pytest.raises(ExternalServiceError, match="more candidates than requested"):
        retrieve(
            client,
            ArxivStateStore(tmp_path / "bounded.json"),
            category_queries(("cs.LG",)),
            now,
            candidate_ceiling=1,
        )

    assert client.maximums == [1]


def test_malformed_provider_metadata_uses_previous_usable_pool(tmp_path: Path) -> None:
    store = ArxivStateStore(tmp_path / "arxiv-state.json")
    previous_at = datetime(2026, 8, 19, tzinfo=UTC)
    previous = _candidate(1, previous_at, "2608.00030")
    store.commit(RetrievalCheckpoint(previous_at), (previous,))

    result = retrieve(
        CandidateClient([ExternalServiceError("arXiv returned malformed Atom XML")]),
        store,
        category_queries(("cs.LG",)),
        previous_at + timedelta(days=1),
    )

    assert result.degraded is True
    assert result.candidates == (previous,)
    assert store.checkpoint() == RetrievalCheckpoint(previous_at)


def test_retrieval_stops_at_the_logical_request_ceiling(tmp_path: Path) -> None:
    now = datetime(2026, 8, 20, tzinfo=UTC)
    client = CandidateClient([(_candidate(1, now, f"2608.{index:05d}"),) for index in range(10)])

    result = retrieve(
        client,
        ArxivStateStore(tmp_path / "bounded.json"),
        category_queries(("cs.LG",)),
        now,
        candidate_ceiling=100,
        page_size=1,
        request_ceiling=3,
    )

    assert result.request_count == 3
    assert len(result.candidates) == 3
    assert client.maximums == [1, 1, 1]
