"""Provider-neutral, failure-atomic runners for judge-v2 and explain-v2 contracts."""

from __future__ import annotations

import json
from collections.abc import Callable
from dataclasses import dataclass
from typing import Protocol

from zotero_arxiv_daily.core.errors import ExternalServiceError
from zotero_arxiv_daily.llm.batch import (
    DEFAULT_REQUEST_BYTE_LIMIT,
    DEFAULT_REQUEST_TOKEN_LIMIT,
    pack_complete_records,
)
from zotero_arxiv_daily.llm.cache import ProposalCache
from zotero_arxiv_daily.llm.contracts import (
    Explanation,
    JudgeAssessment,
    ProviderCompletion,
    parse_explanations,
    parse_judgments,
)


class StructuredProvider(Protocol):
    def complete(
        self, contract: str, records: list[dict[str, object]]
    ) -> str | ProviderCompletion: ...


JUDGE_CONTRACT = "judge-v2"
EXPLANATION_CONTRACT = "explain-v2"


@dataclass(frozen=True, slots=True)
class RefinementUsage:
    requests: int
    cache_hits: int
    estimated_input_tokens: int
    estimated_output_tokens: int
    retry_count: int = 0
    actual_input_tokens: int | None = None
    actual_output_tokens: int | None = None
    latency_seconds: float | None = None


def run_judgments(
    provider: StructuredProvider,
    cache: ProposalCache,
    records: tuple[dict[str, object], ...],
    *,
    cache_keys: dict[str, str],
    allowed_evidence_fields: frozenset[str],
    batch_size: int = 40,
    max_request_tokens: int = DEFAULT_REQUEST_TOKEN_LIMIT,
    max_request_bytes: int = DEFAULT_REQUEST_BYTE_LIMIT,
    max_requests: int = 4,
    retries: int = 1,
) -> tuple[tuple[JudgeAssessment, ...], RefinementUsage]:
    """Run a complete judge layer; never return or cache a partial result."""

    return _run(
        provider,
        cache,
        records,
        cache_keys=cache_keys,
        contract=JUDGE_CONTRACT,
        batch_size=batch_size,
        max_request_tokens=max_request_tokens,
        max_request_bytes=max_request_bytes,
        max_requests=max_requests,
        retries=retries,
        parser=lambda payload, identifiers: parse_judgments(
            payload, identifiers, allowed_evidence_fields
        ),
    )


def run_explanations(
    provider: StructuredProvider,
    cache: ProposalCache,
    records: tuple[dict[str, object], ...],
    *,
    cache_keys: dict[str, str],
    allowed_evidence_fields: frozenset[str],
    batch_size: int = 20,
    max_request_tokens: int = DEFAULT_REQUEST_TOKEN_LIMIT,
    max_request_bytes: int = DEFAULT_REQUEST_BYTE_LIMIT,
    max_requests: int = 4,
    retries: int = 1,
) -> tuple[tuple[Explanation, ...], RefinementUsage]:
    """Run final-only explanations under a separate cache namespace."""

    return _run(
        provider,
        cache,
        records,
        cache_keys=cache_keys,
        contract=EXPLANATION_CONTRACT,
        batch_size=batch_size,
        max_request_tokens=max_request_tokens,
        max_request_bytes=max_request_bytes,
        max_requests=max_requests,
        retries=retries,
        parser=lambda payload, identifiers: parse_explanations(
            payload, identifiers, allowed_evidence_fields
        ),
    )


def _run[T: JudgeAssessment | Explanation](
    provider: StructuredProvider,
    cache: ProposalCache,
    records: tuple[dict[str, object], ...],
    *,
    cache_keys: dict[str, str],
    contract: str,
    batch_size: int,
    max_request_tokens: int,
    max_request_bytes: int,
    max_requests: int,
    retries: int,
    parser: Callable[[str, frozenset[str]], tuple[T, ...]],
) -> tuple[tuple[T, ...], RefinementUsage]:
    identifiers = _identifiers(records, cache_keys)
    cached: list[T] = []
    missing: list[dict[str, object]] = []
    for record in records:
        identifier = str(record["arxiv_id"])
        payload = cache.get(cache_keys[identifier])
        if payload is None:
            missing.append(record)
            continue
        try:
            cached.extend(_parse(parser, payload, frozenset({identifier})))
        except ExternalServiceError:
            missing.append(record)
    if not missing:
        return tuple(cached), RefinementUsage(0, len(cached), 0, 0)
    batches = pack_complete_records(
        missing,
        max_records=batch_size,
        max_tokens=max_request_tokens,
        max_bytes=max_request_bytes,
    )
    if len(batches) > max_requests:
        raise ExternalServiceError("refinement candidate set exceeds model request budget")
    fresh_values: list[T] = []
    requests = 0
    retries_used = 0
    input_tokens = 0
    output_tokens = 0
    actual_input: list[int] = []
    actual_output: list[int] = []
    latencies: list[float] = []
    for batch in batches:
        batch_ids = frozenset(str(record["arxiv_id"]) for record in batch)
        batch_input_tokens = _tokens(batch)
        for attempt in range(retries + 1):
            requests += 1
            try:
                response = provider.complete(contract, list(batch))
                payload = response.content if isinstance(response, ProviderCompletion) else response
                fresh = _parse(parser, payload, batch_ids)
            except (ExternalServiceError, IndexError) as error:
                if attempt == retries:
                    if isinstance(error, ExternalServiceError):
                        raise
                    raise ExternalServiceError("refinement provider failed") from error
                retries_used += 1
                continue
            fresh_values.extend(fresh)
            input_tokens += batch_input_tokens
            output_tokens += _tokens(json.loads(payload))
            if isinstance(response, ProviderCompletion):
                if response.input_tokens is not None:
                    actual_input.append(response.input_tokens)
                if response.output_tokens is not None:
                    actual_output.append(response.output_tokens)
                if response.latency_seconds is not None:
                    latencies.append(response.latency_seconds)
            break
    fresh = tuple(fresh_values)
    if frozenset(value.arxiv_id for value in cached + list(fresh)) != identifiers or len(
        cached
    ) + len(fresh) != len(identifiers):
        raise ExternalServiceError("refinement response does not cover every requested candidate")
    encoded = {value.arxiv_id: json.dumps(_entry(value)) for value in fresh}
    # Cache writes happen only after every adaptive batch validates successfully.
    for identifier, value in encoded.items():
        cache.put(cache_keys[identifier], _wrap(contract, value))
    return (
        tuple(cached) + tuple(fresh),
        RefinementUsage(
            requests,
            len(cached),
            input_tokens,
            output_tokens,
            retries_used,
            sum(actual_input) if len(actual_input) == len(batches) else None,
            sum(actual_output) if len(actual_output) == len(batches) else None,
            sum(latencies) if latencies else None,
        ),
    )


def _parse[T](
    parser: Callable[[str, frozenset[str]], tuple[T, ...]],
    payload: str,
    identifiers: frozenset[str],
) -> tuple[T, ...]:
    try:
        value = json.loads(payload)
    except json.JSONDecodeError as error:
        raise ExternalServiceError("cached refinement payload is invalid") from error
    if not isinstance(value, dict) or len(value) != 1:
        raise ExternalServiceError("cached refinement payload is invalid")
    return parser(payload, identifiers)


def _wrap(contract: str, value: str) -> str:
    key = "judgments" if contract == JUDGE_CONTRACT else "explanations"
    return json.dumps({key: [json.loads(value)]}, separators=(",", ":"))


def _entry(value: JudgeAssessment | Explanation) -> dict[str, object]:
    if isinstance(value, JudgeAssessment):
        return {
            "arxiv_id": value.arxiv_id,
            "dimensions": {dimension.value: score for dimension, score in value.dimensions},
            "uncertainty": value.uncertainty,
            "evidence_fields": list(value.evidence_fields),
        }
    return {
        "arxiv_id": value.arxiv_id,
        "summary": value.summary,
        "reason": value.reason,
        "limitation": value.limitation,
        "evidence_fields": list(value.evidence_fields),
    }


def _identifiers(
    records: tuple[dict[str, object], ...], cache_keys: dict[str, str]
) -> frozenset[str]:
    identifiers = tuple(str(record.get("arxiv_id", "")) for record in records)
    if (
        not identifiers
        or len(set(identifiers)) != len(identifiers)
        or set(identifiers) != set(cache_keys)
    ):
        raise ValueError("refinement records and cache keys must contain the same unique IDs")
    return frozenset(identifiers)


def _tokens(value: object) -> int:
    return len(json.dumps(value, separators=(",", ":"))) // 4
