from __future__ import annotations

from zotero_arxiv_daily.core.config import AppConfig
from zotero_arxiv_daily.core.errors import ExitCode
from zotero_arxiv_daily.doctor import CheckState, doctor_exit_code, run_doctor


class AvailableZotero:
    def probe(self, base_url: str) -> bool:
        return base_url == "http://127.0.0.1:23119"


def test_doctor_reports_dependencies_independently_without_secret_values() -> None:
    diagnostics = run_doctor(AppConfig(), zotero_probe=AvailableZotero())

    assert [item.state for item in diagnostics] == [
        CheckState.OK,
        CheckState.MISSING,
        CheckState.MISSING,
        CheckState.MISSING,
        CheckState.MISSING,
    ]
    assert doctor_exit_code(diagnostics) is ExitCode.CONFIGURATION
    assert "ZAD_DEEPSEEK_API_KEY" in diagnostics[1].detail


def test_doctor_accepts_explicit_public_output() -> None:
    diagnostics = run_doctor(
        AppConfig(
            deepseek_api_key="configured",
            github_repository="owner/repository",
            github_token="configured",
            public_output=True,
        ),
        check_zotero=False,
    )

    assert diagnostics[-1].state is CheckState.OK
    assert doctor_exit_code(diagnostics) is ExitCode.SUCCESS
