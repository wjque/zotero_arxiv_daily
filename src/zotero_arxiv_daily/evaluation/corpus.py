"""Append-only local curated-corpus storage and Zotero collection import."""

from __future__ import annotations

import hashlib
import json
import os
import re
import tempfile
from dataclasses import asdict, dataclass
from datetime import datetime
from pathlib import Path

from zotero_arxiv_daily.core.errors import ApplicationError
from zotero_arxiv_daily.core.time import require_aware_utc
from zotero_arxiv_daily.evaluation.models import (
    CURATED_CORPUS_SCHEMA_VERSION,
    CorpusEvent,
    CorpusLabel,
    CorpusSnapshot,
    JudgmentKind,
    ResolvedJudgment,
)

_REASON_TAG_PREFIX = "ranking-reason:"
_PAPER_ID_TAG_PREFIX = "ranking-paper-id:"
_DOI = re.compile(r"^10\.\d{4,9}/[-._;()/:a-z0-9]+$", re.IGNORECASE)


@dataclass(frozen=True, slots=True)
class CuratedCorpusMapping:
    """Explicit local Zotero collection keys used as corpus labels."""

    positive_collection_keys: tuple[str, ...]
    negative_collection_keys: tuple[str, ...]
    reason_tag_prefix: str = _REASON_TAG_PREFIX

    def __post_init__(self) -> None:
        positive = frozenset(self.positive_collection_keys)
        negative = frozenset(self.negative_collection_keys)
        if not positive or not negative or positive & negative:
            raise ValueError(
                "positive and negative collection mappings must be non-empty and disjoint"
            )
        if not self.reason_tag_prefix.strip():
            raise ValueError("reason_tag_prefix must not be empty")


@dataclass(frozen=True, slots=True)
class ZoteroCorpusItem:
    """Minimal local Zotero projection used by the corpus importer."""

    item_key: str
    identifiers: tuple[str, ...]
    collections: tuple[str, ...]
    tags: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class CorpusImportResult:
    """Counts from one idempotent local corpus import."""

    added_events: int
    duplicate_events: int
    unlabeled_events: int
    skipped_items: int
    revision: int


class CorpusStore:
    """Own an append-only ignored local corpus ledger with atomic replacement writes."""

    def __init__(self, path: Path) -> None:
        self.path = path

    def events(self) -> tuple[CorpusEvent, ...]:
        return _decode_state(self._read()).events

    def append(self, events: tuple[CorpusEvent, ...]) -> tuple[int, int]:
        """Append validated events once; duplicate IDs must have identical content."""

        state = _decode_state(self._read())
        known = {event.event_id: event for event in state.events}
        added = duplicate = 0
        for event in events:
            existing = known.get(event.event_id)
            if existing is not None:
                if existing != event:
                    raise ApplicationError("corpus event ID conflicts with existing local event")
                duplicate += 1
                continue
            _validate_lineage(event, known)
            known[event.event_id] = event
            added += 1
        updated = tuple(sorted(known.values(), key=_event_key))
        self._write(_encode_state(updated))
        return added, duplicate

    def snapshot(self, cutoff_at: datetime) -> CorpusSnapshot:
        """Resolve labels at a UTC cutoff without mutating the append-only ledger."""

        cutoff = require_aware_utc(cutoff_at, "cutoff_at")
        state = _decode_state(self._read())
        applicable = tuple(event for event in state.events if event.occurred_at <= cutoff)
        return _resolve_snapshot(applicable, cutoff)

    def import_zotero(
        self,
        items: tuple[ZoteroCorpusItem, ...],
        mapping: CuratedCorpusMapping,
        observed_at: datetime,
    ) -> CorpusImportResult:
        """Import collection membership as explicit local label or unlabel correction events."""

        observed = require_aware_utc(observed_at, "observed_at")
        state = _decode_state(self._read())
        latest_by_item = _latest_source_events(state.events)
        proposed: list[CorpusEvent] = []
        active_keys: set[str] = set()
        skipped = 0
        for item in items:
            label = _collection_label(item.collections, mapping)
            if label is None:
                continue
            paper_id = _canonical_paper_id(item.identifiers, item.tags)
            if paper_id is None:
                skipped += 1
                continue
            active_keys.add(item.item_key)
            current = latest_by_item.get(item.item_key)
            candidate = _source_event(item, paper_id, label, mapping, observed, current)
            if current is not None and _same_imported_judgment(current, candidate):
                continue
            proposed.append(candidate)
        for item_key, current in latest_by_item.items():
            if item_key not in active_keys and current.kind is not JudgmentKind.UNLABEL:
                proposed.append(_unlabel_event(current, observed, source_item_key=item_key))
        added, duplicate = self.append(tuple(proposed))
        return CorpusImportResult(
            added,
            duplicate,
            sum(event.kind is JudgmentKind.UNLABEL for event in proposed),
            skipped,
            len(self.events()),
        )

    def _read(self) -> dict[str, object]:
        if not self.path.is_file():
            return {}
        try:
            value = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as error:
            raise ApplicationError("local curated corpus is unreadable") from error
        if not isinstance(value, dict):
            raise ApplicationError("local curated corpus root is invalid")
        return value

    def _write(self, payload: dict[str, object]) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        descriptor, temporary_name = tempfile.mkstemp(
            prefix=f".{self.path.name}.", dir=self.path.parent
        )
        temporary = Path(temporary_name)
        try:
            with os.fdopen(descriptor, "w", encoding="utf-8") as output:
                json.dump(
                    payload, output, ensure_ascii=False, sort_keys=True, separators=(",", ":")
                )
                output.flush()
                os.fsync(output.fileno())
            os.replace(temporary, self.path)
            os.chmod(self.path, 0o600)
        finally:
            temporary.unlink(missing_ok=True)


@dataclass(frozen=True, slots=True)
class _CorpusState:
    events: tuple[CorpusEvent, ...]


def _decode_state(value: dict[str, object]) -> _CorpusState:
    if not value:
        return _CorpusState(())
    if value.get("schema_version") != CURATED_CORPUS_SCHEMA_VERSION:
        raise ApplicationError("unsupported local curated corpus schema")
    raw_events = value.get("events")
    if not isinstance(raw_events, list):
        raise ApplicationError("local curated corpus events are invalid")
    try:
        events = tuple(_event_from_dict(event) for event in raw_events)
    except (KeyError, TypeError, ValueError) as error:
        raise ApplicationError("local curated corpus event is invalid") from error
    if len({event.event_id for event in events}) != len(events):
        raise ApplicationError("local curated corpus has duplicate event IDs")
    return _CorpusState(tuple(sorted(events, key=_event_key)))


def _event_from_dict(value: object) -> CorpusEvent:
    if not isinstance(value, dict):
        raise ValueError
    required = {
        "event_id",
        "kind",
        "paper_id",
        "occurred_at",
        "source",
        "label",
        "compared_paper_id",
        "reason_codes",
        "applicable_dimensions",
        "private_rationale",
        "supersedes_event_id",
        "source_item_key",
    }
    if set(value) != required:
        raise ValueError
    occurred = datetime.fromisoformat(str(value["occurred_at"]))
    return CorpusEvent(
        str(value["event_id"]),
        JudgmentKind(str(value["kind"])),
        str(value["paper_id"]),
        occurred,
        str(value["source"]),
        CorpusLabel(str(value["label"])) if value["label"] is not None else None,
        str(value["compared_paper_id"]) if value["compared_paper_id"] is not None else None,
        tuple(str(item) for item in value["reason_codes"]),
        tuple(str(item) for item in value["applicable_dimensions"]),
        str(value["private_rationale"]) if value["private_rationale"] is not None else None,
        str(value["supersedes_event_id"]) if value["supersedes_event_id"] is not None else None,
        str(value["source_item_key"]) if value["source_item_key"] is not None else None,
    )


def _encode_state(events: tuple[CorpusEvent, ...]) -> dict[str, object]:
    return {
        "schema_version": CURATED_CORPUS_SCHEMA_VERSION,
        "events": [
            {
                **asdict(event),
                "kind": event.kind.value,
                "label": event.label.value if event.label else None,
                "occurred_at": require_aware_utc(event.occurred_at).isoformat(),
            }
            for event in events
        ],
    }


def _validate_lineage(event: CorpusEvent, known: dict[str, CorpusEvent]) -> None:
    if event.supersedes_event_id is None:
        return
    previous = known.get(event.supersedes_event_id)
    if previous is None or previous.paper_id != event.paper_id:
        raise ApplicationError(
            "corpus correction must supersede an existing judgment for the same paper"
        )
    if event.occurred_at < previous.occurred_at:
        raise ApplicationError("corpus correction cannot precede the judgment it supersedes")


def _event_key(event: CorpusEvent) -> tuple[datetime, str]:
    return (require_aware_utc(event.occurred_at), event.event_id)


def _resolve_snapshot(events: tuple[CorpusEvent, ...], cutoff: datetime) -> CorpusSnapshot:
    by_paper: dict[str, list[CorpusEvent]] = {}
    for event in events:
        if event.kind is not JudgmentKind.PAIRWISE:
            by_paper.setdefault(event.paper_id, []).append(event)
    labels: list[ResolvedJudgment] = []
    conflicts = 0
    for paper_id, paper_events in by_paper.items():
        latest = max(paper_events, key=_event_key)
        labels_at_latest = {
            event.label
            for event in paper_events
            if event.kind is JudgmentKind.LABEL and event.occurred_at == latest.occurred_at
        }
        if len(labels_at_latest) > 1:
            conflicts += 1
        if latest.kind is JudgmentKind.LABEL:
            labels.append(
                ResolvedJudgment(
                    paper_id,
                    latest.label,
                    latest.occurred_at,
                    latest.event_id,
                    latest.source,
                    latest.reason_codes,
                )
            )
    pairwise = tuple(
        ResolvedJudgment(
            event.paper_id,
            event.label,
            event.occurred_at,
            event.event_id,
            event.source,
            event.reason_codes,
            event.compared_paper_id,
        )
        for event in events
        if event.kind is JudgmentKind.PAIRWISE
    )
    canonical = _encode_state(tuple(sorted(events, key=_event_key)))
    digest = hashlib.sha256(
        json.dumps(canonical, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode(
            "utf-8"
        )
    ).hexdigest()
    return CorpusSnapshot(
        CURATED_CORPUS_SCHEMA_VERSION,
        len(events),
        digest,
        cutoff,
        tuple(sorted(labels, key=lambda item: item.paper_id)),
        tuple(sorted(pairwise, key=lambda item: (item.paper_id, item.event_id))),
        conflicts,
    )


def _latest_source_events(events: tuple[CorpusEvent, ...]) -> dict[str, CorpusEvent]:
    latest: dict[str, CorpusEvent] = {}
    for event in events:
        if event.source == "zotero" and event.source_item_key:
            previous = latest.get(event.source_item_key)
            if previous is None or _event_key(event) > _event_key(previous):
                latest[event.source_item_key] = event
    return latest


def _collection_label(
    collections: tuple[str, ...], mapping: CuratedCorpusMapping
) -> CorpusLabel | None:
    values = frozenset(collections)
    positive = bool(values & frozenset(mapping.positive_collection_keys))
    negative = bool(values & frozenset(mapping.negative_collection_keys))
    if positive and negative:
        raise ApplicationError("a Zotero corpus item cannot be both positive and negative")
    if positive:
        return CorpusLabel.POSITIVE
    return CorpusLabel.NEGATIVE if negative else None


def _canonical_paper_id(identifiers: tuple[str, ...], tags: tuple[str, ...]) -> str | None:
    for tag in tags:
        if tag.casefold().startswith(_PAPER_ID_TAG_PREFIX):
            value = tag[len(_PAPER_ID_TAG_PREFIX) :].strip().casefold()
            if value.startswith(("arxiv:", "doi:")):
                return value
    for identifier in identifiers:
        normalized = identifier.strip().casefold()
        if normalized.startswith("arxiv:") and len(normalized) > 6:
            return normalized
        if normalized.startswith("doi:") and len(normalized) > 4:
            return normalized
        if _DOI.fullmatch(normalized):
            return f"doi:{normalized}"
    return None


def _source_event(
    item: ZoteroCorpusItem,
    paper_id: str,
    label: CorpusLabel,
    mapping: CuratedCorpusMapping,
    observed_at: datetime,
    previous: CorpusEvent | None,
) -> CorpusEvent:
    reasons = tuple(
        sorted(
            tag[len(mapping.reason_tag_prefix) :].strip()
            for tag in item.tags
            if tag.casefold().startswith(mapping.reason_tag_prefix.casefold())
            and tag[len(mapping.reason_tag_prefix) :].strip()
        )
    )
    seed = "|".join(
        (item.item_key, paper_id, label.value, *reasons, previous.event_id if previous else "")
    )
    event_id = "zotero-" + hashlib.sha256(seed.encode("utf-8")).hexdigest()[:24]
    return CorpusEvent(
        event_id,
        JudgmentKind.LABEL,
        paper_id,
        observed_at,
        "zotero",
        label,
        reason_codes=reasons,
        supersedes_event_id=previous.event_id if previous else None,
        source_item_key=item.item_key,
    )


def _unlabel_event(
    previous: CorpusEvent, observed_at: datetime, *, source_item_key: str
) -> CorpusEvent:
    event_id = (
        "zotero-"
        + hashlib.sha256(f"{source_item_key}|{previous.event_id}|unlabel".encode()).hexdigest()[:24]
    )
    return CorpusEvent(
        event_id,
        JudgmentKind.UNLABEL,
        previous.paper_id,
        observed_at,
        "zotero",
        supersedes_event_id=previous.event_id,
        source_item_key=source_item_key,
    )


def _same_imported_judgment(existing: CorpusEvent, candidate: CorpusEvent) -> bool:
    return (
        existing.kind is JudgmentKind.LABEL
        and existing.paper_id == candidate.paper_id
        and existing.label == candidate.label
        and existing.reason_codes == candidate.reason_codes
    )
