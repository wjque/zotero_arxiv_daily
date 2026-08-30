"""Versioned profile schemas with no raw Zotero content fields."""

from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass
from datetime import UTC, datetime

ITEM_DIGEST_SCHEMA_VERSION = 3
LOCAL_INTEREST_PROFILE_SCHEMA_VERSION = 2
REMOTE_SERVING_PROFILE_SCHEMA_VERSION = 5
PROFILE_FEATURE_HASH_VERSION = "hmac-sha256-128-v1"

MAX_WATCHED_IDENTITIES = 32
MAX_IDENTITY_ALIASES = 8
MAX_IDENTITY_BYTES = 160
MAX_PROTECTED_LEXICAL_FEATURES = 48
MAX_INTEREST_PROTOTYPES = 8
MAX_PROTOTYPE_FEATURES = 16

_PROTECTED_DIGEST = re.compile(r"[A-Za-z0-9_-]{22}")
_ARXIV_CATEGORY = re.compile(r"[A-Za-z][A-Za-z0-9.-]{1,31}")


def validate_protected_digest(value: str) -> str:
    """Validate one compact protected feature identity."""

    if not _PROTECTED_DIGEST.fullmatch(value):
        raise ValueError("protected feature digest is invalid")
    return value


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
class PreferenceFacet:
    """A bounded derived interest signal that contains no Zotero prose or identifiers."""

    kind: str
    value: str
    score: float
    confidence: float
    provenance: tuple[str, ...]

    def __post_init__(self) -> None:
        if self.kind not in {"domain", "method", "task"}:
            raise ValueError("preference facet kind is unsupported")
        if not self.value.strip() or len(self.value) > 80:
            raise ValueError("preference facet value is invalid")
        if not 0 <= self.score <= 1 or not 0 <= self.confidence <= 1:
            raise ValueError("preference facet score and confidence must be normalized")
        if not self.provenance or len(self.provenance) > 6:
            raise ValueError("preference facet provenance is invalid")


@dataclass(frozen=True, slots=True)
class ProtectedLexicalFeature:
    """One keyed lexical identity with separate long-term and recent strength."""

    digest: str
    long_term_weight: float
    recent_weight: float

    def __post_init__(self) -> None:
        if (
            not _PROTECTED_DIGEST.fullmatch(self.digest)
            or not 0 <= self.long_term_weight <= 1
            or not 0 <= self.recent_weight <= 1
            or self.long_term_weight == self.recent_weight == 0
        ):
            raise ValueError("protected lexical feature is invalid")


@dataclass(frozen=True, slots=True)
class ProtectedInterestPrototype:
    """Anonymous paper-level token prototype for remote similarity scoring."""

    feature_digests: tuple[str, ...]

    def __post_init__(self) -> None:
        if (
            not 1 <= len(self.feature_digests) <= MAX_PROTOTYPE_FEATURES
            or len(set(self.feature_digests)) != len(self.feature_digests)
            or any(not _PROTECTED_DIGEST.fullmatch(value) for value in self.feature_digests)
        ):
            raise ValueError("protected interest prototype is invalid")


@dataclass(frozen=True, slots=True)
class LocalInterestProfile:
    schema_version: int
    source_library_version: int
    terms: tuple[tuple[str, float], ...]
    recent_terms: tuple[tuple[str, float], ...]
    categories: tuple[tuple[str, float, str], ...]
    digest_count: int
    long_term_facets: tuple[PreferenceFacet, ...] = ()
    recent_facets: tuple[PreferenceFacet, ...] = ()


@dataclass(frozen=True, slots=True)
class RemoteServingProfile:
    schema_version: int
    source_library_version: int
    topics: tuple[str, ...]
    core_categories: tuple[str, ...]
    adjacent_categories: tuple[str, ...]
    representative_terms: tuple[str, ...]
    representative_papers: tuple[tuple[int | None, tuple[str, ...], tuple[str, ...]], ...] = ()
    watched_authors: tuple[WatchedIdentity, ...] = ()
    watched_institutions: tuple[WatchedIdentity, ...] = ()
    source_library_synced_at: str | None = None
    preference_facets: tuple[PreferenceFacet, ...] = ()
    feature_hash_version: str | None = None
    feature_key_verifier: str | None = None
    lexical_features: tuple[ProtectedLexicalFeature, ...] = ()
    baseline_lexical_digests: tuple[str, ...] = ()
    long_term_facets: tuple[PreferenceFacet, ...] = ()
    recent_facets: tuple[PreferenceFacet, ...] = ()
    interest_prototypes: tuple[ProtectedInterestPrototype, ...] = ()
    watched_author_digests: tuple[str, ...] = ()
    watched_institution_digests: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        if self.schema_version not in {1, 2, 3, 4, REMOTE_SERVING_PROFILE_SCHEMA_VERSION}:
            raise ValueError("unsupported remote profile schema version")
        if self.source_library_synced_at is not None:
            try:
                instant = datetime.fromisoformat(self.source_library_synced_at)
            except ValueError as error:
                raise ValueError("source_library_synced_at is invalid") from error
            if instant.tzinfo is None or instant.utcoffset() != UTC.utcoffset(instant):
                raise ValueError("source_library_synced_at must be UTC")
        if len(self.watched_authors) > MAX_WATCHED_IDENTITIES:
            raise ValueError("too many watched authors")
        if len(self.watched_institutions) > MAX_WATCHED_IDENTITIES:
            raise ValueError("too many watched institutions")
        if self.schema_version == REMOTE_SERVING_PROFILE_SCHEMA_VERSION and (
            len(self.core_categories) > 6
            or len(self.adjacent_categories) > 6
            or len(set(self.core_categories)) != len(self.core_categories)
            or len(set(self.adjacent_categories)) != len(self.adjacent_categories)
            or set(self.core_categories) & set(self.adjacent_categories)
            or any(
                not _ARXIV_CATEGORY.fullmatch(value)
                for value in (*self.core_categories, *self.adjacent_categories)
            )
        ):
            raise ValueError("serving profile categories are invalid")
        if (
            len(self.lexical_features) > MAX_PROTECTED_LEXICAL_FEATURES
            or len({item.digest for item in self.lexical_features}) != len(self.lexical_features)
            or len(self.baseline_lexical_digests) > 30
            or len(set(self.baseline_lexical_digests)) != len(self.baseline_lexical_digests)
            or len(self.long_term_facets) > 12
            or len(self.recent_facets) > 8
            or len(self.interest_prototypes) > MAX_INTEREST_PROTOTYPES
            or len({item.feature_digests for item in self.interest_prototypes})
            != len(self.interest_prototypes)
            or len(self.watched_author_digests)
            > MAX_WATCHED_IDENTITIES * (MAX_IDENTITY_ALIASES + 1)
            or len(self.watched_institution_digests)
            > MAX_WATCHED_IDENTITIES * (MAX_IDENTITY_ALIASES + 1)
            or len(set(self.watched_author_digests)) != len(self.watched_author_digests)
            or len(set(self.watched_institution_digests)) != len(self.watched_institution_digests)
            or any(
                not _PROTECTED_DIGEST.fullmatch(value)
                for value in (
                    *((self.feature_key_verifier,) if self.feature_key_verifier else ()),
                    *self.baseline_lexical_digests,
                    *self.watched_author_digests,
                    *self.watched_institution_digests,
                )
            )
        ):
            raise ValueError("protected serving profile fields are invalid")
        if self.schema_version == REMOTE_SERVING_PROFILE_SCHEMA_VERSION:
            if (
                self.feature_hash_version != PROFILE_FEATURE_HASH_VERSION
                or self.feature_key_verifier is None
                or self.topics
                or self.representative_terms
                or self.representative_papers
                or self.watched_authors
                or self.watched_institutions
                or self.preference_facets != (*self.long_term_facets, *self.recent_facets)
            ):
                raise ValueError("serving profile v5 contains legacy or inconsistent fields")
        elif (
            self.feature_hash_version is not None
            or self.feature_key_verifier is not None
            or self.lexical_features
            or self.baseline_lexical_digests
            or self.long_term_facets
            or self.recent_facets
            or self.interest_prototypes
            or self.watched_author_digests
            or self.watched_institution_digests
        ):
            raise ValueError("legacy serving profile contains v5 fields")
