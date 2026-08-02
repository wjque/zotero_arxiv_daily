"""Validated local Zotero records and derived interest signals."""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True, slots=True)
class ZoteroItem:
    key: str
    version: int
    item_type: str
    parent_key: str | None
    title: str
    creators: tuple[str, ...]
    tags: tuple[tuple[str, bool], ...]
    collections: tuple[str, ...]
    identifiers: tuple[str, ...]
    abstract: str
    date: str | None
    trashed: bool
    note_text: str | None = None
    annotation_text: str | None = None
    annotation_comment: str | None = None

    @property
    def is_seed(self) -> bool:
        return not self.trashed and self.item_type not in {"attachment", "note", "annotation"}


@dataclass(frozen=True, slots=True)
class ZoteroCollection:
    key: str
    version: int
    name: str
    parent_key: str | None


@dataclass(frozen=True, slots=True)
class SyncBatch:
    library_version: int
    items: tuple[ZoteroItem, ...]
    collections: tuple[ZoteroCollection, ...]
    deleted_item_keys: tuple[str, ...] = ()
    local_api_version: str | None = None
    complete_snapshot: bool = False


@dataclass(frozen=True, slots=True)
class SyncResult:
    mode: str
    library_version: int
    items_written: int
    items_unchanged: int
    items_deleted: int
    collections_written: int
