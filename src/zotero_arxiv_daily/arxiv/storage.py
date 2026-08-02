"""Atomic local state for public arXiv candidates and successful checkpoints."""

from __future__ import annotations

import json
import os
import tempfile
from dataclasses import asdict
from datetime import UTC, datetime, timedelta
from pathlib import Path

from zotero_arxiv_daily.arxiv.ids import parse_arxiv_id, public_urls
from zotero_arxiv_daily.arxiv.models import ArxivCandidate, ArxivId, RetrievalCheckpoint
from zotero_arxiv_daily.core.errors import ExternalServiceError

CANDIDATE_POOL_SCHEMA_VERSION = 3
CANDIDATE_POOL_RETENTION_DAYS = 30
CANDIDATE_POOL_LIMIT = 1000


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

    def candidates(self) -> tuple[ArxivCandidate, ...]:
        """Read only validated public candidate metadata from the last successful retrieval."""

        payload = self._read()
        schema_version = payload.get("schema_version", 1)
        if schema_version not in {1, 2, CANDIDATE_POOL_SCHEMA_VERSION}:
            raise ExternalServiceError("arXiv candidate state uses an unsupported schema")
        values = payload.get("candidates", [])
        if not isinstance(values, list):
            raise ExternalServiceError("arXiv candidate state is invalid")
        try:
            return tuple(_candidate_from_payload(value) for value in values)
        except (KeyError, TypeError, ValueError) as error:
            raise ExternalServiceError("arXiv candidate state is invalid") from error

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
        """Merge a bounded candidate pool only after all requested pages parsed successfully."""

        previous = self._read()
        previous_seen = previous.get("seen_ids", [])
        seen = (
            set(str(value) for value in previous_seen) if isinstance(previous_seen, list) else set()
        )
        seen.update(candidate.arxiv_id.canonical for candidate in candidates)
        existing = self.candidates()
        pool = _merge_candidate_pool(existing, candidates, checkpoint.completed_at)
        payload = {
            "schema_version": CANDIDATE_POOL_SCHEMA_VERSION,
            "checkpoint": checkpoint.completed_at.isoformat(),
            "seen_ids": sorted(seen),
            "candidates": [_candidate_payload(candidate) for candidate in pool],
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
    payload["revision"] = candidate.arxiv_id.revision
    payload["published"] = candidate.published.isoformat()
    payload["updated"] = candidate.updated.isoformat()
    return payload


def _candidate_from_payload(value: object) -> ArxivCandidate:
    if not isinstance(value, dict):
        raise ValueError
    identifier = parse_arxiv_id(str(value["arxiv_id"]))
    revision = value.get("revision", identifier.revision)
    if revision is not None and (not isinstance(revision, int) or revision < 1):
        raise ValueError
    identifier = ArxivId(identifier.canonical, revision)
    abstract_url, pdf_url = public_urls(identifier)
    affiliations = value.get("affiliations", [])
    if not isinstance(affiliations, list) or not all(
        isinstance(item, str) for item in affiliations
    ):
        raise ValueError
    return ArxivCandidate(
        identifier,
        str(value["title"]),
        tuple(str(item) for item in value["authors"]),
        tuple(str(item) for item in value["categories"]),
        datetime.fromisoformat(str(value["published"])).astimezone(UTC),
        datetime.fromisoformat(str(value["updated"])).astimezone(UTC),
        abstract_url,
        pdf_url,
        str(value["summary"]),
        tuple(affiliations),
    )


def _merge_candidate_pool(
    existing: tuple[ArxivCandidate, ...],
    incoming: tuple[ArxivCandidate, ...],
    completed_at: datetime,
) -> tuple[ArxivCandidate, ...]:
    cutoff = completed_at.astimezone(UTC) - timedelta(days=CANDIDATE_POOL_RETENTION_DAYS)
    by_id: dict[str, ArxivCandidate] = {}
    for candidate in existing + incoming:
        if candidate.updated < cutoff:
            continue
        previous = by_id.get(candidate.arxiv_id.canonical)
        if previous is None or candidate.updated > previous.updated:
            by_id[candidate.arxiv_id.canonical] = candidate
    return tuple(
        sorted(
            by_id.values(),
            key=lambda candidate: (candidate.updated, candidate.arxiv_id.canonical),
            reverse=True,
        )[:CANDIDATE_POOL_LIMIT]
    )
