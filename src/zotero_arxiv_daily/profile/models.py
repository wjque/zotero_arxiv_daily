"""Versioned profile schemas with no raw Zotero content fields."""

from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass

ITEM_DIGEST_SCHEMA_VERSION = 1
INTEREST_PROFILE_SCHEMA_VERSION = 1
REMOTE_PROFILE_SCHEMA_VERSION = 2

MAX_WATCHED_IDENTITIES = 32
MAX_IDENTITY_ALIASES = 8
MAX_IDENTITY_BYTES = 160


@dataclass(frozen=True, slots=True)
class WatchedIdentity:
    """A bounded display name and its exact-match aliases."""

    name: str
    aliases: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        values = (self.name, *self.aliases)
        if not self.name.strip() or len(self.aliases) > MAX_IDENTITY_ALIASES:
            raise ValueError("watched identity name and alias count are invalid")
        if any(
            not value.strip() or len(value.encode("utf-8")) > MAX_IDENTITY_BYTES for value in values
        ):
            raise ValueError("watched identity values must be non-empty and within the byte limit")
        if len({normalize_identity(value) for value in values}) != len(values):
            raise ValueError("watched identity aliases must be unique after normalization")

    @property
    def normalized_names(self) -> frozenset[str]:
        return frozenset(normalize_identity(value) for value in (self.name, *self.aliases))


def normalize_identity(value: str) -> str:
    """Normalize a person or institution name for exact equality only."""

    normalized = unicodedata.normalize("NFKC", value).casefold()
    normalized = re.sub(r"[^\w]+", " ", normalized, flags=re.UNICODE)
    return " ".join(normalized.split())


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
    watched_authors: tuple[WatchedIdentity, ...] = ()
    watched_institutions: tuple[WatchedIdentity, ...] = ()

    def __post_init__(self) -> None:
        if self.schema_version not in {1, REMOTE_PROFILE_SCHEMA_VERSION}:
            raise ValueError("unsupported remote profile schema version")
        if len(self.watched_authors) > MAX_WATCHED_IDENTITIES:
            raise ValueError("too many watched authors")
        if len(self.watched_institutions) > MAX_WATCHED_IDENTITIES:
            raise ValueError("too many watched institutions")
