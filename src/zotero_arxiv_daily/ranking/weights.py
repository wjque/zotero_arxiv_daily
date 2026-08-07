"""Versioned normalized coarse-ranking feature and weight contracts."""

from __future__ import annotations

import json
import os
import tempfile
from dataclasses import asdict, dataclass, replace
from datetime import datetime
from enum import StrEnum
from pathlib import Path

from zotero_arxiv_daily.core.errors import ApplicationError
from zotero_arxiv_daily.core.time import require_aware_utc

RANKING_WEIGHT_SET_SCHEMA_VERSION = 1


class FeatureGroup(StrEnum):
    """Stable groups that can receive an independently versioned ranking weight."""

    INTEREST = "interest"
    RECENCY = "recency"
    FEEDBACK = "feedback"
    IDENTITY = "identity"
    SCIENTIFIC_QUALITY = "scientific_quality"
    REPRODUCIBILITY = "reproducibility"
    CONTEXT = "context"


@dataclass(frozen=True, slots=True)
class NormalizedFeature:
    name: str
    value: float
    applicable: bool
    confidence: float
    provenance: str
    group: FeatureGroup = FeatureGroup.INTEREST
    observed_at: datetime | None = None

    def __post_init__(self) -> None:
        if not self.name or not self.provenance:
            raise ValueError("normalized feature identity is invalid")
        if not 0 <= self.value <= 1 or not 0 <= self.confidence <= 1:
            raise ValueError("normalized feature values must be within zero and one")
        if not self.applicable and (self.value != 0 or self.confidence != 0):
            raise ValueError("unavailable normalized features must not carry a score")
        if self.observed_at is not None:
            require_aware_utc(self.observed_at, "observed_at")


@dataclass(frozen=True, slots=True)
class WeightSet:
    version: str
    interest: float = 0.5
    recency: float = 0.1
    feedback: float = 0.15
    identity: float = 0.1
    scientific_quality: float = 0.07
    reproducibility: float = 0.03
    context: float = 0.05
    negative_feedback_cap: float = 0.2
    ablation: bool = False

    def __post_init__(self) -> None:
        if not self.version or any(not 0 <= value <= 1 for value in self.__dict_values()):
            raise ValueError("weight set values must be normalized")
        if self.positive_total > 1:
            raise ValueError("weight set positive groups cannot exceed one")
        if not self.ablation and self.interest < max(
            self.recency,
            self.feedback,
            self.identity,
            self.scientific_quality,
            self.reproducibility,
            self.context,
        ):
            raise ValueError("interest relevance must remain the largest ranking group")

    @property
    def positive_total(self) -> float:
        return sum(self.group_weights.values())

    @property
    def group_weights(self) -> dict[FeatureGroup, float]:
        return {
            FeatureGroup.INTEREST: self.interest,
            FeatureGroup.RECENCY: self.recency,
            FeatureGroup.FEEDBACK: self.feedback,
            FeatureGroup.IDENTITY: self.identity,
            FeatureGroup.SCIENTIFIC_QUALITY: self.scientific_quality,
            FeatureGroup.REPRODUCIBILITY: self.reproducibility,
            FeatureGroup.CONTEXT: self.context,
        }

    def without(self, group: FeatureGroup) -> WeightSet:
        """Return an immutable ablation with one group excluded from normalization."""

        match group:
            case FeatureGroup.INTEREST:
                return replace(self, interest=0.0, ablation=True)
            case FeatureGroup.RECENCY:
                return replace(self, recency=0.0, ablation=True)
            case FeatureGroup.FEEDBACK:
                return replace(self, feedback=0.0, ablation=True)
            case FeatureGroup.IDENTITY:
                return replace(self, identity=0.0, ablation=True)
            case FeatureGroup.SCIENTIFIC_QUALITY:
                return replace(self, scientific_quality=0.0, ablation=True)
            case FeatureGroup.REPRODUCIBILITY:
                return replace(self, reproducibility=0.0, ablation=True)
            case FeatureGroup.CONTEXT:
                return replace(self, context=0.0, ablation=True)

    def __dict_values(self) -> tuple[float, ...]:
        return (
            self.interest,
            self.recency,
            self.feedback,
            self.identity,
            self.scientific_quality,
            self.reproducibility,
            self.context,
            self.negative_feedback_cap,
        )


DEFAULT_WEIGHT_SET = WeightSet(
    "quality-first-v1",
    interest=0.40,
    recency=0.05,
    feedback=0.0,
    identity=0.10,
    scientific_quality=0.35,
    reproducibility=0.10,
    context=0.0,
)


@dataclass(frozen=True, slots=True)
class _RegistryState:
    weight_sets: tuple[WeightSet, ...]
    active_version: str | None


class WeightSetRegistry:
    """Local immutable weight-set registry with an explicit reversible active pointer."""

    def __init__(self, path: Path) -> None:
        self.path = path

    def register(self, weight_set: WeightSet) -> bool:
        """Store a new version once; an identical repeated registration is idempotent."""

        state = self._read()
        if weight_set.ablation:
            raise ApplicationError("ranking ablation weight sets cannot be registered")
        stored = {item.version: item for item in state.weight_sets}
        existing = stored.get(weight_set.version)
        if existing is not None:
            if existing != weight_set:
                raise ApplicationError("ranking weight-set version is immutable")
            return False
        self._write(
            _RegistryState(
                (*state.weight_sets, weight_set),
                state.active_version,
            )
        )
        return True

    def activate(self, version: str) -> WeightSet:
        """Move only the active pointer; registered weight-set definitions never change."""

        state = self._read()
        values = {item.version: item for item in state.weight_sets}
        if version not in values:
            raise ApplicationError("ranking weight-set version is not registered")
        self._write(_RegistryState(state.weight_sets, version))
        return values[version]

    def active(self, fallback: WeightSet = DEFAULT_WEIGHT_SET) -> WeightSet:
        """Read the active local version, using the built-in conservative default when absent."""

        state = self._read()
        version = state.active_version
        if version is None:
            return fallback
        for item in state.weight_sets:
            if item.version == version:
                return item
        raise ApplicationError("ranking weight-set registry active version is invalid")

    def migrate_release_default(
        self,
        weight_set: WeightSet,
        *,
        previous_builtin_versions: frozenset[str],
    ) -> WeightSet:
        """Activate a release default only over its known built-in predecessor pointer."""

        self.register(weight_set)
        state = self._read()
        if state.active_version in previous_builtin_versions:
            return self.activate(weight_set.version)
        return self.active(weight_set)

    def versions(self) -> tuple[WeightSet, ...]:
        return self._read().weight_sets

    def _read(self) -> _RegistryState:
        if not self.path.exists():
            return _RegistryState((), None)
        try:
            payload = json.loads(self.path.read_text(encoding="utf-8"))
            if not isinstance(payload, dict) or set(payload) != {
                "schema_version",
                "active_version",
                "weight_sets",
            }:
                raise ValueError
            if payload["schema_version"] != RANKING_WEIGHT_SET_SCHEMA_VERSION:
                raise ValueError
            raw_sets = payload["weight_sets"]
            if not isinstance(raw_sets, list):
                raise ValueError
            sets = tuple(_weight_set(raw) for raw in raw_sets)
            if len({item.version for item in sets}) != len(sets):
                raise ValueError
            active = payload["active_version"]
            if active is not None and (
                not isinstance(active, str) or active not in {item.version for item in sets}
            ):
                raise ValueError
            return _RegistryState(sets, active)
        except (OSError, TypeError, ValueError, json.JSONDecodeError) as error:
            raise ApplicationError("ranking weight-set registry is invalid") from error

    def _write(self, state: _RegistryState) -> None:
        values = state.weight_sets
        self.path.parent.mkdir(parents=True, exist_ok=True)
        descriptor, temporary_name = tempfile.mkstemp(
            prefix=f".{self.path.name}.", dir=self.path.parent
        )
        temporary = Path(temporary_name)
        try:
            with os.fdopen(descriptor, "w", encoding="utf-8") as output:
                json.dump(
                    {
                        "schema_version": RANKING_WEIGHT_SET_SCHEMA_VERSION,
                        "active_version": state.active_version,
                        "weight_sets": [asdict(item) for item in values],
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


def _weight_set(value: object) -> WeightSet:
    if not isinstance(value, dict):
        raise ValueError
    expected = {
        "version",
        "interest",
        "recency",
        "feedback",
        "identity",
        "scientific_quality",
        "reproducibility",
        "context",
        "negative_feedback_cap",
        "ablation",
    }
    if set(value) != expected:
        raise ValueError
    if not isinstance(value["ablation"], bool):
        raise ValueError
    return WeightSet(
        str(value["version"]),
        float(value["interest"]),
        float(value["recency"]),
        float(value["feedback"]),
        float(value["identity"]),
        float(value["scientific_quality"]),
        float(value["reproducibility"]),
        float(value["context"]),
        float(value["negative_feedback_cap"]),
        bool(value["ablation"]),
    )
