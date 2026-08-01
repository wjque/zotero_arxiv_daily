"""Atomic local state for public arXiv candidates and successful checkpoints."""

from __future__ import annotations

import json
import os
import tempfile
from dataclasses import asdict
from datetime import UTC, datetime
from pathlib import Path

from zotero_arxiv_daily.arxiv.models import ArxivCandidate, RetrievalCheckpoint


class ArxivStateStore:
    """Keep the last successful checkpoint separate from an in-progress retrieval."""

    def __init__(self, path: Path) -> None:
        self.path = path

    def checkpoint(self) -> RetrievalCheckpoint | None:
        data = self._read()
        value = data.get("checkpoint")
        if not isinstance(value, str):
            return None
        return RetrievalCheckpoint(datetime.fromisoformat(value).astimezone(UTC))

    def seen_ids(self) -> frozenset[str]:
        data = self._read()
        values = data.get("seen_ids", [])
        return (
            frozenset(str(value) for value in values) if isinstance(values, list) else frozenset()
        )

    def in_progress_at(self) -> datetime | None:
        """Return the non-authoritative start of an unfinished retrieval, if present."""

        value = self._read().get("in_progress")
        return datetime.fromisoformat(value).astimezone(UTC) if isinstance(value, str) else None

    def begin(self, started_at: datetime) -> None:
        """Record an in-progress marker without replacing the successful checkpoint."""

        payload = self._read()
        payload["in_progress"] = started_at.astimezone(UTC).isoformat()
        self._write(payload)

    def commit(
        self, checkpoint: RetrievalCheckpoint, candidates: tuple[ArxivCandidate, ...]
    ) -> None:
        """Replace state atomically only after all requested pages parsed successfully."""

        previous = self._read()
        previous_seen = previous.get("seen_ids", [])
        seen = (
            set(str(value) for value in previous_seen) if isinstance(previous_seen, list) else set()
        )
        seen.update(candidate.arxiv_id.canonical for candidate in candidates)
        payload = {
            "schema_version": 1,
            "checkpoint": checkpoint.completed_at.isoformat(),
            "seen_ids": sorted(seen),
            "candidates": [_candidate_payload(candidate) for candidate in candidates],
            "in_progress": None,
        }
        self._write(payload)

    def _write(self, payload: dict[str, object]) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        descriptor, temporary = tempfile.mkstemp(prefix=f".{self.path.name}.", dir=self.path.parent)
        temporary_path = Path(temporary)
        try:
            with os.fdopen(descriptor, "w", encoding="utf-8") as output:
                json.dump(payload, output, ensure_ascii=False, separators=(",", ":"))
                output.flush()
                os.fsync(output.fileno())
            os.replace(temporary_path, self.path)
        finally:
            temporary_path.unlink(missing_ok=True)

    def _read(self) -> dict[str, object]:
        if not self.path.is_file():
            return {}
        value = json.loads(self.path.read_text(encoding="utf-8"))
        return value if isinstance(value, dict) else {}


def _candidate_payload(candidate: ArxivCandidate) -> dict[str, object]:
    payload = asdict(candidate)
    payload["arxiv_id"] = candidate.arxiv_id.canonical
    payload["published"] = candidate.published.isoformat()
    payload["updated"] = candidate.updated.isoformat()
    return payload
