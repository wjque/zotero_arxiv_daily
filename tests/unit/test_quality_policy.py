from __future__ import annotations

import pytest

from zotero_arxiv_daily.profile.quality_policy import (
    DEFAULT_QUALITY_REFERENCE_POLICY,
    LEGACY_QUALITY_REFERENCE_POLICY,
    QUALITY_REFERENCE_FIELDS,
    QUALITY_REFERENCE_POLICY_V1,
    QualityReferenceFieldRole,
    QualityReferencePolicy,
    get_quality_reference_policy,
    quality_reference_policies,
    quality_reference_policy_versions,
)


def test_default_policy_covers_fields_without_trait_specific_rules() -> None:
    policy = DEFAULT_QUALITY_REFERENCE_POLICY
    instruction = policy.judge_instruction()

    assert set(dict(policy.field_roles)) == set(QUALITY_REFERENCE_FIELDS)
    assert policy.fields_for(QualityReferenceFieldRole.DESCRIPTIVE) == (
        "research_problems",
        "motivations",
    )
    assert not policy.reference_evidence_allowed
    assert LEGACY_QUALITY_REFERENCE_POLICY.reference_evidence_allowed
    assert policy.model_fields == (
        "methodological_expectations",
        "evidence_standards",
        "tolerated_limitations",
    )
    assert LEGACY_QUALITY_REFERENCE_POLICY.model_fields == QUALITY_REFERENCE_FIELDS
    assert "relative prevalence within each field" in instruction
    assert "must never increase or decrease a dimension score" in instruction
    assert "demonstrated failure may lower the relevant dimension" in instruction
    assert "insufficient evidence remains unknown" in instruction
    assert "an empty value means unspecified" in instruction
    assert "must never appear in evidence_fields" in instruction
    assert len(policy.fingerprint) == 64
    for trait_specific_value in ("systems", "practical_utility", "error_analysis", "WBench"):
        assert trait_specific_value not in instruction


def test_policy_registry_is_explicit_and_rejects_incomplete_versions() -> None:
    assert quality_reference_policy_versions() == (
        QUALITY_REFERENCE_POLICY_V1.version,
        DEFAULT_QUALITY_REFERENCE_POLICY.version,
    )
    assert quality_reference_policies() == (
        LEGACY_QUALITY_REFERENCE_POLICY,
        QUALITY_REFERENCE_POLICY_V1,
        DEFAULT_QUALITY_REFERENCE_POLICY,
    )
    assert get_quality_reference_policy(DEFAULT_QUALITY_REFERENCE_POLICY.version) is (
        DEFAULT_QUALITY_REFERENCE_POLICY
    )
    assert QUALITY_REFERENCE_POLICY_V1.fingerprint == (
        "88111d76fd6db06387f09bfff8c9f008b3c9c434eb8ea4ddad933f58ea49dd21"
    )
    with pytest.raises(ValueError, match="unsupported"):
        get_quality_reference_policy("missing")
    with pytest.raises(ValueError, match="required field roles"):
        QualityReferencePolicy(
            "incomplete",
            "judge-v9",
            tuple(
                (field, QualityReferenceFieldRole.DESCRIPTIVE) for field in QUALITY_REFERENCE_FIELDS
            ),
        )
