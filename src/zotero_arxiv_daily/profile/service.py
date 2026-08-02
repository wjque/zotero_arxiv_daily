"""Cached profile use cases and protected GitHub Secret publication."""

from __future__ import annotations

import hashlib
import json
import re
import subprocess
from dataclasses import asdict, replace
from pathlib import Path
from typing import Protocol

from zotero_arxiv_daily.core.errors import ConfigurationError, ExternalServiceError
from zotero_arxiv_daily.profile.build import build_profile, make_digest, project_remote
from zotero_arxiv_daily.profile.models import (
    REMOTE_PROFILE_SCHEMA_VERSION,
    InterestProfile,
    ItemDigest,
    RemoteProfile,
    WatchedIdentity,
)
from zotero_arxiv_daily.zotero.storage import ZoteroStore

_PROMPT_VERSION = "deterministic-digest-v1"
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


def build_cached_remote_profile(
    store: ZoteroStore,
    payload_budget: int = 30 * 1024,
    *,
    watched_authors: tuple[WatchedIdentity, ...] = (),
    watched_institutions: tuple[WatchedIdentity, ...] = (),
) -> tuple[RemoteProfile, int]:
    """Build only changed local digests and return an allowlisted remote projection."""

    library_version = store.library_version
    if library_version is None:
        raise ConfigurationError("run profile sync before building a profile")
    sources = store.profile_sources()
    cache_hits = 0
    derived: list[tuple[str, str, str, tuple[str, ...]]] = []
    digests: list[ItemDigest] = []
    for key, _, payload, children in sources:
        cache_key = hashlib.sha256(
            (payload + "\0" + "\0".join(children)).encode("utf-8")
        ).hexdigest()
        cached = store.load_digest(cache_key, _PROMPT_VERSION)
        if cached is None:
            digest = make_digest(key, cache_key, payload, children)
            cached = json.dumps(asdict(digest), ensure_ascii=False, separators=(",", ":"))
            store.save_digest(cache_key, _PROMPT_VERSION, cached)
        else:
            cache_hits += 1
            digest = _digest_from_json(cached)
        derived.append((key, cache_key, payload, children))
        digests.append(digest)
    remote = project_remote(build_profile(tuple(derived), library_version), payload_budget)
    representatives = tuple(
        (digest.year, digest.terms[:5], digest.categories)
        for digest in sorted(
            digests, key=lambda item: (item.year is None, item.year, item.item_key), reverse=True
        )[:8]
    )
    remote = replace(
        remote,
        schema_version=REMOTE_PROFILE_SCHEMA_VERSION,
        representative_papers=representatives,
        watched_authors=watched_authors,
        watched_institutions=watched_institutions,
    )
    _enforce_budget(remote, payload_budget)
    return remote, cache_hits


def read_remote_profile(path: Path, payload_budget: int = 30 * 1024) -> RemoteProfile:
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
    if not isinstance(value, dict) or frozenset(value) not in {
        frozenset(v1_fields),
        frozenset(v2_fields),
    }:
        raise ConfigurationError("remote profile contains unsupported fields")
    try:
        schema_version = int(value["schema_version"])
        if schema_version == 1 and set(value) != v1_fields:
            raise ValueError
        if schema_version == REMOTE_PROFILE_SCHEMA_VERSION and set(value) != v2_fields:
            raise ValueError
        profile = RemoteProfile(
            schema_version,
            int(value["source_library_version"]),
            _string_tuple(value["topics"]),
            _string_tuple(value["core_categories"]),
            _string_tuple(value["adjacent_categories"]),
            _string_tuple(value["representative_terms"]),
            tuple(
                (item[0], _string_tuple(item[1]), _string_tuple(item[2]))
                for item in value["representative_papers"]
            ),
            _watched_identities(value.get("watched_authors", [])),
            _watched_identities(value.get("watched_institutions", [])),
        )
    except (KeyError, TypeError, ValueError, IndexError) as error:
        raise ConfigurationError("remote profile schema is invalid") from error
    validated = replace(
        project_remote(profile_to_interest(profile), payload_budget),
        schema_version=REMOTE_PROFILE_SCHEMA_VERSION,
        representative_papers=profile.representative_papers,
        watched_authors=profile.watched_authors,
        watched_institutions=profile.watched_institutions,
    )
    _enforce_budget(validated, payload_budget)
    return validated


def publish_github_secret(
    profile: RemoteProfile, repository: str, secret_name: str, runner: CommandRunner | None = None
) -> None:
    """Publish a revalidated payload through stdin, never a command-line secret argument."""

    if not _SECRET_NAME.fullmatch(secret_name):
        raise ConfigurationError(
            "GitHub secret name must contain only uppercase letters, digits, and underscores"
        )
    payload = json.dumps(asdict(profile), ensure_ascii=False, separators=(",", ":")).encode("utf-8")
    (runner or GhRunner()).run(["gh", "secret", "set", secret_name, "--repo", repository], payload)


def profile_to_interest(profile: RemoteProfile) -> InterestProfile:
    return InterestProfile(
        1,
        profile.source_library_version,
        tuple((term, 1.0) for term in profile.topics),
        (),
        tuple((category, 1.0, "validated") for category in profile.core_categories),
        0,
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


def _enforce_budget(profile: RemoteProfile, payload_budget: int) -> None:
    size = len(
        json.dumps(asdict(profile), ensure_ascii=False, separators=(",", ":")).encode("utf-8")
    )
    if size > payload_budget:
        raise ConfigurationError(
            f"remote profile is {size} bytes; budget is {payload_budget} bytes"
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
