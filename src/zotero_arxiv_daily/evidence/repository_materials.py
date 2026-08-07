"""Graded public implementation-material evidence without fetching or executing code."""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from enum import StrEnum
from typing import Protocol
from urllib.parse import quote, urlsplit

from zotero_arxiv_daily.core.errors import ExternalServiceError
from zotero_arxiv_daily.evidence.project_page import ProjectPageEvidence

_NAME = re.compile(r"^[A-Za-z0-9_. -]{1,160}$")
_CODE_SUFFIXES = frozenset({".py", ".ipynb", ".cpp", ".c", ".rs", ".go", ".java", ".jl", ".m"})
_IMPLEMENTATION_DIRECTORIES = frozenset({"src", "source", "code", "implementation", "models"})
_EVALUATION_TERMS = ("test", "eval", "experiment", "benchmark")
_DATA_TERMS = ("data", "dataset", "results", "metrics")
_MAX_ENTRIES = 200


class MaterialGrade(StrEnum):
    UNKNOWN = "unknown"
    DOCUMENTATION_ONLY = "documentation_only"
    IMPLEMENTATION = "implementation"
    IMPLEMENTATION_AND_EVALUATION = "implementation_and_evaluation"
    IMPLEMENTATION_DATA_AND_EVALUATION = "implementation_data_and_evaluation"


@dataclass(frozen=True, slots=True)
class RepositoryMaterials:
    repository_url: str | None
    grade: MaterialGrade
    provenance: str

    @property
    def score(self) -> float:
        return {
            MaterialGrade.UNKNOWN: 0.0,
            MaterialGrade.DOCUMENTATION_ONLY: 0.2,
            MaterialGrade.IMPLEMENTATION: 0.6,
            MaterialGrade.IMPLEMENTATION_AND_EVALUATION: 0.8,
            MaterialGrade.IMPLEMENTATION_DATA_AND_EVALUATION: 1.0,
        }[self.grade]

    @property
    def available(self) -> bool:
        return self.grade is not MaterialGrade.UNKNOWN


class RepositoryStructureTransport(Protocol):
    def get(self, url: str, timeout_seconds: float) -> bytes: ...


@dataclass(slots=True)
class RepositoryMaterialsClient:
    transport: RepositoryStructureTransport
    timeout_seconds: float = 10.0

    def __post_init__(self) -> None:
        if not 1 <= self.timeout_seconds <= 20:
            raise ValueError("repository material timeout must be between 1 and 20 seconds")

    def inspect(self, project: ProjectPageEvidence) -> RepositoryMaterials:
        repository = _github_repository(project.url)
        if project.reachable is not True or repository is None:
            return RepositoryMaterials(project.url, MaterialGrade.UNKNOWN, "linked-project-page")
        owner, name, normalized = repository
        endpoint = f"https://api.github.com/repos/{quote(owner)}/{quote(name)}/contents"
        try:
            payload = self.transport.get(endpoint, self.timeout_seconds)
            value = json.loads(payload)
        except (ExternalServiceError, json.JSONDecodeError, UnicodeError):
            return RepositoryMaterials(normalized, MaterialGrade.UNKNOWN, "github-contents-v1")
        if not isinstance(value, list) or len(value) > _MAX_ENTRIES:
            return RepositoryMaterials(normalized, MaterialGrade.UNKNOWN, "github-contents-v1")
        names: list[str] = []
        for entry in value:
            if not isinstance(entry, dict) or not isinstance(entry.get("name"), str):
                return RepositoryMaterials(normalized, MaterialGrade.UNKNOWN, "github-contents-v1")
            item = str(entry["name"])
            if not _NAME.fullmatch(item):
                return RepositoryMaterials(normalized, MaterialGrade.UNKNOWN, "github-contents-v1")
            names.append(item.casefold())
        return RepositoryMaterials(normalized, _grade(tuple(names)), "github-contents-v1")


def _github_repository(url: str | None) -> tuple[str, str, str] | None:
    if url is None:
        return None
    parsed = urlsplit(url)
    parts = [part for part in parsed.path.split("/") if part]
    if parsed.scheme != "https" or parsed.hostname != "github.com" or len(parts) != 2:
        return None
    owner, name = parts
    if name.endswith(".git"):
        name = name[:-4]
    if not owner or not name:
        return None
    return owner, name, f"https://github.com/{owner}/{name}"


def _grade(names: tuple[str, ...]) -> MaterialGrade:
    implementation = any(
        name in _IMPLEMENTATION_DIRECTORIES
        or any(name.endswith(suffix) for suffix in _CODE_SUFFIXES)
        for name in names
    )
    evaluation = any(any(term in name for term in _EVALUATION_TERMS) for name in names)
    data = any(any(term in name for term in _DATA_TERMS) for name in names)
    if implementation and evaluation and data:
        return MaterialGrade.IMPLEMENTATION_DATA_AND_EVALUATION
    if implementation and evaluation:
        return MaterialGrade.IMPLEMENTATION_AND_EVALUATION
    if implementation:
        return MaterialGrade.IMPLEMENTATION
    return MaterialGrade.DOCUMENTATION_ONLY
