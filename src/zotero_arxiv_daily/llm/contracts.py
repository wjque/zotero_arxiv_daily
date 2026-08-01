"""Validate model output as untrusted proposals bound to known candidate IDs."""

from __future__ import annotations

import json
from dataclasses import dataclass

from zotero_arxiv_daily.arxiv.ids import parse_arxiv_id
from zotero_arxiv_daily.core.errors import ConfigurationError, ExternalServiceError


@dataclass(frozen=True, slots=True)
class ModelProposal:
    arxiv_id: str
    quality: float
    summary: str
    reason: str


def parse_proposals(payload: str, allowed_ids: frozenset[str]) -> tuple[ModelProposal, ...]:
    """Accept only a complete, bounded JSON list for the candidates sent to the model."""

    try:
        value = json.loads(payload)
    except json.JSONDecodeError as error:
        raise ExternalServiceError("model returned invalid JSON") from error
    if not isinstance(value, list) or len(value) > len(allowed_ids):
        raise ExternalServiceError("model response has an invalid proposal count")
    proposals: list[ModelProposal] = []
    for item in value:
        if not isinstance(item, dict) or set(item) != {"arxiv_id", "quality", "summary", "reason"}:
            raise ExternalServiceError("model response has unsupported fields")
        identifier, quality, summary, reason = item.values()
        if (
            not isinstance(identifier, str)
            or not isinstance(quality, (int, float))
            or not 0 <= quality <= 1
        ):
            raise ExternalServiceError(
                "model proposal is invalid or outside the requested candidates"
            )
        try:
            canonical_id = parse_arxiv_id(identifier).canonical
        except ConfigurationError as error:
            raise ExternalServiceError(
                "model proposal is invalid or outside the requested candidates"
            ) from error
        if canonical_id not in allowed_ids:
            raise ExternalServiceError(
                "model proposal is invalid or outside the requested candidates"
            )
        if (
            not isinstance(summary, str)
            or not isinstance(reason, str)
            or len(summary) > 800
            or len(reason) > 400
        ):
            raise ExternalServiceError("model proposal text exceeds bounds")
        proposals.append(ModelProposal(canonical_id, float(quality), summary, reason))
    proposal_ids = frozenset(proposal.arxiv_id for proposal in proposals)
    if len(proposal_ids) != len(proposals):
        raise ExternalServiceError("model response contains duplicate candidates")
    if proposal_ids != allowed_ids:
        raise ExternalServiceError("model response does not cover every requested candidate")
    return tuple(proposals)
