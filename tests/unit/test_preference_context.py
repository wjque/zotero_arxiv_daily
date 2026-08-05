from __future__ import annotations

import pytest

from zotero_arxiv_daily.llm.preference_context import validate_preference_signals


def test_preference_context_is_bounded_to_categorical_allowlist() -> None:
    values = validate_preference_signals(
        ("topic_overlap", "category_overlap", "watched_author", "watched_institution")
    )

    assert values == (
        "topic_overlap",
        "category_overlap",
        "watched_author",
        "watched_institution",
    )


@pytest.mark.parametrize(
    "values",
    [
        ("private_topic_text",),
        ("topic_overlap",) * 5,
        ("topic_overlap", "topic_overlap"),
    ],
)
def test_preference_context_rejects_unapproved_or_unbounded_values(
    values: tuple[str, ...],
) -> None:
    with pytest.raises(ValueError):
        validate_preference_signals(values)
