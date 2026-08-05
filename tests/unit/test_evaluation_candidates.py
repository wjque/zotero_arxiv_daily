from __future__ import annotations

from datetime import UTC, datetime

from zotero_arxiv_daily.arxiv.models import ArxivCandidate, ArxivId
from zotero_arxiv_daily.evaluation.candidates import hydrate_labeled_candidates


def _candidate(identifier: str) -> ArxivCandidate:
    now = datetime(2026, 8, 1, tzinfo=UTC)
    return ArxivCandidate(
        ArxivId(identifier, 1),
        "A paper",
        ("Author",),
        ("cs.LG",),
        now,
        now,
        f"https://arxiv.org/abs/{identifier}",
        f"https://arxiv.org/pdf/{identifier}",
        "An abstract.",
    )


class _Client:
    def __init__(self) -> None:
        self.queries: list[str] = []

    def query(self, search_query: str, start: int, maximum: int) -> tuple[ArxivCandidate, ...]:
        self.queries.append(search_query)
        return (_candidate("2401.00001"), _candidate("2401.00002"))


def test_hydration_uses_exact_ids_and_ignores_non_arxiv_labels() -> None:
    client = _Client()

    result = hydrate_labeled_candidates(
        client,
        ("doi:10.48550/arxiv.2401.00001", "doi:10.1000/not-arxiv", "arxiv:2401.00002"),
    )

    assert result.requested_ids == ("2401.00001", "2401.00002")
    assert [item.arxiv_id.canonical for item in result.candidates] == [
        "2401.00001",
        "2401.00002",
    ]
    assert result.unresolved_ids == ("doi:10.1000/not-arxiv",)
    assert client.queries == ["id:2401.00001 OR id:2401.00002"]
