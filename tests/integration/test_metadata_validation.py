from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from zotero_arxiv_daily.arxiv.models import ArxivCandidate, ArxivId, RetrievalCheckpoint
from zotero_arxiv_daily.arxiv.storage import ArxivStateStore
from zotero_arxiv_daily.core.errors import ApplicationError
from zotero_arxiv_daily.pipeline.validation import (
    RunMode,
    record_metadata_validation,
    validation_run_mode,
)


def _receipt(path: Path, deployed_at: datetime, status: str = "deployed") -> None:
    path.write_text(
        json.dumps(
            {
                "schema_version": 1,
                "status": status,
                "deployed_at": deployed_at.isoformat(),
            }
        )
    )


def _candidate(now: datetime) -> ArxivCandidate:
    return ArxivCandidate(
        ArxivId("2401.00001", 1),
        "Metadata validation",
        ("Ada",),
        ("cs.LG",),
        now,
        now,
        "https://arxiv.org/abs/2401.00001",
        "https://arxiv.org/pdf/2401.00001",
        "Public metadata only.",
    )


def test_validation_mode_uses_strict_24_hour_boundary_and_success_only(tmp_path: Path) -> None:
    now = datetime(2026, 8, 7, 12, tzinfo=UTC)
    receipt = tmp_path / "receipt.json"

    assert validation_run_mode(now, receipt) is RunMode.PUBLICATION
    _receipt(receipt, now - timedelta(hours=23, minutes=59, seconds=59))
    assert validation_run_mode(now, receipt) is RunMode.METADATA_VALIDATION
    _receipt(receipt, now - timedelta(hours=24))
    assert validation_run_mode(now, receipt) is RunMode.PUBLICATION
    _receipt(receipt, now - timedelta(hours=1), status="prepared")
    assert validation_run_mode(now, receipt) is RunMode.PUBLICATION


def test_repeated_validation_records_only_encrypted_metadata_safe_manifests(tmp_path: Path) -> None:
    started = datetime(2026, 8, 7, 12, tzinfo=UTC)
    receipt = tmp_path / "deployment-receipt.json"
    _receipt(receipt, started - timedelta(hours=1))
    candidate_state = tmp_path / "arxiv-state.json"
    store = ArxivStateStore(candidate_state)
    store.commit(RetrievalCheckpoint(started), (_candidate(started),), retention_days=None)
    protected = {
        name: tmp_path / name
        for name in (
            "recommendation-history.json",
            "feedback-state.json",
            "pending-publishable-recommendations.json",
            "pending-recommendation-history.json",
        )
    }
    for index, path in enumerate(protected.values()):
        path.write_text(json.dumps({"sentinel": index}))
    before = {name: path.read_bytes() for name, path in protected.items()}

    first = record_metadata_validation(
        store,
        receipt,
        tmp_path / "validation-manifest.json",
        tmp_path / "validation-manifest-history.json",
        started_at=started,
        completed_at=started + timedelta(minutes=2),
        workflow_run_id="123",
    )
    second = record_metadata_validation(
        store,
        receipt,
        tmp_path / "validation-manifest.json",
        tmp_path / "validation-manifest-history.json",
        started_at=started,
        completed_at=started + timedelta(minutes=2),
        workflow_run_id="123",
    )

    assert first == second
    assert first.model_requests == 0
    assert not first.site_built and not first.pages_uploaded and not first.pages_deployed
    assert before == {name: path.read_bytes() for name, path in protected.items()}
    history = json.loads((tmp_path / "validation-manifest-history.json").read_text())
    assert len(history["manifests"]) == 1


def test_validation_rejects_retry_after_the_window_expires(tmp_path: Path) -> None:
    started = datetime(2026, 8, 7, 12, tzinfo=UTC)
    receipt = tmp_path / "receipt.json"
    _receipt(receipt, started - timedelta(hours=24))

    with pytest.raises(ApplicationError, match="under 24 hours"):
        record_metadata_validation(
            ArxivStateStore(tmp_path / "missing.json"),
            receipt,
            tmp_path / "manifest.json",
            tmp_path / "history.json",
            started_at=started,
            completed_at=started,
            workflow_run_id="123",
        )
