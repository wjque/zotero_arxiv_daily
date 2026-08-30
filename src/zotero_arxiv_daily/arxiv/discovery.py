"""Deterministic privacy-bounded planning for category-only arXiv discovery."""

from __future__ import annotations

import re
from dataclasses import dataclass

from zotero_arxiv_daily.arxiv.categories import expand_one_hop
from zotero_arxiv_daily.arxiv.models import ArxivCandidate

_CATEGORY = re.compile(r"[A-Za-z][A-Za-z0-9.-]{1,31}")
_FACET_VALUE = re.compile(r"[a-z0-9]+(?:-[a-z0-9]+)*")
_WORDS = re.compile(r"[a-z][a-z0-9-]{2,}")

# Facets are local allowlisted values. Only mapped public category names cross the arXiv boundary.
_BRIDGE_CATEGORY_MAP: dict[tuple[str, str], tuple[str, ...]] = {
    ("domain", "machine-learning"): ("cs.IR", "cs.RO", "cs.SE", "cs.HC"),
    ("domain", "language"): ("cs.IR", "cs.HC", "cs.SI"),
    ("domain", "computer-vision"): ("cs.RO", "eess.IV", "cs.GR"),
    ("domain", "algorithms"): ("cs.DS", "cs.CC", "math.OC"),
    ("domain", "quantum-computing"): ("cs.ET", "physics.comp-ph"),
    ("method", "transformers"): ("cs.IR", "cs.CV", "cs.RO"),
    ("method", "diffusion"): ("cs.CV", "stat.ML", "eess.IV"),
    ("method", "reinforcement-learning"): ("cs.RO", "cs.MA", "eess.SY"),
    ("method", "optimization"): ("math.OC", "cs.DS", "eess.SY"),
    ("method", "statistical-modeling"): ("stat.ME", "stat.AP", "econ.EM"),
    ("task", "classification"): ("stat.ML", "cs.IR"),
    ("task", "generation"): ("cs.CL", "cs.CV", "cs.MM"),
    ("task", "retrieval"): ("cs.IR", "cs.DB"),
    ("task", "reasoning"): ("cs.AI", "cs.CL", "cs.LO"),
    ("task", "translation"): ("cs.CL", "cs.CV"),
}


@dataclass(frozen=True, slots=True)
class DiscoveryFacet:
    """One bounded local facet eligible for deterministic bridge planning."""

    kind: str
    value: str
    score: float
    confidence: float

    def __post_init__(self) -> None:
        if (
            self.kind not in {"domain", "method", "task"}
            or not _FACET_VALUE.fullmatch(self.value)
            or not 0 <= self.score <= 1
            or not 0 <= self.confidence <= 1
        ):
            raise ValueError("discovery facet is invalid")

    @property
    def strength(self) -> float:
        return self.score * self.confidence


@dataclass(frozen=True, slots=True)
class DiscoveryQuery:
    """One category query plus optional local-only bridge acceptance facets."""

    category: str
    required_facets: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        if not _CATEGORY.fullmatch(self.category):
            raise ValueError("discovery category is invalid")
        if (
            len(self.required_facets) > 4
            or len(set(self.required_facets)) != len(self.required_facets)
            or any(not _FACET_VALUE.fullmatch(value) for value in self.required_facets)
        ):
            raise ValueError("discovery query facets are invalid")

    @property
    def is_bridge(self) -> bool:
        return bool(self.required_facets)


@dataclass(frozen=True, slots=True)
class DiscoveryPolicy:
    """Hard limits for privacy-safe category and bridge query planning."""

    maximum_core_queries: int = 6
    maximum_adjacent_queries: int = 6
    maximum_bridge_queries: int = 4
    maximum_facets: int = 8
    minimum_facet_strength: float = 0.25

    def __post_init__(self) -> None:
        if (
            not 1 <= self.maximum_core_queries <= 6
            or not 0 <= self.maximum_adjacent_queries <= 6
            or not 0 <= self.maximum_bridge_queries <= 4
            or not 0 <= self.maximum_facets <= 12
            or not 0 <= self.minimum_facet_strength <= 1
        ):
            raise ValueError("discovery policy is invalid")


DEFAULT_DISCOVERY_POLICY = DiscoveryPolicy()


def category_queries(categories: tuple[str, ...]) -> tuple[DiscoveryQuery, ...]:
    """Adapt the released category-only path without adding bridge behavior."""

    return tuple(DiscoveryQuery(category) for category in dict.fromkeys(categories))


def plan_discovery_queries(
    core_categories: tuple[str, ...],
    facets: tuple[DiscoveryFacet, ...],
    *,
    policy: DiscoveryPolicy = DEFAULT_DISCOVERY_POLICY,
) -> tuple[DiscoveryQuery, ...]:
    """Plan core, one-hop, then bounded cross-category queries without remote facet text."""

    core = tuple(dict.fromkeys(core_categories))[: policy.maximum_core_queries]
    if not core:
        return ()
    baseline_categories = core + expand_one_hop(core, policy.maximum_adjacent_queries)
    baseline = category_queries(baseline_categories)
    excluded = frozenset(baseline_categories)
    eligible_facets: dict[tuple[str, str], DiscoveryFacet] = {}
    for facet in facets:
        key = (facet.kind, facet.value)
        previous = eligible_facets.get(key)
        if (
            facet.strength >= policy.minimum_facet_strength
            and key in _BRIDGE_CATEGORY_MAP
            and (
                previous is None
                or (facet.strength, facet.score, facet.confidence)
                > (previous.strength, previous.score, previous.confidence)
            )
        ):
            eligible_facets[key] = facet
    ranked_facets = tuple(
        sorted(
            eligible_facets.values(),
            key=lambda facet: (
                -facet.strength,
                -facet.score,
                -facet.confidence,
                facet.kind,
                facet.value,
            ),
        )[: policy.maximum_facets]
    )
    selected_categories: list[str] = []
    facets_by_category: dict[str, list[str]] = {}
    for facet in ranked_facets:
        for category in _BRIDGE_CATEGORY_MAP[(facet.kind, facet.value)]:
            if category in excluded:
                continue
            values = facets_by_category.setdefault(category, [])
            if facet.value not in values and len(values) < 4:
                values.append(facet.value)
            if (
                category not in selected_categories
                and len(selected_categories) < policy.maximum_bridge_queries
            ):
                selected_categories.append(category)
    bridges = tuple(
        DiscoveryQuery(category, tuple(facets_by_category[category]))
        for category in selected_categories
    )
    return (*baseline, *bridges)


def bridge_candidate_matches(candidate: ArxivCandidate, query: DiscoveryQuery) -> bool:
    """Keep a bridge candidate only when public text contains one planned local facet."""

    if not query.is_bridge:
        return True
    words = frozenset(
        token
        for word in _WORDS.findall(f"{candidate.title} {candidate.summary}".casefold())
        for token in word.split("-")
        if len(token) >= 3
    )
    return any(
        tokens and tokens <= words
        for value in query.required_facets
        if (tokens := _facet_tokens(value))
    )


def _facet_tokens(value: str) -> frozenset[str]:
    return frozenset(_WORDS.findall(value.replace("-", " ")))
