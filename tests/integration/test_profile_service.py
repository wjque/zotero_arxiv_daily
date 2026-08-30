from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path

import pytest

from zotero_arxiv_daily.core.errors import ConfigurationError
from zotero_arxiv_daily.profile.models import WatchedIdentity
from zotero_arxiv_daily.profile.service import (
    build_cached_profiles,
    publish_github_profile_secrets,
)
from zotero_arxiv_daily.zotero.models import SyncBatch, ZoteroItem
from zotero_arxiv_daily.zotero.storage import ZoteroStore

_FEATURE_KEY = "test-profile-feature-key-0000000000000001"


class RecordingRunner:
    def __init__(self) -> None:
        self.calls: list[tuple[list[str], bytes]] = []

    def run(self, command: list[str], payload: bytes) -> None:
        self.calls.append((command, payload))


def test_profile_digest_cache_reuses_unchanged_local_content(tmp_path: Path) -> None:
    store = ZoteroStore(tmp_path / "state.sqlite3")
    item = ZoteroItem(
        "PAPER001", 1, "journalArticle", None, "Neural learning", (), (), (), (), "", None, False
    )
    store.apply(SyncBatch(1, (item,), ()))

    first_local, first_serving, first_hits = build_cached_profiles(store, _FEATURE_KEY)
    second_local, second_serving, second_hits = build_cached_profiles(store, _FEATURE_KEY)

    assert first_local == second_local
    assert first_serving == second_serving
    assert first_hits == 0
    assert second_hits == 1


def test_profile_snapshot_uses_the_successful_local_sync_instant(tmp_path: Path) -> None:
    store = ZoteroStore(tmp_path / "state.sqlite3")
    instant = datetime(2026, 8, 2, 12, tzinfo=UTC)
    item = ZoteroItem(
        "PAPER001", 1, "journalArticle", None, "Neural learning", (), (), (), (), "", None, False
    )
    store.apply(SyncBatch(1, (item,), ()), synced_at=instant)

    local, serving, _ = build_cached_profiles(store, _FEATURE_KEY)

    assert store.library_synced_at == instant
    assert local.source_library_version == 1
    assert serving.source_library_synced_at == "2026-08-02T12:00:00+00:00"


def test_github_publication_separates_profile_and_matching_key_on_standard_input(
    tmp_path: Path,
) -> None:
    store = ZoteroStore(tmp_path / "state.sqlite3")
    item = ZoteroItem(
        "PAPER001", 1, "journalArticle", None, "Quantum methods", (), (), (), (), "", None, False
    )
    store.apply(SyncBatch(1, (item,), ()))
    _local, profile, _ = build_cached_profiles(
        store,
        _FEATURE_KEY,
        watched_authors=(WatchedIdentity("Private Researcher"),),
    )
    runner = RecordingRunner()

    publish_github_profile_secrets(
        profile,
        _FEATURE_KEY,
        "owner/repository",
        "ZOTERO_ARXIV_DAILY_PROFILE",
        "ZAD_PROFILE_FEATURE_KEY",
        runner,
    )

    assert runner.calls[0][0] == [
        "gh",
        "secret",
        "set",
        "ZOTERO_ARXIV_DAILY_PROFILE",
        "--repo",
        "owner/repository",
    ]
    assert runner.calls[1][0] == [
        "gh",
        "secret",
        "set",
        "ZAD_PROFILE_FEATURE_KEY",
        "--repo",
        "owner/repository",
    ]
    profile_payload = runner.calls[0][1]
    assert _FEATURE_KEY.encode() not in profile_payload
    assert b"Quantum methods" not in profile_payload
    assert b"Private Researcher" not in profile_payload
    assert runner.calls[1][1] == _FEATURE_KEY.encode()


def test_github_publication_preflights_key_pair_before_changing_either_secret(
    tmp_path: Path,
) -> None:
    store = ZoteroStore(tmp_path / "state.sqlite3")
    item = ZoteroItem(
        "PAPER001", 1, "journalArticle", None, "Learning", (), (), (), (), "", None, False
    )
    store.apply(SyncBatch(1, (item,), ()))
    _local, profile, _ = build_cached_profiles(store, _FEATURE_KEY)
    runner = RecordingRunner()

    with pytest.raises(ConfigurationError, match="does not match"):
        publish_github_profile_secrets(
            profile,
            "wrong-profile-feature-key-00000000000000001",
            "owner/repository",
            "ZOTERO_ARXIV_DAILY_PROFILE",
            "ZAD_PROFILE_FEATURE_KEY",
            runner,
        )
    with pytest.raises(ConfigurationError, match="different names"):
        publish_github_profile_secrets(
            profile,
            _FEATURE_KEY,
            "owner/repository",
            "SAME_SECRET",
            "SAME_SECRET",
            runner,
        )

    assert runner.calls == []
