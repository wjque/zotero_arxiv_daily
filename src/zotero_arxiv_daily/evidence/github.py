"""Read bounded GitHub repository facts only after a verified paper association exists."""

from __future__ import annotations

import json
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Protocol, cast
from urllib.error import HTTPError, URLError
from urllib.parse import urlparse
from urllib.request import Request, urlopen

from zotero_arxiv_daily.core.errors import ExternalServiceError
from zotero_arxiv_daily.core.time import require_aware_utc
from zotero_arxiv_daily.evidence.models import (
    EvidenceAvailability,
    EvidenceValue,
    PublicPaperEvidence,
    RepositoryEvidence,
)

_ENDPOINT = "https://api.github.com/repos"
_PROVIDER = "github"
_ADAPTER_VERSION = "v1"
_MAX_RESPONSE_BYTES = 64 * 1024
_MAINTENANCE_WINDOW = timedelta(days=180)


class TransientGitHubError(ExternalServiceError):
    """A GitHub response that may receive one bounded retry."""


class GitHubNotFoundError(ExternalServiceError):
    """The verified repository URL no longer resolves to a public repository."""


class GitHubRateLimitError(TransientGitHubError):
    """A GitHub rate-limit response with an optional retry delay."""

    def __init__(self, retry_after_seconds: float | None) -> None:
        super().__init__("GitHub rate limited the evidence request")
        self.retry_after_seconds = retry_after_seconds


class Transport(Protocol):
    def get(self, url: str, timeout_seconds: float) -> bytes: ...


class UrlLibTransport:
    """Bound public metadata responses and do not fetch repository content."""

    def get(self, url: str, timeout_seconds: float) -> bytes:
        request = Request(
            url,
            headers={
                "Accept": "application/vnd.github+json",
                "User-Agent": "zotero-arxiv-daily/evidence-v1",
                "X-GitHub-Api-Version": "2022-11-28",
            },
            method="GET",
        )
        try:
            with urlopen(request, timeout=timeout_seconds) as response:  # noqa: S310
                if not 200 <= response.getcode() < 300:
                    raise ExternalServiceError("GitHub returned an unsuccessful status")
                payload = cast(bytes, response.read(_MAX_RESPONSE_BYTES + 1))
                if len(payload) > _MAX_RESPONSE_BYTES:
                    raise ExternalServiceError("GitHub response exceeds the evidence size budget")
                return payload
        except HTTPError as error:
            if error.code == 404:
                raise GitHubNotFoundError("GitHub has no matching public repository") from error
            if error.code in {403, 429}:
                retry_after = error.headers.get("Retry-After")
                try:
                    delay = float(retry_after) if retry_after is not None else None
                except ValueError:
                    delay = None
                raise GitHubRateLimitError(delay) from error
            if error.code >= 500:
                raise TransientGitHubError("GitHub failed transiently") from error
            raise ExternalServiceError("GitHub request failed") from error
        except (URLError, OSError) as error:
            raise TransientGitHubError("GitHub network request failed") from error


@dataclass(slots=True)
class GitHubRepositoryClient:
    """Inspect structured repository facts without creating an association or quality score."""

    transport: Transport = field(default_factory=UrlLibTransport)
    monotonic: Callable[[], float] = time.monotonic
    sleep: Callable[[float], None] = time.sleep
    minimum_interval_seconds: float = 1.0
    timeout_seconds: float = 15.0
    retries: int = 1
    maximum_retry_after_seconds: float = 5.0
    _last_request_at: float | None = field(default=None, init=False)

    def fetch(
        self,
        paper_id: str,
        repository_url: str,
        association: EvidenceValue,
        observed_at: datetime,
    ) -> PublicPaperEvidence:
        """Validate the repository identity and return bounded reproducibility facts."""

        now = require_aware_utc(observed_at, "observed_at")
        owner, repository = _repository_path(repository_url)
        if association.availability is not EvidenceAvailability.AVAILABLE:
            raise ValueError("GitHub inspection requires a verified repository association")
        try:
            payload = self._request(owner, repository)
        except GitHubNotFoundError:
            return unknown_repository_evidence(paper_id, now)
        return _parse_response(
            paper_id, repository_url, owner, repository, association, payload, now
        )

    def _request(self, owner: str, repository: str) -> bytes:
        self._wait_for_slot()
        url = f"{_ENDPOINT}/{owner}/{repository}"
        for attempt in range(self.retries + 1):
            try:
                payload = self.transport.get(url, self.timeout_seconds)
                self._last_request_at = self.monotonic()
                return payload
            except GitHubRateLimitError as error:
                self._last_request_at = self.monotonic()
                if (
                    attempt == self.retries
                    or error.retry_after_seconds is None
                    or error.retry_after_seconds > self.maximum_retry_after_seconds
                ):
                    raise
                self.sleep(error.retry_after_seconds)
            except TransientGitHubError:
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


def unknown_repository_evidence(paper_id: str, observed_at: datetime) -> PublicPaperEvidence:
    """Do not expose a repository URL after a missing or invalid public lookup."""

    now = require_aware_utc(observed_at, "observed_at")
    unknown = EvidenceValue(EvidenceAvailability.UNKNOWN, 0.0, now, _PROVIDER)
    return PublicPaperEvidence(
        paper_id,
        1,
        now,
        now + timedelta(days=1),
        RepositoryEvidence(None, unknown, unknown, unknown, unknown, unknown, unknown),
        provider=_PROVIDER,
        adapter_version=_ADAPTER_VERSION,
    )


def _parse_response(
    paper_id: str,
    repository_url: str,
    owner: str,
    repository: str,
    association: EvidenceValue,
    payload: bytes,
    observed_at: datetime,
) -> PublicPaperEvidence:
    try:
        value = json.loads(payload)
    except json.JSONDecodeError as error:
        raise ExternalServiceError("GitHub returned malformed JSON") from error
    if not isinstance(value, dict):
        raise ExternalServiceError("GitHub response root is invalid")
    full_name = value.get("full_name")
    if not isinstance(full_name, str) or full_name.casefold() != f"{owner}/{repository}".casefold():
        raise ExternalServiceError("GitHub returned a mismatched repository identity")
    now = require_aware_utc(observed_at, "observed_at")
    return PublicPaperEvidence(
        paper_id,
        1,
        now,
        now + timedelta(days=7),
        RepositoryEvidence(
            repository_url,
            association,
            _license_evidence(value, now),
            _boolean_evidence(value, "archived", now),
            EvidenceValue(EvidenceAvailability.UNKNOWN, 0.0, now, _PROVIDER),
            _boolean_evidence(value, "has_wiki", now),
            _maintenance_evidence(value, now),
        ),
        provider=_PROVIDER,
        adapter_version=_ADAPTER_VERSION,
    )


def _repository_path(value: str) -> tuple[str, str]:
    parsed = urlparse(value)
    parts = tuple(part for part in parsed.path.split("/") if part)
    if parsed.scheme != "https" or parsed.hostname != "github.com" or len(parts) != 2:
        raise ValueError("repository URL must identify one HTTPS GitHub repository")
    return parts


def _license_evidence(value: dict[str, object], observed_at: datetime) -> EvidenceValue:
    license_value = value.get("license")
    now = require_aware_utc(observed_at, "observed_at")
    if license_value is None:
        return EvidenceValue(EvidenceAvailability.AVAILABLE, 0.8, now, _PROVIDER, False)
    if isinstance(license_value, dict):
        return EvidenceValue(EvidenceAvailability.AVAILABLE, 0.8, now, _PROVIDER, True)
    return EvidenceValue(EvidenceAvailability.UNKNOWN, 0.0, now, _PROVIDER)


def _boolean_evidence(value: dict[str, object], field: str, observed_at: datetime) -> EvidenceValue:
    source = value.get(field)
    now = require_aware_utc(observed_at, "observed_at")
    if isinstance(source, bool):
        return EvidenceValue(EvidenceAvailability.AVAILABLE, 0.8, now, _PROVIDER, source)
    return EvidenceValue(EvidenceAvailability.UNKNOWN, 0.0, now, _PROVIDER)


def _maintenance_evidence(value: dict[str, object], observed_at: datetime) -> EvidenceValue:
    pushed_at = value.get("pushed_at")
    now = require_aware_utc(observed_at, "observed_at")
    if not isinstance(pushed_at, str):
        return EvidenceValue(EvidenceAvailability.UNKNOWN, 0.0, now, _PROVIDER)
    try:
        pushed = datetime.fromisoformat(pushed_at.replace("Z", "+00:00"))
        maintained = now - require_aware_utc(pushed, "pushed_at") <= _MAINTENANCE_WINDOW
    except ValueError:
        return EvidenceValue(EvidenceAvailability.UNKNOWN, 0.0, now, _PROVIDER)
    return EvidenceValue(EvidenceAvailability.AVAILABLE, 0.7, now, _PROVIDER, maintained)
