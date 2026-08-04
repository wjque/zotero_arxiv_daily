"""Serialized official arXiv API client with bounded retries and throttling."""

from __future__ import annotations

import time
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Protocol, cast
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode
from urllib.request import Request, urlopen

from zotero_arxiv_daily.arxiv.atom import parse_feed
from zotero_arxiv_daily.arxiv.models import ArxivCandidate
from zotero_arxiv_daily.core.errors import ExternalServiceError

_ENDPOINT = "https://export.arxiv.org/api/query"
MAX_RESPONSE_BYTES = 2 * 1024 * 1024


class TransientArxivError(ExternalServiceError):
    """A rate-limit or server failure that may safely receive bounded retry."""

    def __init__(self, message: str, *, retry_after: float | None = None) -> None:
        super().__init__(message)
        self.retry_after = retry_after


class Transport(Protocol):
    def get(self, url: str, timeout_seconds: float) -> bytes: ...


class UrlLibTransport:
    def get(self, url: str, timeout_seconds: float) -> bytes:
        request = Request(url, headers={"User-Agent": "zotero-arxiv-daily/0.1"}, method="GET")
        try:
            with urlopen(request, timeout=timeout_seconds) as response:  # noqa: S310
                if not 200 <= response.getcode() < 300:
                    raise ExternalServiceError(
                        f"arXiv returned HTTP status {int(response.getcode())}"
                    )
                payload = cast(bytes, response.read(MAX_RESPONSE_BYTES + 1))
                if len(payload) > MAX_RESPONSE_BYTES:
                    raise ExternalServiceError("arXiv response exceeded the byte limit")
                return payload
        except HTTPError as error:
            if error.code == 429 or error.code >= 500:
                raise TransientArxivError(
                    f"arXiv transient HTTP status {error.code}",
                    retry_after=_retry_after(error.headers.get("Retry-After")),
                ) from error
            raise ExternalServiceError(
                f"arXiv request failed with HTTP status {error.code}"
            ) from error
        except (URLError, OSError) as error:
            reason = getattr(error, "reason", error)
            raise TransientArxivError(
                f"arXiv network request failed: {type(reason).__name__}"
            ) from error


@dataclass(slots=True)
class ArxivClient:
    """Synchronous client; one instance issues one request at a time by design."""

    transport: Transport = field(default_factory=UrlLibTransport)
    monotonic: Callable[[], float] = time.monotonic
    sleep: Callable[[float], None] = time.sleep
    minimum_interval_seconds: float = 3.0
    timeout_seconds: float = 20.0
    retries: int = 2
    max_response_bytes: int = MAX_RESPONSE_BYTES
    _last_request_at: float | None = field(default=None, init=False)

    def query(self, search_query: str, start: int, maximum: int) -> tuple[ArxivCandidate, ...]:
        """Retrieve one bounded page after the required serialized request interval."""

        if maximum < 1:
            raise ValueError("maximum must be positive")
        if self.max_response_bytes < 1:
            raise ValueError("max_response_bytes must be positive")
        parameters = urlencode(
            {
                "search_query": search_query,
                "start": str(start),
                "max_results": str(maximum),
                "sortBy": "submittedDate",
                "sortOrder": "descending",
            }
        )
        url = f"{_ENDPOINT}?{parameters}"
        for attempt in range(self.retries + 1):
            # The first attempt is immediate; retries and subsequent pages are spaced.
            if attempt or self._last_request_at is not None:
                self._wait_for_slot()
            self._last_request_at = self.monotonic()
            try:
                payload = self.transport.get(url, self.timeout_seconds)
                if len(payload) > self.max_response_bytes:
                    raise ExternalServiceError("arXiv response exceeded the byte limit")
                return parse_feed(payload)
            except TransientArxivError as error:
                if attempt == self.retries:
                    raise
                backoff = min(2.0**attempt, 4.0)
                retry_after = error.retry_after or 0.0
                self.sleep(min(max(backoff, retry_after), 30.0))
        raise AssertionError("unreachable")

    def _wait_for_slot(self) -> None:
        if self._last_request_at is None:
            return
        remaining = self.minimum_interval_seconds - (self.monotonic() - self._last_request_at)
        if remaining > 0:
            self.sleep(remaining)


def category_query(category: str, start_gmt: str, end_gmt: str) -> str:
    """Build a public category/submission-date query without exposing local profile text."""

    return f"cat:{category} AND submittedDate:[{start_gmt} TO {end_gmt}]"


def _retry_after(value: str | None) -> float | None:
    if value is None:
        return None
    try:
        seconds = float(value.strip())
    except ValueError:
        return None
    return seconds if 0 <= seconds <= 300 else None
