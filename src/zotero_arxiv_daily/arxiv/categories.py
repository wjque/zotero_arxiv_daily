"""Versioned one-hop adjacent arXiv category expansion."""

from __future__ import annotations

CATEGORY_GRAPH: dict[str, tuple[str, ...]] = {
    "cs.LG": ("stat.ML", "cs.AI", "cs.NE"),
    "cs.CL": ("cs.AI",),
    "cs.CV": ("cs.LG",),
    "quant-ph": ("cond-mat.str-el",),
}


def adjacent_categories(category: str) -> tuple[str, ...]:
    """Return the authoritative one-hop neighbors for one arXiv category."""

    return CATEGORY_GRAPH.get(category, ())


def expand_one_hop(core_categories: tuple[str, ...], maximum: int = 6) -> tuple[str, ...]:
    """Return deterministic adjacent categories without recursively expanding them."""

    return tuple(
        category
        for category in sorted(
            {item for core in core_categories for item in adjacent_categories(core)}
        )
        if category not in core_categories
    )[:maximum]
