from __future__ import annotations

from pathlib import Path

import pytest

from zotero_arxiv_daily.core.errors import ApplicationError
from zotero_arxiv_daily.ranking.weights import (
    FeatureGroup,
    NormalizedFeature,
    WeightSet,
    WeightSetRegistry,
)


def test_normalized_features_and_weight_sets_reject_unbounded_values() -> None:
    with pytest.raises(ValueError, match="zero and one"):
        NormalizedFeature("interest", 1.1, True, 1.0, "test")
    with pytest.raises(ValueError, match="cannot exceed"):
        WeightSet("invalid", interest=0.8, recency=0.3)


def test_weight_set_is_immutable_and_has_a_stable_version() -> None:
    weight_set = WeightSet("coarse-test", interest=0.5, recency=0.1, feedback=0.1, identity=0.1)

    assert weight_set.version == "coarse-test"
    assert weight_set.negative_feedback_cap == 0.2


def test_weight_set_registry_keeps_definitions_immutable_and_activation_reversible(
    tmp_path: Path,
) -> None:
    registry = WeightSetRegistry(tmp_path / "weights.json")
    first = WeightSet("coarse-a")
    second = WeightSet("coarse-b", feedback=0.1, identity=0.1, context=0.1)

    assert registry.register(first)
    assert registry.active() == WeightSet("coarse-v1")
    assert not registry.register(first)
    assert registry.register(second)
    assert registry.activate("coarse-b") == second
    assert registry.activate("coarse-a") == first
    assert (tmp_path / "weights.json").stat().st_mode & 0o077 == 0
    with pytest.raises(ApplicationError, match="immutable"):
        registry.register(WeightSet("coarse-a", feedback=0.1))


def test_unavailable_feature_cannot_hide_a_nonzero_value() -> None:
    with pytest.raises(ValueError, match="unavailable"):
        NormalizedFeature("quality", 0.2, False, 0.0, "test", FeatureGroup.SCIENTIFIC_QUALITY)
