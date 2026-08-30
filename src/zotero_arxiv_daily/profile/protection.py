"""Keyed feature identities shared by local profile projection and remote scoring."""

from __future__ import annotations

import base64
import hashlib
import hmac
import re

from zotero_arxiv_daily.core.errors import ConfigurationError
from zotero_arxiv_daily.profile.models import PROFILE_FEATURE_HASH_VERSION

MINIMUM_PROFILE_FEATURE_KEY_BYTES = 32

_WORDS = re.compile(r"[a-z][a-z0-9-]{2,}")


def validate_profile_feature_key(value: str | None) -> str:
    """Require an independent high-entropy key without exposing it in diagnostics."""

    if value is None or len(value.encode("utf-8")) < MINIMUM_PROFILE_FEATURE_KEY_BYTES:
        raise ConfigurationError(
            "profile_feature_key must contain at least 32 UTF-8 bytes for serving profile v5"
        )
    return value


def protected_feature_digest(value: str, key: str, *, namespace: str) -> str:
    """Return a domain-separated, compact keyed identity for one normalized feature."""

    validated = validate_profile_feature_key(key)
    payload = f"{PROFILE_FEATURE_HASH_VERSION}\0{namespace}\0{value}".encode()
    digest = hmac.new(validated.encode("utf-8"), payload, hashlib.sha256).digest()[:16]
    return base64.urlsafe_b64encode(digest).decode("ascii").rstrip("=")


def lexical_tokens(value: str) -> frozenset[str]:
    """Normalize public or local text identically before keyed matching."""

    words = _WORDS.findall(value.casefold())
    return frozenset(
        token for word in words for token in (word, *word.split("-")) if len(token) >= 3
    )
