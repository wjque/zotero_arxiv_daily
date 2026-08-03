from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from pathlib import Path

from zotero_arxiv_daily.feedback.ledger import (
    FeedbackEvent,
    FeedbackEventType,
    FeedbackLedgerStore,
    FeedbackOutcome,
)

_NOW = datetime(2026, 8, 3, 12, tzinfo=UTC)


def _outcome(
    event_id: str, paper_id: str, outcome: FeedbackOutcome, *, supersedes: str | None = None
) -> FeedbackEvent:
    return FeedbackEvent(
        event_id,
        FeedbackEventType.CORRECTION if supersedes else FeedbackEventType.OUTCOME,
        paper_id,
        _NOW,
        outcome,
        supersedes_event_id=supersedes,
    )


def test_ledger_migrates_v1_and_activates_only_after_a_weekly_eligible_sample(
    tmp_path: Path,
) -> None:
    path = tmp_path / "feedback.json"
    path.write_text(
        json.dumps(
            {"schema_version": 1, "processed_issue_numbers": [7], "adjustments": {"a": 0.25}}
        ),
        encoding="utf-8",
    )
    store = FeedbackLedgerStore(path)
    store.ingest(
        (
            _outcome("one", "a", FeedbackOutcome.WORTHWHILE),
            _outcome("two", "b", FeedbackOutcome.NOT_INTERESTED),
            _outcome("three", "c", FeedbackOutcome.INTERESTED),
        )
    )

    activated = store.activate_weekly(_NOW, minimum_independent_papers=3)

    assert activated.decision == "activated"
    assert store.active_adjustments() == {"a": 0.6, "b": -0.5, "c": 0.25}
    assert store.activate_weekly(_NOW + timedelta(days=1)).decision == "not-eligible"
    assert json.loads(path.read_text(encoding="utf-8"))["legacy_adjustments"] == {"a": 0.25}


def test_ledger_keeps_corrections_and_never_treats_impressions_as_negative(tmp_path: Path) -> None:
    store = FeedbackLedgerStore(tmp_path / "feedback.json")
    impression = FeedbackEvent(
        "shown", FeedbackEventType.IMPRESSION, "a", _NOW, batch_id="run", displayed_rank=1
    )
    outcome = _outcome("outcome", "a", FeedbackOutcome.NOT_INTERESTED)
    correction = _outcome("corrected", "a", FeedbackOutcome.WORTHWHILE, supersedes="outcome")

    assert store.ingest((impression, outcome, correction)) == (3, 0)
    assert store.ingest((correction,)) == (0, 1)
    assert store.activate_weekly(_NOW, minimum_independent_papers=1).decision == "activated"
    assert store.active_adjustments() == {"a": 0.6}
