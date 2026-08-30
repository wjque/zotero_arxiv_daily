from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from zotero_arxiv_daily.evaluation.worthwhile import (
    WORTHWHILE_REPORT_SCHEMA_VERSION,
    run_worthwhile_evaluation,
    write_worthwhile_report,
)
from zotero_arxiv_daily.feedback.ledger import (
    FeedbackEvent,
    FeedbackEventType,
    FeedbackLedgerStore,
    FeedbackOutcome,
)
from zotero_arxiv_daily.ranking.outcome import DEFAULT_WORTHWHILE_POLICY, WorthwhileEstimate

_NOW = datetime(2026, 8, 20, 12, tzinfo=UTC)


def _outcome(event_id: str, paper_id: str, outcome: FeedbackOutcome, hours: int) -> FeedbackEvent:
    return FeedbackEvent(
        event_id,
        FeedbackEventType.OUTCOME,
        paper_id,
        _NOW + timedelta(hours=hours),
        outcome,
    )


def _estimate(paper_id: str, expected: float) -> WorthwhileEstimate:
    return WorthwhileEstimate(
        paper_id, 0.8, 1.0, expected / 0.8, 1.0, expected, True, "declared-prior-v1", "test"
    )


def test_unlabeled_impressions_are_reported_as_unknown_not_as_not_worthwhile(
    tmp_path: Path,
) -> None:
    store = FeedbackLedgerStore(tmp_path / "feedback.json")
    store.record_impressions("batch-a", ("2408.00001", "2408.00002"), _NOW)
    store.ingest(
        (
            _outcome("read-one", "2408.00001", FeedbackOutcome.READ, 1),
            _outcome("worthwhile-one", "2408.00001", FeedbackOutcome.WORTHWHILE, 2),
            _outcome("read-two", "2408.00002", FeedbackOutcome.READ, 1),
        )
    )

    report = run_worthwhile_evaluation(store.batch_outcomes(), store.position_outcomes())

    assert report.worthwhile_read_count == 1
    assert report.not_worthwhile_read_count == 0
    assert report.unlabeled_impression_count == 1
    assert report.post_reading_outcome_coverage == 0.5
    assert "not counted as not worthwhile" in " ".join(report.warnings)


def test_delayed_feedback_credits_its_original_batch_without_a_retroactive_negative(
    tmp_path: Path,
) -> None:
    store = FeedbackLedgerStore(tmp_path / "feedback.json")
    store.record_impressions("batch-a", ("2408.00001",), _NOW)

    before = run_worthwhile_evaluation(store.batch_outcomes())

    store.ingest(
        (
            _outcome("read-late", "2408.00001", FeedbackOutcome.READ, 72),
            _outcome("worthwhile-late", "2408.00001", FeedbackOutcome.WORTHWHILE, 73),
        )
    )
    after = run_worthwhile_evaluation(store.batch_outcomes())

    assert before.batches[0].worthwhile_read_count == 0
    assert before.batches[0].unlabeled_impression_count == 1
    assert before.not_worthwhile_read_count == 0
    assert after.batches[0].batch_id == "batch-a"
    assert after.batches[0].worthwhile_read_count == 1
    assert after.batches[0].unlabeled_impression_count == 0
    assert after.not_worthwhile_read_count == 0


def test_predictions_are_compared_with_realized_outcomes_per_batch(tmp_path: Path) -> None:
    store = FeedbackLedgerStore(tmp_path / "feedback.json")
    store.record_impressions("batch-a", ("2408.00001", "2408.00002"), _NOW)
    store.ingest(
        (
            _outcome("worthwhile-one", "2408.00001", FeedbackOutcome.WORTHWHILE, 2),
            _outcome("not-worthwhile-two", "2408.00002", FeedbackOutcome.NOT_WORTHWHILE, 2),
        )
    )

    report = run_worthwhile_evaluation(
        store.batch_outcomes(),
        predictions={
            "batch-a": (_estimate("2408.00001", 0.8), _estimate("2408.00002", 0.7)),
        },
    )

    assert report.predicted_worthwhile_reads == pytest.approx(1.5)
    assert report.batches[0].predicted_minus_realized == pytest.approx(0.5)
    assert report.worthwhile_given_explicit_outcome == 0.5


def test_a_report_without_predictions_states_that_no_comparison_was_made(tmp_path: Path) -> None:
    store = FeedbackLedgerStore(tmp_path / "feedback.json")
    store.record_impressions("batch-a", ("2408.00001",), _NOW)

    report = run_worthwhile_evaluation(store.batch_outcomes())

    assert report.predicted_worthwhile_reads is None
    assert report.batches[0].predicted_minus_realized is None
    assert "no batch prediction was supplied" in " ".join(report.warnings)


def test_a_sparse_sample_refuses_to_propose_a_calibration(tmp_path: Path) -> None:
    store = FeedbackLedgerStore(tmp_path / "feedback.json")
    store.record_impressions("batch-a", ("2408.00001",), _NOW)
    store.ingest((_outcome("worthwhile-one", "2408.00001", FeedbackOutcome.WORTHWHILE, 2),))

    report = run_worthwhile_evaluation(store.batch_outcomes())

    assert report.proposed_calibration.sufficient is False
    assert report.proposed_calibration.proposed_reading_prior is None
    assert report.proposed_calibration.proposed_post_reading_value_prior is None
    assert report.batches[0].provisional is True
    assert "insufficient to propose a calibration" in " ".join(report.warnings)


def _labeled_store(
    path: Path, count: int, worthwhile: int, *, silent: int = 0
) -> FeedbackLedgerStore:
    """Build one batch with `count` explicitly labeled papers and `silent` never-labeled ones."""

    store = FeedbackLedgerStore(path)
    papers = tuple(f"2408.{index:05d}" for index in range(count + silent))
    store.record_impressions("batch-a", papers, _NOW)
    events: list[FeedbackEvent] = []
    for index, paper in enumerate(papers[:count]):
        label = FeedbackOutcome.WORTHWHILE if index < worthwhile else FeedbackOutcome.NOT_WORTHWHILE
        events.append(_outcome(f"read-{index}", paper, FeedbackOutcome.READ, 1))
        events.append(_outcome(f"label-{index}", paper, label, 2))
    store.ingest(tuple(events))
    return store


def test_a_sufficient_sample_proposes_a_bounded_calibration_that_activates_nothing(
    tmp_path: Path,
) -> None:
    store = _labeled_store(tmp_path / "feedback.json", 40, 10)

    report = run_worthwhile_evaluation(store.batch_outcomes())
    proposed = report.proposed_calibration

    assert proposed.sufficient is True
    assert proposed.labeled_outcome_count == 40
    assert proposed.observed_worthwhile_rate == 0.25
    assert proposed.proposed_post_reading_value_prior == 0.25
    assert proposed.proposed_reading_prior is not None
    assert (
        DEFAULT_WORTHWHILE_POLICY.reading.floor
        <= proposed.proposed_reading_prior
        <= DEFAULT_WORTHWHILE_POLICY.reading.ceiling
    )
    assert report.eligible_for_activation is False
    assert "explicit operator decision under V030-M6" in " ".join(report.reasons)


def test_a_proposed_reading_prior_never_falls_below_the_declared_floor(tmp_path: Path) -> None:
    store = _labeled_store(tmp_path / "feedback.json", 40, 10, silent=360)

    report = run_worthwhile_evaluation(store.batch_outcomes())
    proposed = report.proposed_calibration

    assert proposed.observed_reading_rate == 0.1
    assert proposed.observed_reading_rate < DEFAULT_WORTHWHILE_POLICY.reading.floor
    assert proposed.proposed_reading_prior == DEFAULT_WORTHWHILE_POLICY.reading.floor


def test_evaluation_is_pure_and_repeatable(tmp_path: Path) -> None:
    store = _labeled_store(tmp_path / "feedback.json", 8, 3)
    batches = store.batch_outcomes()
    positions = store.position_outcomes()

    assert run_worthwhile_evaluation(batches, positions) == run_worthwhile_evaluation(
        batches, positions
    )


def test_written_report_is_owner_only_and_free_of_paper_identifiers(tmp_path: Path) -> None:
    store = _labeled_store(tmp_path / "feedback.json", 6, 2)
    report = run_worthwhile_evaluation(store.batch_outcomes(), store.position_outcomes())
    path = tmp_path / "runtime" / "worthwhile-report.json"

    write_worthwhile_report(report, path)

    payload = path.read_text(encoding="utf-8")
    assert path.stat().st_mode & 0o777 == 0o600
    assert json.loads(payload)["schema_version"] == WORTHWHILE_REPORT_SCHEMA_VERSION
    assert json.loads(payload)["policy_version"] == DEFAULT_WORTHWHILE_POLICY.version
    assert "2408.00000" not in payload


def test_partial_prediction_coverage_is_reported_as_not_comparable(tmp_path: Path) -> None:
    store = FeedbackLedgerStore(tmp_path / "feedback.json")
    store.record_impressions("batch-old", ("2408.00001",), _NOW)
    store.record_impressions("batch-new", ("2408.00002",), _NOW + timedelta(days=1))

    report = run_worthwhile_evaluation(
        store.batch_outcomes(), predictions={"batch-new": (_estimate("2408.00002", 0.3),)}
    )

    assert report.predicted_worthwhile_reads == pytest.approx(0.3)
    assert "predicted totals cover only some batches" in " ".join(report.warnings)
    assert "no batch prediction was supplied" not in " ".join(report.warnings)


def test_full_prediction_coverage_reports_no_comparability_warning(tmp_path: Path) -> None:
    store = FeedbackLedgerStore(tmp_path / "feedback.json")
    store.record_impressions("batch-a", ("2408.00001",), _NOW)
    store.record_impressions("batch-b", ("2408.00002",), _NOW + timedelta(days=1))

    report = run_worthwhile_evaluation(
        store.batch_outcomes(),
        predictions={
            "batch-a": (_estimate("2408.00001", 0.3),),
            "batch-b": (_estimate("2408.00002", 0.2),),
        },
    )

    assert report.predicted_worthwhile_reads == pytest.approx(0.5)
    assert "predicted totals cover only some batches" not in " ".join(report.warnings)
