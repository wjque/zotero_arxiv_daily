"""Bounded extraction of untrusted public paper sections from derived arXiv HTML."""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from datetime import datetime
from html.parser import HTMLParser
from typing import Protocol, cast
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

from zotero_arxiv_daily.arxiv.models import ArxivCandidate
from zotero_arxiv_daily.core.errors import ExternalServiceError
from zotero_arxiv_daily.llm.cache import ProposalCache

PAPER_SECTION_EVIDENCE_VERSION = "paper-sections-v1"
_MAX_RESPONSE_BYTES = 512 * 1024
_MAX_SECTION_CHARACTERS = 4_000
_MAX_TOTAL_CHARACTERS = 10_000
_SPACE = re.compile(r"\s+")
_SECTION_TERMS = {
    "method": ("method", "approach", "algorithm", "model"),
    "evaluation": ("experiment", "evaluation", "implementation", "result"),
    "limitations": ("limitation", "discussion", "threat", "future work"),
}


@dataclass(frozen=True, slots=True)
class PaperSections:
    """Allowlisted public text and provenance; absent fields remain explicitly unknown."""

    source_url: str
    method: str | None = None
    evaluation: str | None = None
    limitations: str | None = None

    @property
    def available_fields(self) -> tuple[str, ...]:
        return tuple(
            name
            for name, value in (
                ("method_evidence", self.method),
                ("evaluation_evidence", self.evaluation),
                ("limitations_evidence", self.limitations),
            )
            if value is not None
        )


class PaperHtmlTransport(Protocol):
    def fetch(self, url: str, timeout_seconds: float) -> bytes: ...


class UrlLibPaperHtmlTransport:
    """Fetch only a bounded ar5iv HTML representation derived from a canonical arXiv ID."""

    def fetch(self, url: str, timeout_seconds: float) -> bytes:
        request = Request(
            url,
            headers={
                "Accept": "text/html",
                "User-Agent": "zotero-arxiv-daily/paper-sections-v1",
            },
            method="GET",
        )
        try:
            with urlopen(request, timeout=timeout_seconds) as response:  # noqa: S310
                if not 200 <= response.getcode() < 300:
                    raise ExternalServiceError("public paper HTML returned an unsuccessful status")
                payload = cast(bytes, response.read(_MAX_RESPONSE_BYTES + 1))
                if len(payload) > _MAX_RESPONSE_BYTES:
                    raise ExternalServiceError("public paper HTML exceeds the evidence size budget")
                return payload
        except HTTPError as error:
            raise ExternalServiceError("public paper HTML request failed") from error
        except (TimeoutError, URLError, OSError) as error:
            raise ExternalServiceError("public paper HTML is unavailable") from error


@dataclass(slots=True)
class PaperSectionClient:
    transport: PaperHtmlTransport = field(default_factory=UrlLibPaperHtmlTransport)
    timeout_seconds: float = 10.0

    def __post_init__(self) -> None:
        if not 1 <= self.timeout_seconds <= 20:
            raise ValueError("paper section timeout must be between 1 and 20 seconds")

    def inspect(self, candidate: ArxivCandidate) -> PaperSections:
        url = _source_url(candidate)
        try:
            payload = self.transport.fetch(url, self.timeout_seconds)
            text = payload.decode("utf-8")
        except (ExternalServiceError, UnicodeError):
            return PaperSections(url)
        parser = _SectionParser()
        try:
            parser.feed(text)
            parser.close()
        except (ValueError, RecursionError):
            return PaperSections(url)
        values = parser.sections()
        return PaperSections(
            url, values.get("method"), values.get("evaluation"), values.get("limitations")
        )


def inspect_paper_sections(
    candidates: tuple[ArxivCandidate, ...],
    client: PaperSectionClient | None,
    cache: ProposalCache,
    observed_at: datetime,
) -> dict[str, PaperSections]:
    """Inspect a bounded candidate set with daily, strictly validated cache entries."""

    values: dict[str, PaperSections] = {}
    day = observed_at.date().isoformat()
    for candidate in candidates:
        identifier = candidate.arxiv_id.canonical
        key = cache.key(
            identifier,
            0,
            f"{PAPER_SECTION_EVIDENCE_VERSION}:{day}",
            "deterministic-public-html",
            candidate.updated.isoformat(),
        )
        cached = cache.get(key)
        decoded = _decode(cached) if cached is not None else None
        if decoded is not None:
            values[identifier] = decoded
            continue
        evidence = (
            client.inspect(candidate)
            if client is not None
            else PaperSections(_source_url(candidate))
        )
        cache.put(key, _encode(evidence))
        values[identifier] = evidence
    return values


class _SectionParser(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self._heading = False
        self._ignored = 0
        self._heading_parts: list[str] = []
        self._active: str | None = None
        self._values: dict[str, list[str]] = {name: [] for name in _SECTION_TERMS}
        self._total = 0

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        del attrs
        if tag in {"script", "style", "nav"}:
            self._ignored += 1
        if tag in {"h1", "h2", "h3", "h4", "h5", "h6"}:
            self._heading = True
            self._heading_parts = []

    def handle_endtag(self, tag: str) -> None:
        if tag in {"script", "style", "nav"} and self._ignored:
            self._ignored -= 1
        if tag in {"h1", "h2", "h3", "h4", "h5", "h6"}:
            heading = _clean(" ".join(self._heading_parts)).casefold()
            self._active = next(
                (
                    name
                    for name, terms in _SECTION_TERMS.items()
                    if any(term in heading for term in terms)
                ),
                None,
            )
            self._heading = False

    def handle_data(self, data: str) -> None:
        if self._ignored:
            return
        if self._heading:
            self._heading_parts.append(data)
            return
        if self._active is None or self._total >= _MAX_TOTAL_CHARACTERS:
            return
        cleaned = _clean(data)
        current = sum(len(value) for value in self._values[self._active])
        remaining = min(_MAX_SECTION_CHARACTERS - current, _MAX_TOTAL_CHARACTERS - self._total)
        if cleaned and remaining > 0:
            bounded = cleaned[:remaining]
            self._values[self._active].append(bounded)
            self._total += len(bounded)

    def sections(self) -> dict[str, str]:
        return {
            name: value
            for name, parts in self._values.items()
            if (value := _clean(" ".join(parts)))
        }


def _source_url(candidate: ArxivCandidate) -> str:
    return f"https://ar5iv.labs.arxiv.org/html/{candidate.arxiv_id.canonical}"


def _clean(value: str) -> str:
    return _SPACE.sub(" ", value).strip()


def _encode(value: PaperSections) -> str:
    return json.dumps(
        {
            "schema_version": 1,
            "source_url": value.source_url,
            "method": value.method,
            "evaluation": value.evaluation,
            "limitations": value.limitations,
        },
        separators=(",", ":"),
        sort_keys=True,
    )


def _decode(payload: str) -> PaperSections | None:
    try:
        value = json.loads(payload)
    except json.JSONDecodeError:
        return None
    fields = {"schema_version", "source_url", "method", "evaluation", "limitations"}
    if not isinstance(value, dict) or set(value) != fields or value["schema_version"] != 1:
        return None
    source = value["source_url"]
    sections = tuple(value[name] for name in ("method", "evaluation", "limitations"))
    if (
        not isinstance(source, str)
        or not source.startswith("https://ar5iv.labs.arxiv.org/html/")
        or any(
            section is not None
            and (not isinstance(section, str) or len(section) > _MAX_SECTION_CHARACTERS)
            for section in sections
        )
        or sum(len(section or "") for section in sections) > _MAX_TOTAL_CHARACTERS
    ):
        return None
    return PaperSections(source, *sections)
