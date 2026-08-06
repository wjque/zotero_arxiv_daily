"""Allowlisted categorical preference signals for model explanations."""

from __future__ import annotations

PREFERENCE_CONTEXT_SCHEMA_VERSION = 1
MAX_PREFERENCE_SIGNALS = 4
ALLOWED_PREFERENCE_SIGNALS = frozenset(
    {
        "topic_overlap",
        "category_overlap",
        "preference_facet_overlap",
        "watched_author",
        "watched_institution",
    }
)


def validate_preference_signals(values: tuple[str, ...]) -> tuple[str, ...]:
    """Return a bounded, deterministic categorical projection or fail closed."""

    if len(values) > MAX_PREFERENCE_SIGNALS:
        raise ValueError("preference context contains too many signals")
    if any(value not in ALLOWED_PREFERENCE_SIGNALS for value in values):
        raise ValueError("preference context contains an unsupported signal")
    if len(set(values)) != len(values):
        raise ValueError("preference context contains duplicate signals")
    return tuple(values)
