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

_V1_ACTIONS = frozenset({"interested", "not_interested", "save_for_later", "read"})
_V2_ACTIONS = _V1_ACTIONS | {"worthwhile", "not_worthwhile"}
_PRE_READING = frozenset(
    {
        FeedbackOutcome.INTERESTED,
        FeedbackOutcome.NOT_INTERESTED,
        FeedbackOutcome.SAVE_FOR_LATER,
    }
)
_POST_READING = frozenset({FeedbackOutcome.WORTHWHILE, FeedbackOutcome.NOT_WORTHWHILE})


@dataclass(frozen=True, slots=True)
class FeedbackAction:
    arxiv_id: str
    action: str
    updated_at: str
    batch_id: str | None = None


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

        if len({number for number, _ in issues}) != len(issues):
            raise ExternalServiceError("feedback Issue input contains duplicate identities")
        parsed = tuple(sorted((number, parse_feedback(body)) for number, body in issues))
        ledger = FeedbackLedgerStore(self.path)
        try:
            processed = ledger.processed_issue_numbers()
            new_issues = tuple(issue for issue in parsed if issue[0] not in processed)
            derived = dict(_ledger_issues(new_issues, ledger.events()))
            ledger_issues = tuple(
                (number, () if number in processed else derived[number]) for number, _ in parsed
            )
        except ValueError as error:
            raise ExternalServiceError(
                "feedback Issue contains an invalid action sequence"
            ) from error
        issue_count, action_count, duplicate_issues = ledger.ingest_issues(ledger_issues)
        return FeedbackIngestionResult(issue_count, action_count, duplicate_issues)


def parse_feedback(body: str) -> tuple[FeedbackAction, ...]:
    """Accept only a supported versioned format emitted by the browser feedback UI."""

    try:
        value = json.loads(body)
        if not isinstance(value, dict) or set(value) != {"schema_version", "feedback"}:
            raise ValueError
        records = value["feedback"]
        if not isinstance(records, list) or len(records) > 100:
            raise ValueError
        if value["schema_version"] == 1:
            return _parse_v1(records)
        if value["schema_version"] == 2:
            return _parse_v2(records)
        raise ValueError
    except (KeyError, TypeError, ValueError, json.JSONDecodeError) as error:
        raise ExternalServiceError("feedback Issue has an invalid payload") from error


def _parse_v1(records: list[object]) -> tuple[FeedbackAction, ...]:
    actions = tuple(
        FeedbackAction(record["arxiv_id"], record["action"], record["updated_at"])
        for record in records
        if isinstance(record, dict) and set(record) == {"arxiv_id", "action", "updated_at"}
    )
    if len(actions) != len(records) or len({item.arxiv_id for item in actions}) != len(actions):
        raise ValueError
    if any(not _valid_action(action, _V1_ACTIONS) for action in actions):
        raise ValueError
    return actions


def _parse_v2(records: list[object]) -> tuple[FeedbackAction, ...]:
    actions: list[FeedbackAction] = []
    record_keys: set[tuple[str, str]] = set()
    for record in records:
        if not isinstance(record, dict) or set(record) != {"arxiv_id", "batch_id", "actions"}:
            raise ValueError
        paper_id = record["arxiv_id"]
        batch_id = record["batch_id"]
        raw_actions = record["actions"]
        if (
            not isinstance(paper_id, str)
            or not paper_id.strip()
            or paper_id != paper_id.strip()
            or len(paper_id) > 80
            or not isinstance(batch_id, str)
            or not batch_id.strip()
            or batch_id != batch_id.strip()
            or len(batch_id) > 200
            or (paper_id, batch_id) in record_keys
            or not isinstance(raw_actions, list)
            or not 1 <= len(raw_actions) <= 3
        ):
            raise ValueError
        record_keys.add((paper_id, batch_id))
        paper_actions = tuple(
            FeedbackAction(paper_id, action["action"], action["updated_at"], batch_id)
            for action in raw_actions
            if isinstance(action, dict) and set(action) == {"action", "updated_at"}
        )
        if len(paper_actions) != len(raw_actions) or any(
            not _valid_action(action, _V2_ACTIONS) or len(action.updated_at) > 64
            for action in paper_actions
        ):
            raise ValueError
        stages = tuple(_outcome_stage(FeedbackOutcome(action.action)) for action in paper_actions)
        if len(set(stages)) != len(stages):
            raise ValueError
        actions.extend(sorted(paper_actions, key=_action_instant))
    return tuple(actions)


def _valid_action(action: FeedbackAction, allowed: frozenset[str]) -> bool:
    return bool(
        isinstance(action.arxiv_id, str)
        and action.arxiv_id.strip()
        and isinstance(action.action, str)
        and action.action in allowed
        and isinstance(action.updated_at, str)
        and action.updated_at.strip()
    )


def _action_instant(action: FeedbackAction) -> datetime:
    value = datetime.fromisoformat(action.updated_at.replace("Z", "+00:00"))
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError("feedback action timestamp must be timezone-aware")
    return value


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


def _ledger_issues(
    issues: tuple[tuple[int, tuple[FeedbackAction, ...]], ...],
    existing: tuple[FeedbackEvent, ...],
) -> tuple[tuple[int, tuple[FeedbackEvent, ...]], ...]:
    active = _active_stage_events(existing)
    values: list[tuple[int, tuple[FeedbackEvent, ...]]] = []
    for issue_number, actions in issues:
        events: list[FeedbackEvent] = []
        for index, action in enumerate(actions):
            event = _event_from_action(issue_number, index, action, active)
            if event is not None:
                events.append(event)
                if event.outcome is not None:
                    active[(event.paper_id, event.batch_id, _outcome_stage(event.outcome))] = event
        values.append((issue_number, tuple(events)))
    return tuple(values)


def _active_stage_events(
    events: tuple[FeedbackEvent, ...],
) -> dict[tuple[str, str | None, str], FeedbackEvent]:
    superseded = {
        event.supersedes_event_id for event in events if event.supersedes_event_id is not None
    }
    return {
        (event.paper_id, event.batch_id, _outcome_stage(event.outcome)): event
        for event in events
        if event.outcome is not None and event.event_id not in superseded
    }


def _outcome_stage(outcome: FeedbackOutcome) -> str:
    if outcome in _PRE_READING:
        return "pre_reading"
    if outcome is FeedbackOutcome.READ:
        return "reading"
    if outcome in _POST_READING:
        return "post_reading"
    raise ValueError("unsupported feedback outcome stage")


def _event_from_action(
    issue_number: int,
    index: int,
    action: FeedbackAction,
    active: dict[tuple[str, str | None, str], FeedbackEvent],
) -> FeedbackEvent | None:
    try:
        occurred_at = datetime.fromisoformat(action.updated_at.replace("Z", "+00:00"))
        outcome = FeedbackOutcome(action.action)
    except ValueError as error:
        raise ExternalServiceError("feedback Issue has an invalid timestamp or action") from error
    stage = _outcome_stage(outcome)
    current = active.get((action.arxiv_id, action.batch_id, stage))
    if current is not None and current.outcome is outcome:
        return None
    if current is not None and occurred_at < current.occurred_at:
        raise ValueError("feedback correction predates the active outcome")
    if stage == "post_reading":
        reading = active.get((action.arxiv_id, action.batch_id, "reading"))
        if reading is None and current is None:
            raise ValueError("post-reading feedback requires an explicit read action")
        if reading is not None and occurred_at < reading.occurred_at:
            raise ValueError("post-reading feedback predates the read action")
    return FeedbackEvent(
        f"issue-{issue_number}-{index}",
        FeedbackEventType.CORRECTION if current is not None else FeedbackEventType.OUTCOME,
        action.arxiv_id,
        occurred_at,
        outcome,
        batch_id=action.batch_id,
        supersedes_event_id=current.event_id if current is not None else None,
    )
