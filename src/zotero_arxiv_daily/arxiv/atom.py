"""Strict Atom parsing for public arXiv metadata only."""

from __future__ import annotations

import re
import xml.etree.ElementTree as ElementTree
from datetime import UTC, datetime

from zotero_arxiv_daily.arxiv.ids import parse_arxiv_id, public_urls
from zotero_arxiv_daily.arxiv.models import ArxivCandidate
from zotero_arxiv_daily.core.errors import ExternalServiceError

_ATOM = "{http://www.w3.org/2005/Atom}"
_ARXIV = "{http://arxiv.org/schemas/atom}"


def parse_feed(payload: bytes) -> tuple[ArxivCandidate, ...]:
    """Parse a feed into typed candidates and reject malformed provider responses."""

    try:
        root = ElementTree.fromstring(payload)
    except ElementTree.ParseError as error:
        raise ExternalServiceError("arXiv returned malformed Atom XML") from error
    if root.tag != f"{_ATOM}feed":
        raise ExternalServiceError("arXiv response is not an Atom feed")
    return tuple(_parse_entry(entry) for entry in root.findall(f"{_ATOM}entry"))


def _parse_entry(entry: ElementTree.Element) -> ArxivCandidate:
    identifier = parse_arxiv_id(_text(entry, f"{_ATOM}id").rsplit("/", maxsplit=1)[-1])
    abstract_url, pdf_url = public_urls(identifier)
    categories = tuple(
        sorted(
            {
                node.attrib["term"]
                for node in entry.findall(f"{_ATOM}category")
                if "term" in node.attrib
            }
        )
    )
    if not categories:
        raise ExternalServiceError("arXiv entry has no categories")
    return ArxivCandidate(
        identifier,
        _collapse(_text(entry, f"{_ATOM}title")),
        tuple(
            _collapse(_text(author, f"{_ATOM}name")) for author in entry.findall(f"{_ATOM}author")
        ),
        categories,
        _timestamp(_text(entry, f"{_ATOM}published")),
        _timestamp(_text(entry, f"{_ATOM}updated")),
        abstract_url,
        pdf_url,
        _collapse(_text(entry, f"{_ATOM}summary")),
    )


def _text(node: ElementTree.Element, tag: str) -> str:
    value = node.findtext(tag)
    if not value or not value.strip():
        raise ExternalServiceError("arXiv entry is missing a required field")
    return value.strip()


def _timestamp(value: str) -> datetime:
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as error:
        raise ExternalServiceError("arXiv entry has an invalid timestamp") from error
    return parsed.astimezone(UTC)


def _collapse(value: str) -> str:
    return re.sub(r"\s+", " ", value).strip()
