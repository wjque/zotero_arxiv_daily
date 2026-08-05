from __future__ import annotations

import json
from dataclasses import replace
from datetime import UTC, datetime
from pathlib import Path

from zotero_arxiv_daily.arxiv.models import ArxivCandidate, ArxivId
from zotero_arxiv_daily.llm.cache import ProposalCache
from zotero_arxiv_daily.llm.contracts import ProviderCompletion
from zotero_arxiv_daily.pipeline.recommend import (
    package_result,
    run_recommendation,
    run_refined_recommendation,
)
from zotero_arxiv_daily.profile.models import RemoteProfile


def test_manifest_contains_only_counts_and_budget_metadata() -> None:
    profile = RemoteProfile(1, 9, (), (), (), ())
    result, manifest = package_result(
        (),
        profile,
        datetime.now(UTC),
        model="deepseek-v4-flash",
        candidate_count=3,
        model_requests=1,
        cache_hits=2,
        estimated_tokens=30,
    )

    assert result.schema_version == 2
    assert result.generation_completed_at is not None
    assert manifest.profile_library_version == 9
    assert manifest.recommendation_count == 0
    assert manifest.estimated_tokens == 30
    assert manifest.duration_seconds == 0.0


def _candidate(identifier: str) -> ArxivCandidate:
    now = datetime(2026, 8, 1, tzinfo=UTC)
    return ArxivCandidate(
        ArxivId(identifier, 1),
        "Learning methods",
        ("Ada",),
        ("cs.LG",),
        now,
        now,
        f"https://arxiv.org/abs/{identifier}",
        f"https://arxiv.org/pdf/{identifier}",
        "Learning methods for useful systems.",
    )


class _Provider:
    def __init__(self) -> None:
        self.calls = 0

    def propose(self, candidates: list[dict[str, object]]) -> str:
        self.calls += 1
        return json.dumps(
            {
                "proposals": [
                    {
                        "arxiv_id": candidate["arxiv_id"],
                        "quality": 0.9,
                        "summary": "A concise Chinese summary.",
                        "reason": "Its concrete learning contribution matches the profile topic.",
                    }
                    for candidate in candidates
                ]
            }
        )


class _ForbiddenProvider:
    def propose(self, candidates: list[dict[str, object]]) -> str:
        raise AssertionError("empty or suppressed input must not call the provider")


class _RefinementProvider:
    def __init__(self) -> None:
        self.calls: list[tuple[str, list[dict[str, object]]]] = []

    def complete(self, contract: str, records: list[dict[str, object]]) -> str:
        self.calls.append((contract, records))
        if contract == "judge-v2":
            judgments = []
            for record in records:
                score = 0.0 if record["arxiv_id"] == "2401.00004" else 0.8
                judgments.append(
                    {
                        "arxiv_id": record["arxiv_id"],
                        "dimensions": {
                            "contribution_clarity": score,
                            "novelty": score,
                            "insight_plausibility": score,
                            "methodological_evidence": score,
                            "empirical_evidence": None,
                            "limitations": 0.4,
                            "reproducibility": None,
                        },
                        "uncertainty": 0.2,
                        "evidence_fields": ["title", "summary"],
                    }
                )
            return json.dumps({"judgments": judgments})
        return json.dumps(
            {
                "explanations": [
                    {
                        "arxiv_id": record["arxiv_id"],
                        "summary": (
                            "It evaluates a bounded learning method under realistic constraints."
                        ),
                        "reason": (
                            "Its bounded learning method addresses the stated ranking constraint."
                        ),
                        "limitation": "The abstract alone does not verify deployment performance.",
                        "evidence_fields": ["title", "summary"],
                    }
                    for record in records
                ]
            }
        )


class _MeasuredRefinementProvider(_RefinementProvider):
    def complete(self, contract: str, records: list[dict[str, object]]) -> ProviderCompletion:
        response = super().complete(contract, records)
        return ProviderCompletion(response, input_tokens=100, output_tokens=20, latency_seconds=0.5)


def test_fully_suppressed_input_creates_empty_batch_without_model_call(tmp_path: Path) -> None:
    profile = RemoteProfile(1, 9, ("learning",), ("cs.LG",), (), ())
    now = datetime(2026, 8, 1, tzinfo=UTC)

    result, manifest = run_recommendation(
        (_candidate("2401.00001"),),
        profile,
        now,
        _ForbiddenProvider(),
        ProposalCache(tmp_path / "proposals.json"),
        prompt_version="v1",
        model="deepseek-v4-flash",
        excluded_ids=frozenset({"2401.00001"}),
        completed_at=now,
    )

    assert result.recommendations == ()
    assert manifest.model_requests == 0


def test_recommendation_run_reuses_validated_cache_and_excludes_known_papers(
    tmp_path: Path,
) -> None:
    profile = RemoteProfile(1, 9, ("learning",), ("cs.LG",), (), ())
    provider = _Provider()
    cache = ProposalCache(tmp_path / "proposals.json")
    now = datetime(2026, 8, 1, tzinfo=UTC)
    candidates = (_candidate("2401.00001"), _candidate("2401.00002"))

    first, first_manifest = run_recommendation(
        candidates,
        profile,
        now,
        provider,
        cache,
        prompt_version="v1",
        model="deepseek-v4-flash",
        excluded_ids=frozenset({"2401.00002"}),
        estimate_cost=lambda tokens: tokens * 0.000001,
    )
    second, second_manifest = run_recommendation(
        candidates,
        profile,
        now,
        provider,
        cache,
        prompt_version="v1",
        model="deepseek-v4-flash",
        excluded_ids=frozenset({"2401.00002"}),
    )

    assert provider.calls == 1
    assert [item.candidate.arxiv_id.canonical for item in first.recommendations] == ["2401.00001"]
    assert [item.candidate.arxiv_id.canonical for item in second.recommendations] == ["2401.00001"]
    assert first_manifest.model_requests == 1
    assert first_manifest.estimated_cost_usd > 0
    assert second_manifest.model_requests == 0
    assert second_manifest.cache_hits == 1


def test_feedback_adjustment_controls_final_selection(tmp_path: Path) -> None:
    profile = RemoteProfile(1, 9, ("learning",), ("cs.LG",), (), ())
    provider = _Provider()
    now = datetime(2026, 8, 1, tzinfo=UTC)
    candidates = tuple(
        replace(
            _candidate(f"2401.{index:05d}"),
            title=f"Learning method topic unique{chr(96 + index)}xx",
            authors=(f"Author {index}",),
        )
        for index in range(1, 22)
    )

    result, _ = run_recommendation(
        candidates,
        profile,
        now,
        provider,
        ProposalCache(tmp_path / "proposals.json"),
        prompt_version="v2",
        model="deepseek-v4-flash",
        feedback_adjustments={"2401.00001": -10.0},
        pre_rank_limit=21,
    )

    identifiers = {item.candidate.arxiv_id.canonical for item in result.recommendations}
    assert len(identifiers) == 20
    assert "2401.00001" not in identifiers


def test_candidate_revision_invalidates_cached_proposal(tmp_path: Path) -> None:
    profile = RemoteProfile(1, 9, ("learning",), ("cs.LG",), (), ())
    provider = _Provider()
    cache = ProposalCache(tmp_path / "proposals.json")
    now = datetime(2026, 8, 1, tzinfo=UTC)
    original = _candidate("2401.00001")
    revised = replace(original, arxiv_id=ArxivId("2401.00001", 2), summary="Revised abstract.")

    run_recommendation(
        (original,),
        profile,
        now,
        provider,
        cache,
        prompt_version="v2",
        model="deepseek-v4-flash",
    )
    _, manifest = run_recommendation(
        (revised,),
        profile,
        now,
        provider,
        cache,
        prompt_version="v2",
        model="deepseek-v4-flash",
    )

    assert provider.calls == 2
    assert manifest.model_requests == 1
    assert manifest.cache_hits == 0


def test_refined_run_judges_shortlist_and_generates_final_only_explanations(tmp_path: Path) -> None:
    profile = RemoteProfile(4, 9, ("learning",), ("cs.LG",), (), ())
    provider = _RefinementProvider()
    cache = ProposalCache(tmp_path / "refinement-cache.json")
    now = datetime(2026, 8, 1, tzinfo=UTC)
    titles = ("Learning alpha", "Learning beta", "Learning gamma", "Learning delta")
    candidates = tuple(
        replace(
            _candidate(f"2401.{index:05d}"),
            title=title,
            authors=(f"Author {index}",),
        )
        for index, title in enumerate(titles, start=1)
    )

    result, manifest = run_refined_recommendation(
        candidates,
        profile,
        now,
        provider,
        cache,
        model="deepseek-v4-flash",
        output_language="en",
        completed_at=now,
    )
    repeated, repeated_manifest = run_refined_recommendation(
        candidates,
        profile,
        now,
        provider,
        cache,
        model="deepseek-v4-flash",
        output_language="en",
        completed_at=now,
    )

    assert len(result.recommendations) == 3
    assert [contract for contract, _ in provider.calls] == ["judge-v2", "explain-v2"]
    assert len(provider.calls[0][1]) == 4
    assert len(provider.calls[1][1]) == 3
    assert all("relevance_signals" not in record for record in provider.calls[1][1])
    assert manifest.judge_requests == 1
    assert manifest.explanation_requests == 1
    assert manifest.estimated_output_tokens > 0
    assert repeated.recommendations == result.recommendations
    assert repeated_manifest.model_requests == 0
    assert repeated_manifest.cache_hits == 7

    run_refined_recommendation(
        candidates,
        profile,
        now,
        provider,
        cache,
        model="deepseek-v4-flash",
        output_language="en",
        allow_preference_context=True,
        completed_at=now,
    )

    assert [contract for contract, _ in provider.calls] == [
        "judge-v2",
        "explain-v2",
        "explain-v2",
    ]
    assert all("relevance_signals" in record for record in provider.calls[-1][1])


def test_refined_manifest_records_measured_usage_and_context_mode(tmp_path: Path) -> None:
    profile = RemoteProfile(4, 9, ("learning",), ("cs.LG",), (), ())
    provider = _MeasuredRefinementProvider()
    now = datetime(2026, 8, 1, tzinfo=UTC)
    candidates = tuple(
        replace(
            _candidate(f"2401.{index:05d}"),
            title=f"Learning topic {index}",
            authors=(f"Author {index}",),
        )
        for index in range(1, 4)
    )

    _, manifest = run_refined_recommendation(
        candidates,
        profile,
        now,
        provider,
        ProposalCache(tmp_path / "measured-cache.json"),
        model="deepseek-v4-flash",
        output_language="en",
        allow_preference_context=True,
        completed_at=now,
    )

    assert manifest.actual_input_tokens == 200
    assert manifest.actual_output_tokens == 40
    assert manifest.provider_latency_seconds == 1.0
    assert manifest.preference_context_enabled
