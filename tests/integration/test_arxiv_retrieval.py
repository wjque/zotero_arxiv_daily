from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from zotero_arxiv_daily.arxiv.client import ArxivClient, TransientArxivError
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
    assert sleeps == [1.0]


class CandidateClient:
    def __init__(self, pages: list[tuple[ArxivCandidate, ...] | Exception]) -> None:
        self.pages = pages

    def query(self, search_query: str, start: int, maximum: int) -> tuple[ArxivCandidate, ...]:
        value = self.pages.pop(0)
        if isinstance(value, Exception):
            raise value
        return value


def _candidate(revision: int, updated: datetime) -> ArxivCandidate:
    return ArxivCandidate(
        ArxivId("2401.01234", revision),
        "Paper",
        (),
        ("cs.LG",),
        updated,
        updated,
        "https://arxiv.org/abs/2401.01234",
        "https://arxiv.org/pdf/2401.01234",
        "summary",
    )


def test_retrieval_deduplicates_revisions_and_commits_only_after_success(tmp_path: Path) -> None:
    store = ArxivStateStore(tmp_path / "arxiv-state.json")
    now = datetime(2026, 8, 1, tzinfo=UTC)
    older = _candidate(1, now - timedelta(days=1))
    newer = _candidate(2, now)

    result = retrieve(CandidateClient([(older, newer)]), store, ("cs.LG",), now, page_size=100)

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
            ("cs.LG",),
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

    result = retrieve(CandidateClient([(first, second), ()]), store, ("cs.LG",), now, page_size=2)

    assert result.request_count == 2
    assert len(result.candidates) == 2
