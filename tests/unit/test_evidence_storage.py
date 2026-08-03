from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path

from zotero_arxiv_daily.evidence.models import (
    EvidenceAvailability,
    EvidenceValue,
    PublicPaperEvidence,
    RepositoryEvidence,
)
from zotero_arxiv_daily.evidence.storage import EvidenceCache


def test_evidence_cache_returns_only_unexpired_validated_facts(tmp_path: Path) -> None:
    now = datetime(2026, 8, 3, tzinfo=UTC)
    value = EvidenceValue(EvidenceAvailability.AVAILABLE, 0.8, now, "synthetic")
    repository = RepositoryEvidence(
        "https://github.com/example/project", value, value, value, value, value, value
    )
    evidence = PublicPaperEvidence("arxiv:2401.00001", 1, now, now + timedelta(days=1), repository)
    cache = EvidenceCache(tmp_path / "evidence.json")

    cache.put(evidence)

    assert cache.get("arxiv:2401.00001", now) == evidence
    assert cache.get("arxiv:2401.00001", now + timedelta(days=2)) is None
