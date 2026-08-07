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
    ApprovedQualityExample,
    QualityProfileStore,
    build_quality_reference_profile,
    read_quality_examples,
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
        "research_problems",
        "methodological_expectations",
        "evidence_standards",
        "motivations",
        "tolerated_limitations",
    }


def test_empty_and_malformed_quality_evidence_is_rejected(tmp_path: Path) -> None:
    with pytest.raises(ApplicationError, match="approved example"):
        build_quality_reference_profile((), ())
    with pytest.raises(ValueError, match="methodological_expectations"):
        ApprovedQualityExample("paper", True, methodological_expectations=("trust_me",))

    path = tmp_path / "examples.json"
    path.write_text('{"schema_version":1,"examples":[{"paper_id":"paper"}]}')
    with pytest.raises(ApplicationError, match="examples are invalid"):
        read_quality_examples(path)


def test_profile_versions_are_idempotent_approvable_and_reversible(tmp_path: Path) -> None:
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
    assert store.path.stat().st_mode & 0o077 == 0


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
