from __future__ import annotations

import json
from pathlib import Path

import pytest

from zotero_arxiv_daily.core.errors import ExternalServiceError
from zotero_arxiv_daily.feedback.ingest import FeedbackStateStore, parse_feedback


def _body(action: str = "interested") -> str:
    return json.dumps(
        {
            "schema_version": 1,
            "feedback": [
                {"arxiv_id": "2401.00001", "action": action, "updated_at": "2026-08-01T00:00:00Z"}
            ],
        }
    )


def test_feedback_ingestion_is_idempotent_and_does_not_advance_weekly_adjustments(
    tmp_path: Path,
) -> None:
    store = FeedbackStateStore(tmp_path / "feedback.json")

    first = store.ingest(((7, _body("not_interested")),))
    repeated = store.ingest(((7, _body("not_interested")),))

    assert first.action_count == 1
    assert repeated.duplicate_issues == 1
    assert store.adjustments() == {}


def test_malformed_issue_cannot_advance_processed_state(tmp_path: Path) -> None:
    store = FeedbackStateStore(tmp_path / "feedback.json")

    with pytest.raises(ExternalServiceError):
        store.ingest(((8, "not JSON"),))

    assert not (tmp_path / "feedback.json").exists()


def test_feedback_parser_rejects_unknown_actions() -> None:
    with pytest.raises(ExternalServiceError):
        parse_feedback(_body("run_command"))
