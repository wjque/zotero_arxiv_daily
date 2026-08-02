"""Deterministic pre-ranking, quotas, quality thresholds, and diversity limits."""

from __future__ import annotations

import re
from collections import Counter
from collections.abc import Mapping
from datetime import UTC, datetime

from zotero_arxiv_daily.arxiv.models import ArxivCandidate
from zotero_arxiv_daily.profile.models import RemoteProfile, WatchedIdentity, normalize_identity
from zotero_arxiv_daily.ranking.models import RecommendationRecord, ScoredCandidate

_WORDS = re.compile(r"[a-z][a-z0-9-]{2,}")


def pre_rank(
    candidates: tuple[ArxivCandidate, ...],
    profile: RemoteProfile,
    now: datetime,
    feedback_adjustments: Mapping[str, float] | None = None,
    *,
    author_bonus: float = 0.75,
    institution_bonus: float = 0.5,
    identity_bonus_cap: float = 1.0,
) -> tuple[ScoredCandidate, ...]:
    """Score public candidates locally using derived profile terms and categories."""

    terms = frozenset(profile.topics)
    core = frozenset(profile.core_categories)
    adjacent = frozenset(profile.adjacent_categories)
    scored: list[ScoredCandidate] = []
    for candidate in candidates:
        words = set(_WORDS.findall((candidate.title + " " + candidate.summary).casefold()))
        lexical = float(len(words & terms))
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
        author_match = _matches_any(candidate.authors, profile.watched_authors)
        institution_match = _matches_any(candidate.affiliations, profile.watched_institutions)
        watched_author = author_bonus if author_match else 0.0
        watched_institution = institution_bonus if institution_match else 0.0
        watched_author = min(watched_author, identity_bonus_cap)
        watched_institution = min(
            watched_institution, max(0.0, identity_bonus_cap - watched_author)
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


def _matches_any(values: tuple[str, ...], identities: tuple[WatchedIdentity, ...]) -> bool:
    normalized_values = {normalize_identity(value) for value in values if value.strip()}
    return any(bool(normalized_values & identity.normalized_names) for identity in identities)


def select_diverse(
    scored: tuple[ScoredCandidate, ...], minimum_score: float = 1.0, target: int = 20
) -> tuple[ScoredCandidate, ...]:
    """Select quality-qualified results with quotas and author/topic diversity."""

    quotas = {"core": 14, "adjacent": 4, "exploration": 2}
    selected: list[ScoredCandidate] = []
    authors: Counter[str] = Counter()
    topic_sets: list[set[str]] = []
    qualified = [item for item in scored if item.score >= minimum_score]
    for source in ("core", "adjacent", "exploration"):
        for item in qualified:
            if (
                item.source == source
                and len([value for value in selected if value.source == source]) < quotas[source]
                and _diverse(item, authors, topic_sets)
            ):
                _append(item, selected, authors, topic_sets)
    for item in qualified:
        if len(selected) >= target:
            break
        if item not in selected and _diverse(item, authors, topic_sets):
            _append(item, selected, authors, topic_sets)
    return tuple(selected[:target])


def order_recommendations(
    records: tuple[RecommendationRecord, ...],
) -> tuple[RecommendationRecord, ...]:
    """Order selected records for reading without changing selection policy.

    Local relevance remains the primary signal. Validated model quality only refines that
    policy-approved ordering; updated time and canonical ID make otherwise equal records stable.
    """

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
