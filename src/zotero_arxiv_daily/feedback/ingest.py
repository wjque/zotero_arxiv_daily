"""Parse browser feedback issues without granting issue text any execution authority."""

from __future__ import annotations

import json
import os
import tempfile
from dataclasses import dataclass
from pathlib import Path

from zotero_arxiv_daily.core.errors import ExternalServiceError

_ACTIONS = {"interested": 0.25, "not_interested": -0.5, "save_for_later": 0.1, "read": 0.05}


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
    """Atomically retain processed Issue IDs and bounded per-paper rank adjustments."""

    def __init__(self, path: Path) -> None:
        self.path = path

    def adjustments(self) -> dict[str, float]:
        values = self._read().get("adjustments", {})
        return (
            {key: float(value) for key, value in values.items()}
            if isinstance(values, dict) and all(isinstance(key, str) for key in values)
            else {}
        )

    def ingest(self, issues: tuple[tuple[int, str], ...]) -> FeedbackIngestionResult:
        """Validate all new issues before atomically marking any of them processed."""

        state = self._read()
        raw_processed = state.get("processed_issue_numbers", [])
        if not isinstance(raw_processed, list) or not all(
            isinstance(value, int) for value in raw_processed
        ):
            raise ExternalServiceError("feedback state has invalid processed Issue IDs")
        processed = set(raw_processed)
        new_issues = tuple(issue for issue in issues if issue[0] not in processed)
        actions = tuple(action for _, body in new_issues for action in parse_feedback(body))
        adjustments = self.adjustments()
        for action in actions:
            adjustments[action.arxiv_id] = (
                adjustments.get(action.arxiv_id, 0.0) + _ACTIONS[action.action]
            )
        payload = {
            "schema_version": 1,
            "processed_issue_numbers": sorted(processed | {number for number, _ in new_issues}),
            "adjustments": dict(sorted(adjustments.items())),
        }
        self._write(payload)
        return FeedbackIngestionResult(len(new_issues), len(actions), len(issues) - len(new_issues))

    def _read(self) -> dict[str, object]:
        if not self.path.is_file():
            return {}
        try:
            value = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as error:
            raise ExternalServiceError("feedback state is unreadable") from error
        return value if isinstance(value, dict) else {}

    def _write(self, payload: dict[str, object]) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        descriptor, temporary_name = tempfile.mkstemp(
            prefix=f".{self.path.name}.", dir=self.path.parent
        )
        temporary = Path(temporary_name)
        try:
            with os.fdopen(descriptor, "w", encoding="utf-8") as output:
                json.dump(payload, output, separators=(",", ":"))
                output.flush()
                os.fsync(output.fileno())
            os.replace(temporary, self.path)
        finally:
            temporary.unlink(missing_ok=True)


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
