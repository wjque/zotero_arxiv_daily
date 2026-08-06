"""Deterministic pre-ranking, quotas, quality thresholds, and diversity limits."""

from __future__ import annotations

import re
from collections import Counter
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import UTC, datetime

from zotero_arxiv_daily.arxiv.models import ArxivCandidate
from zotero_arxiv_daily.profile.models import RemoteProfile, WatchedIdentity, normalize_identity
from zotero_arxiv_daily.ranking.models import RecommendationRecord, ScoredCandidate
from zotero_arxiv_daily.ranking.weights import (
    DEFAULT_WEIGHT_SET,
    FeatureGroup,
    NormalizedFeature,
    WeightSet,
)

_WORDS = re.compile(r"[a-z][a-z0-9-]{2,}")


def _facet_tokens(value: str) -> frozenset[str]:
    """Normalize hyphenated and spaced facet labels to the same token set."""

    return frozenset(_WORDS.findall(value.casefold().replace("-", " ")))


@dataclass(frozen=True, slots=True)
class SelectionPolicy:
    """Stable local selection limits; exploration is a constraint, not a score bonus."""

    minimum_score: float = 0.2
    target: int = 20
    core_cap: int = 14
    adjacent_cap: int = 4
    exploration_cap: int = 2
    minimum_judged_quality: float = 0.25

    def __post_init__(self) -> None:
        if not 0 <= self.minimum_score <= 1 or not 0 <= self.minimum_judged_quality <= 1:
            raise ValueError("selection thresholds must be normalized")
        if self.target < 1 or min(self.core_cap, self.adjacent_cap, self.exploration_cap) < 0:
            raise ValueError("selection counts are invalid")
        if self.exploration_cap < 1:
            raise ValueError("selection policy must reserve an exploration slot")


def pre_rank(
    candidates: tuple[ArxivCandidate, ...],
    profile: RemoteProfile,
    now: datetime,
    *,
    author_bonus: float = 0.75,
    institution_bonus: float = 0.5,
    identity_bonus_cap: float = 1.0,
    weight_set: WeightSet = DEFAULT_WEIGHT_SET,
    extra_features: Mapping[str, tuple[NormalizedFeature, ...]] | None = None,
) -> tuple[ScoredCandidate, ...]:
    """Score public candidates with applicability-aware normalized local features."""

    terms = frozenset(profile.topics)
    core = frozenset(profile.core_categories)
    adjacent = frozenset(profile.adjacent_categories)
    scored: list[ScoredCandidate] = []
    for candidate in candidates:
        words = set(_WORDS.findall((candidate.title + " " + candidate.summary).casefold()))
        lexical = min(len(words & terms) / max(len(terms), 1), 1.0)
        source = _source(candidate.categories, core, adjacent)
        category = {"core": 1.0, "adjacent": 0.6, "exploration": 0.2}[source]
        age = max((now.astimezone(UTC) - candidate.published).total_seconds() / 86400, 0.0)
        recency = max(0.0, 1.0 - age / 14)
        candidate_key = candidate.arxiv_id.canonical
        author_match = _matches_any(candidate.authors, profile.watched_authors)
        institution_match = _matches_any(candidate.affiliations, profile.watched_institutions)
        watched_author = author_bonus if author_match else 0.0
        watched_institution = institution_bonus if institution_match else 0.0
        watched_author = min(watched_author, identity_bonus_cap)
        watched_institution = min(
            watched_institution, max(0.0, identity_bonus_cap - watched_author)
        )
        facet_match = _facet_match(words, profile)
        features = (
            NormalizedFeature(
                "lexical", lexical, True, 1.0, "local-profile", FeatureGroup.INTEREST
            ),
            NormalizedFeature(
                "category", category, True, 1.0, "local-profile", FeatureGroup.INTEREST
            ),
            NormalizedFeature(
                "facet",
                facet_match,
                bool(profile.preference_facets),
                1.0 if profile.preference_facets else 0.0,
                "local-profile",
                FeatureGroup.INTEREST,
            ),
            NormalizedFeature(
                "recency",
                recency,
                True,
                1.0,
                "arxiv-metadata",
                FeatureGroup.RECENCY,
                candidate.updated,
            ),
            NormalizedFeature(
                "identity",
                watched_author + watched_institution,
                bool(profile.watched_authors or profile.watched_institutions),
                1.0 if profile.watched_authors or profile.watched_institutions else 0.0,
                "watchlist",
                FeatureGroup.IDENTITY,
            ),
        )
        merged_features = _merge_features(features, (extra_features or {}).get(candidate_key, ()))
        score, group_values, group_contributions = _score_features(merged_features, weight_set)
        components = tuple((feature.name, feature.value) for feature in merged_features) + (
            ("watched_author", watched_author),
            ("watched_institution", watched_institution),
            *((f"{group.value}_value", value) for group, value in group_values.items()),
            *(
                (f"{group.value}_contribution", value)
                for group, value in group_contributions.items()
            ),
        )
        scored.append(
            ScoredCandidate(
                candidate,
                score,
                components,
                source,
                merged_features,
                weight_set.version,
            )
        )
    return tuple(sorted(scored, key=lambda item: (-item.score, item.candidate.arxiv_id.canonical)))


def _matches_any(values: tuple[str, ...], identities: tuple[WatchedIdentity, ...]) -> bool:
    normalized_values = {normalize_identity(value) for value in values if value.strip()}
    return any(bool(normalized_values & identity.normalized_names) for identity in identities)


def select_diverse(
    scored: tuple[ScoredCandidate, ...],
    minimum_score: float = 0.2,
    target: int = 20,
    *,
    policy: SelectionPolicy | None = None,
) -> tuple[ScoredCandidate, ...]:
    """Select quality-qualified results with quotas and author/topic diversity."""

    if policy is None and minimum_score > 1:
        return ()
    active_policy = policy or SelectionPolicy(minimum_score=minimum_score, target=target)
    quotas = {
        "core": active_policy.core_cap,
        "adjacent": active_policy.adjacent_cap,
        "exploration": active_policy.exploration_cap,
    }
    selected: list[ScoredCandidate] = []
    authors: Counter[str] = Counter()
    topic_sets: list[set[str]] = []
    qualified = [
        item
        for item in scored
        if item.score >= active_policy.minimum_score
        and _meets_judged_quality(item, active_policy.minimum_judged_quality)
    ]
    # Reserve an eligible exploration slot before source-cap filling. Presentation order is applied
    # after selection, so this does not turn the batch into a source-grouped reading experience.
    for source in ("exploration", "core", "adjacent"):
        for item in qualified:
            if (
                item.source == source
                and len([value for value in selected if value.source == source]) < quotas[source]
                and _diverse(item, authors, topic_sets)
                and len(selected) < active_policy.target
            ):
                _append(item, selected, authors, topic_sets)
    for item in qualified:
        if len(selected) >= active_policy.target:
            break
        if item not in selected and _diverse(item, authors, topic_sets):
            _append(item, selected, authors, topic_sets)
    return tuple(selected[: active_policy.target])


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


def _source(categories: tuple[str, ...], core: frozenset[str], adjacent: frozenset[str]) -> str:
    if set(categories) & core:
        return "core"
    if set(categories) & adjacent:
        return "adjacent"
    return "exploration"


def _facet_match(words: set[str], profile: RemoteProfile) -> float:
    canonical_words = frozenset(
        token for word in words for token in word.split("-") if len(token) >= 3
    )
    facets = []
    for facet in profile.preference_facets:
        facet_terms = _facet_tokens(facet.value)
        if facet_terms and facet_terms <= canonical_words:
            facets.append(facet)
    return min(sum(facet.score * facet.confidence for facet in facets) / 3, 1.0)


def _merge_features(
    local: tuple[NormalizedFeature, ...], extra: tuple[NormalizedFeature, ...]
) -> tuple[NormalizedFeature, ...]:
    names = [feature.name for feature in (*local, *extra)]
    if len(set(names)) != len(names):
        raise ValueError("ranking feature names must be unique per candidate")
    return (*local, *extra)


def _score_features(
    features: tuple[NormalizedFeature, ...], weight_set: WeightSet
) -> tuple[float, dict[FeatureGroup, float], dict[FeatureGroup, float]]:
    by_group: dict[FeatureGroup, list[NormalizedFeature]] = {group: [] for group in FeatureGroup}
    for feature in features:
        if feature.applicable:
            by_group[feature.group].append(feature)
    group_values = {
        group: _group_value(group, values) for group, values in by_group.items() if values
    }
    available_weight = sum(weight_set.group_weights[group] for group in group_values)
    contributions = {
        group: (weight_set.group_weights[group] * value / available_weight)
        for group, value in group_values.items()
        if available_weight > 0
    }
    return sum(contributions.values()), group_values, contributions


def _group_value(group: FeatureGroup, features: list[NormalizedFeature]) -> float:
    importance = (
        {
            "lexical": 0.55,
            "category": 0.3,
            "facet": 0.15,
        }
        if group is FeatureGroup.INTEREST
        else {}
    )
    weighted = [(feature, importance.get(feature.name, 1.0)) for feature in features]
    denominator = sum(weight for _, weight in weighted)
    return (
        sum(feature.value * feature.confidence * weight for feature, weight in weighted)
        / denominator
    )


def _meets_judged_quality(item: ScoredCandidate, minimum: float) -> bool:
    values = [
        feature.value * feature.confidence
        for feature in item.feature_values
        if feature.group is FeatureGroup.SCIENTIFIC_QUALITY and feature.applicable
    ]
    return not values or sum(values) / len(values) >= minimum
