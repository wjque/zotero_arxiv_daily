from __future__ import annotations

import pytest

from zotero_arxiv_daily.ranking.weights import NormalizedFeature, WeightSet


def test_normalized_features_and_weight_sets_reject_unbounded_values() -> None:
    with pytest.raises(ValueError, match="zero and one"):
        NormalizedFeature("interest", 1.1, True, 1.0, "test")
    with pytest.raises(ValueError, match="cannot exceed"):
        WeightSet("invalid", interest=0.8, recency=0.3)


def test_weight_set_is_immutable_and_has_a_stable_version() -> None:
    weight_set = WeightSet("coarse-test", interest=0.5, recency=0.1, feedback=0.1, identity=0.1)

    assert weight_set.version == "coarse-test"
    assert weight_set.negative_feedback_cap == 0.2
