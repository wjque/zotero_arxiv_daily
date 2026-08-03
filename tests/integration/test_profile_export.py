from __future__ import annotations

import json
from pathlib import Path

import pytest

from zotero_arxiv_daily.core.errors import ConfigurationError
from zotero_arxiv_daily.profile.export import write_remote_profile
from zotero_arxiv_daily.profile.models import PreferenceFacet, RemoteProfile, WatchedIdentity
from zotero_arxiv_daily.profile.service import read_remote_profile


def test_remote_profile_export_is_compact_atomic_json_with_owner_permissions(
    tmp_path: Path,
) -> None:
    target = tmp_path / "remote.json"
    write_remote_profile(
        RemoteProfile(1, 3, ("learning",), ("cs.LG",), ("stat.ML",), ("learning",)), target
    )

    assert json.loads(target.read_text(encoding="utf-8"))["topics"] == ["learning"]
    assert target.stat().st_mode & 0o077 == 0


def test_remote_profile_read_rejects_unallowlisted_fields(tmp_path: Path) -> None:
    target = tmp_path / "remote.json"
    target.write_text('{"raw_note":"must not cross boundary"}', encoding="utf-8")

    with pytest.raises(ConfigurationError, match="unsupported fields"):
        read_remote_profile(target)


def test_remote_profile_v1_and_v2_migrate_to_v4_preference_schema(tmp_path: Path) -> None:
    legacy = tmp_path / "legacy.json"
    write_remote_profile(RemoteProfile(1, 3, ("learning",), ("cs.LG",), (), ("learning",)), legacy)
    migrated = read_remote_profile(legacy)

    assert migrated.schema_version == 4
    assert migrated.watched_authors == ()
    assert migrated.source_library_synced_at is None

    current = tmp_path / "current.json"
    write_remote_profile(
        RemoteProfile(
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
    loaded = read_remote_profile(current)
    assert loaded.schema_version == 4
    assert loaded.watched_authors[0].name == "Fei-Fei Li"
    assert loaded.source_library_synced_at is None
    assert loaded.preference_facets == ()


def test_remote_profile_v3_round_trips_a_timezone_aware_snapshot(tmp_path: Path) -> None:
    current = tmp_path / "current.json"
    write_remote_profile(
        RemoteProfile(
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

    assert read_remote_profile(current).source_library_synced_at == "2026-08-02T12:00:00+00:00"


def test_remote_profile_v4_round_trips_only_bounded_derived_facets(tmp_path: Path) -> None:
    current = tmp_path / "current.json"
    write_remote_profile(
        RemoteProfile(
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

    loaded = read_remote_profile(current)

    assert loaded.schema_version == 4
    assert loaded.preference_facets[0].value == "transformers"
