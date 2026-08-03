"""Versioned normalized coarse-ranking feature and weight contracts."""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True, slots=True)
class NormalizedFeature:
    name: str
    value: float
    applicable: bool
    confidence: float
    provenance: str

    def __post_init__(self) -> None:
        if not self.name or not self.provenance:
            raise ValueError("normalized feature identity is invalid")
        if not 0 <= self.value <= 1 or not 0 <= self.confidence <= 1:
            raise ValueError("normalized feature values must be within zero and one")


@dataclass(frozen=True, slots=True)
class WeightSet:
    version: str
    interest: float = 0.5
    recency: float = 0.1
    feedback: float = 0.15
    identity: float = 0.1
    negative_feedback_cap: float = 0.2

    def __post_init__(self) -> None:
        if not self.version or any(not 0 <= value <= 1 for value in self.__dict_values()):
            raise ValueError("weight set values must be normalized")
        if self.interest + self.recency + self.feedback + self.identity > 1:
            raise ValueError("weight set positive groups cannot exceed one")

    def __dict_values(self) -> tuple[float, ...]:
        return (
            self.interest,
            self.recency,
            self.feedback,
            self.identity,
            self.negative_feedback_cap,
        )


DEFAULT_WEIGHT_SET = WeightSet("coarse-v1")
