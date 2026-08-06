"""Bounded reachability evidence for project pages explicitly linked by arXiv abstracts."""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from datetime import datetime
from typing import Protocol
from urllib.error import HTTPError, URLError
from urllib.parse import urljoin, urlsplit, urlunsplit
from urllib.request import HTTPRedirectHandler, Request, build_opener

from zotero_arxiv_daily.arxiv.models import ArxivCandidate
from zotero_arxiv_daily.core.errors import ExternalServiceError
from zotero_arxiv_daily.llm.cache import ProposalCache

PROJECT_PAGE_EVIDENCE_VERSION = "project-page-v1"
_URL = re.compile(r"https://[^\s<>\"']+", re.IGNORECASE)
_TRAILING_PUNCTUATION = ".,;:!?)]}"
_EXACT_HOSTS = frozenset({"github.com", "gitlab.com", "huggingface.co", "codeberg.org"})
_HOST_SUFFIXES = (".github.io", ".gitlab.io")
_MAX_URLS = 3
_MAX_REDIRECTS = 2
_MAX_URL_LENGTH = 2_048


@dataclass(frozen=True, slots=True)
class ProjectPageEvidence:
    """A public project-page proxy; reachability never proves source availability."""

    url: str | None
    reachable: bool | None

    @property
    def supports_open_source_proxy(self) -> bool:
        return self.reachable is True


@dataclass(frozen=True, slots=True)
class PageResponse:
    status: int
    redirect_url: str | None = None


class ProjectPageTransport(Protocol):
    def fetch(self, url: str, timeout_seconds: float) -> PageResponse: ...


class _NoRedirect(HTTPRedirectHandler):
    def redirect_request(
        self,
        request: Request,
        file_pointer: object,
        code: int,
        message: str,
        headers: object,
        new_url: str,
    ) -> None:
        return None


class UrlLibProjectPageTransport:
    """Probe one allowlisted page without downloading its body or following redirects implicitly."""

    def fetch(self, url: str, timeout_seconds: float) -> PageResponse:
        request = Request(
            url,
            headers={
                "Accept": "text/html,application/xhtml+xml",
                "Range": "bytes=0-0",
                "User-Agent": "zotero-arxiv-daily/project-page-v1",
            },
            method="GET",
        )
        opener = build_opener(_NoRedirect)
        try:
            with opener.open(request, timeout=timeout_seconds) as response:  # noqa: S310
                response.read(1)
                return PageResponse(response.getcode(), response.headers.get("Location"))
        except HTTPError as error:
            return PageResponse(error.code, error.headers.get("Location"))
        except (TimeoutError, URLError, OSError) as error:
            raise ExternalServiceError("project page reachability check failed") from error


@dataclass(slots=True)
class ProjectPageClient:
    transport: ProjectPageTransport
    timeout_seconds: float = 5.0

    def __post_init__(self) -> None:
        if not 1.0 <= self.timeout_seconds <= 10.0:
            raise ValueError("project page timeout must be between 1 and 10 seconds")

    def inspect(self, abstract: str) -> ProjectPageEvidence:
        """Return the first reachable approved project URL, or bounded unavailable evidence."""

        urls = extract_project_page_urls(abstract)
        if not urls:
            return ProjectPageEvidence(None, None)
        saw_unknown = False
        for original in urls:
            current = original
            for redirect_count in range(_MAX_REDIRECTS + 1):
                try:
                    response = self.transport.fetch(current, self.timeout_seconds)
                except ExternalServiceError:
                    saw_unknown = True
                    break
                if 200 <= response.status < 300:
                    return ProjectPageEvidence(current, True)
                if 300 <= response.status < 400 and response.redirect_url:
                    if redirect_count == _MAX_REDIRECTS:
                        break
                    redirected = approved_project_page_url(urljoin(current, response.redirect_url))
                    if redirected is None:
                        break
                    current = redirected
                    continue
                if response.status in {408, 425, 429} or response.status >= 500:
                    saw_unknown = True
                break
        return ProjectPageEvidence(urls[0], None if saw_unknown else False)


def extract_project_page_urls(abstract: str) -> tuple[str, ...]:
    """Extract only bounded public project-host URLs; arbitrary abstract links are ignored."""

    values: list[str] = []
    for match in _URL.finditer(abstract):
        approved = approved_project_page_url(match.group(0).rstrip(_TRAILING_PUNCTUATION))
        if approved is not None and approved not in values:
            values.append(approved)
        if len(values) == _MAX_URLS:
            break
    return tuple(values)


def approved_project_page_url(value: str) -> str | None:
    if len(value) > _MAX_URL_LENGTH:
        return None
    parsed = urlsplit(value)
    hostname = (parsed.hostname or "").casefold()
    try:
        port = parsed.port
    except ValueError:
        return None
    if (
        parsed.scheme.casefold() != "https"
        or not hostname
        or parsed.username is not None
        or parsed.password is not None
        or port not in {None, 443}
        or not parsed.path
        or not (hostname in _EXACT_HOSTS or hostname.endswith(_HOST_SUFFIXES))
    ):
        return None
    if hostname in _EXACT_HOSTS and len([part for part in parsed.path.split("/") if part]) < 2:
        return None
    return urlunsplit(("https", parsed.netloc.casefold(), parsed.path, parsed.query, ""))


def inspect_project_pages(
    candidates: tuple[ArxivCandidate, ...],
    client: ProjectPageClient | None,
    cache: ProposalCache,
    observed_at: datetime,
) -> dict[str, ProjectPageEvidence]:
    """Inspect candidate abstracts with a daily cache and strict cached-value validation."""

    if client is None:
        return {
            candidate.arxiv_id.canonical: ProjectPageEvidence(None, None)
            for candidate in candidates
        }
    values: dict[str, ProjectPageEvidence] = {}
    day = observed_at.date().isoformat()
    for candidate in candidates:
        identifier = candidate.arxiv_id.canonical
        key = cache.key(
            identifier,
            0,
            f"{PROJECT_PAGE_EVIDENCE_VERSION}:{day}",
            "deterministic-http",
            candidate.summary,
        )
        cached = cache.get(key)
        if cached is not None:
            decoded = _decode_evidence(cached)
            if decoded is not None:
                values[identifier] = decoded
                continue
        evidence = client.inspect(candidate.summary)
        cache.put(key, _encode_evidence(evidence))
        values[identifier] = evidence
    return values


def _encode_evidence(value: ProjectPageEvidence) -> str:
    return json.dumps(
        {"schema_version": 1, "url": value.url, "reachable": value.reachable},
        sort_keys=True,
        separators=(",", ":"),
    )


def _decode_evidence(payload: str) -> ProjectPageEvidence | None:
    try:
        value = json.loads(payload)
    except json.JSONDecodeError:
        return None
    if not isinstance(value, dict) or set(value) != {"schema_version", "url", "reachable"}:
        return None
    url = value["url"]
    reachable = value["reachable"]
    if value["schema_version"] != 1 or (url is not None and not isinstance(url, str)):
        return None
    if reachable is not None and not isinstance(reachable, bool):
        return None
    if url is not None and approved_project_page_url(url) != url:
        return None
    return ProjectPageEvidence(url, reachable)
