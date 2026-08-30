from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from zotero_arxiv_daily.core.errors import ExternalServiceError
from zotero_arxiv_daily.evaluation.predictions import (
    WORTHWHILE_PREDICTION_SCHEMA_VERSION,
    BatchPrediction,
    WorthwhilePredictionStore,
)
from zotero_arxiv_daily.evaluation.worthwhile import run_worthwhile_evaluation
from zotero_arxiv_daily.feedback.ledger import (
    FeedbackEvent,
    FeedbackEventType,
    FeedbackLedgerStore,
    FeedbackOutcome,
)
from zotero_arxiv_daily.ranking.outcome import WorthwhileEstimate

_NOW = datetime(2026, 8, 20, 12, tzinfo=UTC)


def _estimate(paper_id: str, expected: float = 0.24) -> WorthwhileEstimate:
    return WorthwhileEstimate(
        paper_id, 0.8, 0.5, expected / 0.8, 0.5, expected, True, "declared-prior-v1", "test-v1"
    )


def _batch(batch_id: str, *paper_ids: str, hours: int = 0) -> BatchPrediction:
    return BatchPrediction(
        batch_id,
        _NOW + timedelta(hours=hours),
        "declared-prior-v1",
        "test-v1",
        tuple(_estimate(paper_id) for paper_id in paper_ids),
    )


def test_absent_store_reports_no_predictions_rather_than_failing(tmp_path: Path) -> None:
    store = WorthwhilePredictionStore(tmp_path / "worthwhile-predictions.json")

    assert store.predictions() == {}
    assert store.batches() == ()


def test_recorded_batch_round_trips_in_displayed_rank_order(tmp_path: Path) -> None:
    path = tmp_path / "worthwhile-predictions.json"
    store = WorthwhilePredictionStore(path)

    assert store.record(_batch("published-a", "2408.00001", "2408.00002")) is True

    restored = WorthwhilePredictionStore(path).predictions()
    assert tuple(item.arxiv_id for item in restored["published-a"]) == (
        "2408.00001",
        "2408.00002",
    )
    assert restored["published-a"][0] == _estimate("2408.00001")
    assert path.stat().st_mode & 0o077 == 0


def test_identical_re_record_is_a_no_op_so_a_retried_publication_is_safe(tmp_path: Path) -> None:
    store = WorthwhilePredictionStore(tmp_path / "worthwhile-predictions.json")
    store.record(_batch("published-a", "2408.00001"))

    assert store.record(_batch("published-a", "2408.00001")) is False
    assert len(store.batches()) == 1


def test_conflicting_prediction_for_a_recorded_batch_is_refused(tmp_path: Path) -> None:
    store = WorthwhilePredictionStore(tmp_path / "worthwhile-predictions.json")
    store.record(_batch("published-a", "2408.00001"))

    with pytest.raises(ExternalServiceError, match="conflicts"):
        store.record(_batch("published-a", "2408.00002"))

    assert tuple(item.arxiv_id for item in store.predictions()["published-a"]) == ("2408.00001",)


def test_retention_keeps_the_newest_batches_and_drops_the_oldest(tmp_path: Path) -> None:
    store = WorthwhilePredictionStore(tmp_path / "worthwhile-predictions.json")
    for index in range(70):
        store.record(_batch(f"published-{index:03d}", f"2408.{index:05d}", hours=index))

    recorded = store.batches()
    assert len(recorded) == 60
    assert recorded[0].batch_id == "published-010"
    assert recorded[-1].batch_id == "published-069"


def test_unsupported_schema_is_refused_rather_than_silently_ignored(tmp_path: Path) -> None:
    path = tmp_path / "worthwhile-predictions.json"
    path.write_text(json.dumps({"schema_version": 99, "batches": []}), encoding="utf-8")

    with pytest.raises(ExternalServiceError, match="unsupported worthwhile prediction schema"):
        WorthwhilePredictionStore(path).predictions()


def test_out_of_range_stored_estimate_is_refused(tmp_path: Path) -> None:
    path = tmp_path / "worthwhile-predictions.json"
    stored = _batch("published-a", "2408.00001")
    payload = {
        "schema_version": WORTHWHILE_PREDICTION_SCHEMA_VERSION,
        "batches": [
            {
                "batch_id": stored.batch_id,
                "recorded_at": stored.recorded_at.isoformat(),
                "policy_version": stored.policy_version,
                "weight_set_version": stored.weight_set_version,
                "estimates": [
                    {
                        "arxiv_id": "2408.00001",
                        "reading_likelihood": 1.5,
                        "reading_likelihood_confidence": 0.5,
                        "post_reading_value": 0.3,
                        "post_reading_value_confidence": 0.5,
                        "expected_worthwhile": 0.24,
                        "value_evidence_available": True,
                        "policy_version": "declared-prior-v1",
                        "provenance": "test-v1",
                    }
                ],
            }
        ],
    }
    path.write_text(json.dumps(payload), encoding="utf-8")

    with pytest.raises(ExternalServiceError, match="worthwhile prediction batch is invalid"):
        WorthwhilePredictionStore(path).predictions()


def test_batch_prediction_refuses_mixed_policy_versions() -> None:
    other = WorthwhileEstimate(
        "2408.00002", 0.8, 0.5, 0.3, 0.5, 0.24, True, "declared-prior-v2", "test-v1"
    )

    with pytest.raises(ValueError, match="mixes worthwhile policy versions"):
        BatchPrediction(
            "published-a", _NOW, "declared-prior-v1", "test-v1", (_estimate("2408.00001"), other)
        )


def test_stored_predictions_feed_the_offline_report_without_further_translation(
    tmp_path: Path,
) -> None:
    ledger = FeedbackLedgerStore(tmp_path / "feedback-state.json")
    ledger.record_impressions("published-a", ("2408.00001", "2408.00002"), _NOW)
    ledger.ingest(
        (
            FeedbackEvent(
                "read-one",
                FeedbackEventType.OUTCOME,
                "2408.00001",
                _NOW + timedelta(hours=1),
                FeedbackOutcome.READ,
            ),
            FeedbackEvent(
                "worthwhile-one",
                FeedbackEventType.OUTCOME,
                "2408.00001",
                _NOW + timedelta(hours=2),
                FeedbackOutcome.WORTHWHILE,
            ),
        )
    )
    predictions = WorthwhilePredictionStore(tmp_path / "worthwhile-predictions.json")
    predictions.record(_batch("published-a", "2408.00001", "2408.00002"))

    report = run_worthwhile_evaluation(
        ledger.batch_outcomes(), ledger.position_outcomes(), predictions=predictions.predictions()
    )

    assert report.predicted_worthwhile_reads == pytest.approx(0.48)
    assert report.batches[0].predicted_minus_realized == pytest.approx(-0.52)
    assert "no batch prediction was supplied" not in " ".join(report.warnings)


def test_a_batch_that_never_deployed_is_never_compared(tmp_path: Path) -> None:
    ledger = FeedbackLedgerStore(tmp_path / "feedback-state.json")
    ledger.record_impressions("published-a", ("2408.00001",), _NOW)
    predictions = WorthwhilePredictionStore(tmp_path / "worthwhile-predictions.json")
    predictions.record(_batch("published-a", "2408.00001"))
    predictions.record(_batch("published-orphan", "2408.00009", hours=1))

    report = run_worthwhile_evaluation(
        ledger.batch_outcomes(), predictions=predictions.predictions()
    )

    assert report.batch_count == 1
    assert tuple(item.batch_id for item in report.batches) == ("published-a",)
    assert report.predicted_worthwhile_reads == pytest.approx(0.24)
