from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path

from zotero_arxiv_daily.arxiv.models import ArxivCandidate, ArxivId
from zotero_arxiv_daily.core.errors import ExternalServiceError
from zotero_arxiv_daily.evidence.project_page import (
    PageResponse,
    ProjectPageClient,
    approved_project_page_url,
    extract_project_page_urls,
    inspect_project_pages,
)
from zotero_arxiv_daily.llm.cache import ProposalCache


class _Transport:
    def __init__(self, responses: dict[str, PageResponse | Exception]) -> None:
        self.responses = responses
        self.calls: list[str] = []

    def fetch(self, url: str, timeout_seconds: float) -> PageResponse:
        self.calls.append(url)
        value = self.responses[url]
        if isinstance(value, Exception):
            raise value
        return value


def _candidate(summary: str) -> ArxivCandidate:
    now = datetime(2026, 8, 6, tzinfo=UTC)
    return ArxivCandidate(
        ArxivId("2401.00001", 1),
        "Project evidence",
        ("Ada",),
        ("cs.LG",),
        now,
        now,
        "https://arxiv.org/abs/2401.00001",
        "https://arxiv.org/pdf/2401.00001",
        summary,
    )


def test_extracts_only_approved_https_project_urls() -> None:
    summary = (
        "Code: https://github.com/example/project. Demo: https://team.github.io/demo/. "
        "Ignore http://github.com/example/insecure and https://github.com.evil.test/project."
    )

    assert extract_project_page_urls(summary) == (
        "https://github.com/example/project",
        "https://team.github.io/demo/",
    )
    assert approved_project_page_url("https://user:pass@github.com/example/project") is None
    assert approved_project_page_url("https://github.com:444/example/project") is None
    assert approved_project_page_url("https://github.com/") is None


def test_reachable_project_page_follows_only_approved_redirects() -> None:
    first = "https://github.com/example/project"
    final = "https://team.github.io/project/"
    transport = _Transport(
        {
            first: PageResponse(302, final),
            final: PageResponse(200),
        }
    )

    evidence = ProjectPageClient(transport).inspect(f"Project: {first}")

    assert evidence.url == final
    assert evidence.reachable is True
    assert transport.calls == [first, final]


def test_unreachable_or_timed_out_project_pages_do_not_claim_negative_evidence() -> None:
    url = "https://github.com/example/project"
    inaccessible = ProjectPageClient(_Transport({url: PageResponse(404)})).inspect(url)
    unavailable = ProjectPageClient(_Transport({url: ExternalServiceError("timed out")})).inspect(
        url
    )

    assert inaccessible.reachable is False
    assert not inaccessible.supports_open_source_proxy
    assert unavailable.reachable is None
    assert not unavailable.supports_open_source_proxy


def test_project_page_evidence_reuses_a_daily_candidate_cache(tmp_path: Path) -> None:
    url = "https://github.com/example/project"
    transport = _Transport({url: PageResponse(200)})
    client = ProjectPageClient(transport)
    cache = ProposalCache(tmp_path / "cache.json")
    candidate = _candidate(f"Project: {url}")
    now = datetime(2026, 8, 6, tzinfo=UTC)

    first = inspect_project_pages((candidate,), client, cache, now)
    second = inspect_project_pages((candidate,), client, cache, now)

    assert first == second
    assert first[candidate.arxiv_id.canonical].supports_open_source_proxy
    assert transport.calls == [url]
