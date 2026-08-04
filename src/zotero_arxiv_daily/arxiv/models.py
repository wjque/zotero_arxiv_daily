"""Provider-neutral public arXiv candidate and retrieval state models."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime


@dataclass(frozen=True, slots=True)
class ArxivId:
    canonical: str
    revision: int | None


@dataclass(frozen=True, slots=True)
class ArxivCandidate:
    arxiv_id: ArxivId
    title: str
    authors: tuple[str, ...]
    categories: tuple[str, ...]
    published: datetime
    updated: datetime
    abstract_url: str
    pdf_url: str
    summary: str
    affiliations: tuple[str, ...] = ()
    doi: str | None = None


@dataclass(frozen=True, slots=True)
class RetrievalCheckpoint:
    completed_at: datetime
    schema_version: int = 1


@dataclass(frozen=True, slots=True)
class RetrievalResult:
    candidates: tuple[ArxivCandidate, ...]
    checkpoint: RetrievalCheckpoint
    request_count: int
    degraded: bool = False
    degraded_reason: str | None = None
