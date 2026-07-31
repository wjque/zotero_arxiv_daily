"""Offline-safe diagnostics for local and protected dependencies."""

from __future__ import annotations

from dataclasses import asdict, dataclass
from enum import StrEnum
from typing import Protocol, cast
from urllib.error import URLError
from urllib.request import Request, urlopen

from zotero_arxiv_daily.core.config import AppConfig
from zotero_arxiv_daily.core.errors import ExitCode


class CheckState(StrEnum):
    """Health states intentionally distinct from process exit codes."""

    OK = "ok"
    MISSING = "missing"
    UNAVAILABLE = "unavailable"
    NOT_CHECKED = "not_checked"


@dataclass(frozen=True, slots=True)
class Diagnostic:
    """One safe-to-display diagnostic result."""

    name: str
    state: CheckState
    detail: str

    def to_dict(self) -> dict[str, str]:
        return asdict(self)


class ZoteroProbe(Protocol):
    """Small interface that permits a deterministic local-network test."""

    def probe(self, base_url: str) -> bool:
        """Return whether Zotero Local API's connector endpoint responds."""


class HttpZoteroProbe:
    """Probe only Zotero's local connector endpoint with a short timeout."""

    def probe(self, base_url: str) -> bool:
        request = Request(f"{base_url.rstrip('/')}/connector/ping", method="GET")
        try:
            with urlopen(request, timeout=1.0) as response:  # noqa: S310 - validated local URL
                return 200 <= cast(int, response.getcode()) < 300
        except (OSError, URLError):
            return False


def run_doctor(
    config: AppConfig,
    *,
    check_zotero: bool = True,
    zotero_probe: ZoteroProbe | None = None,
) -> list[Diagnostic]:
    """Inspect configuration and optionally a local Zotero endpoint without secrets."""

    diagnostics = [_zotero_diagnostic(config, check_zotero, zotero_probe or HttpZoteroProbe())]
    diagnostics.extend(
        [
            _credential_diagnostic(
                "DeepSeek API key", config.deepseek_api_key, "ZAD_DEEPSEEK_API_KEY"
            ),
            _credential_diagnostic(
                "GitHub repository", config.github_repository, "ZAD_GITHUB_REPOSITORY"
            ),
            _credential_diagnostic("GitHub token", config.github_token, "ZAD_GITHUB_TOKEN"),
            _pages_diagnostic(config),
        ]
    )
    return diagnostics


def doctor_exit_code(diagnostics: list[Diagnostic]) -> ExitCode:
    """Map independent diagnostics to the documented process exit-code policy."""

    if any(item.state is CheckState.MISSING for item in diagnostics):
        return ExitCode.CONFIGURATION
    if any(item.state is CheckState.UNAVAILABLE for item in diagnostics):
        return ExitCode.DEPENDENCY_UNAVAILABLE
    return ExitCode.SUCCESS


def _zotero_diagnostic(
    config: AppConfig, check_zotero: bool, zotero_probe: ZoteroProbe
) -> Diagnostic:
    if not check_zotero:
        return Diagnostic("Zotero Local API", CheckState.NOT_CHECKED, "local probe skipped")
    if zotero_probe.probe(config.zotero_base_url):
        return Diagnostic("Zotero Local API", CheckState.OK, "connector endpoint responded")
    return Diagnostic(
        "Zotero Local API",
        CheckState.UNAVAILABLE,
        "connector endpoint did not respond; start Zotero and enable its Local API",
    )


def _credential_diagnostic(name: str, value: str | None, environment_key: str) -> Diagnostic:
    if value:
        return Diagnostic(name, CheckState.OK, "configured")
    return Diagnostic(name, CheckState.MISSING, f"set {environment_key} in the environment")


def _pages_diagnostic(config: AppConfig) -> Diagnostic:
    if config.public_output:
        return Diagnostic("Pages protection", CheckState.OK, "explicit public-output mode enabled")
    if config.pages_passphrase:
        return Diagnostic(
            "Pages protection", CheckState.OK, "encrypted output passphrase configured"
        )
    return Diagnostic(
        "Pages protection",
        CheckState.MISSING,
        "set ZAD_PAGES_PASSPHRASE or explicitly enable public output",
    )
