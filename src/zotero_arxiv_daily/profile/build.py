"""Deterministic, local-only extraction and privacy-bounded profile projection."""

from __future__ import annotations

import json
import re
from collections import defaultdict
from dataclasses import asdict
from typing import Any

from zotero_arxiv_daily.core.errors import ConfigurationError
from zotero_arxiv_daily.profile.models import InterestProfile, ItemDigest, RemoteProfile

_WORD = re.compile(r"[A-Za-z][A-Za-z0-9-]{2,31}")
_SENSITIVE = re.compile(r"(?:sk-[A-Za-z0-9]{12,}|ghp_[A-Za-z0-9]{12,}|password|secret)", re.I)
_STOP_WORDS = frozenset(
    {
        "about",
        "abstract",
        "and",
        "are",
        "for",
        "from",
        "into",
        "note",
        "paper",
        "that",
        "the",
        "this",
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
_ADJACENT = {
    "cs.LG": ("stat.ML", "cs.AI"),
    "cs.CL": ("cs.AI",),
    "cs.CV": ("cs.LG",),
    "quant-ph": ("cond-mat.str-el",),
}


def build_profile(
    records: tuple[tuple[str, str, str, tuple[str, ...]], ...], library_version: int
) -> InterestProfile:
    """Build a stable weighted profile from local normalized records only."""

    scores: defaultdict[str, float] = defaultdict(float)
    recent: defaultdict[str, float] = defaultdict(float)
    for _, _, payload, child_payloads in records:
        root = _mapping(payload)
        terms = _terms(_text_fields(root))
        for term in terms:
            scores[term] += 1.0
        for child in child_payloads:
            child_data = _mapping(child)
            for field, weight in (
                ("note_text", 1.5),
                ("annotation_text", 1.25),
                ("annotation_comment", 2.5),
            ):
                for term in _terms(str(child_data.get(field) or "")):
                    scores[term] += weight
        for tag, manual in root.get("tags", []):
            if manual:
                for term in _terms(str(tag)):
                    scores[term] += 2.0
        for term in terms:
            recent[term] += 1.0
    ranked = tuple(sorted(scores.items(), key=lambda item: (-item[1], item[0]))[:80])
    recent_ranked = tuple(sorted(recent.items(), key=lambda item: (-item[1], item[0]))[:30])
    categories = _categories(ranked)
    return InterestProfile(1, library_version, ranked, recent_ranked, categories, len(records))


def project_remote(profile: InterestProfile, payload_budget: int = 30 * 1024) -> RemoteProfile:
    """Apply a positive allowlist and bounded projection before remote transport."""

    topics = tuple(term for term, _ in profile.terms[:30] if _safe_term(term))
    core = tuple(category for category, _, _ in profile.categories[:6])
    adjacent = tuple(sorted({value for category in core for value in _ADJACENT.get(category, ())}))[
        :6
    ]
    remote = RemoteProfile(1, profile.source_library_version, topics, core, adjacent, topics[:12])
    encoded = json.dumps(asdict(remote), ensure_ascii=False, separators=(",", ":")).encode("utf-8")
    if len(encoded) > payload_budget:
        raise ConfigurationError(
            f"remote profile is {len(encoded)} bytes; budget is {payload_budget} bytes"
        )
    return remote


def make_digest(
    item_key: str, content_hash: str, payload: str, children: tuple[str, ...]
) -> ItemDigest:
    """Create a cached-compatible digest without retaining raw note prose."""

    root = _mapping(payload)
    signals = (
        ("metadata", 1.0),
        ("manual_tags", 2.0),
        ("notes", 1.5),
        ("highlights", 1.25),
        ("comments", 2.5),
    )
    terms = tuple(sorted(set(_terms(_text_fields(root) + " " + " ".join(children))))[:40])
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


def _mapping(payload: str) -> dict[str, Any]:
    value = json.loads(payload)
    if not isinstance(value, dict):
        raise ConfigurationError("stored Zotero payload is malformed")
    return value


def _text_fields(record: dict[str, Any]) -> str:
    return " ".join(str(record.get(field) or "") for field in ("title", "abstract", "date"))


def _terms(value: str) -> tuple[str, ...]:
    return tuple(
        word.casefold()
        for word in _WORD.findall(value)
        if word.casefold() not in _STOP_WORDS and _safe_term(word)
    )


def _safe_term(value: str) -> bool:
    return not _SENSITIVE.search(value)
