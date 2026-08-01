"""Versioned profile schemas with no raw Zotero content fields."""

from __future__ import annotations

from dataclasses import dataclass

ITEM_DIGEST_SCHEMA_VERSION = 1
INTEREST_PROFILE_SCHEMA_VERSION = 1
REMOTE_PROFILE_SCHEMA_VERSION = 1


@dataclass(frozen=True, slots=True)
class ItemDigest:
    item_key: str
    content_hash: str
    terms: tuple[str, ...]
    signal_weights: tuple[tuple[str, float], ...]
    year: int | None = None
    categories: tuple[str, ...] = ()
    schema_version: int = ITEM_DIGEST_SCHEMA_VERSION


@dataclass(frozen=True, slots=True)
class InterestProfile:
    schema_version: int
    source_library_version: int
    terms: tuple[tuple[str, float], ...]
    recent_terms: tuple[tuple[str, float], ...]
    categories: tuple[tuple[str, float, str], ...]
    digest_count: int


@dataclass(frozen=True, slots=True)
class RemoteProfile:
    schema_version: int
    source_library_version: int
    topics: tuple[str, ...]
    core_categories: tuple[str, ...]
    adjacent_categories: tuple[str, ...]
    representative_terms: tuple[str, ...]
    representative_papers: tuple[tuple[int | None, tuple[str, ...], tuple[str, ...]], ...] = ()
