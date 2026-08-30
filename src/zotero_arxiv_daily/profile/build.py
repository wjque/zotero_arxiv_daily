"""Deterministic, local-only extraction and privacy-bounded profile projection."""

from __future__ import annotations

import json
import math
import re
from collections import defaultdict
from datetime import UTC, datetime
from typing import Any

from zotero_arxiv_daily.arxiv.categories import adjacent_categories
from zotero_arxiv_daily.core.errors import ConfigurationError
from zotero_arxiv_daily.profile.export import serving_profile_payload
from zotero_arxiv_daily.profile.models import (
    LOCAL_INTEREST_PROFILE_SCHEMA_VERSION,
    MAX_PROTECTED_LEXICAL_FEATURES,
    PROFILE_FEATURE_HASH_VERSION,
    REMOTE_SERVING_PROFILE_SCHEMA_VERSION,
    ItemDigest,
    LocalInterestProfile,
    PreferenceFacet,
    ProtectedLexicalFeature,
    RemoteServingProfile,
)
from zotero_arxiv_daily.profile.protection import (
    lexical_tokens,
    protected_feature_digest,
    validate_profile_feature_key,
)

_WORD = re.compile(r"[A-Za-z][A-Za-z0-9-]{2,31}")
_LINK = re.compile(
    r"(?:\bhttps?://|\bwww\.)[^\s<>\"]+|\b(?:github|gitlab|bitbucket)\.com/[^\s<>\"]+",
    re.IGNORECASE,
)
_SENSITIVE = re.compile(r"(?:sk-[A-Za-z0-9]{12,}|ghp_[A-Za-z0-9]{12,}|password|secret)", re.I)
_STOP_WORDS = frozenset(
    {
        "about",
        "abstract",
        "and",
        "are",
        "for",
        "from",
        "github",
        "into",
        "gitlab",
        "bitbucket",
        "com",
        "doi",
        "http",
        "https",
        "note",
        "net",
        "org",
        "paper",
        "that",
        "the",
        "this",
        "www",
        "with",
    }
)
_CATEGORY_HINTS = {
    "machine": "cs.LG",
    "learning": "cs.LG",
    "language": "cs.CL",
    "vision": "cs.CV",
    "quantum": "quant-ph",
    "statistical": "stat.ML",
    "neural": "cs.NE",
    "algorithm": "cs.DS",
}
_FACET_HINTS = {
    "domain": {
        "learning": "machine-learning",
        "neural": "machine-learning",
        "language": "language",
        "vision": "computer-vision",
        "quantum": "quantum-computing",
        "algorithm": "algorithms",
    },
    "method": {
        "transformer": "transformers",
        "diffusion": "diffusion",
        "reinforcement": "reinforcement-learning",
        "optimization": "optimization",
        "statistical": "statistical-modeling",
    },
    "task": {
        "classification": "classification",
        "generation": "generation",
        "retrieval": "retrieval",
        "reasoning": "reasoning",
        "translation": "translation",
    },
}
_SIGNAL_WEIGHTS = {
    "library": 0.25,
    "manual_tag": 2.0,
    "collection": 1.0,
    "note": 1.5,
    "annotation": 2.0,
    "comment": 2.5,
    "curated": 4.0,
}
_FEEDBACK_TAG_PREFIXES = ("ranking-reason:", "zad:")
_LABEL_COLLECTION_NAMES = frozenset({"positive", "negative", "hard negative"})


def build_local_interest_profile(
    records: tuple[tuple[str, str, str, tuple[str, ...]], ...],
    library_version: int,
    *,
    observed_at: datetime | None = None,
    curated_item_keys: frozenset[str] = frozenset(),
) -> LocalInterestProfile:
    """Build a stable weighted profile from local normalized records only."""

    scores: defaultdict[str, float] = defaultdict(float)
    recent: defaultdict[str, float] = defaultdict(float)
    observed = observed_at.astimezone(UTC) if observed_at is not None else None
    for item_key, _, payload, child_payloads in records:
        root = _mapping(payload)
        terms = _terms(_text_fields(root))
        recency = _recency_weight(str(root.get("date") or ""), observed)
        metadata_weight = _SIGNAL_WEIGHTS["library"]
        if item_key in curated_item_keys:
            metadata_weight += _SIGNAL_WEIGHTS["curated"]
        _add_terms(scores, recent, terms, metadata_weight, recency)
        for child in child_payloads:
            child_data = _mapping(child)
            for field, weight in (
                ("note_text", _SIGNAL_WEIGHTS["note"]),
                ("annotation_text", _SIGNAL_WEIGHTS["annotation"]),
                ("annotation_comment", _SIGNAL_WEIGHTS["comment"]),
            ):
                _add_terms(
                    scores, recent, _terms(str(child_data.get(field) or "")), weight, recency
                )
        for tag, manual in root.get("tags", []):
            tag_text = str(tag)
            if tag_text.casefold().startswith(_FEEDBACK_TAG_PREFIXES):
                continue
            weight = _SIGNAL_WEIGHTS["manual_tag"] if manual else _SIGNAL_WEIGHTS["library"]
            if tag_text.casefold().startswith("ranking-curated:"):
                weight = _SIGNAL_WEIGHTS["curated"]
                tag_text = tag_text.split(":", 1)[1]
            _add_terms(scores, recent, _terms(tag_text), weight, recency)
        for collection in root.get("collection_names", []):
            if str(collection).casefold().strip() in _LABEL_COLLECTION_NAMES:
                continue
            _add_terms(
                scores,
                recent,
                _terms(str(collection)),
                _SIGNAL_WEIGHTS["collection"],
                recency,
            )
    ranked = tuple(sorted(scores.items(), key=lambda item: (-item[1], item[0]))[:80])
    recent_ranked = tuple(sorted(recent.items(), key=lambda item: (-item[1], item[0]))[:30])
    categories = _categories(ranked)
    return LocalInterestProfile(
        LOCAL_INTEREST_PROFILE_SCHEMA_VERSION,
        library_version,
        ranked,
        recent_ranked,
        categories,
        len(records),
        _facets(ranked),
        _facets(recent_ranked),
    )


def project_serving_profile(
    profile: LocalInterestProfile,
    profile_feature_key: str,
    payload_budget: int = 30 * 1024,
) -> RemoteServingProfile:
    """Project local interests into keyed, bounded features for remote scoring."""

    key = validate_profile_feature_key(profile_feature_key)
    core = tuple(category for category, _, _ in profile.categories[:6])
    adjacent = tuple(
        sorted(
            {
                value
                for category in core
                for value in adjacent_categories(category)
                if value not in core
            }
        )
    )[:6]
    long_term = _normalized_lexical_weights(profile.terms)
    recent = _normalized_lexical_weights(profile.recent_terms)
    selected_tokens = tuple(
        sorted(
            set(long_term) | set(recent),
            key=lambda value: (
                -(0.7 * long_term.get(value, 0.0) + 0.3 * recent.get(value, 0.0)),
                value,
            ),
        )[:MAX_PROTECTED_LEXICAL_FEATURES]
    )
    lexical_features = tuple(
        sorted(
            (
                ProtectedLexicalFeature(
                    protected_feature_digest(token, key, namespace="lexical"),
                    long_term.get(token, 0.0),
                    recent.get(token, 0.0),
                )
                for token in selected_tokens
            ),
            key=lambda feature: feature.digest,
        )
    )
    long_term_facets = profile.long_term_facets[:12]
    recent_facets = profile.recent_facets[:8]
    serving = RemoteServingProfile(
        REMOTE_SERVING_PROFILE_SCHEMA_VERSION,
        profile.source_library_version,
        (),
        core,
        adjacent,
        (),
        preference_facets=long_term_facets + recent_facets,
        feature_hash_version=PROFILE_FEATURE_HASH_VERSION,
        feature_key_verifier=protected_feature_digest(
            "serving-profile-key", key, namespace="key-verifier"
        ),
        lexical_features=lexical_features,
        baseline_lexical_digests=tuple(
            sorted(
                {
                    protected_feature_digest(term, key, namespace="baseline-lexical")
                    for term, _score in profile.terms[:30]
                    if _safe_term(term)
                }
            )
        ),
        long_term_facets=long_term_facets,
        recent_facets=recent_facets,
    )
    encoded = json.dumps(
        serving_profile_payload(serving), ensure_ascii=False, separators=(",", ":")
    ).encode("utf-8")
    if len(encoded) > payload_budget:
        raise ConfigurationError(
            f"serving profile is {len(encoded)} bytes; budget is {payload_budget} bytes"
        )
    return serving


def project_legacy_serving_profile(
    profile: LocalInterestProfile, payload_budget: int = 30 * 1024
) -> RemoteServingProfile:
    """Revalidate a legacy plaintext profile without expanding its trust boundary."""

    topics = tuple(term for term, _ in profile.terms[:30] if _safe_term(term))
    core = tuple(category for category, _, _ in profile.categories[:6])
    adjacent = tuple(
        sorted(
            {
                value
                for category in core
                for value in adjacent_categories(category)
                if value not in core
            }
        )
    )[:6]
    serving = RemoteServingProfile(
        4,
        profile.source_library_version,
        topics,
        core,
        adjacent,
        topics[:12],
        preference_facets=profile.long_term_facets[:20],
    )
    encoded = json.dumps(
        serving_profile_payload(serving), ensure_ascii=False, separators=(",", ":")
    ).encode("utf-8")
    if len(encoded) > payload_budget:
        raise ConfigurationError(
            f"serving profile is {len(encoded)} bytes; budget is {payload_budget} bytes"
        )
    return serving


def make_digest(
    item_key: str, content_hash: str, payload: str, children: tuple[str, ...]
) -> ItemDigest:
    """Create a cached-compatible digest without retaining raw note prose."""

    root = _mapping(payload)
    signals = tuple(sorted(_SIGNAL_WEIGHTS.items()))
    terms = tuple(sorted(set(_digest_terms(root, children)))[:40])
    categories = tuple(
        category for category, _, _ in _categories(tuple((term, 1.0) for term in terms))
    )
    year_match = re.search(r"\b(?:19|20)\d{2}\b", str(root.get("date") or ""))
    year = int(year_match.group(0)) if year_match else None
    return ItemDigest(item_key, content_hash, terms, signals, year, categories)


def _categories(terms: tuple[tuple[str, float], ...]) -> tuple[tuple[str, float, str], ...]:
    scores: defaultdict[str, float] = defaultdict(float)
    for term, weight in terms:
        if category := _CATEGORY_HINTS.get(term):
            scores[category] += weight
    ranked = sorted(scores.items(), key=lambda item: (-item[1], item[0]))[:6]
    return tuple((category, score, "deterministic term hint") for category, score in ranked)


def _add_terms(
    long_term: defaultdict[str, float],
    recent: defaultdict[str, float],
    terms: tuple[str, ...],
    weight: float,
    recency: float,
) -> None:
    for term in terms:
        long_term[term] += weight
        recent[term] += weight * recency


def _normalized_lexical_weights(
    weighted_terms: tuple[tuple[str, float], ...],
) -> dict[str, float]:
    scores: defaultdict[str, float] = defaultdict(float)
    for term, score in weighted_terms:
        if not _safe_term(term):
            continue
        for token in lexical_tokens(term):
            scores[token] += score
    maximum = max(scores.values(), default=0.0)
    return {token: min(score / maximum, 1.0) for token, score in scores.items() if maximum > 0}


def _digest_terms(root: dict[str, Any], children: tuple[str, ...]) -> tuple[str, ...]:
    parts = [_text_fields(root)]
    for child in children:
        child_data = _mapping(child)
        parts.extend(
            str(child_data.get(field) or "")
            for field in ("note_text", "annotation_text", "annotation_comment")
        )
    for tag, _manual in root.get("tags", []):
        tag_text = str(tag)
        if not tag_text.casefold().startswith(_FEEDBACK_TAG_PREFIXES):
            parts.append(tag_text)
    parts.extend(
        str(collection)
        for collection in root.get("collection_names", [])
        if str(collection).casefold().strip() not in _LABEL_COLLECTION_NAMES
    )
    return _terms(" ".join(parts))


def _recency_weight(value: str, observed_at: datetime | None) -> float:
    if observed_at is None:
        return 1.0
    match = re.search(r"\b(19|20)\d{2}(?:-(\d{2})(?:-(\d{2}))?)?\b", value)
    if match is None:
        return 0.35
    year, month, day = int(match.group(0)[:4]), int(match.group(2) or 1), int(match.group(3) or 1)
    try:
        item_date = datetime(year, month, day, tzinfo=UTC)
    except ValueError:
        return 0.35
    age_days = max((observed_at - item_date).total_seconds() / 86_400, 0.0)
    return max(0.1, math.exp(-age_days / 365))


def _facets(terms: tuple[tuple[str, float], ...]) -> tuple[PreferenceFacet, ...]:
    maximum = terms[0][1] if terms else 1.0
    facets: list[PreferenceFacet] = []
    for kind, hints in _FACET_HINTS.items():
        values: defaultdict[str, float] = defaultdict(float)
        for term, score in terms:
            if value := hints.get(term):
                values[value] += score
        facets.extend(
            PreferenceFacet(
                kind,
                value,
                min(score / maximum, 1.0),
                min(score / maximum, 1.0),
                ("local-derived",),
            )
            for value, score in sorted(values.items(), key=lambda item: (-item[1], item[0]))[:4]
        )
    return tuple(sorted(facets, key=lambda facet: (-facet.score, facet.kind, facet.value))[:12])


def _mapping(payload: str) -> dict[str, Any]:
    value = json.loads(payload)
    if not isinstance(value, dict):
        raise ConfigurationError("stored Zotero payload is malformed")
    return value


def _text_fields(record: dict[str, Any]) -> str:
    return " ".join(str(record.get(field) or "") for field in ("title", "abstract", "date"))


def _terms(value: str) -> tuple[str, ...]:
    value = _LINK.sub(" ", value)
    return tuple(
        word.casefold()
        for word in _WORD.findall(value)
        if word.casefold() not in _STOP_WORDS and _safe_term(word)
    )


def _safe_term(value: str) -> bool:
    return not _SENSITIVE.search(value)
