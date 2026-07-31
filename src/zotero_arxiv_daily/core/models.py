"""Versioned, provider-neutral domain values needed by the initial workflow."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Literal
from urllib.parse import urlparse
from uuid import UUID

PROFILE_SCHEMA_VERSION = 1
RECOMMENDATION_SCHEMA_VERSION = 1
FEEDBACK_SCHEMA_VERSION = 1
RUN_MANIFEST_SCHEMA_VERSION = 1


def _require_utc(timestamp: datetime, field: str) -> None:
    if timestamp.tzinfo is None or timestamp.utcoffset() is None:
        raise ValueError(f"{field} must be timezone-aware")


def _require_https_url(value: str, field: str) -> None:
    parsed = urlparse(value)
    if parsed.scheme != "https" or not parsed.netloc:
        raise ValueError(f"{field} must be an absolute HTTPS URL")


@dataclass(frozen=True, slots=True)
class ProfileEnvelope:
    """A versioned, non-raw profile payload boundary."""

    profile_id: UUID
    created_at: datetime
    schema_version: int = PROFILE_SCHEMA_VERSION

    def __post_init__(self) -> None:
        _require_utc(self.created_at, "created_at")
        if self.schema_version != PROFILE_SCHEMA_VERSION:
            raise ValueError("unsupported profile schema version")


@dataclass(frozen=True, slots=True)
class Recommendation:
    """A validated external-paper recommendation reference."""

    arxiv_id: str
    title: str
    abstract_url: str
    pdf_url: str

    def __post_init__(self) -> None:
        if not self.arxiv_id.strip() or not self.title.strip():
            raise ValueError("arxiv_id and title must not be empty")
        _require_https_url(self.abstract_url, "abstract_url")
        _require_https_url(self.pdf_url, "pdf_url")


@dataclass(frozen=True, slots=True)
class FeedbackRecord:
    """A versioned user feedback action, ready for later normalization."""

    feedback_id: UUID
    recommendation_id: str
    action: Literal["interested", "not_interested", "save_for_later", "read"]
    created_at: datetime
    schema_version: int = FEEDBACK_SCHEMA_VERSION

    def __post_init__(self) -> None:
        _require_utc(self.created_at, "created_at")
        if not self.recommendation_id.strip():
            raise ValueError("recommendation_id must not be empty")
        if self.schema_version != FEEDBACK_SCHEMA_VERSION:
            raise ValueError("unsupported feedback schema version")


@dataclass(frozen=True, slots=True)
class RunManifest:
    """Non-sensitive run metadata used for operational inspection."""

    run_id: UUID
    started_at: datetime
    schema_version: int = RUN_MANIFEST_SCHEMA_VERSION

    def __post_init__(self) -> None:
        _require_utc(self.started_at, "started_at")
        if self.schema_version != RUN_MANIFEST_SCHEMA_VERSION:
            raise ValueError("unsupported run manifest schema version")
