from __future__ import annotations

import hashlib
import json
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from zotero_arxiv_daily.arxiv.models import ArxivCandidate, ArxivId
from zotero_arxiv_daily.arxiv.storage import CANDIDATE_POOL_SCHEMA_VERSION
from zotero_arxiv_daily.llm.cache import ProposalCache
from zotero_arxiv_daily.llm.deepseek import DeepSeekClient
from zotero_arxiv_daily.profile.models import (
    INTEREST_PROFILE_SCHEMA_VERSION,
    ITEM_DIGEST_SCHEMA_VERSION,
    REMOTE_PROFILE_SCHEMA_VERSION,
    RemoteProfile,
    WatchedIdentity,
)
from zotero_arxiv_daily.ranking.baseline import (
    BASELINE_VERSION,
    order_baseline,
    score_baseline,
    select_baseline,
)
from zotero_arxiv_daily.ranking.models import (
    RECOMMENDATION_RUN_MANIFEST_SCHEMA_VERSION,
    RECOMMENDATION_SET_SCHEMA_VERSION,
    RecommendationRecord,
    ScoredCandidate,
)
from zotero_arxiv_daily.security.encryption import ENCRYPTION_SCHEMA_VERSION
from zotero_arxiv_daily.site.models import PUBLISHABLE_SCHEMA_VERSION
from zotero_arxiv_daily.storage.recommendation_history import HISTORY_SCHEMA_VERSION

_BASELINE_PATH = Path("docs/baselines/v0.1.2.json")
_NOW = datetime(2026, 8, 2, tzinfo=UTC)


def _candidate(
    identifier: str,
    category: str,
    title: str,
    *,
    summary: str = "",
    authors: tuple[str, ...] = ("Ada",),
    affiliations: tuple[str, ...] = (),
    age_days: int = 1,
) -> ArxivCandidate:
    return ArxivCandidate(
        ArxivId(identifier, 1),
        title,
        authors,
        (category,),
        _NOW - timedelta(days=age_days),
        _NOW,
        f"https://arxiv.org/abs/{identifier}",
        f"https://arxiv.org/pdf/{identifier}",
        summary,
        affiliations,
    )


def test_baseline_contract_freezes_release_schemas_budgets_and_prompt() -> None:
    contract = json.loads(_BASELINE_PATH.read_text(encoding="utf-8"))
    transport = _CapturingTransport()
    DeepSeekClient("test-key", transport=transport, output_language="en").propose([])

    assert BASELINE_VERSION == contract["baseline_version"] == "v0.1.2"
    assert contract["release"] == {
        "verified_source_revision": "8ed8627eda60c113dd560384f84c9f3c8fd842a9",
        "release_commit": "423c05782fac4ebc9e33ac9da76f8235de84c963",
        "tag": "v0.1.2",
        "release_url": "https://github.com/wjque/zotero_arxiv_daily/releases/tag/v0.1.2",
        "status": "published",
    }
    assert contract["schemas"] == {
        "zotero_store": 2,
        "item_digest": 1,
        "interest_profile": 1,
        "remote_profile": 3,
        "arxiv_candidate_pool": 3,
        "feedback_state": 1,
        "recommendation_history": 1,
        "recommendation_set": 2,
        "run_manifest": 1,
        "publishable_site": 3,
        "encryption_envelope": 1,
    }
    assert contract["model"]["candidate_limit"] == 40
    assert contract["model"]["estimated_input_token_budget"] == 12_000
    assert contract["model"]["provider_output_token_limit"] == 12_000
    assert contract["schemas"]["item_digest"] == ITEM_DIGEST_SCHEMA_VERSION
    assert contract["schemas"]["interest_profile"] == INTEREST_PROFILE_SCHEMA_VERSION
    assert contract["schemas"]["remote_profile"] == REMOTE_PROFILE_SCHEMA_VERSION
    assert contract["schemas"]["arxiv_candidate_pool"] == CANDIDATE_POOL_SCHEMA_VERSION
    assert contract["schemas"]["recommendation_history"] == HISTORY_SCHEMA_VERSION
    assert contract["schemas"]["recommendation_set"] == RECOMMENDATION_SET_SCHEMA_VERSION
    assert contract["schemas"]["run_manifest"] == RECOMMENDATION_RUN_MANIFEST_SCHEMA_VERSION
    assert contract["schemas"]["publishable_site"] == PUBLISHABLE_SCHEMA_VERSION
    assert contract["schemas"]["encryption_envelope"] == ENCRYPTION_SCHEMA_VERSION
    assert transport.system_prompt is not None
    assert (
        hashlib.sha256(transport.system_prompt.encode("utf-8")).hexdigest()
        == contract["model"]["rendered_system_prompt_sha256"]
    )
    cache_key = ProposalCache(Path("unused.json")).key(
        "2401.00001",
        7,
        "recommendation-v2:en",
        "deepseek-v4-flash",
        "candidate-sha",
    )
    assert cache_key == contract["model"]["characterization_cache_key_sha256"]


def test_baseline_score_components_and_stable_ties_are_characterized() -> None:
    profile = RemoteProfile(
        3,
        7,
        ("learning", "methods"),
        ("cs.LG",),
        ("cs.AI",),
        (),
        watched_authors=(WatchedIdentity("Ada"),),
        watched_institutions=(WatchedIdentity("MIT"),),
    )
    first = _candidate(
        "2401.00002",
        "cs.LG",
        "Learning",
        summary="Methods",
        affiliations=("MIT",),
    )
    tied = replace(first, arxiv_id=ArxivId("2401.00001", 1))
    adjacent = _candidate(
        "2401.00003",
        "cs.AI",
        "Learning",
        summary="Methods",
        authors=("Grace",),
        age_days=14,
    )

    scored = score_baseline(
        (first, adjacent, tied),
        profile,
        _NOW,
        {"2401.00001": 0.25, "2401.00002": 0.25, "2401.00003": -0.5},
    )

    assert [item.candidate.arxiv_id.canonical for item in scored] == [
        "2401.00001",
        "2401.00002",
        "2401.00003",
    ]
    assert dict(scored[0].components) == pytest.approx(
        {
            "lexical": 2.0,
            "category": 2.0,
            "recency": 13 / 14,
            "feedback": 0.25,
            "watched_author": 0.75,
            "watched_institution": 0.25,
        }
    )
    assert scored[0].score == pytest.approx(6 + 5 / 28)
    assert dict(scored[2].components) == pytest.approx(
        {
            "lexical": 2.0,
            "category": 1.0,
            "recency": 0.0,
            "feedback": -0.5,
            "watched_author": 0.0,
            "watched_institution": 0.0,
        }
    )


def test_baseline_selection_freezes_quotas_diversity_and_empty_input() -> None:
    scored = tuple(
        _scored(index, source)
        for index, source in enumerate(
            ("core",) * 16 + ("adjacent",) * 5 + ("exploration",) * 3,
            start=1,
        )
    )

    selected = select_baseline(scored)

    assert len(selected) == 20
    assert [item.source for item in selected].count("core") == 14
    assert [item.source for item in selected].count("adjacent") == 4
    assert [item.source for item in selected].count("exploration") == 2
    assert select_baseline(()) == ()
    assert select_baseline(scored, minimum_score=99.0) == ()

    repeated_author = tuple(
        replace(
            _scored(index, "core"),
            candidate=replace(_scored(index, "core").candidate, authors=("Same Author",)),
        )
        for index in range(1, 4)
    )
    assert len(select_baseline(repeated_author)) == 2

    same_title = (
        _scored(1, "core", title="Shared topic words"),
        _scored(2, "core", title="Shared topic words"),
    )
    assert len(select_baseline(same_title)) == 1


def test_baseline_final_order_is_relevance_quality_update_and_id() -> None:
    records = (
        _record("2401.00004", score=4.0, quality=0.9, updated_days=0),
        _record("2401.00003", score=4.0, quality=0.9, updated_days=0),
        _record("2401.00002", score=4.0, quality=0.9, updated_days=1),
        _record("2401.00001", score=4.0, quality=0.8, updated_days=0),
        _record("2401.00005", score=3.0, quality=1.0, updated_days=0),
    )

    ordered = order_baseline(records)

    assert [record.candidate.arxiv_id.canonical for record in ordered] == [
        "2401.00003",
        "2401.00004",
        "2401.00002",
        "2401.00001",
        "2401.00005",
    ]


def _scored(index: int, source: str, *, title: str | None = None) -> ScoredCandidate:
    candidate = _candidate(
        f"2401.{index:05d}",
        "cs.LG",
        title or f"Unique topic number{index}",
        authors=(f"Author {index}",),
    )
    return ScoredCandidate(candidate, 10.0 - index / 100, (), source)


def _record(
    identifier: str, *, score: float, quality: float, updated_days: int
) -> RecommendationRecord:
    candidate = replace(
        _candidate(identifier, "cs.LG", "Unique title"),
        updated=_NOW - timedelta(days=updated_days),
    )
    return RecommendationRecord(candidate, score, "core", quality, "summary", "reason")


class _CapturingTransport:
    def __init__(self) -> None:
        self.system_prompt: str | None = None

    def post(
        self, url: str, headers: dict[str, str], payload: bytes, timeout_seconds: float
    ) -> str:
        request = json.loads(payload)
        self.system_prompt = request["messages"][0]["content"]
        return '{"choices":[{"message":{"content":"{\\"proposals\\":[]}"}}]}'
