"""Parse browser feedback issues without granting issue text any execution authority."""

from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

from zotero_arxiv_daily.core.errors import ExternalServiceError
from zotero_arxiv_daily.feedback.ledger import (
    FeedbackEvent,
    FeedbackEventType,
    FeedbackLedgerStore,
    FeedbackOutcome,
)

_ACTIONS = frozenset({"interested", "not_interested", "save_for_later", "read"})


@dataclass(frozen=True, slots=True)
class FeedbackAction:
    arxiv_id: str
    action: str
    updated_at: str


@dataclass(frozen=True, slots=True)
class FeedbackIngestionResult:
    issue_count: int
    action_count: int
    duplicate_issues: int


class FeedbackStateStore:
    """Atomically retain validated per-paper feedback events and processed Issue IDs."""

    def __init__(self, path: Path) -> None:
        self.path = path

    def ingest(self, issues: tuple[tuple[int, str], ...]) -> FeedbackIngestionResult:
        """Validate all new issues before atomically marking any of them processed."""

        parsed = tuple((number, parse_feedback(body)) for number, body in issues)
        ledger_issues = tuple(
            (
                number,
                tuple(
                    _event_from_action(number, index, action)
                    for index, action in enumerate(actions)
                ),
            )
            for number, actions in parsed
        )
        issue_count, action_count, duplicate_issues = FeedbackLedgerStore(self.path).ingest_issues(
            ledger_issues
        )
        return FeedbackIngestionResult(issue_count, action_count, duplicate_issues)


def parse_feedback(body: str) -> tuple[FeedbackAction, ...]:
    """Accept only the versioned JSON format emitted by the browser feedback UI."""

    try:
        value = json.loads(body)
        if not isinstance(value, dict) or set(value) != {"schema_version", "feedback"}:
            raise ValueError
        records = value["feedback"]
        if value["schema_version"] != 1 or not isinstance(records, list) or len(records) > 100:
            raise ValueError
        actions = tuple(
            FeedbackAction(record["arxiv_id"], record["action"], record["updated_at"])
            for record in records
            if isinstance(record, dict) and set(record) == {"arxiv_id", "action", "updated_at"}
        )
        if len(actions) != len(records) or len({item.arxiv_id for item in actions}) != len(actions):
            raise ValueError
        if any(
            not action.arxiv_id.strip()
            or action.action not in _ACTIONS
            or not action.updated_at.strip()
            for action in actions
        ):
            raise ValueError
        return actions
    except (KeyError, TypeError, ValueError, json.JSONDecodeError) as error:
        raise ExternalServiceError("feedback Issue has an invalid payload") from error


def read_github_issues(path: Path) -> tuple[tuple[int, str], ...]:
    """Read the minimal `gh issue list` JSON projection without shell interpolation."""

    try:
        value = json.loads(path.read_text(encoding="utf-8"))
        if not isinstance(value, list):
            raise ValueError
        issues = tuple((item["number"], item["body"]) for item in value)
        if any(
            not isinstance(number, int) or number < 1 or not isinstance(body, str)
            for number, body in issues
        ):
            raise ValueError
        return issues
    except (OSError, KeyError, TypeError, ValueError, json.JSONDecodeError) as error:
        raise ExternalServiceError("GitHub Issue input is invalid") from error


def _event_from_action(issue_number: int, index: int, action: FeedbackAction) -> FeedbackEvent:
    try:
        occurred_at = datetime.fromisoformat(action.updated_at.replace("Z", "+00:00"))
        outcome = FeedbackOutcome(action.action)
    except ValueError as error:
        raise ExternalServiceError("feedback Issue has an invalid timestamp or action") from error
    return FeedbackEvent(
        f"issue-{issue_number}-{index}",
        FeedbackEventType.OUTCOME,
        action.arxiv_id,
        occurred_at,
        outcome,
    )
