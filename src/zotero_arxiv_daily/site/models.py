"""Minimal, validated data permitted to enter the static site."""

from __future__ import annotations

import json
from dataclasses import asdict, dataclass
from datetime import datetime
from pathlib import Path
from urllib.parse import urlparse

from zotero_arxiv_daily.ranking.models import RecommendationSet

PUBLISHABLE_SCHEMA_VERSION = 1


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

    def __post_init__(self) -> None:
        if not self.arxiv_id.strip() or not self.title.strip() or not self.authors:
            raise ValueError("published recommendations require an ID, title, and authors")
        if not 0 <= self.confidence <= 1:
            raise ValueError("published recommendation confidence must be between zero and one")
        if self.quota_source not in {"core", "adjacent", "exploration"}:
            raise ValueError("published recommendation has an invalid quota source")
        for value in (self.abstract_url, self.pdf_url):
            parsed = urlparse(value)
            if parsed.scheme != "https" or parsed.hostname != "arxiv.org":
                raise ValueError("published recommendation links must use arxiv.org HTTPS URLs")


@dataclass(frozen=True, slots=True)
class PublishedRecommendationSet:
    schema_version: int
    generated_at: str
    recommendations: tuple[PublishedRecommendation, ...]

    def to_dict(self) -> dict[str, object]:
        return asdict(self)


def write_published_set(value: PublishedRecommendationSet, path: Path) -> None:
    """Write a validated publishable input without exposing internal ranking fields."""

    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value.to_dict(), ensure_ascii=False, separators=(",", ":")), "utf-8")


def read_published_set(path: Path) -> PublishedRecommendationSet:
    """Read the strict public schema accepted by the static site builder."""

    try:
        value = json.loads(path.read_text(encoding="utf-8"))
        if not isinstance(value, dict) or set(value) != {
            "schema_version",
            "generated_at",
            "recommendations",
        }:
            raise ValueError
        records = value["recommendations"]
        if not isinstance(records, list):
            raise ValueError
        return PublishedRecommendationSet(
            value["schema_version"],
            value["generated_at"],
            tuple(_published_recommendation(record) for record in records),
        )
    except (OSError, TypeError, ValueError, json.JSONDecodeError) as error:
        raise ValueError(f"publishable recommendation input is invalid: {path}") from error


def _published_recommendation(value: object) -> PublishedRecommendation:
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
    if not isinstance(value, dict) or set(value) != fields:
        raise ValueError
    authors = value["authors"]
    categories = value["categories"]
    if not (
        isinstance(authors, list)
        and all(isinstance(author, str) for author in authors)
        and isinstance(categories, list)
        and all(isinstance(category, str) for category in categories)
    ):
        raise ValueError
    return PublishedRecommendation(
        value["arxiv_id"],
        value["title"],
        tuple(authors),
        tuple(categories),
        value["published_on"],
        value["summary"],
        value["reason"],
        float(value["confidence"]),
        value["quota_source"],
        value["abstract_url"],
        value["pdf_url"],
    )


def make_published_set(result: RecommendationSet) -> PublishedRecommendationSet:
    """Project internal records through the sole static-site allowlist."""

    return PublishedRecommendationSet(
        PUBLISHABLE_SCHEMA_VERSION,
        result.generated_at.isoformat(),
        tuple(
            PublishedRecommendation(
                record.candidate.arxiv_id.canonical,
                record.candidate.title,
                record.candidate.authors,
                record.candidate.categories,
                _date(record.candidate.published),
                record.summary,
                record.reason,
                record.quality,
                record.source,
                record.candidate.abstract_url,
                record.candidate.pdf_url,
            )
            for record in result.recommendations
        ),
    )


def _date(value: datetime) -> str:
    return value.date().isoformat()
