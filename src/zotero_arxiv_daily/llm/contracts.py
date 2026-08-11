"""Validate model output as untrusted proposals bound to known candidate IDs."""

from __future__ import annotations

import json
from dataclasses import dataclass
from enum import StrEnum

from zotero_arxiv_daily.arxiv.ids import parse_arxiv_id
from zotero_arxiv_daily.core.errors import ConfigurationError, ExternalServiceError


@dataclass(frozen=True, slots=True)
class ProviderCompletion:
    """Provider-neutral completion content and optional measured usage."""

    content: str
    input_tokens: int | None = None
    output_tokens: int | None = None
    latency_seconds: float | None = None

    def __post_init__(self) -> None:
        if not self.content.strip():
            raise ValueError("provider completion content must not be empty")
        for value in (self.input_tokens, self.output_tokens):
            if value is not None and value < 0:
                raise ValueError("provider token usage must not be negative")
        if self.latency_seconds is not None and self.latency_seconds < 0:
            raise ValueError("provider latency must not be negative")


@dataclass(frozen=True, slots=True)
class ModelProposal:
    arxiv_id: str
    quality: float
    summary: str
    reason: str


class QualityDimension(StrEnum):
    CONTRIBUTION_CLARITY = "contribution_clarity"
    NOVELTY = "novelty"
    SOLUTION_ADVANCE = "solution_advance"
    TECHNICAL_DEPTH = "technical_depth"
    INSIGHT_PLAUSIBILITY = "insight_plausibility"
    METHODOLOGICAL_EVIDENCE = "methodological_evidence"
    EMPIRICAL_EVIDENCE = "empirical_evidence"
    LIMITATIONS = "limitations"


_LEGACY_QUALITY_DIMENSIONS = (
    QualityDimension.CONTRIBUTION_CLARITY,
    QualityDimension.NOVELTY,
    QualityDimension.INSIGHT_PLAUSIBILITY,
    QualityDimension.METHODOLOGICAL_EVIDENCE,
    QualityDimension.EMPIRICAL_EVIDENCE,
    QualityDimension.LIMITATIONS,
)
_VALUE_QUALITY_DIMENSIONS = tuple(QualityDimension)


@dataclass(frozen=True, slots=True)
class JudgeAssessment:
    """An uncertain, evidence-bound assessment; unknown is never silently a low score."""

    arxiv_id: str
    dimensions: tuple[tuple[QualityDimension, float | None], ...]
    uncertainty: float
    evidence_fields: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class Explanation:
    """Final-only prose attributed to bounded candidate or evidence fields."""

    arxiv_id: str
    summary: str
    reason: str
    limitation: str
    evidence_fields: tuple[str, ...]


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
        identifier = item["arxiv_id"]
        quality = item["quality"]
        summary = item["summary"]
        reason = item["reason"]
        if not isinstance(identifier, str):
            raise ExternalServiceError("model proposal arxiv_id must be a string")
        if not isinstance(quality, (int, float)) or not 0 <= quality <= 1:
            raise ExternalServiceError("model proposal quality is invalid")
        try:
            canonical_id = parse_arxiv_id(identifier).canonical
        except ConfigurationError as error:
            raise ExternalServiceError("model proposal arxiv_id is malformed") from error
        if canonical_id not in allowed_ids:
            raise ExternalServiceError("model proposal arxiv_id was not requested")
        if (
            not isinstance(summary, str)
            or not isinstance(reason, str)
            or not 24 <= len(summary) <= 800
            or not 24 <= len(reason) <= 400
        ):
            raise ExternalServiceError("model proposal text is invalid")
        if _generic_reason(reason):
            raise ExternalServiceError("model proposal reason is generic")
        proposals.append(ModelProposal(canonical_id, float(quality), summary, reason))
    proposal_ids = frozenset(proposal.arxiv_id for proposal in proposals)
    if len(proposal_ids) != len(proposals):
        raise ExternalServiceError("model response contains duplicate candidates")
    if proposal_ids != allowed_ids:
        raise ExternalServiceError("model response does not cover every requested candidate")
    return tuple(proposals)


def parse_judgments(
    payload: str,
    allowed_ids: frozenset[str],
    allowed_evidence_fields: frozenset[str],
    *,
    contract: str = "judge-v5",
) -> tuple[JudgeAssessment, ...]:
    """Validate versioned judge output without allowing new facts or identifiers."""

    entries = _entries(payload, "judgments", allowed_ids)
    expected = {"arxiv_id", "dimensions", "uncertainty", "evidence_fields"}
    results: list[JudgeAssessment] = []
    for entry in entries:
        if set(entry) != expected:
            raise ExternalServiceError("judge response has unsupported fields")
        dimensions = _dimensions(entry["dimensions"], contract)
        uncertainty = entry["uncertainty"]
        if not isinstance(uncertainty, (int, float)) or isinstance(uncertainty, bool):
            raise ExternalServiceError("judge uncertainty is invalid")
        if not 0 <= uncertainty <= 1:
            raise ExternalServiceError("judge uncertainty is invalid")
        results.append(
            JudgeAssessment(
                _allowed_id(entry["arxiv_id"], allowed_ids),
                dimensions,
                float(uncertainty),
                _evidence_fields(entry["evidence_fields"], allowed_evidence_fields, minimum=2),
            )
        )
    _complete_unique(results, allowed_ids)
    return tuple(results)


def parse_explanations(
    payload: str,
    allowed_ids: frozenset[str],
    allowed_evidence_fields: frozenset[str],
) -> tuple[Explanation, ...]:
    """Validate versioned final-only prose and its bounded evidence references."""

    entries = _entries(payload, "explanations", allowed_ids)
    expected = {"arxiv_id", "summary", "reason", "limitation", "evidence_fields"}
    results: list[Explanation] = []
    for entry in entries:
        if set(entry) != expected:
            raise ExternalServiceError("explanation response has unsupported fields")
        text = tuple(entry[field] for field in ("summary", "reason", "limitation"))
        if not all(isinstance(value, str) and 20 <= len(value) <= 800 for value in text):
            raise ExternalServiceError("explanation text is invalid")
        if _generic_reason(str(text[1])):
            raise ExternalServiceError("explanation reason is generic")
        results.append(
            Explanation(
                _allowed_id(entry["arxiv_id"], allowed_ids),
                str(text[0]),
                str(text[1]),
                str(text[2]),
                _evidence_fields(entry["evidence_fields"], allowed_evidence_fields, minimum=2),
            )
        )
    _complete_unique(results, allowed_ids)
    return tuple(results)


def _entries(payload: str, key: str, allowed_ids: frozenset[str]) -> list[dict[str, object]]:
    try:
        value = json.loads(payload)
    except json.JSONDecodeError as error:
        raise ExternalServiceError("model returned invalid JSON") from error
    entries = value.get(key) if isinstance(value, dict) and set(value) == {key} else None
    if not isinstance(entries, list) or len(entries) != len(allowed_ids):
        raise ExternalServiceError("model response has an invalid entry count")
    if not all(isinstance(entry, dict) for entry in entries):
        raise ExternalServiceError("model response entry is invalid")
    return entries


def _allowed_id(value: object, allowed_ids: frozenset[str]) -> str:
    if not isinstance(value, str):
        raise ExternalServiceError("model response arxiv_id must be a string")
    try:
        identifier = parse_arxiv_id(value).canonical
    except ConfigurationError as error:
        raise ExternalServiceError("model response arxiv_id is malformed") from error
    if identifier not in allowed_ids:
        raise ExternalServiceError("model response arxiv_id was not requested")
    return identifier


def _dimensions(value: object, contract: str) -> tuple[tuple[QualityDimension, float | None], ...]:
    contract_dimensions: tuple[QualityDimension, ...]
    if contract in {"judge-v3", "judge-v4"}:
        contract_dimensions = _LEGACY_QUALITY_DIMENSIONS
    elif contract == "judge-v5":
        contract_dimensions = _VALUE_QUALITY_DIMENSIONS
    else:
        raise ExternalServiceError("judge contract is unsupported")
    expected = {dimension.value for dimension in contract_dimensions}
    if not isinstance(value, dict) or set(value) != expected:
        raise ExternalServiceError("judge dimensions are invalid")
    dimensions: list[tuple[QualityDimension, float | None]] = []
    for dimension in contract_dimensions:
        score = value[dimension.value]
        if score is not None and (
            not isinstance(score, (int, float)) or isinstance(score, bool) or not 0 <= score <= 1
        ):
            raise ExternalServiceError("judge dimension score is invalid")
        dimensions.append((dimension, float(score) if score is not None else None))
    return tuple(dimensions)


def _evidence_fields(
    value: object, allowed: frozenset[str], *, minimum: int = 1
) -> tuple[str, ...]:
    if (
        not isinstance(value, list)
        or not minimum <= len(value) <= 6
        or not all(isinstance(field, str) and field in allowed for field in value)
    ):
        raise ExternalServiceError("model evidence references are invalid")
    fields = tuple(value)
    if len(set(fields)) != len(fields):
        raise ExternalServiceError("model evidence references are duplicated")
    return fields


def _complete_unique(
    values: list[JudgeAssessment] | list[Explanation], allowed_ids: frozenset[str]
) -> None:
    identifiers = [value.arxiv_id for value in values]
    if len(set(identifiers)) != len(identifiers):
        raise ExternalServiceError("model response contains duplicate candidates")
    if frozenset(identifiers) != allowed_ids:
        raise ExternalServiceError("model response does not cover every requested candidate")


def _generic_reason(value: str) -> bool:
    normalized = value.casefold().strip()
    generic = {
        "this paper is relevant to your interests.",
        "this is a relevant paper for you.",
        "this paper is relevant.",
        "this is a useful paper.",
    }
    if not any("a" <= character <= "z" for character in normalized):
        return False
    words = [word for word in normalized.replace("-", " ").split() if word.isalpha()]
    return normalized in generic or len(set(words)) < 5
