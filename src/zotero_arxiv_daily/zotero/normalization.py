"""Normalize untrusted Zotero JSON into local-only typed records."""

from __future__ import annotations

import re
import unicodedata
from html import unescape
from html.parser import HTMLParser
from typing import Any

from zotero_arxiv_daily.core.errors import ExternalServiceError
from zotero_arxiv_daily.zotero.models import ZoteroCollection, ZoteroItem


class _TextExtractor(HTMLParser):
    def __init__(self) -> None:
        super().__init__()
        self.parts: list[str] = []
        self.ignored_depth = 0

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag in {"script", "style"}:
            self.ignored_depth += 1
        elif tag in {"br", "div", "p", "li"} and not self.ignored_depth:
            self.parts.append(" ")

    def handle_endtag(self, tag: str) -> None:
        if tag in {"script", "style"} and self.ignored_depth:
            self.ignored_depth -= 1
        elif tag in {"div", "p", "li"} and not self.ignored_depth:
            self.parts.append(" ")

    def handle_data(self, data: str) -> None:
        if not self.ignored_depth:
            self.parts.append(data)


def normalize_text(value: object) -> str:
    """Strip HTML markup, normalize Unicode, and collapse whitespace."""

    if not isinstance(value, str):
        return ""
    parser = _TextExtractor()
    parser.feed(value)
    parser.close()
    return re.sub(
        r"\s+", " ", unicodedata.normalize("NFKC", unescape("".join(parser.parts)))
    ).strip()


def normalize_item(raw: object) -> ZoteroItem:
    """Validate one Zotero item response; reject malformed external records."""

    record = _mapping(raw, "item")
    data = _mapping(record.get("data", record), "item data")
    key = _required_text(record.get("key", data.get("key")), "item key")
    item_type = _required_text(data.get("itemType"), "itemType")
    version = _integer(record.get("version", data.get("version", 0)), "item version")
    tags = tuple(
        (normalize_text(_mapping(tag, "tag").get("tag")), _mapping(tag, "tag").get("type", 0) == 0)
        for tag in _sequence(data.get("tags", []), "tags")
        if normalize_text(_mapping(tag, "tag").get("tag"))
    )
    identifiers = tuple(
        value
        for value in (normalize_text(data.get("DOI")), normalize_text(data.get("ISBN")))
        if value
    )
    creators = tuple(
        name
        for creator in _sequence(data.get("creators", []), "creators")
        if (name := _creator_name(_mapping(creator, "creator")))
    )
    return ZoteroItem(
        key=key,
        version=version,
        item_type=item_type,
        parent_key=_optional_text(data.get("parentItem")),
        title=normalize_text(data.get("title")),
        creators=creators,
        tags=tags,
        collections=tuple(
            key
            for key in (
                _optional_text(value)
                for value in _sequence(data.get("collections", []), "collections")
            )
            if key
        ),
        identifiers=identifiers,
        abstract=normalize_text(data.get("abstractNote")),
        date=_optional_text(data.get("date")),
        trashed=bool(data.get("deleted", False)),
        note_text=normalize_text(data.get("note")) or None,
        annotation_text=normalize_text(data.get("annotationText")) or None,
        annotation_comment=normalize_text(data.get("annotationComment")) or None,
    )


def normalize_collection(raw: object) -> ZoteroCollection:
    record = _mapping(raw, "collection")
    data = _mapping(record.get("data", record), "collection data")
    return ZoteroCollection(
        key=_required_text(record.get("key", data.get("key")), "collection key"),
        version=_integer(record.get("version", data.get("version", 0)), "collection version"),
        name=_required_text(data.get("name"), "collection name"),
        parent_key=_optional_text(data.get("parentCollection")),
    )


def _mapping(value: object, name: str) -> dict[str, Any]:
    if isinstance(value, dict) and all(isinstance(key, str) for key in value):
        return value
    raise ExternalServiceError(f"malformed Zotero {name}")


def _sequence(value: object, name: str) -> list[object]:
    if isinstance(value, list):
        return value
    raise ExternalServiceError(f"malformed Zotero {name}")


def _required_text(value: object, name: str) -> str:
    normalized = normalize_text(value)
    if not normalized:
        raise ExternalServiceError(f"missing Zotero {name}")
    return normalized


def _optional_text(value: object) -> str | None:
    return normalize_text(value) or None


def _integer(value: object, name: str) -> int:
    if isinstance(value, int) and value >= 0:
        return value
    raise ExternalServiceError(f"malformed Zotero {name}")


def _creator_name(creator: dict[str, Any]) -> str:
    return normalize_text(creator.get("name")) or " ".join(
        part
        for part in (
            normalize_text(creator.get("firstName")),
            normalize_text(creator.get("lastName")),
        )
        if part
    )
