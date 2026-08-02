"""Transactional local SQLite state for normalized Zotero records."""

from __future__ import annotations

import hashlib
import json
import sqlite3
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import asdict
from datetime import UTC, datetime
from pathlib import Path

from zotero_arxiv_daily.zotero.models import SyncBatch, SyncResult, ZoteroItem

_SCHEMA_VERSION = 2


class ZoteroStore:
    """Own a local-only normalized cache and preserve it on failed writes."""

    def __init__(self, path: Path) -> None:
        self.path = path

    def apply(self, batch: SyncBatch, *, synced_at: datetime | None = None) -> SyncResult:
        """Atomically apply a full or incremental batch and advance version last."""

        mode = "full" if batch.complete_snapshot or self.library_version is None else "incremental"
        completed_at = synced_at or datetime.now(UTC)
        if completed_at.tzinfo is None or completed_at.utcoffset() is None:
            raise ValueError("synced_at must be timezone-aware")
        with self._connection() as connection, connection:
            items_written, unchanged = self._write_items(connection, batch.items)
            collections_written = self._write_collections(connection, batch)
            deleted = self._delete_items(connection, batch.deleted_item_keys)
            if batch.complete_snapshot:
                deleted += self._delete_missing_items(
                    connection, frozenset(item.key for item in batch.items)
                )
                self._delete_missing_collections(
                    connection, frozenset(collection.key for collection in batch.collections)
                )
            connection.execute(
                "INSERT OR REPLACE INTO metadata (key, value) VALUES ('library_version', ?)",
                (str(batch.library_version),),
            )
            connection.execute(
                "INSERT OR REPLACE INTO metadata (key, value) VALUES ('library_synced_at', ?)",
                (completed_at.astimezone(UTC).isoformat(),),
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

    @property
    def library_synced_at(self) -> datetime | None:
        with self._connection() as connection:
            row = connection.execute(
                "SELECT value FROM metadata WHERE key = 'library_synced_at'"
            ).fetchone()
        if row is None:
            return None
        value = datetime.fromisoformat(str(row[0]))
        if value.tzinfo is None or value.utcoffset() is None:
            raise ValueError("stored library_synced_at must be timezone-aware")
        return value.astimezone(UTC)

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

    def profile_sources(self) -> tuple[tuple[str, str, str, tuple[str, ...]], ...]:
        """Return local-only seed payloads and their child signals for profile construction."""

        with self._connection() as connection:
            roots = connection.execute(
                "SELECT key, content_hash, payload FROM items WHERE is_seed = 1 ORDER BY key"
            ).fetchall()
            rows: list[tuple[str, str, str, tuple[str, ...]]] = []
            for key, content_hash, payload in roots:
                children = connection.execute(
                    "SELECT payload FROM items WHERE parent_key = ? AND trashed = 0 ORDER BY key",
                    (key,),
                ).fetchall()
                rows.append(
                    (
                        str(key),
                        str(content_hash),
                        str(payload),
                        tuple(str(row[0]) for row in children),
                    )
                )
        return tuple(rows)

    def known_identifiers(self) -> frozenset[str]:
        """Expose normalized local identifiers for later public-candidate exclusion."""

        with self._connection() as connection:
            payloads = connection.execute("SELECT payload FROM items WHERE is_seed = 1").fetchall()
        identifiers: set[str] = set()
        for (payload,) in payloads:
            value = json.loads(str(payload))
            if isinstance(value, dict):
                identifiers.update(
                    str(item).casefold() for item in value.get("identifiers", []) if item
                )
        return frozenset(identifiers)

    def load_digest(self, cache_key: str, prompt_version: str) -> str | None:
        """Return a local derived digest by content hash and deterministic prompt version."""

        with self._connection() as connection:
            row = connection.execute(
                "SELECT payload FROM item_digests WHERE cache_key = ? AND prompt_version = ?",
                (cache_key, prompt_version),
            ).fetchone()
        return str(row[0]) if row else None

    def save_digest(self, cache_key: str, prompt_version: str, payload: str) -> None:
        """Atomically persist only a derived digest, never raw model input."""

        with self._connection() as connection, connection:
            connection.execute(
                "INSERT OR REPLACE INTO item_digests "
                "(cache_key, prompt_version, payload) VALUES (?, ?, ?)",
                (cache_key, prompt_version, payload),
            )

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
        if not connection.execute("SELECT 1 FROM schema_migrations WHERE version = 1").fetchone():
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
            connection.execute("INSERT INTO schema_migrations (version) VALUES (1)")
        if not connection.execute("SELECT 1 FROM schema_migrations WHERE version = 2").fetchone():
            connection.execute(
                "CREATE TABLE item_digests (cache_key TEXT NOT NULL, prompt_version TEXT NOT NULL, "
                "payload TEXT NOT NULL, PRIMARY KEY (cache_key, prompt_version))"
            )
            connection.execute(
                "INSERT INTO schema_migrations (version) VALUES (?)", (_SCHEMA_VERSION,)
            )

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

    def _delete_missing_items(self, connection: sqlite3.Connection, keys: frozenset[str]) -> int:
        existing = {str(row[0]) for row in connection.execute("SELECT key FROM items").fetchall()}
        return self._delete_items(connection, tuple(sorted(existing - keys)))

    def _delete_missing_collections(
        self, connection: sqlite3.Connection, keys: frozenset[str]
    ) -> None:
        existing = {
            str(row[0]) for row in connection.execute("SELECT key FROM collections").fetchall()
        }
        connection.executemany(
            "DELETE FROM collections WHERE key = ?", ((key,) for key in sorted(existing - keys))
        )


def _item_payload(item: ZoteroItem) -> str:
    return json.dumps(asdict(item), ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def _content_hash(payload: str) -> str:
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()
