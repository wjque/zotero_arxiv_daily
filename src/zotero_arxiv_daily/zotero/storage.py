"""Transactional local SQLite state for normalized Zotero records."""

from __future__ import annotations

import hashlib
import json
import sqlite3
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import asdict
from pathlib import Path

from zotero_arxiv_daily.zotero.models import SyncBatch, SyncResult, ZoteroItem

_SCHEMA_VERSION = 1


class ZoteroStore:
    """Own a local-only normalized cache and preserve it on failed writes."""

    def __init__(self, path: Path) -> None:
        self.path = path

    def apply(self, batch: SyncBatch) -> SyncResult:
        """Atomically apply a full or incremental batch and advance version last."""

        mode = "incremental" if self.library_version is not None else "full"
        with self._connection() as connection, connection:
            items_written, unchanged = self._write_items(connection, batch.items)
            collections_written = self._write_collections(connection, batch)
            deleted = self._delete_items(connection, batch.deleted_item_keys)
            connection.execute(
                "INSERT OR REPLACE INTO metadata (key, value) VALUES ('library_version', ?)",
                (str(batch.library_version),),
            )
            if batch.local_api_version:
                connection.execute(
                    "INSERT OR REPLACE INTO metadata (key, value) VALUES ('local_api_version', ?)",
                    (batch.local_api_version,),
                )
        return SyncResult(
            mode, batch.library_version, items_written, unchanged, deleted, collections_written
        )

    @property
    def library_version(self) -> int | None:
        with self._connection() as connection:
            row = connection.execute(
                "SELECT value FROM metadata WHERE key = 'library_version'"
            ).fetchone()
        return int(row[0]) if row else None

    @property
    def local_api_version(self) -> str | None:
        with self._connection() as connection:
            row = connection.execute(
                "SELECT value FROM metadata WHERE key = 'local_api_version'"
            ).fetchone()
        return str(row[0]) if row else None

    def item_count(self) -> int:
        with self._connection() as connection:
            return int(connection.execute("SELECT COUNT(*) FROM items").fetchone()[0])

    def signal_parent(self, child_key: str) -> str | None:
        with self._connection() as connection:
            row = connection.execute(
                "SELECT parent_key FROM items WHERE key = ?", (child_key,)
            ).fetchone()
        return str(row[0]) if row and row[0] else None

    def collection_keys(self, item_key: str) -> tuple[str, ...]:
        """Return local collection membership for deterministic sync verification."""

        with self._connection() as connection:
            rows = connection.execute(
                "SELECT collection_key FROM item_collections "
                "WHERE item_key = ? ORDER BY collection_key",
                (item_key,),
            ).fetchall()
        return tuple(str(row[0]) for row in rows)

    @contextmanager
    def _connection(self) -> Iterator[sqlite3.Connection]:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        connection = sqlite3.connect(self.path)
        connection.execute("PRAGMA foreign_keys = ON")
        try:
            self._migrate(connection)
            connection.commit()
            yield connection
        finally:
            connection.close()

    def _migrate(self, connection: sqlite3.Connection) -> None:
        connection.execute(
            "CREATE TABLE IF NOT EXISTS schema_migrations (version INTEGER PRIMARY KEY)"
        )
        if connection.execute("SELECT 1 FROM schema_migrations WHERE version = 1").fetchone():
            return
        connection.executescript(
            """
            CREATE TABLE metadata (key TEXT PRIMARY KEY, value TEXT NOT NULL);
            CREATE TABLE items (
                key TEXT PRIMARY KEY, version INTEGER NOT NULL, item_type TEXT NOT NULL,
                parent_key TEXT, content_hash TEXT NOT NULL, payload TEXT NOT NULL,
                is_seed INTEGER NOT NULL, trashed INTEGER NOT NULL
            );
            CREATE TABLE collections (
                key TEXT PRIMARY KEY, version INTEGER NOT NULL, name TEXT NOT NULL, parent_key TEXT
            );
            CREATE TABLE item_collections (
                item_key TEXT NOT NULL, collection_key TEXT NOT NULL,
                PRIMARY KEY (item_key, collection_key),
                FOREIGN KEY (item_key) REFERENCES items(key) ON DELETE CASCADE
            );
            CREATE INDEX item_parent_index ON items(parent_key);
            """
        )
        connection.execute("INSERT INTO schema_migrations (version) VALUES (?)", (_SCHEMA_VERSION,))

    def _write_items(
        self, connection: sqlite3.Connection, items: tuple[ZoteroItem, ...]
    ) -> tuple[int, int]:
        written = unchanged = 0
        for item in items:
            payload = _item_payload(item)
            digest = _content_hash(payload)
            previous = connection.execute(
                "SELECT content_hash FROM items WHERE key = ?", (item.key,)
            ).fetchone()
            if previous and previous[0] == digest:
                unchanged += 1
                continue
            connection.execute(
                "INSERT OR REPLACE INTO items VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    item.key,
                    item.version,
                    item.item_type,
                    item.parent_key,
                    digest,
                    payload,
                    int(item.is_seed),
                    int(item.trashed),
                ),
            )
            connection.execute("DELETE FROM item_collections WHERE item_key = ?", (item.key,))
            connection.executemany(
                "INSERT INTO item_collections VALUES (?, ?)",
                ((item.key, key) for key in item.collections),
            )
            written += 1
        return written, unchanged

    def _write_collections(self, connection: sqlite3.Connection, batch: SyncBatch) -> int:
        written = 0
        for collection in batch.collections:
            previous = connection.execute(
                "SELECT version FROM collections WHERE key = ?", (collection.key,)
            ).fetchone()
            if previous and previous[0] == collection.version:
                continue
            connection.execute(
                "INSERT OR REPLACE INTO collections VALUES (?, ?, ?, ?)",
                (collection.key, collection.version, collection.name, collection.parent_key),
            )
            written += 1
        return written

    def _delete_items(self, connection: sqlite3.Connection, keys: tuple[str, ...]) -> int:
        deleted = 0
        for key in keys:
            deleted += connection.execute("DELETE FROM items WHERE key = ?", (key,)).rowcount
        return deleted


def _item_payload(item: ZoteroItem) -> str:
    return json.dumps(asdict(item), ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def _content_hash(payload: str) -> str:
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()
