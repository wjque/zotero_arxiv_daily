"""Bounded OpenAlex context enrichment for public DOI-identified candidates only."""

from __future__ import annotations

import json
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Protocol, cast
from urllib.error import HTTPError, URLError
from urllib.parse import quote, urlencode
from urllib.request import Request, urlopen

from zotero_arxiv_daily.arxiv.ids import normalize_doi
from zotero_arxiv_daily.arxiv.models import ArxivCandidate
from zotero_arxiv_daily.core.errors import ExternalServiceError
from zotero_arxiv_daily.core.time import require_aware_utc
from zotero_arxiv_daily.evidence.models import (
    CitationContextEvidence,
    EvidenceAvailability,
    EvidenceValue,
    PublicPaperEvidence,
)
from zotero_arxiv_daily.evidence.storage import EvidenceCache

_ENDPOINT = "https://api.openalex.org/works"
_PROVIDER = "openalex"
_ADAPTER_VERSION = "v1"
_SELECT_FIELDS = "doi,cited_by_count,referenced_works_count,is_retracted,open_access"
_MAX_RESPONSE_BYTES = 64 * 1024


class TransientOpenAlexError(ExternalServiceError):
    """A provider failure that can receive one bounded retry."""


class OpenAlexNotFoundError(ExternalServiceError):
    """The requested DOI has no usable OpenAlex work record."""


class OpenAlexRateLimitError(TransientOpenAlexError):
    """A rate-limit response with an optional provider retry delay."""

    def __init__(self, retry_after_seconds: float | None) -> None:
        super().__init__("OpenAlex rate limited the evidence request")
        self.retry_after_seconds = retry_after_seconds


class Transport(Protocol):
    def get(self, url: str, timeout_seconds: float) -> bytes: ...


class UrlLibTransport:
    """Read a small JSON response without following provider content beyond the budget."""

    def get(self, url: str, timeout_seconds: float) -> bytes:
        request = Request(
            url,
            headers={"Accept": "application/json", "User-Agent": "zotero-arxiv-daily/evidence-v1"},
            method="GET",
        )
        try:
            with urlopen(request, timeout=timeout_seconds) as response:  # noqa: S310
                if not 200 <= response.getcode() < 300:
                    raise ExternalServiceError("OpenAlex returned an unsuccessful status")
                payload = cast(bytes, response.read(_MAX_RESPONSE_BYTES + 1))
                if len(payload) > _MAX_RESPONSE_BYTES:
                    raise ExternalServiceError("OpenAlex response exceeds the evidence size budget")
                return payload
        except HTTPError as error:
            if error.code == 404:
                raise OpenAlexNotFoundError("OpenAlex has no matching work") from error
            if error.code == 429:
                retry_after = error.headers.get("Retry-After")
                try:
                    delay = float(retry_after) if retry_after is not None else None
                except ValueError:
                    delay = None
                raise OpenAlexRateLimitError(delay) from error
            if error.code >= 500:
                raise TransientOpenAlexError("OpenAlex failed transiently") from error
            raise ExternalServiceError("OpenAlex request failed") from error
        except (URLError, OSError) as error:
            raise TransientOpenAlexError("OpenAlex network request failed") from error


@dataclass(slots=True)
class OpenAlexClient:
    """Serialize optional DOI requests and never send local profile or Zotero data."""

    transport: Transport = field(default_factory=UrlLibTransport)
    monotonic: Callable[[], float] = time.monotonic
    sleep: Callable[[float], None] = time.sleep
    minimum_interval_seconds: float = 0.2
    timeout_seconds: float = 15.0
    retries: int = 1
    maximum_retry_after_seconds: float = 5.0
    _last_request_at: float | None = field(default=None, init=False)

    def fetch(self, candidate: ArxivCandidate, observed_at: datetime) -> PublicPaperEvidence:
        """Fetch one exact DOI match, or return explicit unknown context without a request."""

        now = require_aware_utc(observed_at, "observed_at")
        if candidate.doi is None:
            return unknown_evidence(candidate, now)
        try:
            payload = self._request(candidate.doi)
        except OpenAlexNotFoundError:
            return unknown_evidence(candidate, now)
        return _parse_response(candidate, payload, now)

    def _request(self, doi: str) -> bytes:
        self._wait_for_slot()
        url = (
            f"{_ENDPOINT}/{quote(f'doi:{doi}', safe=':/')}?{urlencode({'select': _SELECT_FIELDS})}"
        )
        for attempt in range(self.retries + 1):
            try:
                payload = self.transport.get(url, self.timeout_seconds)
                self._last_request_at = self.monotonic()
                return payload
            except OpenAlexRateLimitError as error:
                self._last_request_at = self.monotonic()
                if (
                    attempt == self.retries
                    or error.retry_after_seconds is None
                    or error.retry_after_seconds > self.maximum_retry_after_seconds
                ):
                    raise
                self.sleep(error.retry_after_seconds)
            except TransientOpenAlexError:
                self._last_request_at = self.monotonic()
                if attempt == self.retries:
                    raise
                self.sleep(min(2.0**attempt, self.maximum_retry_after_seconds))
        raise AssertionError("unreachable")

    def _wait_for_slot(self) -> None:
        if self._last_request_at is None:
            return
        remaining = self.minimum_interval_seconds - (self.monotonic() - self._last_request_at)
        if remaining > 0:
            self.sleep(remaining)


@dataclass(frozen=True, slots=True)
class OpenAlexEvidenceEnricher:
    """Cache top-K public evidence while allowing each item to degrade independently."""

    client: OpenAlexClient
    cache: EvidenceCache
    ttl: timedelta = timedelta(days=7)

    def enrich(
        self,
        candidates: tuple[ArxivCandidate, ...],
        now: datetime,
        *,
        limit: int,
    ) -> tuple[PublicPaperEvidence, ...]:
        if not 1 <= limit <= 80:
            raise ValueError("evidence enrichment limit must be between 1 and 80")
        observed_at = require_aware_utc(now, "now")
        evidence: list[PublicPaperEvidence] = []
        for candidate in candidates[:limit]:
            paper_id = _paper_id(candidate)
            cached = self.cache.get(
                paper_id,
                observed_at,
                provider=_PROVIDER,
                adapter_version=_ADAPTER_VERSION,
            )
            if cached is not None:
                evidence.append(cached)
                continue
            if candidate.doi is None:
                missing_identifier = unknown_evidence(candidate, observed_at)
                self.cache.put(missing_identifier)
                evidence.append(missing_identifier)
                continue
            try:
                fetched = self.client.fetch(candidate, observed_at)
            except ExternalServiceError:
                evidence.append(unknown_evidence(candidate, observed_at))
                continue
            self.cache.put(_with_expiry(fetched, observed_at + self.ttl))
            evidence.append(fetched)
        return tuple(evidence)


def unknown_evidence(candidate: ArxivCandidate, observed_at: datetime) -> PublicPaperEvidence:
    """Represent missing IDs and provider failures without treating them as negative evidence."""

    now = require_aware_utc(observed_at, "observed_at")
    unknown = EvidenceValue(EvidenceAvailability.UNKNOWN, 0.0, now, _PROVIDER)
    return PublicPaperEvidence(
        _paper_id(candidate),
        1,
        now,
        now + timedelta(days=1),
        context=CitationContextEvidence(None, None, unknown, unknown),
        provider=_PROVIDER,
        adapter_version=_ADAPTER_VERSION,
    )


def _parse_response(
    candidate: ArxivCandidate, payload: bytes, observed_at: datetime
) -> PublicPaperEvidence:
    try:
        value = json.loads(payload)
    except json.JSONDecodeError as error:
        raise ExternalServiceError("OpenAlex returned malformed JSON") from error
    if not isinstance(value, dict):
        raise ExternalServiceError("OpenAlex response root is invalid")
    expected_doi = candidate.doi
    returned_doi = value.get("doi")
    if (
        expected_doi is None
        or not isinstance(returned_doi, str)
        or normalize_doi(returned_doi) != expected_doi
    ):
        raise ExternalServiceError("OpenAlex returned a mismatched DOI")
    now = require_aware_utc(observed_at, "observed_at")
    return PublicPaperEvidence(
        _paper_id(candidate),
        1,
        now,
        now + timedelta(days=7),
        context=CitationContextEvidence(
            _non_negative_integer(value.get("cited_by_count"), "cited_by_count"),
            _non_negative_integer(value.get("referenced_works_count"), "referenced_works_count"),
            _boolean_evidence(value.get("open_access"), "is_oa", now),
            _boolean_evidence(value, "is_retracted", now),
        ),
        provider=_PROVIDER,
        adapter_version=_ADAPTER_VERSION,
    )


def _boolean_evidence(value: object, field: str, observed_at: datetime) -> EvidenceValue:
    source = value.get(field) if isinstance(value, dict) else None
    now = require_aware_utc(observed_at, "observed_at")
    if isinstance(source, bool):
        return EvidenceValue(EvidenceAvailability.AVAILABLE, 0.8, now, _PROVIDER, source)
    return EvidenceValue(EvidenceAvailability.UNKNOWN, 0.0, now, _PROVIDER)


def _non_negative_integer(value: object, field: str) -> int | None:
    if value is None:
        return None
    if not isinstance(value, int) or isinstance(value, bool) or value < 0:
        raise ExternalServiceError(f"OpenAlex {field} is invalid")
    return value


def _paper_id(candidate: ArxivCandidate) -> str:
    return f"arxiv:{candidate.arxiv_id.canonical}"


def _with_expiry(evidence: PublicPaperEvidence, expires_at: datetime) -> PublicPaperEvidence:
    return PublicPaperEvidence(
        evidence.canonical_paper_id,
        evidence.schema_version,
        evidence.observed_at,
        require_aware_utc(expires_at, "expires_at"),
        evidence.repository,
        evidence.context,
        evidence.provider,
        evidence.adapter_version,
    )
