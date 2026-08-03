from __future__ import annotations

from pathlib import Path

from pytest import CaptureFixture, MonkeyPatch

from zotero_arxiv_daily import cli
from zotero_arxiv_daily.core.config import AppConfig
from zotero_arxiv_daily.feedback.ledger import ActivationResult
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
