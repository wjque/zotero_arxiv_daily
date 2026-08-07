from __future__ import annotations

from pathlib import Path

import pytest

from zotero_arxiv_daily.core.errors import ApplicationError
from zotero_arxiv_daily.ranking.weights import (
    DEFAULT_WEIGHT_SET,
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


def test_v021_default_weights_match_the_quality_first_release_contract() -> None:
    assert DEFAULT_WEIGHT_SET.version == "quality-first-v1"
    assert DEFAULT_WEIGHT_SET.group_weights == {
        FeatureGroup.INTEREST: 0.40,
        FeatureGroup.RECENCY: 0.05,
        FeatureGroup.FEEDBACK: 0.0,
        FeatureGroup.IDENTITY: 0.10,
        FeatureGroup.SCIENTIFIC_QUALITY: 0.35,
        FeatureGroup.REPRODUCIBILITY: 0.10,
        FeatureGroup.CONTEXT: 0.0,
    }


def test_weight_set_registry_keeps_definitions_immutable_and_activation_reversible(
    tmp_path: Path,
) -> None:
    registry = WeightSetRegistry(tmp_path / "weights.json")
    first = WeightSet("coarse-a")
    second = WeightSet("coarse-b", feedback=0.1, identity=0.1, context=0.1)

    assert registry.register(first)
    assert registry.active() == DEFAULT_WEIGHT_SET
    assert not registry.register(first)
    assert registry.register(second)
    assert registry.activate("coarse-b") == second
    assert registry.activate("coarse-a") == first
    assert (tmp_path / "weights.json").stat().st_mode & 0o077 == 0
    with pytest.raises(ApplicationError, match="immutable"):
        registry.register(WeightSet("coarse-a", feedback=0.1))


def test_v020_builtin_pointer_migrates_and_can_roll_back_without_overriding_custom_state(
    tmp_path: Path,
) -> None:
    registry = WeightSetRegistry(tmp_path / "weights.json")
    legacy = WeightSet("coarse-v1")
    custom = WeightSet("operator-reviewed", interest=0.6, recency=0.1, feedback=0.0)
    registry.register(legacy)
    registry.activate(legacy.version)

    assert (
        registry.migrate_release_default(
            DEFAULT_WEIGHT_SET, previous_builtin_versions=frozenset({legacy.version})
        )
        == DEFAULT_WEIGHT_SET
    )
    assert registry.activate(legacy.version) == legacy
    registry.register(custom)
    registry.activate(custom.version)

    assert (
        registry.migrate_release_default(
            DEFAULT_WEIGHT_SET, previous_builtin_versions=frozenset({legacy.version})
        )
        == custom
    )


def test_unavailable_feature_cannot_hide_a_nonzero_value() -> None:
    with pytest.raises(ValueError, match="unavailable"):
        NormalizedFeature("quality", 0.2, False, 0.0, "test", FeatureGroup.SCIENTIFIC_QUALITY)
