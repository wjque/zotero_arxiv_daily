from __future__ import annotations

from pytest import CaptureFixture, MonkeyPatch

from zotero_arxiv_daily import cli
from zotero_arxiv_daily.core.config import AppConfig


def test_doctor_command_returns_configuration_exit_code_without_secret_output(
    monkeypatch: MonkeyPatch, capsys: CaptureFixture[str]
) -> None:
    monkeypatch.setattr(cli, "load_config", lambda **_: AppConfig())

    exit_code = cli.main(["doctor", "--skip-zotero-check", "--format", "json"])

    captured = capsys.readouterr()
    assert exit_code == 2
    assert "ZAD_DEEPSEEK_API_KEY" in captured.out
    assert "configured" not in captured.out
