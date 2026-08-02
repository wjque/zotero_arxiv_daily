from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path

from zotero_arxiv_daily.arxiv.models import ArxivCandidate, ArxivId
from zotero_arxiv_daily.ranking.models import RecommendationRecord, RecommendationSet
from zotero_arxiv_daily.storage.recommendation_history import RecommendationHistoryStore


def _result(now: datetime, identifier: str = "2401.00001") -> RecommendationSet:
    candidate = ArxivCandidate(
        ArxivId(identifier, 2),
        "Paper",
        ("Ada",),
        ("cs.LG",),
        now,
        now,
        f"https://arxiv.org/abs/{identifier}",
        f"https://arxiv.org/pdf/{identifier}",
        "Summary",
    )
    record = RecommendationRecord(candidate, 2, "core", 0.9, "Summary", "Reason")
    return RecommendationSet(2, 4, now, (record,), now)


def test_history_is_prepared_separately_and_suppresses_recent_success(tmp_path: Path) -> None:
    now = datetime(2026, 8, 2, tzinfo=UTC)
    current = tmp_path / "history.json"
    prepared = tmp_path / "history.next.json"
    store = RecommendationHistoryStore(current)

    store.prepare_success(_result(now), prepared, now)

    assert store.excluded_ids(now) == frozenset()
    promoted = RecommendationHistoryStore(prepared)
    assert promoted.excluded_ids(now) == frozenset({"2401.00001"})
    assert promoted.excluded_ids(now + timedelta(days=15)) == frozenset()
