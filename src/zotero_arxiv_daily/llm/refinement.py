"""Provider-neutral, failure-atomic runners for judge-v1 and explain-v1 contracts."""

from __future__ import annotations

import json
from collections.abc import Callable
from dataclasses import dataclass
from typing import Protocol

from zotero_arxiv_daily.core.errors import ExternalServiceError
from zotero_arxiv_daily.llm.cache import ProposalCache
from zotero_arxiv_daily.llm.contracts import (
    Explanation,
    JudgeAssessment,
    parse_explanations,
    parse_judgments,
)


class StructuredProvider(Protocol):
    def complete(self, contract: str, records: list[dict[str, object]]) -> str: ...


@dataclass(frozen=True, slots=True)
class RefinementUsage:
    requests: int
    cache_hits: int
    estimated_input_tokens: int
    estimated_output_tokens: int


def run_judgments(
    provider: StructuredProvider,
    cache: ProposalCache,
    records: tuple[dict[str, object], ...],
    *,
    cache_keys: dict[str, str],
    allowed_evidence_fields: frozenset[str],
) -> tuple[tuple[JudgeAssessment, ...], RefinementUsage]:
    """Run a complete judge layer; never return or cache a partial result."""

    return _run(
        provider,
        cache,
        records,
        cache_keys=cache_keys,
        contract="judge-v1",
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
) -> tuple[tuple[Explanation, ...], RefinementUsage]:
    """Run final-only explanations under a separate cache namespace."""

    return _run(
        provider,
        cache,
        records,
        cache_keys=cache_keys,
        contract="explain-v1",
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
    payload = provider.complete(contract, missing)
    fresh = _parse(parser, payload, frozenset(str(record["arxiv_id"]) for record in missing))
    if frozenset(value.arxiv_id for value in cached + list(fresh)) != identifiers:
        raise ExternalServiceError("refinement response does not cover every requested candidate")
    encoded = {value.arxiv_id: json.dumps(_entry(value)) for value in fresh}
    for identifier, value in encoded.items():
        cache.put(cache_keys[identifier], _wrap(contract, value))
    return (
        tuple(cached) + tuple(fresh),
        RefinementUsage(
            1,
            len(cached),
            _tokens(missing),
            _tokens([json.loads(value) for value in encoded.values()]),
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
    key = "judgments" if contract == "judge-v1" else "explanations"
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
