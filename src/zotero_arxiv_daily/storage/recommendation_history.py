"""Versioned history of successfully deployed public arXiv identifiers."""

from __future__ import annotations

import json
import os
import tempfile
from dataclasses import asdict, dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path

from zotero_arxiv_daily.arxiv.ids import parse_arxiv_id
from zotero_arxiv_daily.ranking.models import RecommendationSet

HISTORY_SCHEMA_VERSION = 1
MAX_RETENTION_DAYS = 30


@dataclass(frozen=True, slots=True)
class RecommendationHistoryEntry:
    arxiv_id: str
    revision: int | None
    published_at: datetime


class RecommendationHistoryStore:
    """Read successful state and prepare an atomic next-state file without advancing it."""

    def __init__(self, path: Path) -> None:
        self.path = path

    def entries(self) -> tuple[RecommendationHistoryEntry, ...]:
        if not self.path.is_file():
            return ()
        try:
            payload = json.loads(self.path.read_text(encoding="utf-8"))
            if not isinstance(payload, dict) or set(payload) != {"schema_version", "entries"}:
                raise ValueError
            if payload["schema_version"] != HISTORY_SCHEMA_VERSION:
                raise ValueError
            raw_entries = payload["entries"]
            if not isinstance(raw_entries, list):
                raise ValueError
            return tuple(_entry(value) for value in raw_entries)
        except (OSError, TypeError, ValueError, json.JSONDecodeError) as error:
            raise ValueError("recommendation history is invalid") from error

    def excluded_ids(self, now: datetime, suppression_days: int = 14) -> frozenset[str]:
        if not 1 <= suppression_days <= MAX_RETENTION_DAYS:
            raise ValueError("suppression_days must be between 1 and 30")
        cutoff = now.astimezone(UTC) - timedelta(days=suppression_days)
        return frozenset(entry.arxiv_id for entry in self.entries() if entry.published_at >= cutoff)

    def prepare_success(
        self, result: RecommendationSet, output_path: Path, published_at: datetime
    ) -> tuple[RecommendationHistoryEntry, ...]:
        """Write candidate state separately; callers promote it only after deployment succeeds."""

        completed = published_at.astimezone(UTC)
        cutoff = completed - timedelta(days=MAX_RETENTION_DAYS)
        by_id = {entry.arxiv_id: entry for entry in self.entries() if entry.published_at >= cutoff}
        for record in result.recommendations:
            identifier = record.candidate.arxiv_id
            by_id[identifier.canonical] = RecommendationHistoryEntry(
                identifier.canonical, identifier.revision, completed
            )
        entries = tuple(sorted(by_id.values(), key=lambda item: (item.published_at, item.arxiv_id)))
        _write_atomic(
            output_path,
            {
                "schema_version": HISTORY_SCHEMA_VERSION,
                "entries": [
                    {
                        **asdict(entry),
                        "published_at": entry.published_at.isoformat(),
                    }
                    for entry in entries
                ],
            },
        )
        return entries


def _entry(value: object) -> RecommendationHistoryEntry:
    if not isinstance(value, dict) or set(value) != {"arxiv_id", "revision", "published_at"}:
        raise ValueError
    identifier = parse_arxiv_id(str(value["arxiv_id"]))
    revision = value["revision"]
    if revision is not None and (not isinstance(revision, int) or revision < 1):
        raise ValueError
    timestamp = datetime.fromisoformat(str(value["published_at"]))
    if timestamp.tzinfo is None:
        raise ValueError
    return RecommendationHistoryEntry(identifier.canonical, revision, timestamp.astimezone(UTC))


def _write_atomic(path: Path, payload: dict[str, object]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    temporary_path = Path(temporary)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as output:
            json.dump(payload, output, ensure_ascii=False, separators=(",", ":"))
            output.flush()
            os.fsync(output.fileno())
        os.replace(temporary_path, path)
    finally:
        temporary_path.unlink(missing_ok=True)
