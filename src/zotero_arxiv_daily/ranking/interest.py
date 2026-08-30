"""Privacy-preserving interest matching for legacy and keyed serving profiles."""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass

from zotero_arxiv_daily.core.errors import ConfigurationError
from zotero_arxiv_daily.profile.models import (
    REMOTE_SERVING_PROFILE_SCHEMA_VERSION,
    RemoteServingProfile,
    WatchedIdentity,
    normalize_identity,
)
from zotero_arxiv_daily.profile.protection import (
    lexical_tokens,
    protected_feature_digest,
    validate_profile_feature_key,
)


@dataclass(frozen=True, slots=True)
class ProtectedInterestMatch:
    """Separate inspectable matches retained by the v5 serving ranker."""

    long_term_lexical: float
    recent_lexical: float
    prototype: float


def serving_profile_key(profile: RemoteServingProfile, key: str | None) -> str | None:
    """Require the independent matching key only for protected profile versions."""

    if profile.schema_version != REMOTE_SERVING_PROFILE_SCHEMA_VERSION:
        return None
    validated = validate_profile_feature_key(key)
    verifier = protected_feature_digest("serving-profile-key", validated, namespace="key-verifier")
    if verifier != profile.feature_key_verifier:
        raise ConfigurationError("profile_feature_key does not match the serving profile")
    return validated


def protected_interest_match(
    text: str, profile: RemoteServingProfile, key: str
) -> ProtectedInterestMatch:
    """Match public candidate text against keyed aggregates without recovering local terms."""

    candidate_digests = frozenset(
        protected_feature_digest(token, key, namespace="lexical") for token in lexical_tokens(text)
    )
    long_term = _weighted_match(
        (
            (feature.digest, feature.long_term_weight)
            for feature in profile.lexical_features
            if feature.long_term_weight > 0
        ),
        candidate_digests,
    )
    recent = _weighted_match(
        (
            (feature.digest, feature.recent_weight)
            for feature in profile.lexical_features
            if feature.recent_weight > 0
        ),
        candidate_digests,
    )
    prototype = max(
        (
            len(candidate_digests & frozenset(item.feature_digests)) / len(item.feature_digests)
            for item in profile.interest_prototypes
        ),
        default=0.0,
    )
    return ProtectedInterestMatch(long_term, recent, prototype)


def watched_identity_match(
    values: tuple[str, ...],
    legacy_identities: tuple[WatchedIdentity, ...],
    protected_digests: tuple[str, ...],
    key: str | None,
    *,
    namespace: str,
) -> bool:
    """Match either legacy plaintext identities or v5 keyed identities exactly."""

    normalized_values = {normalize_identity(value) for value in values if value.strip()}
    if protected_digests:
        if key is None:
            return False
        candidate_digests = {
            protected_feature_digest(value, key, namespace=namespace) for value in normalized_values
        }
        return bool(candidate_digests & set(protected_digests))
    return any(
        bool(normalized_values & identity.normalized_names) for identity in legacy_identities
    )


def _weighted_match(
    values: Iterable[tuple[str, float]],
    candidate_digests: frozenset[str],
) -> float:
    weighted = tuple(values)
    denominator = sum(weight for _, weight in weighted)
    if denominator == 0:
        return 0.0
    return sum(weight for digest, weight in weighted if digest in candidate_digests) / denominator
