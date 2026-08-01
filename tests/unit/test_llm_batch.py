from __future__ import annotations

import pytest

from zotero_arxiv_daily.core.errors import ExternalServiceError
from zotero_arxiv_daily.llm.batch import propose_bounded


class Provider:
    def __init__(self, responses: list[str]) -> None:
        self.responses = responses

    def propose(self, candidates: list[dict[str, object]]) -> str:
        return self.responses.pop(0)


def test_partial_invalid_batch_fails_without_returning_partial_proposals() -> None:
    candidates: list[dict[str, object]] = [
        {"arxiv_id": "2401.00001"},
        {"arxiv_id": "2401.00002"},
    ]
    provider = Provider(
        [
            '{"proposals":[{"arxiv_id":"2401.00001","quality":1,"summary":"s","reason":"r"}]}',
            "not-json",
        ]
    )

    with pytest.raises(ExternalServiceError):
        propose_bounded(provider, candidates, batch_size=1, max_requests=2)


def test_transient_provider_failure_retries_once_within_a_bounded_budget() -> None:
    class RetryingProvider:
        def __init__(self) -> None:
            self.calls = 0

        def propose(self, candidates: list[dict[str, object]]) -> str:
            self.calls += 1
            if self.calls == 1:
                raise ExternalServiceError("temporary outage")
            return (
                '{"proposals":[{"arxiv_id":"2401.00001","quality":1,"summary":"s","reason":"r"}]}'
            )

    _, usage = propose_bounded(RetryingProvider(), [{"arxiv_id": "2401.00001"}], retries=1)

    assert usage.requests == 2
