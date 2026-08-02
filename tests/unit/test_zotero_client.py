from __future__ import annotations

from zotero_arxiv_daily.zotero.client import ZoteroLocalClient, _DeletedEndpointUnavailable


class SnapshotFallbackClient(ZoteroLocalClient):
    def __init__(self) -> None:
        super().__init__("http://localhost:23119")
        self.requests: list[tuple[str, dict[str, str]]] = []

    def _get_json(self, path: str, parameters: dict[str, str]) -> tuple[object, int, str | None]:
        self.requests.append((path, parameters))
        if path == "/api/users/0/deleted":
            raise _DeletedEndpointUnavailable
        if path == "/api/users/0/items":
            return (
                [{"key": "PAPER001", "version": 2, "data": {"itemType": "journalArticle"}}],
                2,
                "3",
            )
        return ([{"key": "COLL0001", "version": 2, "data": {"name": "Synthetic"}}], 2, "3")


def test_missing_deleted_endpoint_falls_back_to_a_complete_snapshot() -> None:
    client = SnapshotFallbackClient()

    batch = client.fetch(1)

    assert batch.complete_snapshot is True
    assert batch.deleted_item_keys == ()
    assert batch.library_version == 2
    assert client.requests[-2:] == [
        ("/api/users/0/items", {"format": "json", "include": "data"}),
        ("/api/users/0/collections", {"format": "json", "include": "data"}),
    ]
