"""Versioned interpretation policies for aggregate quality references."""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from enum import StrEnum

QUALITY_REFERENCE_FIELDS = (
    "research_problems",
    "methodological_expectations",
    "evidence_standards",
    "motivations",
    "tolerated_limitations",
)


class QualityReferenceFieldRole(StrEnum):
    """Stable semantic roles applied to whole trait fields, never individual papers or traits."""

    DESCRIPTIVE = "descriptive"
    POSITIVE_REFERENCE = "positive_reference"
    LIMITATION_CONTEXT = "limitation_context"


@dataclass(frozen=True, slots=True)
class QualityReferencePolicy:
    """Map profile fields to bounded judge behavior under one immutable policy version."""

    version: str
    judge_contract: str
    field_roles: tuple[tuple[str, QualityReferenceFieldRole], ...]
    generation_enabled: bool = True
    reference_evidence_allowed: bool = False
    descriptive_fields_sent: bool = False

    def __post_init__(self) -> None:
        roles = dict(self.field_roles)
        if (
            not self.version.strip()
            or not self.judge_contract.startswith("judge-")
            or len(roles) != len(self.field_roles)
            or set(roles) != set(QUALITY_REFERENCE_FIELDS)
        ):
            raise ValueError("quality reference policy is invalid")
        if set(roles.values()) != set(QualityReferenceFieldRole):
            raise ValueError("quality reference policy must use every field role")
        if self.generation_enabled and self.reference_evidence_allowed:
            raise ValueError("new quality reference policies cannot replace candidate evidence")
        if self.generation_enabled and self.descriptive_fields_sent:
            raise ValueError("new quality reference policies cannot send descriptive fields")

    def fields_for(self, role: QualityReferenceFieldRole) -> tuple[str, ...]:
        return tuple(field for field, assigned in self.field_roles if assigned is role)

    @property
    def model_fields(self) -> tuple[str, ...]:
        if self.descriptive_fields_sent:
            return tuple(field for field, _ in self.field_roles)
        return tuple(
            field
            for field, role in self.field_roles
            if role is not QualityReferenceFieldRole.DESCRIPTIVE
        )

    @property
    def fingerprint(self) -> str:
        payload = {
            "version": self.version,
            "judge_contract": self.judge_contract,
            "field_roles": [(field, role.value) for field, role in self.field_roles],
            "generation_enabled": self.generation_enabled,
            "reference_evidence_allowed": self.reference_evidence_allowed,
            "descriptive_fields_sent": self.descriptive_fields_sent,
            "judge_instruction": self.judge_instruction(),
        }
        return hashlib.sha256(
            json.dumps(payload, sort_keys=True, separators=(",", ":")).encode()
        ).hexdigest()

    def judge_instruction(self) -> str:
        """Render trusted policy instructions from validated roles."""

        descriptive = ", ".join(self.fields_for(QualityReferenceFieldRole.DESCRIPTIVE))
        positive = ", ".join(self.fields_for(QualityReferenceFieldRole.POSITIVE_REFERENCE))
        limitations = ", ".join(self.fields_for(QualityReferenceFieldRole.LIMITATION_CONTEXT))
        return (
            f"Interpret quality_reference only under policy {self.version}. Its support values are "
            "relative prevalence within each field, never scores, probabilities, thresholds, or "
            "cross-field comparisons. "
            f"Fields {descriptive} are descriptive context only and must never increase or "
            "decrease "
            "a dimension score or uncertainty. "
            f"Fields {positive} are optional positive references: apply a criterion only when the "
            "candidate's supplied public evidence directly demonstrates it; absence must never "
            "lower "
            "a score or increase uncertainty. "
            f"Field {limitations} is review context only: it never excuses missing evidence or "
            "directly changes a score, and an empty value means unspecified. Candidate-specific "
            "supplied evidence and the fixed score anchors always control the judgment. "
            "quality_reference is context, not candidate evidence, and must never appear in "
            "evidence_fields. "
        )


_STABLE_FIELD_ROLES = (
    ("research_problems", QualityReferenceFieldRole.DESCRIPTIVE),
    ("methodological_expectations", QualityReferenceFieldRole.POSITIVE_REFERENCE),
    ("evidence_standards", QualityReferenceFieldRole.POSITIVE_REFERENCE),
    ("motivations", QualityReferenceFieldRole.DESCRIPTIVE),
    ("tolerated_limitations", QualityReferenceFieldRole.LIMITATION_CONTEXT),
)
LEGACY_QUALITY_REFERENCE_POLICY = QualityReferencePolicy(
    "quality-reference-policy-legacy-v1",
    "judge-v3",
    _STABLE_FIELD_ROLES,
    generation_enabled=False,
    reference_evidence_allowed=True,
    descriptive_fields_sent=True,
)
DEFAULT_QUALITY_REFERENCE_POLICY = QualityReferencePolicy(
    "quality-reference-policy-v1",
    "judge-v4",
    _STABLE_FIELD_ROLES,
)
LEGACY_QUALITY_PROFILE_POLICY_VERSION = LEGACY_QUALITY_REFERENCE_POLICY.version

# Append-only: persisted profiles may continue selecting any previously registered policy.
_REGISTERED_POLICIES = (LEGACY_QUALITY_REFERENCE_POLICY, DEFAULT_QUALITY_REFERENCE_POLICY)
if len({item.version for item in _REGISTERED_POLICIES}) != len(_REGISTERED_POLICIES) or len(
    {item.judge_contract for item in _REGISTERED_POLICIES}
) != len(_REGISTERED_POLICIES):
    raise RuntimeError("quality reference policy registry contains duplicate identities")
_POLICIES = {item.version: item for item in _REGISTERED_POLICIES}


def get_quality_reference_policy(version: str) -> QualityReferencePolicy:
    try:
        return _POLICIES[version]
    except KeyError as error:
        raise ValueError("quality reference policy version is unsupported") from error


def quality_reference_policy_versions() -> tuple[str, ...]:
    return tuple(item.version for item in _REGISTERED_POLICIES if item.generation_enabled)


def quality_reference_policies() -> tuple[QualityReferencePolicy, ...]:
    return tuple(_POLICIES.values())
