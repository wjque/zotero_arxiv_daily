from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path

import pytest

from zotero_arxiv_daily.core.errors import ApplicationError
from zotero_arxiv_daily.feedback.ledger import (
    FeedbackEvent,
    FeedbackEventType,
    FeedbackOutcome,
)
from zotero_arxiv_daily.profile.quality import (
    QUALITY_PROFILE_SCHEMA_VERSION,
    QUALITY_PROFILE_STATE_SCHEMA_VERSION,
    ApprovedQualityExample,
    QualityProfileStore,
    build_quality_reference_profile,
    read_quality_examples,
)
from zotero_arxiv_daily.profile.quality_policy import (
    DEFAULT_QUALITY_REFERENCE_POLICY,
    LEGACY_QUALITY_PROFILE_POLICY_VERSION,
    get_quality_reference_policy,
)
from zotero_arxiv_daily.security.state import decrypt_state_bundle, encrypt_state_directory


def _example(paper_id: str = "2401.00001") -> ApprovedQualityExample:
    return ApprovedQualityExample(
        paper_id,
        True,
        ("evaluation",),
        ("baselines", "ablations"),
        ("held_out_evaluation",),
        ("practical_utility",),
        ("limited_scale",),
    )


def _feedback(event_id: str = "outcome-1") -> FeedbackEvent:
    return FeedbackEvent(
        event_id,
        FeedbackEventType.OUTCOME,
        "2401.00001",
        datetime(2026, 8, 7, tzinfo=UTC),
        FeedbackOutcome.WORTHWHILE,
    )


def test_profile_uses_only_approved_examples_and_explicit_feedback() -> None:
    profile = build_quality_reference_profile(
        (_example(), ApprovedQualityExample("2401.00002", False, ("systems",))),
        (
            _feedback(),
            FeedbackEvent(
                "impression-1",
                FeedbackEventType.IMPRESSION,
                "2401.00001",
                datetime(2026, 8, 7, tzinfo=UTC),
                batch_id="batch",
                displayed_rank=1,
            ),
        ),
    )

    assert profile.approved_example_count == 1
    assert profile.explicit_feedback_event_count == 1
    assert tuple(item.name for item in profile.research_problems) == ("evaluation",)
    payload = profile.prompt_payload()
    assert "2401.00001" not in json.dumps(payload)
    assert "worthwhile" not in json.dumps(payload)
    assert set(payload) == {
        "version",
        "policy_version",
        "policy_fingerprint",
        "methodological_expectations",
        "evidence_standards",
        "tolerated_limitations",
    }
    assert "research_problems" not in payload
    assert "motivations" not in payload
    assert set(profile.inspection_payload()) == {
        "version",
        "policy_version",
        "policy_fingerprint",
        "research_problems",
        "methodological_expectations",
        "evidence_standards",
        "motivations",
        "tolerated_limitations",
    }
    assert profile.schema_version == QUALITY_PROFILE_SCHEMA_VERSION
    assert payload["policy_version"] == DEFAULT_QUALITY_REFERENCE_POLICY.version
    assert payload["policy_fingerprint"] == DEFAULT_QUALITY_REFERENCE_POLICY.fingerprint


def test_empty_and_malformed_quality_evidence_is_rejected(tmp_path: Path) -> None:
    with pytest.raises(ApplicationError, match="approved example"):
        build_quality_reference_profile((), ())
    with pytest.raises(ValueError, match="methodological_expectations"):
        ApprovedQualityExample("paper", True, methodological_expectations=("trust_me",))
    with pytest.raises(ValueError, match="policy version"):
        build_quality_reference_profile((_example(),), (), policy_version="unsupported")
    with pytest.raises(ValueError, match="unavailable for generation"):
        build_quality_reference_profile(
            (_example(),), (), policy_version=LEGACY_QUALITY_PROFILE_POLICY_VERSION
        )

    path = tmp_path / "examples.json"
    path.write_text('{"schema_version":1,"examples":[{"paper_id":"paper"}]}')
    with pytest.raises(ApplicationError, match="examples are invalid"):
        read_quality_examples(path)


def test_profile_versions_are_idempotent_approvable_and_reversible(tmp_path: Path) -> None:
    empty_store = QualityProfileStore(tmp_path / "empty-quality-profile.json")
    assert empty_store.clear_approval() is None
    assert not empty_store.path.exists()

    store = QualityProfileStore(tmp_path / "quality-profile.json")
    first = build_quality_reference_profile((_example(),), ())
    second = build_quality_reference_profile((_example(),), (_feedback(),))

    assert store.register(first)
    assert not store.register(first)
    assert store.register(second)
    assert store.approved() is None
    assert store.approve(second.version) == second
    assert store.rollback(first.version) == first
    assert store.approved() == first
    assert store.get(second.version) == second
    assert store.clear_approval() == first.version
    assert store.approved() is None
    assert store.clear_approval() is None
    assert store.get(first.version) == first
    assert store.path.stat().st_mode & 0o077 == 0

    with pytest.raises(ApplicationError, match="not registered"):
        store.get("missing")


def test_quality_profile_round_trips_only_inside_encrypted_state(tmp_path: Path) -> None:
    state = tmp_path / "state"
    state.mkdir()
    for name in (
        "arxiv-state.json",
        "feedback-state.json",
        "recommendation-history.json",
    ):
        (state / name).write_text("{}")
    profile = build_quality_reference_profile((_example(),), (_feedback(),))
    store = QualityProfileStore(state / "quality-profile.json")
    store.register(profile)
    store.approve(profile.version)
    encrypted = tmp_path / "state.enc.json"

    encrypt_state_directory(state, encrypted, "correct horse battery staple")

    assert profile.version not in encrypted.read_text(encoding="utf-8")
    restored = tmp_path / "restored"
    decrypt_state_bundle(encrypted, restored, "correct horse battery staple")
    assert QualityProfileStore(restored / "quality-profile.json").approved() == profile


def test_profile_policy_fingerprint_prevents_silent_reinterpretation(tmp_path: Path) -> None:
    store = QualityProfileStore(tmp_path / "quality-profile.json")
    store.register(build_quality_reference_profile((_example(),), ()))
    value = json.loads(store.path.read_text(encoding="utf-8"))
    value["profiles"][0]["policy_fingerprint"] = "0" * 64
    store.path.write_text(json.dumps(value), encoding="utf-8")

    with pytest.raises(ApplicationError, match="state is invalid"):
        store.versions()


def test_legacy_quality_profile_state_is_read_and_upgraded_without_reinterpretation(
    tmp_path: Path,
) -> None:
    state = tmp_path / "quality-profile.json"
    fields = {
        "research_problems": [{"name": "evaluation", "support": 1.0}],
        "methodological_expectations": [{"name": "baselines", "support": 1.0}],
        "evidence_standards": [],
        "motivations": [],
        "tolerated_limitations": [],
    }
    state.write_text(
        json.dumps(
            {
                "schema_version": 1,
                "approved_version": "quality-profile-legacy",
                "profiles": [
                    {
                        "version": "quality-profile-legacy",
                        "schema_version": 1,
                        **fields,
                        "approved_example_count": 1,
                        "explicit_feedback_event_count": 0,
                    }
                ],
            }
        ),
        encoding="utf-8",
    )
    store = QualityProfileStore(state)

    legacy = store.approved()

    assert legacy is not None
    assert legacy.schema_version == 1
    assert legacy.policy_version == LEGACY_QUALITY_PROFILE_POLICY_VERSION
    assert (
        legacy.policy_fingerprint
        == get_quality_reference_policy(LEGACY_QUALITY_PROFILE_POLICY_VERSION).fingerprint
    )
    assert "policy_version" not in legacy.prompt_payload()
    assert legacy.inspection_payload()["policy_version"] == LEGACY_QUALITY_PROFILE_POLICY_VERSION
    store.clear_approval()
    migrated = json.loads(state.read_text(encoding="utf-8"))
    assert migrated["schema_version"] == QUALITY_PROFILE_STATE_SCHEMA_VERSION
    assert migrated["profiles"][0]["schema_version"] == 1
    assert "policy_version" not in migrated["profiles"][0]
    assert "policy_fingerprint" not in migrated["profiles"][0]
