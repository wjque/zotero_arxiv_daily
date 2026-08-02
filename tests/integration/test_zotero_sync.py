from __future__ import annotations

from pathlib import Path

from zotero_arxiv_daily.zotero.models import SyncBatch, ZoteroCollection, ZoteroItem
from zotero_arxiv_daily.zotero.storage import ZoteroStore
from zotero_arxiv_daily.zotero.sync import synchronize


class BatchClient:
    def __init__(self, batches: list[SyncBatch]) -> None:
        self.batches = batches
        self.requested_versions: list[int | None] = []

    def fetch(self, since: int | None) -> SyncBatch:
        self.requested_versions.append(since)
        return self.batches.pop(0)


def _paper(version: int = 1) -> ZoteroItem:
    return ZoteroItem(
        "PAPER001",
        version,
        "journalArticle",
        None,
        "Synthetic",
        (),
        (),
        ("COLL0001",),
        (),
        "",
        None,
        False,
    )


def _note() -> ZoteroItem:
    return ZoteroItem(
        "NOTE0001", 1, "note", "PAPER001", "", (), (), (), (), "", None, False, note_text="local"
    )


def test_full_then_unchanged_incremental_sync_is_idempotent_and_keeps_parent_ownership(
    tmp_path: Path,
) -> None:
    batch = SyncBatch(4, (_paper(), _note()), (ZoteroCollection("COLL0001", 1, "Synthetic", None),))
    client = BatchClient([batch, batch])
    store = ZoteroStore(tmp_path / "state.sqlite3")

    first = synchronize(client, store)
    second = synchronize(client, store)

    assert first.mode == "full"
    assert first.items_written == 2
    assert second.mode == "incremental"
    assert second.items_written == 0
    assert second.items_unchanged == 2
    assert client.requested_versions == [None, 4]
    assert store.item_count() == 2
    assert store.signal_parent("NOTE0001") == "PAPER001"


def test_incremental_delete_removes_the_local_record(tmp_path: Path) -> None:
    client = BatchClient([SyncBatch(1, (_paper(),), ()), SyncBatch(2, (), (), ("PAPER001",))])
    store = ZoteroStore(tmp_path / "state.sqlite3")

    synchronize(client, store)
    result = synchronize(client, store)

    assert result.items_deleted == 1
    assert store.item_count() == 0
    assert store.library_version == 2


def test_complete_snapshot_reconciles_records_when_incremental_tombstones_are_unavailable(
    tmp_path: Path,
) -> None:
    client = BatchClient(
        [
            SyncBatch(1, (_paper(),), (ZoteroCollection("COLL0001", 1, "Old", None),)),
            SyncBatch(2, (), (), complete_snapshot=True),
        ]
    )
    store = ZoteroStore(tmp_path / "state.sqlite3")

    synchronize(client, store)
    result = synchronize(client, store)

    assert result.mode == "full"
    assert result.items_deleted == 1
    assert store.item_count() == 0


def test_incremental_edit_and_collection_move_replace_local_state(tmp_path: Path) -> None:
    edited = ZoteroItem(
        "PAPER001",
        2,
        "journalArticle",
        None,
        "Edited synthetic",
        (),
        (),
        ("COLL0002",),
        (),
        "",
        None,
        False,
    )
    client = BatchClient(
        [
            SyncBatch(1, (_paper(),), (ZoteroCollection("COLL0001", 1, "Old", None),)),
            SyncBatch(2, (edited,), (ZoteroCollection("COLL0002", 1, "New", None),)),
        ]
    )
    store = ZoteroStore(tmp_path / "state.sqlite3")

    synchronize(client, store)
    result = synchronize(client, store)

    assert result.items_written == 1
    assert store.collection_keys("PAPER001") == ("COLL0002",)
