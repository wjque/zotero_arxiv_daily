from __future__ import annotations

import json
from dataclasses import replace
from datetime import UTC, datetime
from pathlib import Path

from zotero_arxiv_daily.arxiv.models import ArxivCandidate, ArxivId
from zotero_arxiv_daily.evidence.paper_sections import PaperSectionClient
from zotero_arxiv_daily.evidence.project_page import PageResponse, ProjectPageClient
from zotero_arxiv_daily.llm.cache import ProposalCache
from zotero_arxiv_daily.llm.contracts import ProviderCompletion
from zotero_arxiv_daily.pipeline.recommend import (
    package_result,
    run_baseline_recommendation,
    run_recommendation,
    run_refined_recommendation,
)
from zotero_arxiv_daily.profile.build import project_serving_profile
from zotero_arxiv_daily.profile.models import LocalInterestProfile, RemoteServingProfile
from zotero_arxiv_daily.profile.quality import (
    ApprovedQualityExample,
    QualityCriterion,
    QualityReferenceProfile,
    build_quality_reference_profile,
)
from zotero_arxiv_daily.profile.quality_policy import (
    LEGACY_QUALITY_PROFILE_POLICY_VERSION,
    get_quality_reference_policy,
)
from zotero_arxiv_daily.ranking.outcome import DEFAULT_WORTHWHILE_POLICY


def test_manifest_contains_only_counts_and_budget_metadata() -> None:
    profile = RemoteServingProfile(1, 9, (), (), (), ())
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
        self.records: list[dict[str, object]] = []

    def propose(self, candidates: list[dict[str, object]]) -> str:
        self.calls += 1
        self.records.extend(candidates)
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
        if contract.startswith("judge-"):
            judgments = []
            for record in records:
                score = 0.0 if record["arxiv_id"] == "2401.00004" else 0.8
                judgments.append(
                    {
                        "arxiv_id": record["arxiv_id"],
                        "dimensions": {
                            "contribution_clarity": score,
                            "novelty": score,
                            **(
                                {"solution_advance": score, "technical_depth": score}
                                if contract == "judge-v5"
                                else {}
                            ),
                            "insight_plausibility": score,
                            "methodological_evidence": score,
                            "empirical_evidence": None,
                            "limitations": 0.4,
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


class _MeasuredRefinementProvider:
    def complete(self, contract: str, records: list[dict[str, object]]) -> ProviderCompletion:
        response = _RefinementProvider().complete(contract, records)
        return ProviderCompletion(response, input_tokens=100, output_tokens=20, latency_seconds=0.5)


class _IncrementalRefinementProvider(_RefinementProvider):
    def complete(self, contract: str, records: list[dict[str, object]]) -> str:
        response = super().complete(contract, records)
        if contract != "judge-v5":
            return response
        value = json.loads(response)
        for judgment in value["judgments"]:
            if judgment["arxiv_id"] == "2401.00002":
                judgment["dimensions"]["solution_advance"] = 0.25
        return json.dumps(value)


class _ReachableProjectPageTransport:
    def fetch(self, url: str, timeout_seconds: float) -> PageResponse:
        assert url == "https://github.com/example/project"
        assert timeout_seconds == 5.0
        return PageResponse(200)


class _PaperSectionTransport:
    def fetch(self, url: str, timeout_seconds: float) -> bytes:
        assert url.startswith("https://ar5iv.labs.arxiv.org/html/")
        assert timeout_seconds == 10.0
        return (
            b"<h2>Method</h2><p>A direct adapter is added to the baseline.</p>"
            b"<h2>Evaluation</h2><p>The claimed gain is measured on one benchmark.</p>"
        )


def test_fully_suppressed_input_creates_empty_batch_without_model_call(tmp_path: Path) -> None:
    profile = RemoteServingProfile(1, 9, ("learning",), ("cs.LG",), (), ())
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


def test_baseline_rollback_uses_frozen_ranker_and_marks_manifest(tmp_path: Path) -> None:
    profile = RemoteServingProfile(1, 9, ("learning", "methods"), ("cs.LG",), (), ())
    now = datetime(2026, 8, 1, tzinfo=UTC)
    provider = _Provider()

    result, manifest = run_baseline_recommendation(
        (_candidate("2401.00001"),),
        profile,
        now,
        provider,
        ProposalCache(tmp_path / "proposals.json"),
        prompt_version="recommendation-v2:en",
        model="deepseek-v4-flash",
        completed_at=now,
    )

    assert [item.candidate.arxiv_id.canonical for item in result.recommendations] == ["2401.00001"]
    assert result.recommendations[0].score > 1
    assert manifest.weight_set_version == "v0.1.2"
    assert manifest.model_requests == 1


def test_protected_profile_scores_remotely_without_entering_the_model_payload(
    tmp_path: Path,
) -> None:
    feature_key = "test-profile-feature-key-0000000000000001"
    profile = project_serving_profile(
        LocalInterestProfile(
            2,
            9,
            (("confidential-neologism", 2.0), ("learning", 1.0)),
            (("confidential-neologism", 1.0),),
            (("cs.LG", 1.0, "test"),),
            1,
        ),
        feature_key,
    )
    provider = _Provider()

    run_recommendation(
        (_candidate("2401.00001"),),
        profile,
        datetime(2026, 8, 1, tzinfo=UTC),
        provider,
        ProposalCache(tmp_path / "protected-profile-cache.json"),
        prompt_version="v5",
        model="deepseek-v4-flash",
        profile_feature_key=feature_key,
    )
    payload = json.dumps(provider.records)

    assert provider.calls == 1
    assert feature_key not in payload
    assert "confidential-neologism" not in payload
    assert profile.feature_key_verifier is not None
    assert profile.feature_key_verifier not in payload
    assert all(feature.digest not in payload for feature in profile.lexical_features)


def test_recommendation_run_reuses_validated_cache_and_excludes_known_papers(
    tmp_path: Path,
) -> None:
    profile = RemoteServingProfile(1, 9, ("learning",), ("cs.LG",), (), ())
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


def test_v020_selection_has_no_feedback_input(tmp_path: Path) -> None:
    profile = RemoteServingProfile(1, 9, ("learning",), ("cs.LG",), (), ())
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
        pre_rank_limit=21,
    )

    identifiers = {item.candidate.arxiv_id.canonical for item in result.recommendations}
    assert len(identifiers) == 20
    assert "2401.00001" in identifiers


def test_candidate_revision_invalidates_cached_proposal(tmp_path: Path) -> None:
    profile = RemoteServingProfile(1, 9, ("learning",), ("cs.LG",), (), ())
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
    profile = RemoteServingProfile(4, 9, ("learning",), ("cs.LG",), (), ())
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

    result, manifest, predictions = run_refined_recommendation(
        candidates,
        profile,
        now,
        provider,
        cache,
        model="deepseek-v4-flash",
        output_language="en",
        completed_at=now,
    )
    repeated, repeated_manifest, _ = run_refined_recommendation(
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
    assert [contract for contract, _ in provider.calls] == ["judge-v5", "explain-v3"]
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
        "judge-v5",
        "explain-v3",
        "explain-v3",
    ]
    assert all("relevance_signals" in record for record in provider.calls[-1][1])


def test_refined_run_filters_evidence_supported_incremental_solution(tmp_path: Path) -> None:
    provider = _IncrementalRefinementProvider()
    candidates = (
        replace(_candidate("2401.00001"), title="Learning alpha", authors=("Alice",)),
        replace(_candidate("2401.00002"), title="Learning beta", authors=("Bob",)),
    )

    result, manifest, _ = run_refined_recommendation(
        candidates,
        RemoteServingProfile(4, 9, ("learning",), ("cs.LG",), (), ()),
        datetime(2026, 8, 1, tzinfo=UTC),
        provider,
        ProposalCache(tmp_path / "value-gate-cache.json"),
        model="deepseek-v4-flash",
        output_language="en",
    )

    assert [record.candidate.arxiv_id.canonical for record in result.recommendations] == [
        "2401.00001"
    ]
    assert manifest.scientific_value_filtered_count == 1
    assert [record["arxiv_id"] for record in provider.calls[-1][1]] == ["2401.00001"]


def test_refined_ranking_adds_reachable_abstract_project_page_evidence(tmp_path: Path) -> None:
    profile = RemoteServingProfile(4, 9, ("learning",), ("cs.LG",), (), ())
    provider = _RefinementProvider()
    now = datetime(2026, 8, 1, tzinfo=UTC)
    without_page = replace(
        _candidate("2401.00001"), title="Alpha learning method", authors=("Alice",)
    )
    with_page = replace(
        _candidate("2401.00002"),
        title="Beta learning method",
        authors=("Bob",),
        summary="Learning methods. Project: https://github.com/example/project",
    )

    result, _, _ = run_refined_recommendation(
        (without_page, with_page),
        profile,
        now,
        provider,
        ProposalCache(tmp_path / "cache.json"),
        model="deepseek-v4-flash",
        output_language="en",
        project_page_client=ProjectPageClient(_ReachableProjectPageTransport()),
        completed_at=now,
    )

    assert [record.candidate.arxiv_id.canonical for record in result.recommendations] == [
        "2401.00002",
        "2401.00001",
    ]


def test_explanation_receives_method_and_evaluation_without_limitation_fallback(
    tmp_path: Path,
) -> None:
    provider = _RefinementProvider()

    run_refined_recommendation(
        (_candidate("2401.00001"),),
        RemoteServingProfile(4, 9, ("learning",), ("cs.LG",), (), ()),
        datetime(2026, 8, 1, tzinfo=UTC),
        provider,
        ProposalCache(tmp_path / "critical-assessment-cache.json"),
        model="deepseek-v4-flash",
        output_language="en",
        paper_section_client=PaperSectionClient(_PaperSectionTransport()),
    )

    explanation_record = provider.calls[-1][1][0]
    assert explanation_record["method_evidence"] == "A direct adapter is added to the baseline."
    assert explanation_record["evaluation_evidence"] == (
        "The claimed gain is measured on one benchmark."
    )
    assert "limitations_evidence" not in explanation_record


def test_refined_manifest_records_measured_usage_and_context_mode(tmp_path: Path) -> None:
    profile = RemoteServingProfile(4, 9, ("learning",), ("cs.LG",), (), ())
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

    _, manifest, _ = run_refined_recommendation(
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


def test_refined_run_references_only_aggregate_approved_quality_profile(tmp_path: Path) -> None:
    interest_profile = RemoteServingProfile(4, 9, ("learning",), ("cs.LG",), (), ())
    provider = _RefinementProvider()
    now = datetime(2026, 8, 1, tzinfo=UTC)
    quality_profile = build_quality_reference_profile(
        (
            ApprovedQualityExample(
                "private-source-paper-id",
                True,
                ("evaluation",),
                ("baselines",),
                ("held_out_evaluation",),
                ("practical_utility",),
                ("limited_scale",),
            ),
        ),
        (),
    )

    _, manifest, _ = run_refined_recommendation(
        (_candidate("2401.00001"),),
        interest_profile,
        now,
        provider,
        ProposalCache(tmp_path / "quality-profile-cache.json"),
        model="deepseek-v4-flash",
        output_language="en",
        quality_profile=quality_profile,
        completed_at=now,
    )

    judge_record = provider.calls[0][1][0]
    encoded = json.dumps(judge_record["quality_reference"])
    assert "private-source-paper-id" not in encoded
    assert "research_problems" not in encoded
    assert "motivations" not in encoded
    assert judge_record["quality_reference"] == quality_profile.prompt_payload()
    assert judge_record["quality_reference"]["policy_version"] == (quality_profile.policy_version)
    assert manifest.quality_profile_version == quality_profile.version
    assert manifest.quality_profile_criterion_count == 5
    assert manifest.quality_profile_feedback_event_count == 0


def test_legacy_quality_profile_keeps_legacy_judge_contract(tmp_path: Path) -> None:
    provider = _RefinementProvider()
    legacy_profile = QualityReferenceProfile(
        "quality-profile-legacy",
        1,
        LEGACY_QUALITY_PROFILE_POLICY_VERSION,
        get_quality_reference_policy(LEGACY_QUALITY_PROFILE_POLICY_VERSION).fingerprint,
        (QualityCriterion("evaluation", 1.0),),
        (),
        (),
        (),
        (),
        1,
        0,
    )

    run_refined_recommendation(
        (_candidate("2401.00001"),),
        RemoteServingProfile(4, 9, ("learning",), ("cs.LG",), (), ()),
        datetime(2026, 8, 1, tzinfo=UTC),
        provider,
        ProposalCache(tmp_path / "legacy-quality-profile-cache.json"),
        model="deepseek-v4-flash",
        output_language="en",
        quality_profile=legacy_profile,
    )

    assert provider.calls[0][0] == "judge-v3"
    assert provider.calls[0][1][0]["quality_reference"] == legacy_profile.prompt_payload()


def test_refined_run_predicts_the_published_records_without_changing_selection(
    tmp_path: Path,
) -> None:
    """Estimates cover exactly the published batch, in rank order, and selection is untouched."""

    profile = RemoteServingProfile(4, 9, ("learning",), ("cs.LG",), (), ())
    provider = _RefinementProvider()
    cache = ProposalCache(tmp_path / "refinement-cache.json")
    now = datetime(2026, 8, 1, tzinfo=UTC)
    titles = ("Learning alpha", "Learning beta", "Learning gamma", "Learning delta")
    candidates = tuple(
        replace(_candidate(f"2401.{index:05d}"), title=title, authors=(f"Author {index}",))
        for index, title in enumerate(titles, start=1)
    )

    result, manifest, predictions = run_refined_recommendation(
        candidates,
        profile,
        now,
        provider,
        cache,
        model="deepseek-v4-flash",
        output_language="en",
        completed_at=now,
    )

    published = tuple(record.candidate.arxiv_id.canonical for record in result.recommendations)
    assert tuple(estimate.arxiv_id for estimate in predictions) == published
    assert all(
        estimate.policy_version == DEFAULT_WORTHWHILE_POLICY.version for estimate in predictions
    )
    assert all(estimate.provenance == manifest.weight_set_version for estimate in predictions)
    assert all(0 < estimate.expected_worthwhile <= 1 for estimate in predictions)
    # Selection is the v0.2.1 behavior this slice must not disturb.
    assert published == ("2401.00001", "2401.00002", "2401.00003")


def test_refined_run_without_a_shortlist_predicts_nothing(tmp_path: Path) -> None:
    profile = RemoteServingProfile(4, 9, ("learning",), ("cs.LG",), (), ())
    provider = _RefinementProvider()
    cache = ProposalCache(tmp_path / "refinement-cache.json")
    now = datetime(2026, 8, 1, tzinfo=UTC)

    result, _, predictions = run_refined_recommendation(
        (),
        profile,
        now,
        provider,
        cache,
        model="deepseek-v4-flash",
        output_language="en",
        completed_at=now,
    )

    assert result.recommendations == ()
    assert predictions == ()
