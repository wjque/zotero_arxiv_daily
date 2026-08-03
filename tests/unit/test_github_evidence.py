from __future__ import annotations

from datetime import UTC, datetime

import pytest

from zotero_arxiv_daily.core.errors import ExternalServiceError
from zotero_arxiv_daily.evidence.github import (
    GitHubNotFoundError,
    GitHubRateLimitError,
    GitHubRepositoryClient,
)
from zotero_arxiv_daily.evidence.models import EvidenceAvailability, EvidenceValue

_NOW = datetime(2026, 8, 3, tzinfo=UTC)
_PAYLOAD = b"""{
  "full_name":"example/project",
  "license":{"key":"mit"},
  "archived":false,
  "has_wiki":true,
  "pushed_at":"2026-07-20T00:00:00Z"
}"""


class Transport:
    def __init__(self, responses: list[bytes | Exception]) -> None:
        self.responses = responses
        self.urls: list[str] = []

    def get(self, url: str, timeout_seconds: float) -> bytes:
        self.urls.append(url)
        response = self.responses.pop(0)
        if isinstance(response, Exception):
            raise response
        return response


def _association() -> EvidenceValue:
    return EvidenceValue(EvidenceAvailability.AVAILABLE, 1.0, _NOW, "manual-verified", True)


def test_github_inspects_only_a_verified_exact_repository_association() -> None:
    transport = Transport([_PAYLOAD])

    evidence = GitHubRepositoryClient(transport, retries=0).fetch(
        "arxiv:2401.00001", "https://github.com/example/project", _association(), _NOW
    )

    assert transport.urls == ["https://api.github.com/repos/example/project"]
    assert evidence.repository is not None
    assert evidence.repository.license_present.claim is True
    assert evidence.repository.archived.claim is False
    assert evidence.repository.documentation.claim is True
    assert evidence.repository.maintained.claim is True
    assert evidence.repository.releases.availability is EvidenceAvailability.UNKNOWN


def test_github_rejects_unverified_or_mismatched_associations_without_trusting_a_url() -> None:
    transport = Transport([])
    unverified = EvidenceValue(EvidenceAvailability.UNKNOWN, 0.0, _NOW, "manual-verified")

    with pytest.raises(ValueError, match="verified"):
        GitHubRepositoryClient(transport, retries=0).fetch(
            "arxiv:2401.00001", "https://github.com/example/project", unverified, _NOW
        )
    with pytest.raises(ExternalServiceError, match="mismatched repository"):
        GitHubRepositoryClient(
            Transport([_PAYLOAD.replace(b"example/project", b"other/project")]), retries=0
        ).fetch("arxiv:2401.00001", "https://github.com/example/project", _association(), _NOW)
    assert transport.urls == []


def test_github_retries_only_short_rate_limit_delays() -> None:
    sleeps: list[float] = []
    client = GitHubRepositoryClient(
        Transport([GitHubRateLimitError(2.0), _PAYLOAD]),
        sleep=sleeps.append,
        minimum_interval_seconds=0,
    )

    evidence = client.fetch(
        "arxiv:2401.00001", "https://github.com/example/project", _association(), _NOW
    )

    assert evidence.repository is not None
    assert sleeps == [2.0]


def test_github_not_found_hides_the_now_unverified_repository_url() -> None:
    evidence = GitHubRepositoryClient(Transport([GitHubNotFoundError("missing")]), retries=0).fetch(
        "arxiv:2401.00001", "https://github.com/example/project", _association(), _NOW
    )

    assert evidence.repository is not None
    assert evidence.repository.repository_url is None
    assert evidence.repository.association.availability is EvidenceAvailability.UNKNOWN
