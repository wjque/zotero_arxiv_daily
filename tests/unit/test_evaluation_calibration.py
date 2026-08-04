from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path

from zotero_arxiv_daily.arxiv.models import ArxivCandidate, ArxivId
from zotero_arxiv_daily.evaluation.calibration import run_shadow_evaluation, write_shadow_report
from zotero_arxiv_daily.evaluation.corpus import CorpusStore
from zotero_arxiv_daily.evaluation.models import CorpusEvent, CorpusLabel, JudgmentKind
from zotero_arxiv_daily.evaluation.offline import make_evaluation_snapshot
from zotero_arxiv_daily.profile.models import RemoteProfile
from zotero_arxiv_daily.ranking.weights import DEFAULT_WEIGHT_SET

_NOW = datetime(2026, 8, 4, tzinfo=UTC)


def _candidate(identifier: str, title: str, category: str = "cs.LG") -> ArxivCandidate:
    return ArxivCandidate(
        ArxivId(identifier, 1),
        title,
        ("Author",),
        (category,),
        _NOW - timedelta(days=1),
        _NOW,
        f"https://arxiv.org/abs/{identifier}",
        f"https://arxiv.org/pdf/{identifier}",
        "A public synthetic learning abstract.",
    )


def _event(event_id: str, paper_id: str, label: CorpusLabel, days: int) -> CorpusEvent:
    return CorpusEvent(
        event_id,
        JudgmentKind.LABEL,
        paper_id,
        _NOW + timedelta(days=days),
        "synthetic",
        label,
    )


def test_shadow_evaluation_is_provisional_and_never_mutates_weight_state(tmp_path: Path) -> None:
    corpus_store = CorpusStore(tmp_path / "corpus.json")
    corpus_store.append(
        (
            _event("positive", "arxiv:2401.00001", CorpusLabel.POSITIVE, -2),
            _event("negative", "arxiv:2401.00002", CorpusLabel.NEGATIVE, -1),
        )
    )
    snapshot = make_evaluation_snapshot(corpus_store.snapshot(_NOW), created_at=_NOW)
    profile = RemoteProfile(4, 1, ("learning",), ("cs.LG",), (), ())
    report = run_shadow_evaluation(
        (
            _candidate("2401.00001", "Learning alpha"),
            _candidate("2401.00002", "Unrelated delta", "math.OC"),
        ),
        profile,
        snapshot,
        _NOW,
        weight_set=DEFAULT_WEIGHT_SET,
    )
    output = tmp_path / "shadow.json"
    write_shadow_report(report, output)

    assert report.snapshot_id == snapshot.snapshot_id
    assert report.eligible_for_activation
    assert not report.comparison.eligible_for_tuning
    assert "sparse independent-label sample; metric uncertainty is high" in report.warnings
    assert {item.group.value for item in report.ablations} == {
        "interest",
        "recency",
        "feedback",
        "identity",
        "scientific_quality",
        "reproducibility",
        "context",
    }
    assert output.is_file()
    assert output.stat().st_mode & 0o077 == 0
