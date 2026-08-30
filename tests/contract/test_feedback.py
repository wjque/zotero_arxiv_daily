from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path

import pytest

from zotero_arxiv_daily.core.errors import ExternalServiceError
from zotero_arxiv_daily.feedback.ingest import FeedbackStateStore, parse_feedback
from zotero_arxiv_daily.feedback.ledger import FeedbackLedgerStore, FeedbackOutcome


def _body(action: str = "interested") -> str:
    return json.dumps(
        {
            "schema_version": 1,
            "feedback": [
                {"arxiv_id": "2401.00001", "action": action, "updated_at": "2026-08-01T00:00:00Z"}
            ],
        }
    )


def _body_v2(*actions: tuple[str, str], batch_id: str = "published-one") -> str:
    return json.dumps(
        {
            "schema_version": 2,
            "feedback": [
                {
                    "arxiv_id": "2401.00001",
                    "batch_id": batch_id,
                    "actions": [
                        {"action": action, "updated_at": updated_at}
                        for action, updated_at in actions
                    ],
                }
            ],
        }
    )


def test_feedback_ingestion_is_idempotent_and_stores_only_explicit_events(
    tmp_path: Path,
) -> None:
    store = FeedbackStateStore(tmp_path / "feedback.json")

    first = store.ingest(((7, _body("not_interested")),))
    repeated = store.ingest(((7, _body("not_interested")),))

    assert first.action_count == 1
    assert repeated.duplicate_issues == 1
    assert len(FeedbackLedgerStore(store.path).events()) == 1


def test_malformed_issue_cannot_advance_processed_state(tmp_path: Path) -> None:
    store = FeedbackStateStore(tmp_path / "feedback.json")

    with pytest.raises(ExternalServiceError):
        store.ingest(((8, "not JSON"),))

    assert not (tmp_path / "feedback.json").exists()


def test_feedback_parser_rejects_unknown_actions() -> None:
    with pytest.raises(ExternalServiceError):
        parse_feedback(_body("run_command"))


def test_legacy_feedback_rejects_non_string_fields() -> None:
    body = json.dumps(
        {
            "schema_version": 1,
            "feedback": [{"arxiv_id": 7, "action": "read", "updated_at": "2026-08-01T00:00:00Z"}],
        }
    )

    with pytest.raises(ExternalServiceError):
        parse_feedback(body)


def test_duplicate_issue_identity_is_rejected_atomically(tmp_path: Path) -> None:
    store = FeedbackStateStore(tmp_path / "feedback.json")

    with pytest.raises(ExternalServiceError, match="duplicate identities"):
        store.ingest(((9, _body()), (9, _body("read"))))

    assert not store.path.exists()


def test_v2_feedback_records_reading_and_explicit_worthwhile_outcome(tmp_path: Path) -> None:
    store = FeedbackStateStore(tmp_path / "feedback.json")
    ledger = FeedbackLedgerStore(store.path)
    ledger.record_impressions("published-one", ("2401.00001",), datetime(2026, 8, 1, tzinfo=UTC))

    result = store.ingest(
        (
            (
                9,
                _body_v2(
                    ("read", "2026-08-01T01:00:00Z"),
                    ("worthwhile", "2026-08-01T02:00:00Z"),
                ),
            ),
        )
    )

    assert result.action_count == 2
    assert [event.outcome for event in ledger.events() if event.outcome is not None] == [
        FeedbackOutcome.READ,
        FeedbackOutcome.WORTHWHILE,
    ]
    metrics = ledger.batch_outcomes()[0]
    assert metrics.worthwhile_read_count == 1
    assert metrics.reading_completion_count == 1
    assert metrics.post_reading_outcome_coverage == 1.0
    assert metrics.worthwhile_given_explicit_outcome == 1.0
    assert metrics.explicit_feedback_coverage == 1.0


def test_v2_post_reading_outcome_requires_an_explicit_read(tmp_path: Path) -> None:
    store = FeedbackStateStore(tmp_path / "feedback.json")

    with pytest.raises(ExternalServiceError, match="invalid action sequence"):
        store.ingest(((10, _body_v2(("worthwhile", "2026-08-01T02:00:00Z"))),))

    assert not store.path.exists()


def test_v2_delayed_outcome_can_be_corrected_without_duplicate_reading(tmp_path: Path) -> None:
    store = FeedbackStateStore(tmp_path / "feedback.json")
    ledger = FeedbackLedgerStore(store.path)
    ledger.record_impressions("published-one", ("2401.00001",), datetime(2026, 8, 1, tzinfo=UTC))
    store.ingest(((11, _body_v2(("read", "2026-08-01T01:00:00Z"))),))
    store.ingest(((12, _body_v2(("worthwhile", "2026-08-02T01:00:00Z"))),))

    corrected = store.ingest(((13, _body_v2(("not_worthwhile", "2026-08-03T01:00:00Z"))),))
    repeated = store.ingest(((14, _body_v2(("not_worthwhile", "2026-08-04T01:00:00Z"))),))

    assert corrected.action_count == 1
    assert repeated.action_count == 0
    active = {
        event.outcome
        for event in ledger.events()
        if event.event_id
        not in {
            value.supersedes_event_id
            for value in ledger.events()
            if value.supersedes_event_id is not None
        }
    }
    assert FeedbackOutcome.WORTHWHILE not in active
    assert FeedbackOutcome.NOT_WORTHWHILE in active
    metrics = ledger.batch_outcomes()[0]
    assert metrics.reading_completion_count == 1
    assert metrics.worthwhile_read_count == 0
    assert metrics.not_worthwhile_read_count == 1


def test_v2_rejects_multiple_actions_for_the_same_stage() -> None:
    with pytest.raises(ExternalServiceError):
        parse_feedback(
            _body_v2(
                ("interested", "2026-08-01T00:00:00Z"),
                ("save_for_later", "2026-08-01T01:00:00Z"),
            )
        )


@pytest.mark.parametrize(
    "action",
    ("interested", "not_interested", "save_for_later", "read"),
)
def test_v2_accepts_each_explicit_pre_reading_or_reading_action(action: str) -> None:
    assert parse_feedback(_body_v2((action, "2026-08-01T00:00:00Z")))[0].action == action


@pytest.mark.parametrize("outcome", ("worthwhile", "not_worthwhile"))
def test_v2_accepts_each_post_reading_outcome_after_read(tmp_path: Path, outcome: str) -> None:
    store = FeedbackStateStore(tmp_path / f"{outcome}.json")

    result = store.ingest(
        (
            (
                20,
                _body_v2(
                    ("read", "2026-08-01T01:00:00Z"),
                    (outcome, "2026-08-01T02:00:00Z"),
                ),
            ),
        )
    )

    assert result.action_count == 2


def test_v2_pre_reading_action_can_be_corrected(tmp_path: Path) -> None:
    store = FeedbackStateStore(tmp_path / "feedback.json")
    store.ingest(((21, _body_v2(("interested", "2026-08-01T01:00:00Z"))),))

    result = store.ingest(((22, _body_v2(("not_interested", "2026-08-02T01:00:00Z"))),))

    assert result.action_count == 1
    latest = FeedbackLedgerStore(store.path).events()[-1]
    assert latest.outcome is FeedbackOutcome.NOT_INTERESTED
    assert latest.supersedes_event_id == "issue-21-0"


def test_v2_keeps_reading_and_outcome_sequences_isolated_by_batch(tmp_path: Path) -> None:
    store = FeedbackStateStore(tmp_path / "feedback.json")
    store.ingest(((23, _body_v2(("read", "2026-08-01T01:00:00Z"), batch_id="batch-a")),))

    with pytest.raises(ExternalServiceError, match="invalid action sequence"):
        store.ingest(
            (
                (
                    24,
                    _body_v2(("worthwhile", "2026-08-02T01:00:00Z"), batch_id="batch-b"),
                ),
            )
        )

    ledger = FeedbackLedgerStore(store.path)
    assert ledger.processed_issue_numbers() == frozenset({23})
    assert [event.batch_id for event in ledger.events()] == ["batch-a"]


def test_v2_allows_the_same_paper_in_distinct_batches() -> None:
    payload = {
        "schema_version": 2,
        "feedback": [
            {
                "arxiv_id": "2401.00001",
                "batch_id": batch_id,
                "actions": [{"action": "read", "updated_at": updated_at}],
            }
            for batch_id, updated_at in (
                ("batch-a", "2026-08-01T01:00:00Z"),
                ("batch-b", "2026-08-02T01:00:00Z"),
            )
        ],
    }

    assert [action.batch_id for action in parse_feedback(json.dumps(payload))] == [
        "batch-a",
        "batch-b",
    ]


def test_v2_empty_feedback_is_valid_and_records_no_outcome(tmp_path: Path) -> None:
    store = FeedbackStateStore(tmp_path / "feedback.json")
    body = json.dumps({"schema_version": 2, "feedback": []})

    result = store.ingest(((25, body),))

    assert result.issue_count == 1
    assert result.action_count == 0
    assert FeedbackLedgerStore(store.path).events() == ()


@pytest.mark.parametrize(
    ("paper_id", "batch_id"),
    ((" paper ", "batch"), ("paper", " "), ("p" * 81, "batch"), ("paper", "b" * 201)),
)
def test_v2_rejects_unbounded_or_ambiguous_identifiers(paper_id: str, batch_id: str) -> None:
    body = json.dumps(
        {
            "schema_version": 2,
            "feedback": [
                {
                    "arxiv_id": paper_id,
                    "batch_id": batch_id,
                    "actions": [{"action": "read", "updated_at": "2026-08-01T01:00:00Z"}],
                }
            ],
        }
    )

    with pytest.raises(ExternalServiceError):
        parse_feedback(body)
