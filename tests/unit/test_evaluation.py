from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from zotero_arxiv_daily.core.errors import ApplicationError
from zotero_arxiv_daily.evaluation.corpus import (
    CorpusStore,
    CuratedCorpusMapping,
    ZoteroCorpusItem,
)
from zotero_arxiv_daily.evaluation.models import CorpusEvent, CorpusLabel, JudgmentKind, RankedPaper
from zotero_arxiv_daily.evaluation.offline import (
    EvaluationSnapshotStore,
    compare_rankings,
    evaluate_ranking,
    evaluate_snapshot_ranking,
    make_evaluation_snapshot,
)

_NOW = datetime(2026, 8, 3, 12, tzinfo=UTC)


def _event(
    event_id: str,
    paper_id: str,
    label: CorpusLabel | None,
    *,
    days: int = 0,
    kind: JudgmentKind = JudgmentKind.LABEL,
    compared_paper_id: str | None = None,
    supersedes: str | None = None,
) -> CorpusEvent:
    return CorpusEvent(
        event_id,
        kind,
        paper_id,
        _NOW + timedelta(days=days),
        "synthetic",
        label,
        compared_paper_id,
        ("strong-evidence",) if label else (),
        supersedes_event_id=supersedes,
    )


def test_corpus_ledger_preserves_corrections_and_explicit_unlabeling(tmp_path: Path) -> None:
    store = CorpusStore(tmp_path / "runtime" / "corpus.json")
    positive = _event("first", "arxiv:2401.00001", CorpusLabel.POSITIVE)
    correction = _event(
        "second", "arxiv:2401.00001", CorpusLabel.NEGATIVE, days=1, supersedes="first"
    )
    unlabel = _event(
        "third", "arxiv:2401.00001", None, days=2, kind=JudgmentKind.UNLABEL, supersedes="second"
    )

    assert store.append((positive,)) == (1, 0)
    assert store.append((positive,)) == (0, 1)
    assert store.append((correction, unlabel)) == (2, 0)
    assert store.snapshot(_NOW + timedelta(days=1)).labels[0].label is CorpusLabel.NEGATIVE
    assert store.snapshot(_NOW + timedelta(days=3)).labels == ()
    assert len(store.events()) == 3
    assert (tmp_path / "runtime" / "corpus.json").stat().st_mode & 0o077 == 0


def test_zotero_import_is_idempotent_and_collection_removal_unlabels(tmp_path: Path) -> None:
    store = CorpusStore(tmp_path / "corpus.json")
    mapping = CuratedCorpusMapping(("POSITIVE",), ("NEGATIVE",))
    positive = ZoteroCorpusItem(
        "ITEM1", ("arxiv:2401.00001",), ("POSITIVE",), ("ranking-reason:novel-insight",)
    )

    first = store.import_zotero((positive,), mapping, _NOW)
    repeated = store.import_zotero((positive,), mapping, _NOW + timedelta(days=1))
    negative = store.import_zotero(
        (ZoteroCorpusItem("ITEM1", positive.identifiers, ("NEGATIVE",), positive.tags),),
        mapping,
        _NOW + timedelta(days=2),
    )
    removed = store.import_zotero((), mapping, _NOW + timedelta(days=3))

    assert (first.added_events, repeated.added_events, negative.added_events) == (1, 0, 1)
    assert removed.unlabeled_events == 1
    assert store.snapshot(_NOW + timedelta(days=4)).labels == ()
    assert [event.kind for event in store.events()] == [
        JudgmentKind.LABEL,
        JudgmentKind.LABEL,
        JudgmentKind.UNLABEL,
    ]


def test_zotero_import_accepts_normalized_doi_and_explicit_arxiv_identity_tag(
    tmp_path: Path,
) -> None:
    store = CorpusStore(tmp_path / "corpus.json")
    mapping = CuratedCorpusMapping(("POSITIVE",), ("NEGATIVE",))

    result = store.import_zotero(
        (
            ZoteroCorpusItem("DOI", ("10.1000/example",), ("POSITIVE",), ()),
            ZoteroCorpusItem(
                "ARXIV",
                (),
                ("NEGATIVE",),
                ("ranking-paper-id:arxiv:2401.00001",),
            ),
        ),
        mapping,
        _NOW,
    )

    assert result.added_events == 2
    assert [label.paper_id for label in store.snapshot(_NOW).labels] == [
        "arxiv:2401.00001",
        "doi:10.1000/example",
    ]


def test_corpus_rejects_invalid_correction_lineage(tmp_path: Path) -> None:
    store = CorpusStore(tmp_path / "corpus.json")

    with pytest.raises(ApplicationError, match="supersede"):
        store.append(
            (_event("correction", "arxiv:2401.00001", CorpusLabel.POSITIVE, supersedes="nope"),)
        )


def test_evaluation_snapshot_freezes_temporal_anchor_rolling_and_pairwise_splits(
    tmp_path: Path,
) -> None:
    store = CorpusStore(tmp_path / "corpus.json")
    store.append(
        (
            _event("p1", "arxiv:1", CorpusLabel.POSITIVE, days=-20),
            _event("p2", "arxiv:2", CorpusLabel.NEGATIVE, days=-10),
            _event("p3", "arxiv:3", CorpusLabel.POSITIVE, days=-2),
            _event(
                "pair",
                "arxiv:3",
                CorpusLabel.POSITIVE,
                kind=JudgmentKind.PAIRWISE,
                compared_paper_id="arxiv:2",
            ),
        )
    )
    corpus = store.snapshot(_NOW)
    snapshot = make_evaluation_snapshot(
        corpus, created_at=_NOW, anchor_paper_ids=("arxiv:1",), rolling_days=7
    )
    paths = EvaluationSnapshotStore(tmp_path / "snapshots")

    assert {split.name: split.paper_ids for split in snapshot.splits} == {
        "stable-anchor": ("arxiv:1",),
        "rolling": ("arxiv:3",),
        "temporal-holdout": ("arxiv:3",),
        "temporal-train": ("arxiv:1", "arxiv:2"),
        "pairwise": ("arxiv:2", "arxiv:3"),
    }
    assert paths.write(snapshot) == paths.write(snapshot)
    assert (tmp_path / "snapshots" / f"{snapshot.snapshot_id}.json").exists()
    assert paths.read(snapshot.snapshot_id) == snapshot
    assert snapshot.labels == (
        ("arxiv:1", CorpusLabel.POSITIVE),
        ("arxiv:2", CorpusLabel.NEGATIVE),
        ("arxiv:3", CorpusLabel.POSITIVE),
    )
    assert snapshot.pairwise_preferences == (("arxiv:3", "arxiv:2"),)


def test_metrics_report_core_values_pairwise_accuracy_and_conservative_comparison(
    tmp_path: Path,
) -> None:
    store = CorpusStore(tmp_path / "corpus.json")
    store.append(
        (
            _event("p1", "arxiv:1", CorpusLabel.POSITIVE),
            _event("p2", "arxiv:2", CorpusLabel.NEGATIVE),
            _event("p3", "arxiv:3", CorpusLabel.POSITIVE),
            _event(
                "pair",
                "arxiv:1",
                CorpusLabel.POSITIVE,
                kind=JudgmentKind.PAIRWISE,
                compared_paper_id="arxiv:2",
            ),
        )
    )
    corpus = store.snapshot(_NOW)
    snapshot = make_evaluation_snapshot(corpus, created_at=_NOW)
    candidate = evaluate_ranking(
        (
            RankedPaper("arxiv:1", 0.9, "core", "cs.LG", ("learning",)),
            RankedPaper("arxiv:2", 0.8, "core", "cs.LG", ("learning",)),
            RankedPaper("arxiv:3", 0.1, "adjacent", "cs.AI", ("reasoning",)),
        ),
        corpus,
        ("arxiv:1", "arxiv:2", "arxiv:3"),
        k=2,
    )
    baseline = evaluate_ranking(
        (
            RankedPaper("arxiv:2", 0.9),
            RankedPaper("arxiv:1", 0.8),
            RankedPaper("arxiv:3", 0.1),
        ),
        corpus,
        ("arxiv:1", "arxiv:2", "arxiv:3"),
        k=2,
    )
    report = compare_rankings(
        baseline_name="v0.1.2",
        candidate_name="candidate",
        snapshot=snapshot,
        baseline=baseline,
        candidate=candidate,
    )

    assert candidate.recall_at_k == pytest.approx(0.5)
    assert candidate.precision_at_k == pytest.approx(0.5)
    assert candidate.negative_rate_at_k == pytest.approx(0.5)
    assert candidate.pairwise_accuracy == 1.0
    assert candidate.candidate_overlap == 3
    assert candidate.source_coverage_at_k == 1
    assert candidate.category_coverage_at_k == 1
    assert candidate.provisional
    assert report.ndcg_delta is not None and report.ndcg_delta > 0
    assert not report.eligible_for_tuning
    assert "few overlapping labels; metric uncertainty is high" in report.warnings
    frozen_metrics = evaluate_snapshot_ranking(
        (
            RankedPaper("arxiv:1", 0.9),
            RankedPaper("arxiv:2", 0.8),
            RankedPaper("arxiv:3", 0.1),
        ),
        snapshot,
        "temporal-holdout",
        k=2,
    )
    assert frozen_metrics.evaluated_labels == 1
    assert frozen_metrics.candidate_overlap == 1


def test_metrics_are_explicitly_insufficient_without_labels(tmp_path: Path) -> None:
    corpus = CorpusStore(tmp_path / "corpus.json").snapshot(_NOW)
    metrics = evaluate_ranking((RankedPaper("arxiv:1", 1.0),), corpus, ("arxiv:1",))

    assert metrics.recall_at_k is None
    assert metrics.insufficiency_reason == "no eligible labels or pairwise judgments"


def test_metrics_report_zero_candidate_overlap_separately_from_label_count(
    tmp_path: Path,
) -> None:
    store = CorpusStore(tmp_path / "corpus.json")
    store.append(
        (
            _event("positive", "arxiv:1", CorpusLabel.POSITIVE),
            _event("negative", "arxiv:2", CorpusLabel.NEGATIVE),
            _event("positive-2", "arxiv:3", CorpusLabel.POSITIVE),
            _event("negative-2", "arxiv:4", CorpusLabel.NEGATIVE),
            _event("positive-3", "arxiv:5", CorpusLabel.POSITIVE),
        )
    )
    corpus = store.snapshot(_NOW)

    metrics = evaluate_ranking((), corpus, tuple(f"arxiv:{index}" for index in range(1, 6)))

    assert metrics.evaluated_labels == 5
    assert metrics.candidate_overlap == 0
    assert metrics.provisional


def test_metrics_match_an_exact_doi_alias_without_fuzzy_identity(tmp_path: Path) -> None:
    store = CorpusStore(tmp_path / "corpus.json")
    store.append((_event("positive", "doi:10.1000/example", CorpusLabel.POSITIVE),))
    corpus = store.snapshot(_NOW)

    metrics = evaluate_ranking(
        (
            RankedPaper(
                "arxiv:2401.00001",
                0.9,
                identifiers=("doi:10.1000/example",),
            ),
        ),
        corpus,
        ("doi:10.1000/example",),
    )

    assert metrics.candidate_overlap == 1
    assert metrics.recall_at_k == 1.0
    assert metrics.precision_at_k == 1.0
