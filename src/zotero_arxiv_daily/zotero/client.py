"""Serialized adapter for Zotero's local JSON API."""

from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Protocol, cast
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode
from urllib.request import Request, urlopen

from zotero_arxiv_daily.core.errors import ExternalServiceError
from zotero_arxiv_daily.zotero.models import SyncBatch
from zotero_arxiv_daily.zotero.normalization import normalize_collection, normalize_item


class ZoteroClient(Protocol):
    """Boundary used by sync orchestration and offline tests."""

    def fetch(self, since: int | None) -> SyncBatch:
        """Fetch a complete or `since`-incremental local library batch."""


@dataclass(slots=True)
class ZoteroLocalClient:
    """Local API adapter with bounded timeouts and no parallel requests."""

    base_url: str
    timeout_seconds: float = 10.0

    def fetch(self, since: int | None) -> SyncBatch:
        parameters: dict[str, str] = {"format": "json", "include": "data"}
        if since is not None:
            parameters["since"] = str(since)
        items, item_version, api_version = self._get_json("/api/users/0/items", parameters)
        collections, collection_version, _ = self._get_json("/api/users/0/collections", parameters)
        deleted: object = []
        if since is not None:
            deleted, deleted_version, _ = self._get_json(
                "/api/users/0/deleted", {"since": str(since)}
            )
            item_version = max(item_version, deleted_version)
        else:
            deleted_version = 0
        if isinstance(deleted, dict):
            deleted = deleted.get("items", [])
        if (
            not isinstance(items, list)
            or not isinstance(collections, list)
            or not isinstance(deleted, list)
        ):
            raise ExternalServiceError("malformed Zotero API list response")
        deleted_keys = tuple(
            key
            for entry in deleted
            if isinstance(entry, dict)
            for key in [entry.get("key")]
            if isinstance(key, str)
        )
        return SyncBatch(
            library_version=max(item_version, collection_version, deleted_version),
            items=tuple(normalize_item(item) for item in items),
            collections=tuple(normalize_collection(collection) for collection in collections),
            deleted_item_keys=deleted_keys,
            local_api_version=api_version,
        )

    def _get_json(self, path: str, parameters: dict[str, str]) -> tuple[object, int, str | None]:
        url = f"{self.base_url.rstrip('/')}{path}?{urlencode(parameters)}"
        request = Request(url, headers={"Accept": "application/json"}, method="GET")
        try:
            with urlopen(request, timeout=self.timeout_seconds) as response:  # noqa: S310
                payload = json.loads(response.read().decode("utf-8"))
                version = _header_version(
                    cast(str | None, response.headers.get("Last-Modified-Version"))
                )
                api_version = cast(str | None, response.headers.get("Zotero-API-Version"))
        except (HTTPError, URLError, OSError, UnicodeDecodeError, json.JSONDecodeError) as error:
            raise ExternalServiceError("Zotero Local API request failed") from error
        return payload, version, api_version


def _header_version(value: str | None) -> int:
    try:
        return int(value) if value is not None else 0
    except ValueError as error:
        raise ExternalServiceError("malformed Zotero library version header") from error
