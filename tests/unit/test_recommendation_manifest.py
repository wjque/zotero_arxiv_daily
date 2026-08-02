from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path

from zotero_arxiv_daily.arxiv.models import ArxivCandidate, ArxivId
from zotero_arxiv_daily.llm.cache import ProposalCache
from zotero_arxiv_daily.pipeline.recommend import package_result, run_recommendation
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
                        "reason": "Matches the profile topic.",
                    }
                    for candidate in candidates
                ]
            }
        )


class _ForbiddenProvider:
    def propose(self, candidates: list[dict[str, object]]) -> str:
        raise AssertionError("empty or suppressed input must not call the provider")


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
