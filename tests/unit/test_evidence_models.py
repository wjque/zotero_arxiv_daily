from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from zotero_arxiv_daily.evidence.models import (
    EvidenceAvailability,
    EvidenceValue,
    PublicPaperEvidence,
    RepositoryEvidence,
)

_NOW = datetime(2026, 8, 3, tzinfo=UTC)


def _value(availability: EvidenceAvailability = EvidenceAvailability.AVAILABLE) -> EvidenceValue:
    return EvidenceValue(availability, 0.8, _NOW, "synthetic")


def test_repository_evidence_requires_verified_allowlisted_association() -> None:
    repository = RepositoryEvidence(
        "https://github.com/example/project",
        _value(),
        _value(),
        _value(),
        _value(),
        _value(),
        _value(),
    )
    evidence = PublicPaperEvidence(
        "arxiv:2401.00001", 1, _NOW, _NOW + timedelta(days=7), repository
    )

    assert evidence.repository is repository
    with pytest.raises(ValueError, match="unverified"):
        RepositoryEvidence(
            "https://github.com/example/project",
            _value(EvidenceAvailability.UNKNOWN),
            _value(),
            _value(),
            _value(),
            _value(),
            _value(),
        )


def test_missing_repository_can_be_explicitly_inapplicable() -> None:
    unavailable = _value(EvidenceAvailability.INAPPLICABLE)
    repository = RepositoryEvidence(
        None, unavailable, unavailable, unavailable, unavailable, unavailable, unavailable
    )

    assert repository.repository_url is None
