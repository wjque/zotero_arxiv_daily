"""Cached profile use cases and protected GitHub Secret publication."""

from __future__ import annotations

import hashlib
import json
import re
import subprocess
from dataclasses import asdict, replace
from datetime import UTC, datetime
from pathlib import Path
from typing import Protocol

from zotero_arxiv_daily.core.errors import ConfigurationError, ExternalServiceError
from zotero_arxiv_daily.evaluation.corpus import CorpusStore
from zotero_arxiv_daily.profile.build import (
    build_local_interest_profile,
    make_digest,
    project_legacy_serving_profile,
    project_serving_profile,
)
from zotero_arxiv_daily.profile.export import serving_profile_payload
from zotero_arxiv_daily.profile.models import (
    MAX_IDENTITY_ALIASES,
    MAX_INTEREST_PROTOTYPES,
    MAX_PROTECTED_LEXICAL_FEATURES,
    MAX_PROTOTYPE_FEATURES,
    MAX_WATCHED_IDENTITIES,
    REMOTE_SERVING_PROFILE_SCHEMA_VERSION,
    ItemDigest,
    LocalInterestProfile,
    PreferenceFacet,
    ProtectedInterestPrototype,
    ProtectedLexicalFeature,
    RemoteServingProfile,
    WatchedIdentity,
    normalize_identity,
    validate_protected_digest,
)
from zotero_arxiv_daily.profile.protection import (
    lexical_tokens,
    protected_feature_digest,
    validate_profile_feature_key,
)
from zotero_arxiv_daily.zotero.storage import ZoteroStore

_PROMPT_VERSION = "deterministic-digest-v4"
_SECRET_NAME = re.compile(r"^[A-Z][A-Z0-9_]{0,99}$")


class CommandRunner(Protocol):
    def run(self, command: list[str], payload: bytes) -> None: ...


class GhRunner:
    def run(self, command: list[str], payload: bytes) -> None:
        try:
            subprocess.run(command, input=payload, check=True, capture_output=True)
        except (OSError, subprocess.CalledProcessError) as error:
            raise ExternalServiceError(
                "GitHub Secret publication failed; verify gh authentication and repository access"
            ) from error


def build_cached_profiles(
    store: ZoteroStore,
    profile_feature_key: str,
    payload_budget: int = 30 * 1024,
    *,
    watched_authors: tuple[WatchedIdentity, ...] = (),
    watched_institutions: tuple[WatchedIdentity, ...] = (),
    curated_item_keys: frozenset[str] = frozenset(),
) -> tuple[LocalInterestProfile, RemoteServingProfile, int]:
    """Build the complete local profile and a keyed, allowlisted serving projection."""

    feature_key = validate_profile_feature_key(profile_feature_key)
    library_version = store.library_version
    if library_version is None:
        raise ConfigurationError("run profile sync before building a profile")
    sources = store.profile_sources()
    cache_hits = 0
    derived: list[tuple[str, str, str, tuple[str, ...]]] = []
    digests: list[ItemDigest] = []
    for item_key, _, payload, children in sources:
        cache_key = hashlib.sha256(
            (payload + "\0" + "\0".join(children)).encode("utf-8")
        ).hexdigest()
        cached = store.load_digest(cache_key, _PROMPT_VERSION)
        if cached is None:
            digest = make_digest(item_key, cache_key, payload, children)
            cached = json.dumps(asdict(digest), ensure_ascii=False, separators=(",", ":"))
            store.save_digest(cache_key, _PROMPT_VERSION, cached)
        else:
            cache_hits += 1
            digest = _digest_from_json(cached)
        derived.append((item_key, cache_key, payload, children))
        digests.append(digest)
    local = build_local_interest_profile(
        tuple(derived),
        library_version,
        observed_at=store.library_synced_at,
        curated_item_keys=curated_item_keys,
    )
    serving = project_serving_profile(
        local,
        feature_key,
        payload_budget,
    )
    serving = replace(
        serving,
        schema_version=REMOTE_SERVING_PROFILE_SCHEMA_VERSION,
        source_library_synced_at=(
            store.library_synced_at.isoformat() if store.library_synced_at is not None else None
        ),
        interest_prototypes=_build_protected_prototypes(digests, feature_key),
        watched_author_digests=_identity_digests(watched_authors, feature_key, "author"),
        watched_institution_digests=_identity_digests(
            watched_institutions, feature_key, "institution"
        ),
    )
    _enforce_budget(serving, payload_budget)
    return local, serving, cache_hits


def local_curated_item_keys(path: Path) -> frozenset[str]:
    """Read the optional ignored corpus ledger without making it a remote dependency."""

    return CorpusStore(path).positive_source_item_keys() if path.is_file() else frozenset()


def read_serving_profile(path: Path, payload_budget: int = 30 * 1024) -> RemoteServingProfile:
    """Revalidate an exported profile before it crosses the local/remote boundary."""

    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as error:
        raise ConfigurationError("remote profile file is unreadable") from error
    v1_fields = {
        "schema_version",
        "source_library_version",
        "topics",
        "core_categories",
        "adjacent_categories",
        "representative_terms",
        "representative_papers",
    }
    v2_fields = v1_fields | {"watched_authors", "watched_institutions"}
    v3_fields = v2_fields | {"source_library_synced_at"}
    v4_fields = v3_fields | {"preference_facets"}
    v5_fields = {
        "schema_version",
        "source_library_version",
        "core_categories",
        "adjacent_categories",
        "source_library_synced_at",
        "feature_hash_version",
        "feature_key_verifier",
        "lexical_features",
        "baseline_lexical_digests",
        "long_term_facets",
        "recent_facets",
        "interest_prototypes",
        "watched_author_digests",
        "watched_institution_digests",
    }
    if not isinstance(value, dict) or frozenset(value) not in {
        frozenset(v1_fields),
        frozenset(v2_fields),
        frozenset(v3_fields),
        frozenset(v4_fields),
        frozenset(v5_fields),
    }:
        raise ConfigurationError("remote profile contains unsupported fields")
    try:
        schema_version = _integer(value["schema_version"])
        if schema_version == 1 and set(value) != v1_fields:
            raise ValueError
        if schema_version == REMOTE_SERVING_PROFILE_SCHEMA_VERSION and set(value) != v5_fields:
            raise ValueError
        if schema_version == 2 and set(value) != v2_fields:
            raise ValueError
        if schema_version == 3 and set(value) != v3_fields:
            raise ValueError
        if schema_version == 4 and set(value) != v4_fields:
            raise ValueError
        profile = (
            _serving_profile_v5(value)
            if schema_version == REMOTE_SERVING_PROFILE_SCHEMA_VERSION
            else _legacy_serving_profile(value, schema_version)
        )
    except (KeyError, TypeError, ValueError, IndexError) as error:
        raise ConfigurationError("remote profile schema is invalid") from error
    if profile.schema_version == REMOTE_SERVING_PROFILE_SCHEMA_VERSION:
        validated = profile
    else:
        validated = replace(
            project_legacy_serving_profile(
                legacy_profile_to_local_interest(profile), payload_budget
            ),
            representative_papers=profile.representative_papers,
            watched_authors=profile.watched_authors,
            watched_institutions=profile.watched_institutions,
            source_library_synced_at=profile.source_library_synced_at,
            preference_facets=profile.preference_facets,
        )
    _enforce_budget(validated, payload_budget)
    return validated


def publish_github_profile_secrets(
    profile: RemoteServingProfile,
    profile_feature_key: str | None,
    repository: str,
    profile_secret_name: str,
    feature_key_secret_name: str,
    runner: CommandRunner | None = None,
) -> None:
    """Publish the profile and its separate matching key only through standard input."""

    if not _SECRET_NAME.fullmatch(profile_secret_name) or not _SECRET_NAME.fullmatch(
        feature_key_secret_name
    ):
        raise ConfigurationError(
            "GitHub secret name must contain only uppercase letters, digits, and underscores"
        )
    if profile_secret_name == feature_key_secret_name:
        raise ConfigurationError("profile and feature-key GitHub secrets must use different names")
    key: str | None = None
    if profile.schema_version == REMOTE_SERVING_PROFILE_SCHEMA_VERSION:
        key = validate_profile_feature_key(profile_feature_key)
        verifier = protected_feature_digest("serving-profile-key", key, namespace="key-verifier")
        if verifier != profile.feature_key_verifier:
            raise ConfigurationError("profile_feature_key does not match the serving profile")
    active_runner = runner or GhRunner()
    payload = json.dumps(
        serving_profile_payload(profile), ensure_ascii=False, separators=(",", ":")
    ).encode("utf-8")
    active_runner.run(["gh", "secret", "set", profile_secret_name, "--repo", repository], payload)
    if key is not None:
        active_runner.run(
            ["gh", "secret", "set", feature_key_secret_name, "--repo", repository],
            key.encode("utf-8"),
        )


def legacy_profile_to_local_interest(profile: RemoteServingProfile) -> LocalInterestProfile:
    return LocalInterestProfile(
        2,
        profile.source_library_version,
        tuple((term, 1.0) for term in profile.topics),
        (),
        tuple((category, 1.0, "validated") for category in profile.core_categories),
        0,
        profile.preference_facets,
    )


def _build_protected_prototypes(
    digests: list[ItemDigest], key: str
) -> tuple[ProtectedInterestPrototype, ...]:
    prototypes: list[ProtectedInterestPrototype] = []
    known: set[tuple[str, ...]] = set()
    ordered = sorted(
        digests,
        key=lambda item: (item.year is not None, item.year or 0, item.item_key),
        reverse=True,
    )
    for digest in ordered:
        tokens = frozenset(token for term in digest.terms for token in lexical_tokens(term))
        feature_digests = tuple(
            sorted(protected_feature_digest(token, key, namespace="lexical") for token in tokens)[
                :MAX_PROTOTYPE_FEATURES
            ]
        )
        if not feature_digests or feature_digests in known:
            continue
        known.add(feature_digests)
        prototypes.append(ProtectedInterestPrototype(feature_digests))
        if len(prototypes) >= MAX_INTEREST_PROTOTYPES:
            break
    return tuple(prototypes)


def _identity_digests(
    identities: tuple[WatchedIdentity, ...], key: str, namespace: str
) -> tuple[str, ...]:
    return tuple(
        sorted(
            {
                protected_feature_digest(normalize_identity(value), key, namespace=namespace)
                for identity in identities
                for value in (identity.name, *identity.aliases)
            }
        )
    )


def _legacy_serving_profile(value: dict[str, object], schema_version: int) -> RemoteServingProfile:
    return RemoteServingProfile(
        schema_version,
        _integer(value["source_library_version"]),
        _string_tuple(value["topics"]),
        _string_tuple(value["core_categories"]),
        _string_tuple(value["adjacent_categories"]),
        _string_tuple(value["representative_terms"]),
        _representative_papers(value["representative_papers"]),
        _watched_identities(value.get("watched_authors", [])),
        _watched_identities(value.get("watched_institutions", [])),
        _optional_snapshot(value.get("source_library_synced_at")),
        _preference_facets(value.get("preference_facets", [])),
    )


def _serving_profile_v5(value: dict[str, object]) -> RemoteServingProfile:
    long_term_facets = _preference_facets(value["long_term_facets"], maximum=12)
    recent_facets = _preference_facets(value["recent_facets"], maximum=8)
    return RemoteServingProfile(
        REMOTE_SERVING_PROFILE_SCHEMA_VERSION,
        _integer(value["source_library_version"]),
        (),
        _string_tuple(value["core_categories"]),
        _string_tuple(value["adjacent_categories"]),
        (),
        source_library_synced_at=_optional_snapshot(value["source_library_synced_at"]),
        preference_facets=long_term_facets + recent_facets,
        feature_hash_version=str(value["feature_hash_version"]),
        feature_key_verifier=_protected_digest(value["feature_key_verifier"]),
        lexical_features=_protected_lexical_features(value["lexical_features"]),
        baseline_lexical_digests=_protected_digests(value["baseline_lexical_digests"], maximum=30),
        long_term_facets=long_term_facets,
        recent_facets=recent_facets,
        interest_prototypes=_protected_interest_prototypes(value["interest_prototypes"]),
        watched_author_digests=_protected_digests(
            value["watched_author_digests"],
            maximum=MAX_WATCHED_IDENTITIES * (MAX_IDENTITY_ALIASES + 1),
        ),
        watched_institution_digests=_protected_digests(
            value["watched_institution_digests"],
            maximum=MAX_WATCHED_IDENTITIES * (MAX_IDENTITY_ALIASES + 1),
        ),
    )


def _digest_from_json(payload: str) -> ItemDigest:
    try:
        value = json.loads(payload)
        return ItemDigest(
            str(value["item_key"]),
            str(value["content_hash"]),
            tuple(str(item) for item in value["terms"]),
            tuple((str(item[0]), float(item[1])) for item in value["signal_weights"]),
            value.get("year"),
            tuple(str(item) for item in value.get("categories", [])),
            int(value.get("schema_version", 1)),
        )
    except (KeyError, TypeError, ValueError, IndexError) as error:
        raise ConfigurationError("cached item digest is invalid") from error


def _enforce_budget(profile: RemoteServingProfile, payload_budget: int) -> None:
    size = len(
        json.dumps(
            serving_profile_payload(profile), ensure_ascii=False, separators=(",", ":")
        ).encode("utf-8")
    )
    if size > payload_budget:
        raise ConfigurationError(
            f"serving profile is {size} bytes; budget is {payload_budget} bytes"
        )


def _watched_identities(value: object) -> tuple[WatchedIdentity, ...]:
    if not isinstance(value, list):
        raise ValueError
    identities: list[WatchedIdentity] = []
    for entry in value:
        if not isinstance(entry, dict) or set(entry) != {"name", "aliases"}:
            raise ValueError
        aliases = entry["aliases"]
        if not isinstance(entry["name"], str) or not isinstance(aliases, list):
            raise ValueError
        if not all(isinstance(alias, str) for alias in aliases):
            raise ValueError
        identities.append(WatchedIdentity(entry["name"], tuple(aliases)))
    return tuple(identities)


def _string_tuple(value: object) -> tuple[str, ...]:
    if not isinstance(value, list) or not all(isinstance(item, str) for item in value):
        raise ValueError
    return tuple(value)


def _integer(value: object) -> int:
    if not isinstance(value, int) or isinstance(value, bool):
        raise ValueError
    return value


def _representative_papers(
    value: object,
) -> tuple[tuple[int | None, tuple[str, ...], tuple[str, ...]], ...]:
    papers: list[tuple[int | None, tuple[str, ...], tuple[str, ...]]] = []
    for item in _sequence(value):
        if not isinstance(item, list) or len(item) != 3:
            raise ValueError
        year = item[0]
        if year is not None and (not isinstance(year, int) or isinstance(year, bool)):
            raise ValueError
        papers.append((year, _string_tuple(item[1]), _string_tuple(item[2])))
    return tuple(papers)


def _sequence(value: object) -> list[object]:
    if not isinstance(value, list):
        raise ValueError
    return value


def _optional_snapshot(value: object) -> str | None:
    if value is None:
        return None
    if not isinstance(value, str):
        raise ValueError
    instant = datetime.fromisoformat(value)
    if instant.tzinfo is None or instant.utcoffset() != UTC.utcoffset(instant):
        raise ValueError
    return instant.isoformat()


def _preference_facets(value: object, *, maximum: int = 20) -> tuple[PreferenceFacet, ...]:
    if not isinstance(value, list) or len(value) > maximum:
        raise ValueError
    facets: list[PreferenceFacet] = []
    for entry in value:
        if not isinstance(entry, dict) or set(entry) != {
            "kind",
            "value",
            "score",
            "confidence",
            "provenance",
        }:
            raise ValueError
        provenance = entry["provenance"]
        if not isinstance(provenance, list) or not all(
            isinstance(item, str) for item in provenance
        ):
            raise ValueError
        facets.append(
            PreferenceFacet(
                str(entry["kind"]),
                str(entry["value"]),
                float(entry["score"]),
                float(entry["confidence"]),
                tuple(provenance),
            )
        )
    return tuple(facets)


def _protected_lexical_features(value: object) -> tuple[ProtectedLexicalFeature, ...]:
    entries = _sequence(value)
    if len(entries) > MAX_PROTECTED_LEXICAL_FEATURES:
        raise ValueError
    features: list[ProtectedLexicalFeature] = []
    for entry in entries:
        if not isinstance(entry, dict) or set(entry) != {
            "digest",
            "long_term_weight",
            "recent_weight",
        }:
            raise ValueError
        digest = entry["digest"]
        long_term_weight = entry["long_term_weight"]
        recent_weight = entry["recent_weight"]
        if (
            not isinstance(digest, str)
            or not isinstance(long_term_weight, (int, float))
            or isinstance(long_term_weight, bool)
            or not isinstance(recent_weight, (int, float))
            or isinstance(recent_weight, bool)
        ):
            raise ValueError
        features.append(ProtectedLexicalFeature(digest, long_term_weight, recent_weight))
    return tuple(features)


def _protected_interest_prototypes(value: object) -> tuple[ProtectedInterestPrototype, ...]:
    entries = _sequence(value)
    if len(entries) > MAX_INTEREST_PROTOTYPES:
        raise ValueError
    prototypes: list[ProtectedInterestPrototype] = []
    for entry in entries:
        if not isinstance(entry, dict) or set(entry) != {"feature_digests"}:
            raise ValueError
        prototypes.append(
            ProtectedInterestPrototype(
                _protected_digests(entry["feature_digests"], maximum=MAX_PROTOTYPE_FEATURES)
            )
        )
    return tuple(prototypes)


def _protected_digests(value: object, *, maximum: int) -> tuple[str, ...]:
    values = _string_tuple(value)
    if len(values) > maximum or len(set(values)) != len(values):
        raise ValueError
    return values


def _protected_digest(value: object) -> str:
    if not isinstance(value, str):
        raise ValueError
    return validate_protected_digest(value)
