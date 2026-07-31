"""Use-case orchestration for full and incremental local Zotero sync."""

from __future__ import annotations

from zotero_arxiv_daily.zotero.client import ZoteroClient
from zotero_arxiv_daily.zotero.models import SyncResult
from zotero_arxiv_daily.zotero.storage import ZoteroStore


def synchronize(client: ZoteroClient, store: ZoteroStore) -> SyncResult:
    """Fetch from the last successful version and atomically persist the result."""

    return store.apply(client.fetch(store.library_version))
