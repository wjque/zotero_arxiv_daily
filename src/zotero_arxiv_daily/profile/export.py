"""Atomic local export for privacy-bounded remote profile payloads."""

from __future__ import annotations

import json
import os
import tempfile
from dataclasses import asdict
from pathlib import Path

from zotero_arxiv_daily.profile.models import (
    REMOTE_SERVING_PROFILE_SCHEMA_VERSION,
    LocalInterestProfile,
    RemoteServingProfile,
)


def write_serving_profile(profile: RemoteServingProfile, path: Path) -> None:
    """Write allowlisted data atomically with owner-only permissions when supported."""

    _write_json(serving_profile_payload(profile), path)


def write_local_interest_profile(profile: LocalInterestProfile, path: Path) -> None:
    """Persist the complete derived local profile without crossing the remote boundary."""

    _write_json(asdict(profile), path)


def serving_profile_payload(profile: RemoteServingProfile) -> dict[str, object]:
    """Return the exact versioned allowlist that may enter protected remote state."""

    payload = asdict(profile)
    if profile.schema_version == REMOTE_SERVING_PROFILE_SCHEMA_VERSION:
        return {
            "schema_version": payload["schema_version"],
            "source_library_version": payload["source_library_version"],
            "core_categories": payload["core_categories"],
            "adjacent_categories": payload["adjacent_categories"],
            "source_library_synced_at": payload["source_library_synced_at"],
            "feature_hash_version": payload["feature_hash_version"],
            "feature_key_verifier": payload["feature_key_verifier"],
            "lexical_features": payload["lexical_features"],
            "baseline_lexical_digests": payload["baseline_lexical_digests"],
            "long_term_facets": payload["long_term_facets"],
            "recent_facets": payload["recent_facets"],
            "interest_prototypes": payload["interest_prototypes"],
            "watched_author_digests": payload["watched_author_digests"],
            "watched_institution_digests": payload["watched_institution_digests"],
        }
    if profile.schema_version == 1:
        for field in (
            "watched_authors",
            "watched_institutions",
            "source_library_synced_at",
            "preference_facets",
        ):
            payload.pop(field)
    elif profile.schema_version == 2:
        payload.pop("source_library_synced_at")
        payload.pop("preference_facets")
    elif profile.schema_version == 3:
        payload.pop("preference_facets")
    for field in (
        "feature_hash_version",
        "feature_key_verifier",
        "lexical_features",
        "baseline_lexical_digests",
        "long_term_facets",
        "recent_facets",
        "interest_prototypes",
        "watched_author_digests",
        "watched_institution_digests",
    ):
        payload.pop(field)
    return payload


def _write_json(payload: dict[str, object], path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary_name = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    temporary_path = Path(temporary_name)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as output:
            json.dump(payload, output, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
            output.flush()
            os.fsync(output.fileno())
        os.chmod(temporary_path, 0o600)
        os.replace(temporary_path, path)
    finally:
        temporary_path.unlink(missing_ok=True)
