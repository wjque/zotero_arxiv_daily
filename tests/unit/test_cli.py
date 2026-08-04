from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path

from pytest import CaptureFixture, MonkeyPatch

from zotero_arxiv_daily import cli
from zotero_arxiv_daily.core.config import AppConfig
from zotero_arxiv_daily.evidence.models import PublicPaperEvidence
from zotero_arxiv_daily.feedback.ledger import ActivationResult
from zotero_arxiv_daily.pipeline.recommend import package_result
from zotero_arxiv_daily.profile.models import RemoteProfile
from zotero_arxiv_daily.site.models import PublishedRecommendationSet, write_published_set
from zotero_arxiv_daily.zotero.models import ZoteroCollection


def test_doctor_command_returns_configuration_exit_code_without_secret_output(
    monkeypatch: MonkeyPatch, capsys: CaptureFixture[str]
) -> None:
    monkeypatch.setattr(cli, "load_config", lambda **_: AppConfig())

    exit_code = cli.main(["doctor", "--skip-zotero-check", "--format", "json"])

    captured = capsys.readouterr()
    assert exit_code == 2
    assert "ZAD_DEEPSEEK_API_KEY" in captured.out
    assert "configured" not in captured.out


def test_site_build_command_uses_protected_output_by_default(
    monkeypatch: MonkeyPatch, capsys: CaptureFixture[str], tmp_path: Path
) -> None:
    input_path = tmp_path / "recommendations.json"
    output_path = tmp_path / "site"
    write_published_set(PublishedRecommendationSet(1, "2026-08-01T00:00:00+00:00", ()), input_path)
    monkeypatch.setattr(
        cli,
        "load_config",
        lambda **_: AppConfig(pages_passphrase="a sufficiently long test passphrase"),
    )

    exit_code = cli.main(
        ["site", "build", "--input", str(input_path), "--output", str(output_path)]
    )

    assert exit_code == 0
    assert "encrypted site built" in capsys.readouterr().out
    assert (output_path / "data/recommendations.enc.json").is_file()


def test_recommend_command_records_candidate_pool_degradation_in_manifest(
    monkeypatch: MonkeyPatch, tmp_path: Path
) -> None:
    profile = RemoteProfile(1, 9, (), (), (), ())
    source_checkpoint = datetime(2026, 8, 1, 12, 0, tzinfo=UTC)

    class _CandidateStore:
        def __init__(self, path: Path) -> None:
            self.path = path

        def candidates(self) -> tuple[object, ...]:
            return ()

        def retrieval_status(self) -> tuple[bool, str, datetime]:
            return True, "arXiv timeout", source_checkpoint

    class _History:
        def __init__(self, path: Path) -> None:
            self.path = path

        def excluded_ids(self, now: object, suppression_days: int) -> frozenset[str]:
            return frozenset()

        def prepare_success(self, result: object, path: Path, completed_at: object) -> None:
            return None

    def _run(*args: object, **kwargs: object) -> tuple[object, object]:
        return package_result(
            (),
            profile,
            datetime(2026, 8, 2, tzinfo=UTC),
            model="test",
            candidate_count=0,
            model_requests=0,
            cache_hits=0,
            estimated_tokens=0,
        )

    monkeypatch.setattr(cli, "load_config", lambda **_: AppConfig(deepseek_api_key="key"))
    monkeypatch.setattr(cli, "read_remote_profile", lambda path: profile)
    monkeypatch.setattr(cli, "ArxivStateStore", _CandidateStore)
    monkeypatch.setattr(cli, "RecommendationHistoryStore", _History)
    monkeypatch.setattr(cli, "run_recommendation", _run)
    manifest_path = tmp_path / "run-manifest.json"

    assert (
        cli.main(
            [
                "recommend",
                "run",
                "--profile",
                str(tmp_path / "profile.json"),
                "--candidate-state",
                str(tmp_path / "arxiv-state.json"),
                "--output",
                str(tmp_path / "recommendations.json"),
                "--history",
                str(tmp_path / "history.json"),
                "--prepared-history",
                str(tmp_path / "history.next.json"),
                "--manifest",
                str(manifest_path),
                "--weight-state",
                str(tmp_path / "weights.json"),
            ]
        )
        == 0
    )

    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    assert manifest["candidate_pool_degraded"] is True
    assert manifest["candidate_pool_degraded_reason"] == "arXiv timeout"
    assert manifest["candidate_pool_source_checkpoint"] == str(source_checkpoint)


def test_corpus_list_collections_command_exposes_only_local_mapping_fields(
    monkeypatch: MonkeyPatch, capsys: CaptureFixture[str]
) -> None:
    class _Store:
        def __init__(self, path: Path) -> None:
            self.path = path

        def collections(self) -> tuple[ZoteroCollection, ...]:
            return (ZoteroCollection("POSITIVE", 1, "Curated positives", None),)

    monkeypatch.setattr(cli, "load_config", lambda **_: AppConfig())
    monkeypatch.setattr(cli, "ZoteroStore", _Store)

    exit_code = cli.main(["corpus", "list-collections"])

    assert exit_code == 0
    assert capsys.readouterr().out == "POSITIVE\tCurated positives\n"


def test_feedback_activate_uses_configured_weekly_bounds(
    monkeypatch: MonkeyPatch, capsys: CaptureFixture[str], tmp_path: Path
) -> None:
    class _Ledger:
        def __init__(self, path: Path) -> None:
            self.path = path

        def activate_weekly(
            self, now: object, *, interval_days: int, minimum_independent_papers: int
        ) -> ActivationResult:
            assert interval_days == 7
            assert minimum_independent_papers == 3
            return ActivationResult("insufficient-evidence", None, None)

    monkeypatch.setattr(cli, "load_config", lambda **_: AppConfig())
    monkeypatch.setattr(cli, "FeedbackLedgerStore", _Ledger)

    assert cli.main(["feedback", "activate", "--state", str(tmp_path / "feedback.json")]) == 0
    assert capsys.readouterr().out == "feedback activation: insufficient-evidence\n"


def test_evidence_enrich_keeps_the_cli_projection_public_and_bounded(
    monkeypatch: MonkeyPatch, capsys: CaptureFixture[str], tmp_path: Path
) -> None:
    class _Enricher:
        def __init__(self, client: object, cache: object) -> None:
            self.client = client
            self.cache = cache

        def enrich(
            self, candidates: tuple[object, ...], now: object, *, limit: int
        ) -> tuple[PublicPaperEvidence, ...]:
            assert candidates == ()
            assert limit == 1
            return ()

    monkeypatch.setattr(cli, "load_config", lambda **_: AppConfig())
    monkeypatch.setattr(cli, "OpenAlexEvidenceEnricher", _Enricher)

    exit_code = cli.main(
        [
            "evidence",
            "enrich",
            "--candidate-state",
            str(tmp_path / "missing.json"),
            "--limit",
            "1",
        ]
    )

    assert exit_code == 0
    assert capsys.readouterr().out == "public evidence enriched: 0 candidates, 0 context records\n"


def test_weight_activation_requires_an_eligible_matching_shadow_report(
    monkeypatch: MonkeyPatch, capsys: CaptureFixture[str], tmp_path: Path
) -> None:
    state = tmp_path / "weights.json"
    report = tmp_path / "shadow.json"
    monkeypatch.setattr(cli, "load_config", lambda **_: AppConfig())
    register = [
        "ranking",
        "register-weights",
        "--state",
        str(state),
        "--version",
        "coarse-test",
        "--interest",
        "0.5",
        "--recency",
        "0.1",
        "--feedback",
        "0.1",
        "--identity",
        "0.1",
        "--scientific-quality",
        "0.05",
        "--reproducibility",
        "0.03",
        "--context",
        "0.02",
        "--negative-feedback-cap",
        "0.2",
    ]

    assert cli.main(register) == 0
    report.write_text(
        json.dumps({"weight_set_version": "coarse-test", "eligible_for_activation": False}),
        encoding="utf-8",
    )
    assert (
        cli.main(
            [
                "ranking",
                "activate-weights",
                "--state",
                str(state),
                "--version",
                "coarse-test",
                "--shadow-report",
                str(report),
            ]
        )
        == 4
    )
    report.write_text(
        json.dumps({"weight_set_version": "coarse-test", "eligible_for_activation": True}),
        encoding="utf-8",
    )

    assert (
        cli.main(
            [
                "ranking",
                "activate-weights",
                "--state",
                str(state),
                "--version",
                "coarse-test",
                "--shadow-report",
                str(report),
            ]
        )
        == 0
    )
    assert "ranking weight set activated: coarse-test" in capsys.readouterr().out
