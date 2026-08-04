"""Append-only local feedback events and guarded weekly aggregate activation."""

from __future__ import annotations

import hashlib
import json
import math
import os
import tempfile
from dataclasses import asdict, dataclass
from datetime import datetime, timedelta
from enum import StrEnum
from pathlib import Path
from typing import Any

from zotero_arxiv_daily.core.errors import ExternalServiceError
from zotero_arxiv_daily.core.time import require_aware_utc

FEEDBACK_LEDGER_SCHEMA_VERSION = 2


class FeedbackEventType(StrEnum):
    IMPRESSION = "impression"
    OUTCOME = "outcome"
    CORRECTION = "correction"


class FeedbackOutcome(StrEnum):
    INTERESTED = "interested"
    SAVE_FOR_LATER = "save_for_later"
    READ = "read"
    WORTHWHILE = "worthwhile"
    NOT_WORTHWHILE = "not_worthwhile"
    NOT_INTERESTED = "not_interested"


_OUTCOME_WEIGHTS = {
    FeedbackOutcome.INTERESTED: 0.25,
    FeedbackOutcome.SAVE_FOR_LATER: 0.1,
    FeedbackOutcome.READ: 0.05,
    FeedbackOutcome.WORTHWHILE: 0.6,
    FeedbackOutcome.NOT_WORTHWHILE: -0.75,
    FeedbackOutcome.NOT_INTERESTED: -0.5,
}
_REASON_CODES = frozenset(
    {"topic-mismatch", "incremental", "weak-evidence", "poor-clarity", "no-practical-relevance"}
)


@dataclass(frozen=True, slots=True)
class FeedbackEvent:
    event_id: str
    event_type: FeedbackEventType
    paper_id: str
    occurred_at: datetime
    outcome: FeedbackOutcome | None = None
    batch_id: str | None = None
    displayed_rank: int | None = None
    reason_codes: tuple[str, ...] = ()
    supersedes_event_id: str | None = None

    def __post_init__(self) -> None:
        require_aware_utc(self.occurred_at, "occurred_at")
        if not self.event_id.strip() or not self.paper_id.strip():
            raise ValueError("feedback event identifiers must not be empty")
        if self.event_type is FeedbackEventType.IMPRESSION:
            if not self.batch_id or self.displayed_rank is None or self.outcome is not None:
                raise ValueError("impressions require only a batch ID and displayed rank")
        elif self.event_type is FeedbackEventType.OUTCOME:
            if self.outcome is None or self.displayed_rank is not None:
                raise ValueError("outcomes require an explicit outcome only")
        elif self.event_type is FeedbackEventType.CORRECTION and (
            self.outcome is None or not self.supersedes_event_id
        ):
            raise ValueError("corrections require an outcome and prior event")
        if len(self.reason_codes) > 3 or any(
            code not in _REASON_CODES for code in self.reason_codes
        ):
            raise ValueError("feedback reason codes are invalid")


@dataclass(frozen=True, slots=True)
class FeedbackAggregate:
    version: str
    cutoff_at: datetime
    event_count: int
    independent_papers: int
    adjustments: tuple[tuple[str, float], ...]


@dataclass(frozen=True, slots=True)
class ActivationResult:
    decision: str
    active_version: str | None
    cutoff_at: datetime | None


@dataclass(frozen=True, slots=True)
class PositionOutcomeRate:
    displayed_rank: int
    impression_count: int
    explicit_outcome_count: int
    positive_outcome_count: int


class FeedbackLedgerStore:
    """Own versioned local feedback state without inferring outcomes from silence."""

    def __init__(self, path: Path) -> None:
        self.path = path

    def events(self) -> tuple[FeedbackEvent, ...]:
        return _state_events(self._read())

    def ingest(self, events: tuple[FeedbackEvent, ...]) -> tuple[int, int]:
        """Append validated events atomically; same event ID is idempotent only when identical."""

        state = self._state()
        known = {event.event_id: event for event in state["events"]}
        added = duplicates = 0
        for event in events:
            existing = known.get(event.event_id)
            if existing is not None:
                if existing != event:
                    raise ExternalServiceError("feedback event ID conflicts with stored event")
                duplicates += 1
                continue
            if event.supersedes_event_id and event.supersedes_event_id not in known:
                raise ExternalServiceError("feedback correction references an unknown event")
            known[event.event_id] = event
            added += 1
        state["events"] = tuple(
            sorted(known.values(), key=lambda item: (item.occurred_at, item.event_id))
        )
        self._write(state)
        return added, duplicates

    def ingest_issues(
        self, issues: tuple[tuple[int, tuple[FeedbackEvent, ...]], ...]
    ) -> tuple[int, int, int]:
        """Atomically append parsed Issue events and mark their Issue IDs consumed."""

        state = self._state()
        processed = state["processed"]
        if not isinstance(processed, list) or not all(isinstance(item, int) for item in processed):
            raise ExternalServiceError("feedback processed Issue IDs are invalid")
        known_processed = set(processed)
        new = tuple(issue for issue in issues if issue[0] not in known_processed)
        known = {event.event_id: event for event in state["events"]}
        added = 0
        for _, events in new:
            for event in events:
                if event.event_id in known:
                    raise ExternalServiceError("new feedback Issue contains an existing event ID")
                if event.supersedes_event_id and event.supersedes_event_id not in known:
                    raise ExternalServiceError("feedback correction references an unknown event")
                known[event.event_id] = event
                added += 1
        state["events"] = tuple(
            sorted(known.values(), key=lambda item: (item.occurred_at, item.event_id))
        )
        state["processed"] = sorted(known_processed | {number for number, _ in new})
        self._write(state)
        return len(new), added, len(issues) - len(new)

    def activate_weekly(
        self, now: datetime, *, interval_days: int = 7, minimum_independent_papers: int = 3
    ) -> ActivationResult:
        """Atomically promote one decayed aggregate, or retain the last active version safely."""

        instant = require_aware_utc(now, "now")
        if not 1 <= interval_days <= 28 or minimum_independent_papers < 1:
            raise ValueError("weekly activation bounds are invalid")
        state = self._state()
        activation = state["activation"]
        previous = activation.get("activated_at")
        if previous is not None:
            previous_at = datetime.fromisoformat(previous)
            if instant < previous_at + timedelta(days=interval_days):
                return ActivationResult("not-eligible", activation.get("active_version"), None)
        aggregate = _aggregate(state["events"], instant)
        if aggregate.independent_papers < minimum_independent_papers:
            return ActivationResult("insufficient-evidence", activation.get("active_version"), None)
        state["activation"] = {
            "activated_at": instant.isoformat(),
            "active_version": aggregate.version,
            "cutoff_at": aggregate.cutoff_at.isoformat(),
            "adjustments": dict(aggregate.adjustments),
        }
        self._write(state)
        return ActivationResult("activated", aggregate.version, aggregate.cutoff_at)

    def active_adjustments(self) -> dict[str, float]:
        state = self._state()
        raw = state["activation"].get("adjustments", {})
        if isinstance(raw, dict) and raw:
            return {str(key): float(value) for key, value in raw.items()}
        legacy = state["legacy_adjustments"]
        return (
            {str(key): float(value) for key, value in legacy.items()}
            if isinstance(legacy, dict)
            else {}
        )

    def position_outcomes(self) -> tuple[PositionOutcomeRate, ...]:
        """Report explicit outcomes conditional on recorded impression position only."""

        impressions: dict[str, list[FeedbackEvent]] = {}
        outcomes: dict[int, list[FeedbackEvent]] = {}
        for event in self.events():
            if event.event_type is FeedbackEventType.IMPRESSION:
                impressions.setdefault(event.paper_id, []).append(event)
        superseded = {
            event.supersedes_event_id
            for event in self.events()
            if event.supersedes_event_id is not None
        }
        for event in self.events():
            if event.outcome is None or event.event_id in superseded:
                continue
            candidates = [
                impression
                for impression in impressions.get(event.paper_id, [])
                if impression.occurred_at <= event.occurred_at
                and (event.batch_id is None or impression.batch_id == event.batch_id)
            ]
            if not candidates and event.batch_id is not None:
                candidates = [
                    impression
                    for impression in impressions.get(event.paper_id, [])
                    if impression.occurred_at <= event.occurred_at
                ]
            if candidates:
                impression = max(candidates, key=lambda item: (item.occurred_at, item.event_id))
                if impression.displayed_rank is not None:
                    outcomes.setdefault(impression.displayed_rank, []).append(event)
        counts: dict[int, int] = {}
        for values in impressions.values():
            for impression in values:
                if impression.displayed_rank is not None:
                    counts[impression.displayed_rank] = counts.get(impression.displayed_rank, 0) + 1
        return tuple(
            PositionOutcomeRate(
                rank,
                count,
                len(outcomes.get(rank, [])),
                sum(
                    event.outcome in {FeedbackOutcome.INTERESTED, FeedbackOutcome.WORTHWHILE}
                    for event in outcomes.get(rank, [])
                ),
            )
            for rank, count in sorted(counts.items())
        )

    def record_impressions(
        self, batch_id: str, paper_ids: tuple[str, ...], occurred_at: datetime
    ) -> tuple[int, int]:
        """Append idempotent displayed-rank events after a successful publication only."""

        instant = require_aware_utc(occurred_at, "occurred_at")
        if not batch_id.strip() or len(set(paper_ids)) != len(paper_ids):
            raise ValueError("impression batch identity is invalid")
        return self.ingest(
            tuple(
                FeedbackEvent(
                    "impression-"
                    + hashlib.sha256(f"{batch_id}|{rank}|{paper_id}".encode()).hexdigest()[:24],
                    FeedbackEventType.IMPRESSION,
                    paper_id,
                    instant,
                    batch_id=batch_id,
                    displayed_rank=rank,
                )
                for rank, paper_id in enumerate(paper_ids, start=1)
            )
        )

    def _read(self) -> dict[str, Any]:
        if not self.path.exists():
            return {}
        try:
            value = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as error:
            raise ExternalServiceError("feedback ledger is unreadable") from error
        if not isinstance(value, dict):
            raise ExternalServiceError("feedback ledger root is invalid")
        return value

    def _state(self) -> dict[str, Any]:
        value = self._read()
        if not value:
            return {"events": (), "legacy_adjustments": {}, "activation": {}, "processed": []}
        if value.get("schema_version") == 1:
            adjustments = value.get("adjustments", {})
            if not isinstance(adjustments, dict):
                raise ExternalServiceError("legacy feedback adjustments are invalid")
            processed = value.get("processed_issue_numbers", [])
            if not isinstance(processed, list) or not all(
                isinstance(item, int) for item in processed
            ):
                raise ExternalServiceError("legacy feedback Issue IDs are invalid")
            return {
                "events": (),
                "legacy_adjustments": adjustments,
                "activation": {},
                "processed": processed,
            }
        if value.get("schema_version") != FEEDBACK_LEDGER_SCHEMA_VERSION:
            raise ExternalServiceError("unsupported feedback ledger schema")
        return {
            "events": _state_events(value),
            "legacy_adjustments": value.get("legacy_adjustments", {}),
            "activation": value.get("activation", {}),
            "processed": value.get("processed_issue_numbers", []),
        }

    def _write(self, state: dict[str, Any]) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        descriptor, temporary_name = tempfile.mkstemp(
            prefix=f".{self.path.name}.", dir=self.path.parent
        )
        temporary = Path(temporary_name)
        try:
            with os.fdopen(descriptor, "w", encoding="utf-8") as output:
                json.dump(_encode_state(state), output, sort_keys=True, separators=(",", ":"))
                output.flush()
                os.fsync(output.fileno())
            os.replace(temporary, self.path)
            os.chmod(self.path, 0o600)
        finally:
            temporary.unlink(missing_ok=True)


def _aggregate(events: tuple[FeedbackEvent, ...], cutoff: datetime) -> FeedbackAggregate:
    latest: dict[str, FeedbackEvent] = {}
    superseded = {
        event.supersedes_event_id
        for event in events
        if event.supersedes_event_id is not None and event.occurred_at <= cutoff
    }
    for event in events:
        if (
            event.event_type is FeedbackEventType.IMPRESSION
            or event.occurred_at > cutoff
            or event.event_id in superseded
        ):
            continue
        previous = latest.get(event.paper_id)
        if previous is None or (event.occurred_at, event.event_id) > (
            previous.occurred_at,
            previous.event_id,
        ):
            latest[event.paper_id] = event
    adjustments = tuple(
        sorted(
            (
                paper,
                _OUTCOME_WEIGHTS[event.outcome]
                * math.exp(-max((cutoff - event.occurred_at).total_seconds(), 0.0) / 7_776_000),
            )
            for paper, event in latest.items()
            if event.outcome is not None
        )
    )
    digest = hashlib.sha256(
        json.dumps(
            [(paper, event.event_id) for paper, event in sorted(latest.items())],
            separators=(",", ":"),
        ).encode()
    ).hexdigest()[:16]
    return FeedbackAggregate(f"feedback-{digest}", cutoff, len(latest), len(latest), adjustments)


def _state_events(value: dict[str, Any]) -> tuple[FeedbackEvent, ...]:
    raw = value.get("events", [])
    if not isinstance(raw, list):
        raise ExternalServiceError("feedback ledger events are invalid")
    try:
        return tuple(_event_from_value(item) for item in raw)
    except (KeyError, TypeError, ValueError) as error:
        raise ExternalServiceError("feedback ledger event is invalid") from error


def _event_from_value(value: object) -> FeedbackEvent:
    if not isinstance(value, dict):
        raise ValueError
    return FeedbackEvent(
        str(value["event_id"]),
        FeedbackEventType(str(value["event_type"])),
        str(value["paper_id"]),
        datetime.fromisoformat(str(value["occurred_at"])),
        FeedbackOutcome(str(value["outcome"])) if value.get("outcome") else None,
        str(value["batch_id"]) if value.get("batch_id") else None,
        int(value["displayed_rank"]) if value.get("displayed_rank") is not None else None,
        tuple(str(code) for code in value.get("reason_codes", [])),
        str(value["supersedes_event_id"]) if value.get("supersedes_event_id") else None,
    )


def _encode_state(state: dict[str, Any]) -> dict[str, object]:
    events = state["events"]
    return {
        "schema_version": FEEDBACK_LEDGER_SCHEMA_VERSION,
        "legacy_adjustments": state["legacy_adjustments"],
        "processed_issue_numbers": state["processed"],
        "events": [
            {
                **asdict(event),
                "event_type": event.event_type.value,
                "outcome": event.outcome.value if event.outcome else None,
                "occurred_at": event.occurred_at.isoformat(),
            }
            for event in events
            if isinstance(event, FeedbackEvent)
        ],
        "activation": state["activation"],
    }
