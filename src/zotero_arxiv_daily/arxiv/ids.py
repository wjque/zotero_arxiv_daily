"""Canonical arXiv identifier parsing and safe public URL construction."""

from __future__ import annotations

import re

from zotero_arxiv_daily.arxiv.models import ArxivId
from zotero_arxiv_daily.core.errors import ConfigurationError

_NEW = re.compile(r"(?P<id>\d{4}\.\d{4,5})(?:v(?P<revision>\d+))?$")
_OLD = re.compile(r"(?P<id>[a-z-]+(?:\.[A-Z]{2})?/\d{7})(?:v(?P<revision>\d+))?$", re.I)
_DOI = re.compile(r"^10\.\d{4,9}/\S+$", re.I)


def parse_arxiv_id(value: str) -> ArxivId:
    """Parse modern or legacy IDs, dropping only the revision from the canonical key."""

    normalized = value.strip().removeprefix("arXiv:").removeprefix("https://arxiv.org/abs/")
    match = _NEW.fullmatch(normalized) or _OLD.fullmatch(normalized)
    if not match:
        raise ConfigurationError("invalid arXiv identifier")
    revision = match.group("revision")
    return ArxivId(match.group("id").lower(), int(revision) if revision else None)


def public_urls(identifier: ArxivId) -> tuple[str, str]:
    """Construct validated public abstract and PDF URLs from canonical IDs only."""

    return (
        f"https://arxiv.org/abs/{identifier.canonical}",
        f"https://arxiv.org/pdf/{identifier.canonical}",
    )


def normalize_doi(value: str) -> str:
    """Normalize a public DOI for exact provider identity matching."""

    normalized = value.strip().casefold()
    normalized = normalized.removeprefix("doi:").removeprefix("https://doi.org/")
    normalized = normalized.removeprefix("http://doi.org/")
    if not _DOI.fullmatch(normalized):
        raise ConfigurationError("invalid DOI")
    return normalized
