"""Deterministic pre-ranking, quotas, quality thresholds, and diversity limits."""

from __future__ import annotations

import re
from collections import Counter
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from enum import StrEnum

from zotero_arxiv_daily.arxiv.models import ArxivCandidate
from zotero_arxiv_daily.profile.models import PreferenceFacet, RemoteServingProfile
from zotero_arxiv_daily.ranking.exploration import ExplorationDecision
from zotero_arxiv_daily.ranking.interest import (
    protected_interest_match,
    serving_profile_key,
    watched_identity_match,
)
from zotero_arxiv_daily.ranking.models import (
    RecommendationRecord,
    ScientificValueAssessment,
    ScoredCandidate,
)
from zotero_arxiv_daily.ranking.outcome import WorthwhileEstimate, unknown_estimate
from zotero_arxiv_daily.ranking.weights import (
    DEFAULT_WEIGHT_SET,
    FeatureGroup,
    NormalizedFeature,
    WeightSet,
    group_value,
)

_WORDS = re.compile(r"[a-z][a-z0-9-]{2,}")


def _facet_tokens(value: str) -> frozenset[str]:
    """Normalize hyphenated and spaced facet labels to the same token set."""

    return frozenset(_WORDS.findall(value.casefold().replace("-", " ")))


class SelectionObjective(StrEnum):
    """What the qualified pool is ordered by; constraints are identical under both."""

    RELEVANCE = "relevance"
    EXPECTED_WORTHWHILE = "expected_worthwhile"


@dataclass(frozen=True, slots=True)
class SelectionPolicy:
    """Stable local selection limits; exploration is a constraint, not a score bonus."""

    minimum_score: float = 0.2
    target: int = 20
    core_cap: int = 14
    adjacent_cap: int = 4
    exploration_cap: int = 2
    minimum_judged_quality: float = 0.25
    minimum_solution_advance: float = 0.5
    minimum_technical_depth: float = 0.5
    minimum_value_confidence: float = 0.5
    objective: SelectionObjective = SelectionObjective.RELEVANCE

    def __post_init__(self) -> None:
        thresholds = (
            self.minimum_score,
            self.minimum_judged_quality,
            self.minimum_solution_advance,
            self.minimum_technical_depth,
            self.minimum_value_confidence,
        )
        if any(not 0 <= value <= 1 for value in thresholds):
            raise ValueError("selection thresholds must be normalized")
        if self.target < 1 or min(self.core_cap, self.adjacent_cap, self.exploration_cap) < 0:
            raise ValueError("selection counts are invalid")
        if self.exploration_cap < 1:
            raise ValueError("selection policy must reserve an exploration slot")


def pre_rank(
    candidates: tuple[ArxivCandidate, ...],
    profile: RemoteServingProfile,
    now: datetime,
    *,
    author_bonus: float = 0.75,
    institution_bonus: float = 0.5,
    identity_bonus_cap: float = 1.0,
    profile_feature_key: str | None = None,
    weight_set: WeightSet = DEFAULT_WEIGHT_SET,
    extra_features: Mapping[str, tuple[NormalizedFeature, ...]] | None = None,
) -> tuple[ScoredCandidate, ...]:
    """Score public candidates with applicability-aware normalized local features."""

    terms = frozenset(profile.topics)
    core = frozenset(profile.core_categories)
    adjacent = frozenset(profile.adjacent_categories)
    matching_key = serving_profile_key(profile, profile_feature_key)
    scored: list[ScoredCandidate] = []
    for candidate in candidates:
        candidate_text = candidate.title + " " + candidate.summary
        words = set(_WORDS.findall(candidate_text.casefold()))
        lexical = min(len(words & terms) / max(len(terms), 1), 1.0)
        source = _source(candidate.categories, core, adjacent)
        category = {"core": 1.0, "adjacent": 0.6, "exploration": 0.2}[source]
        age = max((now.astimezone(UTC) - candidate.published).total_seconds() / 86400, 0.0)
        recency = max(0.0, 1.0 - age / 14)
        candidate_key = candidate.arxiv_id.canonical
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
        watched_author = author_bonus if author_match else 0.0
        watched_institution = institution_bonus if institution_match else 0.0
        watched_author = min(watched_author, identity_bonus_cap)
        watched_institution = min(
            watched_institution, max(0.0, identity_bonus_cap - watched_author)
        )
        features = (
            _protected_interest_features(candidate_text, category, words, profile, matching_key)
            if matching_key is not None
            else _legacy_interest_features(lexical, category, words, profile)
        ) + (
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
                bool(
                    profile.watched_authors
                    or profile.watched_institutions
                    or profile.watched_author_digests
                    or profile.watched_institution_digests
                ),
                1.0
                if (
                    profile.watched_authors
                    or profile.watched_institutions
                    or profile.watched_author_digests
                    or profile.watched_institution_digests
                )
                else 0.0,
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


def _legacy_interest_features(
    lexical: float,
    category: float,
    words: set[str],
    profile: RemoteServingProfile,
) -> tuple[NormalizedFeature, ...]:
    facet_match = _facet_match(words, profile.preference_facets)
    return (
        NormalizedFeature("lexical", lexical, True, 1.0, "local-profile", FeatureGroup.INTEREST),
        NormalizedFeature("category", category, True, 1.0, "local-profile", FeatureGroup.INTEREST),
        NormalizedFeature(
            "facet",
            facet_match,
            bool(profile.preference_facets),
            1.0 if profile.preference_facets else 0.0,
            "local-profile",
            FeatureGroup.INTEREST,
        ),
    )


def _protected_interest_features(
    candidate_text: str,
    category: float,
    words: set[str],
    profile: RemoteServingProfile,
    key: str,
) -> tuple[NormalizedFeature, ...]:
    match = protected_interest_match(candidate_text, profile, key)
    long_term_lexical_available = any(
        feature.long_term_weight > 0 for feature in profile.lexical_features
    )
    recent_lexical_available = any(
        feature.recent_weight > 0 for feature in profile.lexical_features
    )
    return (
        NormalizedFeature(
            "long_term_lexical",
            match.long_term_lexical,
            long_term_lexical_available,
            1.0 if long_term_lexical_available else 0.0,
            profile.feature_hash_version or "unknown",
            FeatureGroup.INTEREST,
        ),
        NormalizedFeature(
            "recent_lexical",
            match.recent_lexical,
            recent_lexical_available,
            1.0 if recent_lexical_available else 0.0,
            profile.feature_hash_version or "unknown",
            FeatureGroup.INTEREST,
        ),
        NormalizedFeature("category", category, True, 1.0, "local-profile", FeatureGroup.INTEREST),
        NormalizedFeature(
            "long_term_facet",
            _facet_match(words, profile.long_term_facets),
            bool(profile.long_term_facets),
            1.0 if profile.long_term_facets else 0.0,
            "controlled-facet-v1",
            FeatureGroup.INTEREST,
        ),
        NormalizedFeature(
            "recent_facet",
            _facet_match(words, profile.recent_facets),
            bool(profile.recent_facets),
            1.0 if profile.recent_facets else 0.0,
            "controlled-facet-v1",
            FeatureGroup.INTEREST,
        ),
        NormalizedFeature(
            "prototype",
            match.prototype,
            bool(profile.interest_prototypes),
            1.0 if profile.interest_prototypes else 0.0,
            profile.feature_hash_version or "unknown",
            FeatureGroup.INTEREST,
        ),
    )


def select_diverse(
    scored: tuple[ScoredCandidate, ...],
    minimum_score: float = 0.2,
    target: int = 20,
    *,
    policy: SelectionPolicy | None = None,
    scientific_values: Mapping[str, ScientificValueAssessment] | None = None,
    estimates: Mapping[str, WorthwhileEstimate] | None = None,
    exploration: ExplorationDecision | None = None,
) -> tuple[ScoredCandidate, ...]:
    """Select quality-qualified results with quotas and author/topic diversity.

    The declared objective decides only the order in which the qualified pool is walked. Every
    published constraint - minimum score, judged quality, confident scientific-value rejection,
    source quotas, author and topic diversity, and the batch target - applies identically under
    both objectives, so no estimate can enlarge a batch or admit an ineligible paper.

    A supplied exploration decision reserves the slots it has already paid for. Its picks come
    from this same qualified pool, so exploration can only reorder a batch, never enlarge it or
    admit a rejected paper, and an empty decision simply leaves the slots to ordinary selection.
    """

    if policy is None and minimum_score > 1:
        return ()
    active_policy = policy or SelectionPolicy(minimum_score=minimum_score, target=target)
    quotas = {
        "core": active_policy.core_cap,
        "adjacent": active_policy.adjacent_cap,
        # A bounded exploration policy owns the whole exploration allowance, so a batch cannot
        # hold more off-category papers than the declared budget under either reading of the word.
        "exploration": (
            active_policy.exploration_cap if exploration is None else exploration.budget
        ),
    }
    selected: list[ScoredCandidate] = []
    authors: Counter[str] = Counter()
    topic_sets: list[set[str]] = []
    qualified = list(qualified_candidates(scored, scientific_values, policy=active_policy))
    if active_policy.objective is SelectionObjective.EXPECTED_WORTHWHILE:
        qualified = _by_expected_worthwhile(qualified, estimates or {})
    if exploration is not None:
        _reserve(exploration, qualified, selected, authors, topic_sets, active_policy.target)
    # Reserve an eligible exploration slot before source-cap filling. Presentation order is applied
    # after selection, so this does not turn the batch into a source-grouped reading experience.
    for source in ("exploration", "core", "adjacent"):
        for item in qualified:
            if (
                item.source == source
                and item not in selected
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


def qualified_candidates(
    scored: Sequence[ScoredCandidate],
    scientific_values: Mapping[str, ScientificValueAssessment] | None = None,
    *,
    policy: SelectionPolicy | None = None,
) -> tuple[ScoredCandidate, ...]:
    """Return the candidates a batch is allowed to contain, before quotas and diversity.

    Exploration and ordinary selection share this single eligibility definition, so a reserved
    exploration slot can never admit a paper ordinary selection would have rejected.
    """

    active_policy = policy or SelectionPolicy()
    rejections = scientific_value_rejections(
        tuple(scored), scientific_values or {}, policy=active_policy
    )
    return tuple(
        item
        for item in scored
        if item.score >= active_policy.minimum_score
        and _meets_judged_quality(item, active_policy.minimum_judged_quality)
        and item.candidate.arxiv_id.canonical not in rejections
    )


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


def _by_expected_worthwhile(
    qualified: list[ScoredCandidate], estimates: Mapping[str, WorthwhileEstimate]
) -> list[ScoredCandidate]:
    """Order an already-qualified pool by expected worthwhile reads, deterministically.

    A candidate without an estimate falls back to the declared no-evidence prior rather than to
    zero, so an unestimated paper is treated as unknown instead of as a predicted failure.
    """

    def key(item: ScoredCandidate) -> tuple[float, str]:
        canonical = item.candidate.arxiv_id.canonical
        estimate = estimates.get(canonical) or unknown_estimate(canonical)
        return (-estimate.expected_worthwhile, canonical)

    return sorted(qualified, key=key)


def _reserve(
    decision: ExplorationDecision,
    qualified: Sequence[ScoredCandidate],
    selected: list[ScoredCandidate],
    authors: Counter[str],
    topic_sets: list[set[str]],
    target: int,
) -> None:
    """Admit the already-budgeted exploration picks before quota filling.

    A pick missing from the qualified pool is dropped rather than forced, so a decision taken
    against a stale pool degrades to ordinary selection instead of admitting an ineligible paper.
    """

    by_id = {item.candidate.arxiv_id.canonical: item for item in qualified}
    for canonical in decision.selected:
        item = by_id.get(canonical)
        if item is not None and item not in selected and len(selected) < target:
            _append(item, selected, authors, topic_sets)


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


def _facet_match(words: set[str], facets: tuple[PreferenceFacet, ...]) -> float:
    canonical_words = frozenset(
        token for word in words for token in word.split("-") if len(token) >= 3
    )
    matches = []
    for facet in facets:
        facet_terms = _facet_tokens(facet.value)
        if facet_terms and facet_terms <= canonical_words:
            matches.append(facet)
    return min(sum(facet.score * facet.confidence for facet in matches) / 3, 1.0)


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
    values = {group: group_value(group, members) for group, members in by_group.items() if members}
    available_weight = sum(weight_set.group_weights[group] for group in values)
    contributions = {
        group: (weight_set.group_weights[group] * value / available_weight)
        for group, value in values.items()
        if available_weight > 0
    }
    return sum(contributions.values()), values, contributions


def _meets_judged_quality(item: ScoredCandidate, minimum: float) -> bool:
    values = [
        feature.value * feature.confidence
        for feature in item.feature_values
        if feature.group is FeatureGroup.SCIENTIFIC_QUALITY and feature.applicable
    ]
    return not values or sum(values) / len(values) >= minimum


def _meets_scientific_value(
    assessment: ScientificValueAssessment | None, policy: SelectionPolicy
) -> bool:
    if assessment is None or assessment.confidence < policy.minimum_value_confidence:
        return True
    return (
        assessment.solution_advance is None
        or assessment.solution_advance >= policy.minimum_solution_advance
    ) and (
        assessment.technical_depth is None
        or assessment.technical_depth >= policy.minimum_technical_depth
    )


def scientific_value_rejections(
    scored: tuple[ScoredCandidate, ...],
    scientific_values: Mapping[str, ScientificValueAssessment],
    *,
    policy: SelectionPolicy | None = None,
) -> frozenset[str]:
    """Return only IDs rejected by confident, evidence-bounded local value gates."""

    active_policy = policy or SelectionPolicy()
    return frozenset(
        item.candidate.arxiv_id.canonical
        for item in scored
        if not _meets_scientific_value(
            scientific_values.get(item.candidate.arxiv_id.canonical), active_policy
        )
    )
