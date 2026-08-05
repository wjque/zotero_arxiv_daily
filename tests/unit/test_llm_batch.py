from __future__ import annotations

import pytest

from zotero_arxiv_daily.core.errors import ExternalServiceError
from zotero_arxiv_daily.llm.batch import pack_complete_records, propose_bounded
from zotero_arxiv_daily.llm.contracts import ProviderCompletion


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
        propose_bounded(provider, candidates, batch_size=1, max_requests=2, retries=0)


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


def test_provider_usage_is_aggregated_when_available() -> None:
    class UsageProvider:
        def propose(self, candidates: list[dict[str, object]]) -> ProviderCompletion:
            return ProviderCompletion(
                '{"proposals":[{"arxiv_id":"2401.00001","quality":1,"summary":"s","reason":"r"}]}',
                input_tokens=12,
                output_tokens=7,
                latency_seconds=0.25,
            )

    _, usage = propose_bounded(UsageProvider(), [{"arxiv_id": "2401.00001"}])

    assert usage.actual_input_tokens == 12
    assert usage.actual_output_tokens == 7
    assert usage.latency_seconds == 0.25


def test_invalid_structured_response_retries_once() -> None:
    provider = Provider(
        [
            "not-json",
            '{"proposals":[{"arxiv_id":"2401.00001","quality":1,"summary":"s","reason":"r"}]}',
        ]
    )

    proposals, usage = propose_bounded(provider, [{"arxiv_id": "2401.00001"}], retries=1)

    assert proposals[0].arxiv_id == "2401.00001"
    assert usage.requests == 2


def test_complete_record_packing_never_truncates_title_or_abstract() -> None:
    records: list[dict[str, object]] = [
        {
            "arxiv_id": "2401.00001",
            "title": "A" * 180,
            "summary": "The principal result appears at the end. " + "B" * 240,
        },
        {"arxiv_id": "2401.00002", "title": "Second", "summary": "Abstract"},
    ]

    batches = pack_complete_records(records, max_records=1, max_tokens=300)

    assert len(batches) == 2
    assert batches[0][0]["title"] == records[0]["title"]
    assert batches[0][0]["summary"] == records[0]["summary"]


def test_oversized_complete_record_fails_explicitly() -> None:
    with pytest.raises(ExternalServiceError, match="exceeds"):
        pack_complete_records(
            [{"arxiv_id": "2401.00001", "title": "x", "summary": "y" * 100}],
            max_records=1,
            max_tokens=10,
        )
