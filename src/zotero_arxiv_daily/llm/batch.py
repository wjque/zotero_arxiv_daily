"""Bounded model-batch execution with all-or-nothing validation."""

from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Protocol

from zotero_arxiv_daily.core.errors import ExternalServiceError
from zotero_arxiv_daily.llm.contracts import ModelProposal, parse_proposals


class ProposalProvider(Protocol):
    def propose(self, candidates: list[dict[str, object]]) -> str: ...


@dataclass(frozen=True, slots=True)
class ModelUsage:
    requests: int
    cache_hits: int
    estimated_tokens: int
    retry_count: int = 0


DEFAULT_REQUEST_TOKEN_LIMIT = 12_000
DEFAULT_REQUEST_BYTE_LIMIT = 64 * 1024


def pack_complete_records(
    records: tuple[dict[str, object], ...] | list[dict[str, object]],
    *,
    max_records: int,
    max_tokens: int,
    max_bytes: int = DEFAULT_REQUEST_BYTE_LIMIT,
) -> tuple[tuple[dict[str, object], ...], ...]:
    """Pack whole records deterministically without slicing title or abstract fields."""

    if max_records < 1 or max_tokens < 1 or max_bytes < 1:
        raise ValueError("record batch limits must be positive")
    batches: list[tuple[dict[str, object], ...]] = []
    current: list[dict[str, object]] = []
    current_tokens = 0
    current_bytes = 2
    for record in records:
        encoded = json.dumps(record, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
        record_tokens = max(1, len(encoded) // 4)
        record_bytes = len(encoded)
        if record_tokens > max_tokens or record_bytes + 2 > max_bytes:
            identifier = str(record.get("arxiv_id", "unknown"))
            raise ExternalServiceError(
                f"record {identifier} exceeds the complete-record request budget"
            )
        if current and (
            len(current) >= max_records
            or current_tokens + record_tokens > max_tokens
            or current_bytes + record_bytes + 1 > max_bytes
        ):
            batches.append(tuple(current))
            current = []
            current_tokens = 0
            current_bytes = 2
        current.append(record)
        current_tokens += record_tokens
        current_bytes += record_bytes + (1 if len(current) > 1 else 0)
    if current:
        batches.append(tuple(current))
    return tuple(batches)


def propose_bounded(
    provider: ProposalProvider,
    candidates: list[dict[str, object]],
    *,
    batch_size: int = 40,
    max_requests: int = 2,
    max_tokens: int = DEFAULT_REQUEST_TOKEN_LIMIT,
    max_request_bytes: int = DEFAULT_REQUEST_BYTE_LIMIT,
    retries: int = 1,
) -> tuple[tuple[ModelProposal, ...], ModelUsage]:
    """Fail the whole operation if any model batch is unavailable or invalid."""

    if batch_size < 1 or max_requests < 1 or max_tokens < 1 or max_request_bytes < 1 or retries < 0:
        raise ValueError("model batch limits must be positive and retries must not be negative")
    batches = pack_complete_records(
        candidates,
        max_records=batch_size,
        max_tokens=max_tokens,
        max_bytes=max_request_bytes,
    )
    if len(batches) > max_requests:
        raise ExternalServiceError("candidate set exceeds model request budget")
    proposals: list[ModelProposal] = []
    tokens = sum(
        max(1, len(json.dumps(record, ensure_ascii=False, separators=(",", ":"))) // 4)
        for record in candidates
    )
    requests = 0
    retries_used = 0
    for batch in batches:
        for attempt in range(retries + 1):
            requests += 1
            try:
                content = provider.propose(list(batch))
                value = json.loads(content)
                raw = json.dumps(value["proposals"])
                parsed = parse_proposals(raw, frozenset(str(item["arxiv_id"]) for item in batch))
            except (KeyError, TypeError, json.JSONDecodeError) as error:
                failure = ExternalServiceError("model response lacks proposals")
                if attempt == retries:
                    raise failure from error
                retries_used += 1
                continue
            except ExternalServiceError:
                if attempt == retries:
                    raise
                retries_used += 1
                continue
            break
        proposals.extend(parsed)
    return tuple(proposals), ModelUsage(requests, 0, tokens, retries_used)
