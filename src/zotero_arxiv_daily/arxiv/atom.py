"""Strict Atom parsing for public arXiv metadata only."""

from __future__ import annotations

import re
import xml.etree.ElementTree as ElementTree
from datetime import UTC, datetime

from zotero_arxiv_daily.arxiv.ids import normalize_doi, parse_arxiv_id, public_urls
from zotero_arxiv_daily.arxiv.models import ArxivCandidate
from zotero_arxiv_daily.core.errors import ConfigurationError, ExternalServiceError

_ATOM = "{http://www.w3.org/2005/Atom}"
_ARXIV = "{http://arxiv.org/schemas/atom}"
_MAX_AFFILIATIONS = 32
_MAX_AFFILIATION_BYTES = 256


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
    authors = entry.findall(f"{_ATOM}author")
    affiliations = tuple(
        dict.fromkeys(
            _bounded_affiliation(node.text)
            for author in authors
            for node in author.findall(f"{_ARXIV}affiliation")
            if node.text and node.text.strip()
        )
    )
    if len(affiliations) > _MAX_AFFILIATIONS:
        raise ExternalServiceError("arXiv entry has too many affiliation values")
    return ArxivCandidate(
        identifier,
        _collapse(_text(entry, f"{_ATOM}title")),
        tuple(_collapse(_text(author, f"{_ATOM}name")) for author in authors),
        categories,
        _timestamp(_text(entry, f"{_ATOM}published")),
        _timestamp(_text(entry, f"{_ATOM}updated")),
        abstract_url,
        pdf_url,
        _collapse(_text(entry, f"{_ATOM}summary")),
        affiliations,
        _optional_doi(entry),
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


def _bounded_affiliation(value: str) -> str:
    collapsed = _collapse(value)
    if not collapsed or len(collapsed.encode("utf-8")) > _MAX_AFFILIATION_BYTES:
        raise ExternalServiceError("arXiv entry has an invalid affiliation value")
    return collapsed


def _optional_doi(entry: ElementTree.Element) -> str | None:
    value = entry.findtext(f"{_ARXIV}doi")
    if value is None or not value.strip():
        return None
    try:
        return normalize_doi(value)
    except ConfigurationError as error:
        raise ExternalServiceError("arXiv entry has an invalid DOI") from error
