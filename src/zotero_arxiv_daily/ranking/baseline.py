"""Frozen v0.1.2 ranking behavior for offline comparison and rollback."""

from __future__ import annotations

import re
from collections import Counter
from collections.abc import Mapping
from datetime import UTC, datetime

from zotero_arxiv_daily.arxiv.models import ArxivCandidate
from zotero_arxiv_daily.profile.models import RemoteServingProfile
from zotero_arxiv_daily.profile.protection import protected_feature_digest
from zotero_arxiv_daily.ranking.interest import serving_profile_key, watched_identity_match
from zotero_arxiv_daily.ranking.models import RecommendationRecord, ScoredCandidate

BASELINE_VERSION = "v0.1.2"
BASELINE_SCHEMA_VERSIONS = {
    "item_digest": 1,
    "interest_profile": 1,
    "remote_profile": 3,
}

_WORDS = re.compile(r"[a-z][a-z0-9-]{2,}")
_SOURCE_QUOTAS = (("core", 14), ("adjacent", 4), ("exploration", 2))


def score_baseline(
    candidates: tuple[ArxivCandidate, ...],
    profile: RemoteServingProfile,
    now: datetime,
    feedback_adjustments: Mapping[str, float] | None = None,
    *,
    author_bonus: float = 0.75,
    institution_bonus: float = 0.5,
    identity_bonus_cap: float = 1.0,
    profile_feature_key: str | None = None,
) -> tuple[ScoredCandidate, ...]:
    """Apply the immutable v0.1.2 unnormalized coarse-scoring formula."""

    terms = frozenset(profile.topics)
    core = frozenset(profile.core_categories)
    adjacent = frozenset(profile.adjacent_categories)
    matching_key = serving_profile_key(profile, profile_feature_key)
    scored: list[ScoredCandidate] = []
    for candidate in candidates:
        words = set(_WORDS.findall((candidate.title + " " + candidate.summary).casefold()))
        lexical = (
            float(
                len(
                    {
                        protected_feature_digest(word, matching_key, namespace="baseline-lexical")
                        for word in words
                    }
                    & set(profile.baseline_lexical_digests)
                )
            )
            if matching_key is not None
            else float(len(words & terms))
        )
        category = (
            2.0
            if set(candidate.categories) & core
            else 1.0
            if set(candidate.categories) & adjacent
            else 0.25
        )
        age = max((now.astimezone(UTC) - candidate.published).total_seconds() / 86400, 0.0)
        recency = max(0.0, 1.0 - age / 14)
        source = "core" if category == 2.0 else "adjacent" if category == 1.0 else "exploration"
        feedback = (feedback_adjustments or {}).get(candidate.arxiv_id.canonical, 0.0)
        author_match = watched_identity_match(
            candidate.authors,
            profile.watched_authors,
            profile.watched_author_digests,
            matching_key,
            namespace="author",
        )
        institution_match = watched_identity_match(
            candidate.affiliations,
            profile.watched_institutions,
            profile.watched_institution_digests,
            matching_key,
            namespace="institution",
        )
        watched_author = min(author_bonus if author_match else 0.0, identity_bonus_cap)
        watched_institution = min(
            institution_bonus if institution_match else 0.0,
            max(0.0, identity_bonus_cap - watched_author),
        )
        components = (
            ("lexical", lexical),
            ("category", category),
            ("recency", recency),
            ("feedback", feedback),
            ("watched_author", watched_author),
            ("watched_institution", watched_institution),
        )
        scored.append(
            ScoredCandidate(candidate, sum(value for _, value in components), components, source)
        )
    return tuple(sorted(scored, key=lambda item: (-item.score, item.candidate.arxiv_id.canonical)))


def select_baseline(
    scored: tuple[ScoredCandidate, ...], minimum_score: float = 1.0, target: int = 20
) -> tuple[ScoredCandidate, ...]:
    """Apply the immutable v0.1.2 quotas and author/title diversity rules."""

    selected: list[ScoredCandidate] = []
    authors: Counter[str] = Counter()
    topic_sets: list[set[str]] = []
    qualified = [item for item in scored if item.score >= minimum_score]
    for source, quota in _SOURCE_QUOTAS:
        for item in qualified:
            if (
                item.source == source
                and sum(value.source == source for value in selected) < quota
                and _diverse(item, authors, topic_sets)
            ):
                _append(item, selected, authors, topic_sets)
    for item in qualified:
        if len(selected) >= target:
            break
        if item not in selected and _diverse(item, authors, topic_sets):
            _append(item, selected, authors, topic_sets)
    return tuple(selected[:target])


def order_baseline(
    records: tuple[RecommendationRecord, ...],
) -> tuple[RecommendationRecord, ...]:
    """Apply the immutable v0.1.2 relevance-first presentation order."""

    return tuple(
        sorted(
            records,
            key=lambda record: (
                -record.score,
                -record.quality,
                -record.candidate.updated.timestamp(),
                record.candidate.arxiv_id.canonical,
            ),
        )
    )


def _diverse(item: ScoredCandidate, authors: Counter[str], topic_sets: list[set[str]]) -> bool:
    if any(authors[author.casefold()] >= 2 for author in item.candidate.authors):
        return False
    words = set(_WORDS.findall(item.candidate.title.casefold()))
    return not any(
        words and len(words & existing) / len(words | existing) > 0.8 for existing in topic_sets
    )


def _append(
    item: ScoredCandidate,
    selected: list[ScoredCandidate],
    authors: Counter[str],
    topic_sets: list[set[str]],
) -> None:
    selected.append(item)
    authors.update(author.casefold() for author in item.candidate.authors)
    topic_sets.append(set(_WORDS.findall(item.candidate.title.casefold())))
