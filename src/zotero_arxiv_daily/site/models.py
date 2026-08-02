"""Strict versioned data permitted to enter the static site."""

from __future__ import annotations

import json
import re
from dataclasses import asdict, dataclass
from datetime import datetime
from pathlib import Path
from urllib.parse import urlparse

from zotero_arxiv_daily.core.time import product_date, require_aware_utc
from zotero_arxiv_daily.ranking.models import RecommendationSet

PUBLISHABLE_SCHEMA_VERSION = 3
_REPOSITORY = re.compile(r"^[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+$")
_REVISION = re.compile(r"^[0-9a-fA-F]{7,64}$")


@dataclass(frozen=True, slots=True)
class WorkflowRun:
    run_id: int
    attempt: int
    source_revision: str
    repository: str
    run_url: str

    def __post_init__(self) -> None:
        expected = f"https://github.com/{self.repository}/actions/runs/{self.run_id}"
        if (
            self.run_id < 1
            or self.attempt < 1
            or not _REPOSITORY.fullmatch(self.repository)
            or not _REVISION.fullmatch(self.source_revision)
            or self.run_url != expected
        ):
            raise ValueError("workflow run identity is invalid")


@dataclass(frozen=True, slots=True)
class PublishedRecommendation:
    arxiv_id: str
    title: str
    authors: tuple[str, ...]
    categories: tuple[str, ...]
    published_on: str
    summary: str
    reason: str
    confidence: float
    quota_source: str
    abstract_url: str
    pdf_url: str
    preference_signals: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        if not self.arxiv_id.strip() or not self.title.strip() or not self.authors:
            raise ValueError("published recommendations require an ID, title, and authors")
        if not 0 <= self.confidence <= 1:
            raise ValueError("published recommendation confidence must be between zero and one")
        if self.quota_source not in {"core", "adjacent", "exploration"}:
            raise ValueError("published recommendation has an invalid quota source")
        if not set(self.preference_signals) <= {"watched_author", "watched_institution"}:
            raise ValueError("published recommendation has an invalid preference signal")
        for value in (self.abstract_url, self.pdf_url):
            parsed = urlparse(value)
            if parsed.scheme != "https" or parsed.hostname != "arxiv.org":
                raise ValueError("published recommendation links must use arxiv.org HTTPS URLs")


@dataclass(frozen=True, slots=True)
class PublishedRecommendationSet:
    schema_version: int
    generation_started_at: str
    recommendations: tuple[PublishedRecommendation, ...]
    generation_completed_at: str | None = None
    artifact_built_at: str | None = None
    profile_library_version: int | None = None
    profile_snapshot_at: str | None = None
    profile_schema_version: int | None = None
    workflow_run: WorkflowRun | None = None
    output_language: str = "en"

    @property
    def generated_at(self) -> str:
        """Compatibility alias for legacy callers."""

        return self.generation_started_at

    def to_dict(self) -> dict[str, object]:
        if self.schema_version == 1:
            return {
                "schema_version": 1,
                "generated_at": self.generation_started_at,
                "recommendations": [
                    {
                        key: value
                        for key, value in asdict(record).items()
                        if key != "preference_signals"
                    }
                    for record in self.recommendations
                ],
            }
        return asdict(self)


def write_published_set(value: PublishedRecommendationSet, path: Path) -> None:
    """Write a validated publishable input without exposing internal ranking fields."""

    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value.to_dict(), ensure_ascii=False, separators=(",", ":")), "utf-8")


def read_published_set(path: Path) -> PublishedRecommendationSet:
    """Read schema v2 or adapt the exact v0.1.0 schema-v1 shape."""

    try:
        value = json.loads(path.read_text(encoding="utf-8"))
        if not isinstance(value, dict):
            raise ValueError
        schema_version = value.get("schema_version")
        if schema_version == 1:
            return _read_v1(value)
        if schema_version == 2:
            return _read_v2(value)
        if schema_version == PUBLISHABLE_SCHEMA_VERSION:
            return _read_v3(value)
        raise ValueError
    except (OSError, TypeError, ValueError, json.JSONDecodeError) as error:
        raise ValueError(f"publishable recommendation input is invalid: {path}") from error


def make_published_set(
    result: RecommendationSet,
    *,
    profile_schema_version: int = 1,
    workflow_run: WorkflowRun | None = None,
    output_language: str = "en",
) -> PublishedRecommendationSet:
    """Project internal records through the sole static-site allowlist."""

    completed = result.generation_completed_at or result.generation_started_at
    return PublishedRecommendationSet(
        PUBLISHABLE_SCHEMA_VERSION,
        _instant(result.generation_started_at),
        tuple(
            PublishedRecommendation(
                record.candidate.arxiv_id.canonical,
                record.candidate.title,
                record.candidate.authors,
                record.candidate.categories,
                product_date(record.candidate.published),
                record.summary,
                record.reason,
                record.quality,
                record.source,
                record.candidate.abstract_url,
                record.candidate.pdf_url,
                record.identity_matches,
            )
            for record in result.recommendations
        ),
        _instant(completed),
        None,
        result.profile_version,
        result.profile_snapshot_at,
        profile_schema_version,
        workflow_run,
        output_language,
    )


def _read_v1(value: dict[str, object]) -> PublishedRecommendationSet:
    if set(value) != {"schema_version", "generated_at", "recommendations"}:
        raise ValueError
    started = _instant_text(value["generated_at"])
    records = _records(value["recommendations"], version=1)
    return PublishedRecommendationSet(1, started, records)


def _read_v2(value: dict[str, object]) -> PublishedRecommendationSet:
    fields = {
        "schema_version",
        "generation_started_at",
        "generation_completed_at",
        "artifact_built_at",
        "profile_library_version",
        "profile_schema_version",
        "workflow_run",
        "output_language",
        "recommendations",
    }
    if set(value) != fields:
        raise ValueError
    workflow = value["workflow_run"]
    return PublishedRecommendationSet(
        2,
        _instant_text(value["generation_started_at"]),
        _records(value["recommendations"], version=2),
        _optional_instant(value["generation_completed_at"]),
        _optional_instant(value["artifact_built_at"]),
        _positive_int(value["profile_library_version"]),
        None,
        _positive_int(value["profile_schema_version"]),
        _workflow_run(workflow) if workflow is not None else None,
        _nonempty_string(value["output_language"]),
    )


def _read_v3(value: dict[str, object]) -> PublishedRecommendationSet:
    fields = {
        "schema_version",
        "generation_started_at",
        "generation_completed_at",
        "artifact_built_at",
        "profile_library_version",
        "profile_snapshot_at",
        "profile_schema_version",
        "workflow_run",
        "output_language",
        "recommendations",
    }
    if set(value) != fields:
        raise ValueError
    workflow = value["workflow_run"]
    return PublishedRecommendationSet(
        PUBLISHABLE_SCHEMA_VERSION,
        _instant_text(value["generation_started_at"]),
        _records(value["recommendations"], version=2),
        _optional_instant(value["generation_completed_at"]),
        _optional_instant(value["artifact_built_at"]),
        _positive_int(value["profile_library_version"]),
        _optional_instant(value["profile_snapshot_at"]),
        _positive_int(value["profile_schema_version"]),
        _workflow_run(workflow) if workflow is not None else None,
        _nonempty_string(value["output_language"]),
    )


def _records(value: object, *, version: int) -> tuple[PublishedRecommendation, ...]:
    if not isinstance(value, list):
        raise ValueError
    return tuple(_published_recommendation(record, version=version) for record in value)


def _published_recommendation(value: object, *, version: int) -> PublishedRecommendation:
    fields = {
        "arxiv_id",
        "title",
        "authors",
        "categories",
        "published_on",
        "summary",
        "reason",
        "confidence",
        "quota_source",
        "abstract_url",
        "pdf_url",
    }
    if version == 2:
        fields.add("preference_signals")
    if not isinstance(value, dict) or set(value) != fields:
        raise ValueError
    authors = _strings(value["authors"])
    categories = _strings(value["categories"])
    signals = _strings(value["preference_signals"]) if version == 2 else ()
    return PublishedRecommendation(
        _nonempty_string(value["arxiv_id"]),
        _nonempty_string(value["title"]),
        authors,
        categories,
        _nonempty_string(value["published_on"]),
        _nonempty_string(value["summary"]),
        _nonempty_string(value["reason"]),
        float(value["confidence"]),
        _nonempty_string(value["quota_source"]),
        _nonempty_string(value["abstract_url"]),
        _nonempty_string(value["pdf_url"]),
        signals,
    )


def _workflow_run(value: object) -> WorkflowRun:
    if not isinstance(value, dict) or set(value) != {
        "run_id",
        "attempt",
        "source_revision",
        "repository",
        "run_url",
    }:
        raise ValueError
    return WorkflowRun(
        int(value["run_id"]),
        int(value["attempt"]),
        str(value["source_revision"]),
        str(value["repository"]),
        str(value["run_url"]),
    )


def _instant(value: datetime) -> str:
    return require_aware_utc(value).isoformat()


def _instant_text(value: object) -> str:
    if not isinstance(value, str):
        raise ValueError
    parsed = datetime.fromisoformat(value)
    return _instant(parsed)


def _optional_instant(value: object) -> str | None:
    return None if value is None else _instant_text(value)


def _positive_int(value: object) -> int:
    if not isinstance(value, int) or value < 1:
        raise ValueError
    return value


def _nonempty_string(value: object) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ValueError
    return value


def _strings(value: object) -> tuple[str, ...]:
    if not isinstance(value, list) or not all(isinstance(item, str) for item in value):
        raise ValueError
    return tuple(value)
