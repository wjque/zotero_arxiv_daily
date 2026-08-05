from __future__ import annotations

from pathlib import Path

import pytest

from zotero_arxiv_daily.core.errors import ExternalServiceError
from zotero_arxiv_daily.llm.cache import ProposalCache
from zotero_arxiv_daily.llm.contracts import QualityDimension, parse_explanations, parse_judgments

_IDS = frozenset({"2401.00001"})
_FIELDS = frozenset({"title", "summary", "categories", "evidence.context"})
_DIMENSIONS = {
    "contribution_clarity": 0.8,
    "novelty": None,
    "insight_plausibility": 0.6,
    "methodological_evidence": 0.7,
    "empirical_evidence": None,
    "limitations": 0.4,
    "reproducibility": None,
}


def test_judge_contract_preserves_unknown_dimensions_and_bounded_evidence() -> None:
    judgments = parse_judgments(
        '{"judgments":[{"arxiv_id":"2401.00001","dimensions":'
        + repr(_DIMENSIONS).replace("'", '"').replace("None", "null")
        + ',"uncertainty":0.3,"evidence_fields":["summary","evidence.context"]}]}',
        _IDS,
        _FIELDS,
    )

    assert dict(judgments[0].dimensions)[QualityDimension.NOVELTY] is None
    assert judgments[0].evidence_fields == ("summary", "evidence.context")


def test_judge_rejects_unsupplied_evidence_references() -> None:
    with pytest.raises(ExternalServiceError, match="evidence references"):
        parse_judgments(
            '{"judgments":[{"arxiv_id":"2401.00001","dimensions":'
            + repr(_DIMENSIONS).replace("'", '"').replace("None", "null")
            + ',"uncertainty":0.3,"evidence_fields":["private_note"]}]}',
            _IDS,
            _FIELDS,
        )


def test_explain_contract_rejects_generic_reasons_and_requires_limits() -> None:
    with pytest.raises(ExternalServiceError, match="generic"):
        parse_explanations(
            '{"explanations":[{"arxiv_id":"2401.00001",'
            '"summary":"A concise summary of the supplied paper.",'
            '"reason":"This paper is relevant to your interests.",'
            '"limitation":"The abstract-only record cannot verify deployment performance.",'
            '"evidence_fields":["title","summary"]}]}',
            _IDS,
            _FIELDS,
        )


def test_layered_cache_keys_separate_judge_and_explanation_inputs(tmp_path: Path) -> None:
    cache = ProposalCache(tmp_path / "cache.json")
    common = {
        "arxiv_id": "2401.00001",
        "candidate_fingerprint": "candidate",
        "protected_profile_digest": "profile",
        "evidence_snapshot": "evidence",
        "contract_version": "v1",
        "model": "deepseek-v4-flash",
        "output_language": "en",
    }

    assert cache.layered_key(layer="judge", **common) != cache.layered_key(
        layer="explain", **common
    )
