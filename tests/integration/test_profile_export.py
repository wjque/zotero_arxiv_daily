from __future__ import annotations

import json
from pathlib import Path

import pytest

from zotero_arxiv_daily.core.errors import ConfigurationError
from zotero_arxiv_daily.profile.build import (
    build_local_interest_profile,
    project_serving_profile,
)
from zotero_arxiv_daily.profile.export import (
    serving_profile_payload,
    write_local_interest_profile,
    write_serving_profile,
)
from zotero_arxiv_daily.profile.models import PreferenceFacet, RemoteServingProfile, WatchedIdentity
from zotero_arxiv_daily.profile.service import read_serving_profile

_FEATURE_KEY = "test-profile-feature-key-0000000000000001"


def test_remote_profile_export_is_compact_atomic_json_with_owner_permissions(
    tmp_path: Path,
) -> None:
    target = tmp_path / "remote.json"
    write_serving_profile(
        RemoteServingProfile(1, 3, ("learning",), ("cs.LG",), ("stat.ML",), ("learning",)), target
    )

    assert json.loads(target.read_text(encoding="utf-8"))["topics"] == ["learning"]
    assert target.stat().st_mode & 0o077 == 0


def test_remote_profile_read_rejects_unallowlisted_fields(tmp_path: Path) -> None:
    target = tmp_path / "remote.json"
    target.write_text('{"raw_note":"must not cross boundary"}', encoding="utf-8")

    with pytest.raises(ConfigurationError, match="unsupported fields"):
        read_serving_profile(target)


def test_remote_profile_v1_and_v2_migrate_to_v4_preference_schema(tmp_path: Path) -> None:
    legacy = tmp_path / "legacy.json"
    write_serving_profile(
        RemoteServingProfile(1, 3, ("learning",), ("cs.LG",), (), ("learning",)), legacy
    )
    migrated = read_serving_profile(legacy)

    assert migrated.schema_version == 4
    assert migrated.watched_authors == ()
    assert migrated.source_library_synced_at is None

    current = tmp_path / "current.json"
    write_serving_profile(
        RemoteServingProfile(
            2,
            3,
            ("learning",),
            ("cs.LG",),
            (),
            ("learning",),
            watched_authors=(WatchedIdentity("Fei-Fei Li", ("Li Fei-Fei",)),),
        ),
        current,
    )
    loaded = read_serving_profile(current)
    assert loaded.schema_version == 4
    assert loaded.watched_authors[0].name == "Fei-Fei Li"
    assert loaded.source_library_synced_at is None
    assert loaded.preference_facets == ()


def test_remote_profile_v3_round_trips_a_timezone_aware_snapshot(tmp_path: Path) -> None:
    current = tmp_path / "current.json"
    write_serving_profile(
        RemoteServingProfile(
            3,
            3,
            ("learning",),
            ("cs.LG",),
            (),
            ("learning",),
            source_library_synced_at="2026-08-02T12:00:00+00:00",
        ),
        current,
    )

    assert read_serving_profile(current).source_library_synced_at == "2026-08-02T12:00:00+00:00"


def test_remote_profile_v4_round_trips_only_bounded_derived_facets(tmp_path: Path) -> None:
    current = tmp_path / "current.json"
    write_serving_profile(
        RemoteServingProfile(
            4,
            3,
            ("learning",),
            ("cs.LG",),
            (),
            ("learning",),
            preference_facets=(
                PreferenceFacet("method", "transformers", 0.8, 0.8, ("local-derived",)),
            ),
        ),
        current,
    )

    loaded = read_serving_profile(current)

    assert loaded.schema_version == 4
    assert loaded.preference_facets[0].value == "transformers"


def test_serving_profile_v5_round_trips_only_the_protected_allowlist(tmp_path: Path) -> None:
    raw = json.dumps(
        {
            "title": "Private quantum retrieval project",
            "abstract": "Transformer methods",
            "tags": [["confidential-interest", True]],
        }
    )
    local = build_local_interest_profile((("PRIVATE_ITEM", "hash", raw, ()),), 7)
    serving = project_serving_profile(local, _FEATURE_KEY)
    target = tmp_path / "serving.json"

    write_serving_profile(serving, target)
    loaded = read_serving_profile(target)
    serialized = target.read_text(encoding="utf-8")

    assert loaded == serving
    assert set(json.loads(serialized)) == set(serving_profile_payload(serving))
    assert "PRIVATE_ITEM" not in serialized
    assert "confidential-interest" not in serialized
    assert "Private quantum retrieval project" not in serialized
    assert "topics" not in json.loads(serialized)
    assert target.stat().st_mode & 0o077 == 0


def test_complete_local_profile_is_stored_separately_with_owner_permissions(
    tmp_path: Path,
) -> None:
    raw = json.dumps({"title": "Local private interest", "tags": []})
    local = build_local_interest_profile((("PRIVATE_ITEM", "hash", raw, ()),), 7)
    target = tmp_path / "local-interest.json"

    write_local_interest_profile(local, target)

    serialized = target.read_text(encoding="utf-8")
    assert "private" in serialized
    assert "PRIVATE_ITEM" not in serialized
    assert target.stat().st_mode & 0o077 == 0


def test_serving_profile_v5_rejects_a_malformed_key_verifier(tmp_path: Path) -> None:
    raw = json.dumps({"title": "Learning methods", "tags": []})
    profile = project_serving_profile(
        build_local_interest_profile((("PAPER001", "hash", raw, ()),), 1), _FEATURE_KEY
    )
    payload = serving_profile_payload(profile)
    payload["feature_key_verifier"] = "plaintext"
    target = tmp_path / "invalid.json"
    target.write_text(json.dumps(payload), encoding="utf-8")

    with pytest.raises(ConfigurationError, match="schema is invalid"):
        read_serving_profile(target)


def test_serving_profile_v5_rejects_unbounded_or_malformed_categories(tmp_path: Path) -> None:
    raw = json.dumps({"title": "Learning methods", "tags": []})
    profile = project_serving_profile(
        build_local_interest_profile((("PAPER001", "hash", raw, ()),), 1), _FEATURE_KEY
    )
    payload = serving_profile_payload(profile)
    payload["core_categories"] = ["invalid category"]
    target = tmp_path / "invalid-category.json"
    target.write_text(json.dumps(payload), encoding="utf-8")

    with pytest.raises(ConfigurationError, match="schema is invalid"):
        read_serving_profile(target)
