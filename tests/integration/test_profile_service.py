from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path

from zotero_arxiv_daily.profile.service import build_cached_remote_profile, publish_github_secret
from zotero_arxiv_daily.zotero.models import SyncBatch, ZoteroItem
from zotero_arxiv_daily.zotero.storage import ZoteroStore


class RecordingRunner:
    def __init__(self) -> None:
        self.command: list[str] | None = None
        self.payload: bytes | None = None

    def run(self, command: list[str], payload: bytes) -> None:
        self.command = command
        self.payload = payload


def test_profile_digest_cache_reuses_unchanged_local_content(tmp_path: Path) -> None:
    store = ZoteroStore(tmp_path / "state.sqlite3")
    item = ZoteroItem(
        "PAPER001", 1, "journalArticle", None, "Neural learning", (), (), (), (), "", None, False
    )
    store.apply(SyncBatch(1, (item,), ()))

    first, first_hits = build_cached_remote_profile(store)
    second, second_hits = build_cached_remote_profile(store)

    assert first == second
    assert first_hits == 0
    assert second_hits == 1


def test_profile_snapshot_uses_the_successful_local_sync_instant(tmp_path: Path) -> None:
    store = ZoteroStore(tmp_path / "state.sqlite3")
    instant = datetime(2026, 8, 2, 12, tzinfo=UTC)
    item = ZoteroItem(
        "PAPER001", 1, "journalArticle", None, "Neural learning", (), (), (), (), "", None, False
    )
    store.apply(SyncBatch(1, (item,), ()), synced_at=instant)

    profile, _ = build_cached_remote_profile(store)

    assert store.library_synced_at == instant
    assert profile.source_library_synced_at == "2026-08-02T12:00:00+00:00"


def test_github_publication_passes_profile_only_through_standard_input(tmp_path: Path) -> None:
    store = ZoteroStore(tmp_path / "state.sqlite3")
    item = ZoteroItem(
        "PAPER001", 1, "journalArticle", None, "Quantum methods", (), (), (), (), "", None, False
    )
    store.apply(SyncBatch(1, (item,), ()))
    profile, _ = build_cached_remote_profile(store)
    runner = RecordingRunner()

    publish_github_secret(profile, "owner/repository", "ZOTERO_ARXIV_DAILY_PROFILE", runner)

    assert runner.command == [
        "gh",
        "secret",
        "set",
        "ZOTERO_ARXIV_DAILY_PROFILE",
        "--repo",
        "owner/repository",
    ]
    assert runner.payload is not None
    assert b"Quantum methods" not in runner.payload
