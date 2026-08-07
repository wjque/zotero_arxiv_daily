"""Versioned, explicitly approved quality-reference profiles in protected local state."""

from __future__ import annotations

import hashlib
import json
import os
import tempfile
from collections import defaultdict
from dataclasses import asdict, dataclass
from pathlib import Path

from zotero_arxiv_daily.core.errors import ApplicationError
from zotero_arxiv_daily.feedback.ledger import (
    FeedbackEvent,
    FeedbackEventType,
    FeedbackOutcome,
)

QUALITY_PROFILE_SCHEMA_VERSION = 1
_TRAIT_ALLOWLISTS = {
    "research_problems": frozenset(
        {
            "agents",
            "evaluation",
            "generative_modeling",
            "optimization",
            "representation_learning",
            "retrieval",
            "systems",
        }
    ),
    "methodological_expectations": frozenset(
        {
            "ablations",
            "baselines",
            "resource_accounting",
            "robustness",
            "statistical_validation",
            "theoretical_justification",
        }
    ),
    "evidence_standards": frozenset(
        {
            "error_analysis",
            "held_out_evaluation",
            "public_data",
            "reproducible_implementation",
            "uncertainty_reporting",
        }
    ),
    "motivations": frozenset(
        {"efficiency", "explanatory_insight", "practical_utility", "scientific_novelty"}
    ),
    "tolerated_limitations": frozenset(
        {"limited_scale", "missing_code", "narrow_domain", "preliminary_theory", "synthetic_data"}
    ),
}
_POSITIVE = frozenset(
    {FeedbackOutcome.INTERESTED, FeedbackOutcome.READ, FeedbackOutcome.WORTHWHILE}
)
_NEGATIVE = frozenset({FeedbackOutcome.NOT_INTERESTED, FeedbackOutcome.NOT_WORTHWHILE})


@dataclass(frozen=True, slots=True)
class ApprovedQualityExample:
    paper_id: str
    approved: bool
    research_problems: tuple[str, ...] = ()
    methodological_expectations: tuple[str, ...] = ()
    evidence_standards: tuple[str, ...] = ()
    motivations: tuple[str, ...] = ()
    tolerated_limitations: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        if not self.paper_id.strip() or len(self.paper_id) > 80:
            raise ValueError("quality example paper ID is invalid")
        for field_name, allowed in _TRAIT_ALLOWLISTS.items():
            values = getattr(self, field_name)
            if len(values) > 12 or len(set(values)) != len(values) or not set(values) <= allowed:
                raise ValueError(f"quality example {field_name} is invalid")


@dataclass(frozen=True, slots=True)
class QualityCriterion:
    name: str
    support: float

    def __post_init__(self) -> None:
        if not self.name.strip() or not 0 <= self.support <= 1:
            raise ValueError("quality criterion is invalid")


@dataclass(frozen=True, slots=True)
class QualityReferenceProfile:
    version: str
    schema_version: int
    research_problems: tuple[QualityCriterion, ...]
    methodological_expectations: tuple[QualityCriterion, ...]
    evidence_standards: tuple[QualityCriterion, ...]
    motivations: tuple[QualityCriterion, ...]
    tolerated_limitations: tuple[QualityCriterion, ...]
    approved_example_count: int
    explicit_feedback_event_count: int

    def __post_init__(self) -> None:
        if self.schema_version != QUALITY_PROFILE_SCHEMA_VERSION or not self.version.strip():
            raise ValueError("quality reference profile identity is invalid")
        if min(self.approved_example_count, self.explicit_feedback_event_count) < 0:
            raise ValueError("quality reference profile counts are invalid")
        for field_name, allowed in _TRAIT_ALLOWLISTS.items():
            criteria = getattr(self, field_name)
            if (
                len({item.name for item in criteria}) != len(criteria)
                or not {item.name for item in criteria} <= allowed
            ):
                raise ValueError(f"quality reference profile {field_name} is invalid")

    @property
    def criterion_count(self) -> int:
        return sum(len(getattr(self, field)) for field in _TRAIT_ALLOWLISTS)

    def prompt_payload(self) -> dict[str, object]:
        """Return aggregates without paper IDs or feedback events crossing the boundary."""

        return {
            "version": self.version,
            **{
                field: [
                    {"name": item.name, "support": item.support} for item in getattr(self, field)
                ]
                for field in _TRAIT_ALLOWLISTS
            },
        }


def build_quality_reference_profile(
    examples: tuple[ApprovedQualityExample, ...], feedback: tuple[FeedbackEvent, ...]
) -> QualityReferenceProfile:
    """Build a deterministic candidate profile from approvals and explicit outcomes only."""

    approved = tuple(example for example in examples if example.approved)
    if not approved:
        raise ApplicationError("quality profile requires at least one explicitly approved example")
    by_paper = {example.paper_id: example for example in approved}
    if len(by_paper) != len(approved):
        raise ApplicationError("quality profile examples contain duplicate approved paper IDs")
    superseded = {
        event.supersedes_event_id for event in feedback if event.supersedes_event_id is not None
    }
    explicit = tuple(
        event
        for event in feedback
        if event.paper_id in by_paper
        and event.event_id not in superseded
        and event.event_type in {FeedbackEventType.OUTCOME, FeedbackEventType.CORRECTION}
        and event.outcome is not None
    )
    weights = {paper_id: 1.0 for paper_id in by_paper}
    for event in explicit:
        if event.outcome in _POSITIVE:
            weights[event.paper_id] += 1.0
        elif event.outcome in _NEGATIVE:
            weights[event.paper_id] = max(0.25, weights[event.paper_id] - 0.5)
    fields: dict[str, tuple[QualityCriterion, ...]] = {}
    for field_name in _TRAIT_ALLOWLISTS:
        counts: defaultdict[str, float] = defaultdict(float)
        for paper_id, example in by_paper.items():
            for trait in getattr(example, field_name):
                counts[trait] += weights[paper_id]
        maximum = max(counts.values(), default=1.0)
        fields[field_name] = tuple(
            QualityCriterion(name, round(count / maximum, 6))
            for name, count in sorted(counts.items(), key=lambda item: (-item[1], item[0]))
        )
    identity = {
        "schema_version": QUALITY_PROFILE_SCHEMA_VERSION,
        "fields": {field: [asdict(item) for item in values] for field, values in fields.items()},
        "approved_example_count": len(approved),
        "explicit_feedback_event_count": len(explicit),
    }
    digest = hashlib.sha256(
        json.dumps(identity, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()[:16]
    return QualityReferenceProfile(
        f"quality-profile-{digest}",
        QUALITY_PROFILE_SCHEMA_VERSION,
        fields["research_problems"],
        fields["methodological_expectations"],
        fields["evidence_standards"],
        fields["motivations"],
        fields["tolerated_limitations"],
        len(approved),
        len(explicit),
    )


class QualityProfileStore:
    """Keep immutable generated versions and a separately reversible approval pointer."""

    def __init__(self, path: Path) -> None:
        self.path = path

    def register(self, profile: QualityReferenceProfile) -> bool:
        profiles, approved = self._read()
        existing = {item.version: item for item in profiles}.get(profile.version)
        if existing is not None:
            if existing != profile:
                raise ApplicationError("quality profile version is immutable")
            return False
        self._write((*profiles, profile), approved)
        return True

    def approve(self, version: str) -> QualityReferenceProfile:
        profiles, _ = self._read()
        profile = next((item for item in profiles if item.version == version), None)
        if profile is None:
            raise ApplicationError("quality profile version is not registered")
        self._write(profiles, version)
        return profile

    def rollback(self, version: str) -> QualityReferenceProfile:
        return self.approve(version)

    def approved(self) -> QualityReferenceProfile | None:
        profiles, approved = self._read()
        return next((item for item in profiles if item.version == approved), None)

    def versions(self) -> tuple[QualityReferenceProfile, ...]:
        return self._read()[0]

    def _read(self) -> tuple[tuple[QualityReferenceProfile, ...], str | None]:
        if not self.path.exists():
            return (), None
        try:
            value = json.loads(self.path.read_text(encoding="utf-8"))
            if not isinstance(value, dict) or set(value) != {
                "schema_version",
                "approved_version",
                "profiles",
            }:
                raise ValueError
            if value["schema_version"] != QUALITY_PROFILE_SCHEMA_VERSION:
                raise ValueError
            raw = value["profiles"]
            if not isinstance(raw, list):
                raise ValueError
            profiles = tuple(_profile(item) for item in raw)
            if len({item.version for item in profiles}) != len(profiles):
                raise ValueError
            approved = value["approved_version"]
            if approved is not None and (
                not isinstance(approved, str) or approved not in {item.version for item in profiles}
            ):
                raise ValueError
            return profiles, approved
        except (OSError, TypeError, ValueError, json.JSONDecodeError) as error:
            raise ApplicationError("quality profile state is invalid") from error

    def _write(self, profiles: tuple[QualityReferenceProfile, ...], approved: str | None) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        descriptor, temporary_name = tempfile.mkstemp(
            prefix=f".{self.path.name}.", dir=self.path.parent
        )
        temporary = Path(temporary_name)
        try:
            with os.fdopen(descriptor, "w", encoding="utf-8") as output:
                json.dump(
                    {
                        "schema_version": QUALITY_PROFILE_SCHEMA_VERSION,
                        "approved_version": approved,
                        "profiles": [_profile_payload(profile) for profile in profiles],
                    },
                    output,
                    sort_keys=True,
                    separators=(",", ":"),
                )
                output.flush()
                os.fsync(output.fileno())
            os.replace(temporary, self.path)
            os.chmod(self.path, 0o600)
        finally:
            temporary.unlink(missing_ok=True)


def read_quality_examples(path: Path) -> tuple[ApprovedQualityExample, ...]:
    """Read an exact allowlisted operator-reviewed input contract."""

    try:
        value = json.loads(path.read_text(encoding="utf-8"))
        if not isinstance(value, dict) or set(value) != {"schema_version", "examples"}:
            raise ValueError
        if value["schema_version"] != 1 or not isinstance(value["examples"], list):
            raise ValueError
        return tuple(_example(item) for item in value["examples"])
    except (OSError, TypeError, ValueError, json.JSONDecodeError) as error:
        raise ApplicationError("quality profile examples are invalid") from error


def _example(value: object) -> ApprovedQualityExample:
    fields = {"paper_id", "approved", *_TRAIT_ALLOWLISTS}
    if (
        not isinstance(value, dict)
        or set(value) != fields
        or not isinstance(value["approved"], bool)
    ):
        raise ValueError
    return ApprovedQualityExample(
        str(value["paper_id"]),
        bool(value["approved"]),
        *(tuple(str(item) for item in value[field]) for field in _TRAIT_ALLOWLISTS),
    )


def _profile_payload(profile: QualityReferenceProfile) -> dict[str, object]:
    return {
        "version": profile.version,
        "schema_version": profile.schema_version,
        **{
            field: [asdict(item) for item in getattr(profile, field)] for field in _TRAIT_ALLOWLISTS
        },
        "approved_example_count": profile.approved_example_count,
        "explicit_feedback_event_count": profile.explicit_feedback_event_count,
    }


def _profile(value: object) -> QualityReferenceProfile:
    fields = {
        "version",
        "schema_version",
        *_TRAIT_ALLOWLISTS,
        "approved_example_count",
        "explicit_feedback_event_count",
    }
    if not isinstance(value, dict) or set(value) != fields:
        raise ValueError
    criteria: dict[str, tuple[QualityCriterion, ...]] = {}
    for field in _TRAIT_ALLOWLISTS:
        raw = value[field]
        if not isinstance(raw, list):
            raise ValueError
        criteria[field] = tuple(
            QualityCriterion(str(item["name"]), float(item["support"]))
            for item in raw
            if isinstance(item, dict) and set(item) == {"name", "support"}
        )
        if len(criteria[field]) != len(raw):
            raise ValueError
    return QualityReferenceProfile(
        str(value["version"]),
        int(value["schema_version"]),
        criteria["research_problems"],
        criteria["methodological_expectations"],
        criteria["evidence_standards"],
        criteria["motivations"],
        criteria["tolerated_limitations"],
        int(value["approved_example_count"]),
        int(value["explicit_feedback_event_count"]),
    )
