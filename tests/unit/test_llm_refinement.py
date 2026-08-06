from __future__ import annotations

import json
from pathlib import Path

import pytest

from zotero_arxiv_daily.core.errors import ExternalServiceError
from zotero_arxiv_daily.llm.cache import ProposalCache
from zotero_arxiv_daily.llm.refinement import run_explanations, run_judgments

_FIELDS = frozenset({"title", "summary", "evidence.context"})
_DIMENSIONS = {
    "contribution_clarity": 0.8,
    "novelty": None,
    "insight_plausibility": 0.7,
    "methodological_evidence": 0.6,
    "empirical_evidence": None,
    "limitations": 0.4,
}


class Provider:
    def __init__(self, responses: list[str | Exception]) -> None:
        self.responses = responses
        self.calls: list[tuple[str, list[dict[str, object]]]] = []

    def complete(self, contract: str, records: list[dict[str, object]]) -> str:
        self.calls.append((contract, records))
        response = self.responses.pop(0)
        if isinstance(response, Exception):
            raise response
        return response


def _records() -> tuple[dict[str, object], ...]:
    return (
        {"arxiv_id": "2401.00001", "title": "Paper one"},
        {"arxiv_id": "2401.00002", "title": "Paper two"},
    )


def _keys() -> dict[str, str]:
    return {"2401.00001": "first", "2401.00002": "second"}


def _judgments(identifiers: tuple[str, ...]) -> str:
    return json.dumps(
        {
            "judgments": [
                {
                    "arxiv_id": identifier,
                    "dimensions": _DIMENSIONS,
                    "uncertainty": 0.2,
                    "evidence_fields": ["title", "summary"],
                }
                for identifier in identifiers
            ]
        }
    )


def test_judge_runner_caches_only_a_complete_validated_response(tmp_path: Path) -> None:
    provider = Provider([_judgments(("2401.00001", "2401.00002"))])
    cache = ProposalCache(tmp_path / "cache.json")

    first, first_usage = run_judgments(
        provider, cache, _records(), cache_keys=_keys(), allowed_evidence_fields=_FIELDS
    )
    second, second_usage = run_judgments(
        provider, cache, _records(), cache_keys=_keys(), allowed_evidence_fields=_FIELDS
    )

    assert [value.arxiv_id for value in first] == ["2401.00001", "2401.00002"]
    assert [value.arxiv_id for value in second] == ["2401.00001", "2401.00002"]
    assert first_usage.requests == 1 and first_usage.estimated_input_tokens > 0
    assert second_usage.requests == 0 and second_usage.cache_hits == 2


def test_judge_runner_does_not_write_partial_or_invalid_output(tmp_path: Path) -> None:
    provider = Provider([_judgments(("2401.00001",))])
    cache = ProposalCache(tmp_path / "cache.json")

    with pytest.raises(ExternalServiceError):
        run_judgments(
            provider, cache, _records(), cache_keys=_keys(), allowed_evidence_fields=_FIELDS
        )

    assert cache.get("first") is None
    assert cache.get("second") is None


def test_explanation_runner_uses_a_distinct_contract_and_cache_namespace(tmp_path: Path) -> None:
    provider = Provider(
        [
            json.dumps(
                {
                    "explanations": [
                        {
                            "arxiv_id": "2401.00001",
                            "summary": "It studies robust ranking under limited feedback.",
                            "reason": (
                                "Its bounded feedback aggregation matches the requested "
                                "weekly update policy."
                            ),
                            "limitation": "The abstract does not verify deployment performance.",
                            "evidence_fields": ["title", "summary"],
                        }
                    ]
                }
            )
        ]
    )

    values, usage = run_explanations(
        provider,
        ProposalCache(tmp_path / "cache.json"),
        (_records()[0],),
        cache_keys={"2401.00001": "explain-key"},
        allowed_evidence_fields=_FIELDS,
    )

    assert values[0].arxiv_id == "2401.00001"
    assert provider.calls[0][0] == "explain-v2"
    assert usage.estimated_output_tokens > 0
