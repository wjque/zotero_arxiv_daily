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


def propose_bounded(
    provider: ProposalProvider,
    candidates: list[dict[str, object]],
    *,
    batch_size: int = 40,
    max_requests: int = 2,
    max_tokens: int = 12_000,
    retries: int = 1,
) -> tuple[tuple[ModelProposal, ...], ModelUsage]:
    """Fail the whole operation if any model batch is unavailable or invalid."""

    if batch_size < 1 or max_requests < 1 or max_tokens < 1 or retries < 0:
        raise ValueError("model batch limits must be positive and retries must not be negative")
    if len(candidates) > batch_size * max_requests:
        raise ExternalServiceError("candidate set exceeds model request budget")
    proposals: list[ModelProposal] = []
    tokens = 0
    requests = 0
    for offset in range(0, len(candidates), batch_size):
        batch = candidates[offset : offset + batch_size]
        encoded = json.dumps(batch, separators=(",", ":"))
        tokens += len(encoded) // 4
        if tokens > max_tokens:
            raise ExternalServiceError("model token budget exceeded")
        for attempt in range(retries + 1):
            requests += 1
            try:
                content = provider.propose(batch)
            except ExternalServiceError:
                if attempt == retries:
                    raise
                continue
            break
        try:
            value = json.loads(content)
            raw = json.dumps(value["proposals"])
        except (KeyError, TypeError, json.JSONDecodeError) as error:
            raise ExternalServiceError("model response lacks proposals") from error
        proposals.extend(parse_proposals(raw, frozenset(str(item["arxiv_id"]) for item in batch)))
    return tuple(proposals), ModelUsage(requests, 0, tokens)
