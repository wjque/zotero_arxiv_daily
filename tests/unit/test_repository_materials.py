from __future__ import annotations

import json

from zotero_arxiv_daily.core.errors import ExternalServiceError
from zotero_arxiv_daily.evidence.project_page import ProjectPageEvidence
from zotero_arxiv_daily.evidence.repository_materials import (
    MaterialGrade,
    RepositoryMaterialsClient,
)


class _Transport:
    def __init__(self, entries: list[dict[str, str]] | Exception) -> None:
        self.entries = entries
        self.urls: list[str] = []

    def get(self, url: str, timeout_seconds: float) -> bytes:
        self.urls.append(url)
        if isinstance(self.entries, Exception):
            raise self.entries
        return json.dumps(self.entries).encode()


def _inspect(names: tuple[str, ...]) -> MaterialGrade:
    transport = _Transport([{"name": name} for name in names])
    value = RepositoryMaterialsClient(transport).inspect(
        ProjectPageEvidence("https://github.com/example/project", True)
    )
    assert transport.urls == ["https://api.github.com/repos/example/project/contents"]
    return value.grade


def test_repository_structure_distinguishes_documentation_and_material_grades() -> None:
    assert _inspect(("README.md", "LICENSE")) is MaterialGrade.DOCUMENTATION_ONLY
    assert _inspect(("README.md", "src")) is MaterialGrade.IMPLEMENTATION
    assert _inspect(("model.py", "tests")) is MaterialGrade.IMPLEMENTATION_AND_EVALUATION
    assert (
        _inspect(("src", "benchmark", "datasets"))
        is MaterialGrade.IMPLEMENTATION_DATA_AND_EVALUATION
    )


def test_repository_inspection_requires_an_explicit_reachable_github_link() -> None:
    transport = _Transport([{"name": "src"}])
    client = RepositoryMaterialsClient(transport)

    assert (
        client.inspect(ProjectPageEvidence("https://example.github.io/project", True)).grade
        is MaterialGrade.UNKNOWN
    )
    assert (
        client.inspect(ProjectPageEvidence("https://github.com/example/project", None)).grade
        is MaterialGrade.UNKNOWN
    )
    assert transport.urls == []


def test_repository_failure_and_malformed_structure_remain_unknown() -> None:
    unavailable = RepositoryMaterialsClient(_Transport(ExternalServiceError("timeout"))).inspect(
        ProjectPageEvidence("https://github.com/example/project", True)
    )
    malformed = RepositoryMaterialsClient(_Transport([{"path": "src"}])).inspect(
        ProjectPageEvidence("https://github.com/example/project", True)
    )

    assert unavailable.grade is MaterialGrade.UNKNOWN
    assert malformed.grade is MaterialGrade.UNKNOWN
