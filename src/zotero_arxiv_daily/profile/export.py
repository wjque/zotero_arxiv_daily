"""Atomic local export for privacy-bounded remote profile payloads."""

from __future__ import annotations

import json
import os
import tempfile
from dataclasses import asdict
from pathlib import Path

from zotero_arxiv_daily.profile.models import RemoteProfile


def write_remote_profile(profile: RemoteProfile, path: Path) -> None:
    """Write allowlisted data atomically with owner-only permissions when supported."""

    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary_name = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    temporary_path = Path(temporary_name)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as output:
            payload = asdict(profile)
            if profile.schema_version == 1:
                payload.pop("watched_authors")
                payload.pop("watched_institutions")
                payload.pop("source_library_synced_at")
                payload.pop("preference_facets")
            elif profile.schema_version == 2:
                payload.pop("source_library_synced_at")
                payload.pop("preference_facets")
            elif profile.schema_version == 3:
                payload.pop("preference_facets")
            json.dump(payload, output, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
            output.flush()
            os.fsync(output.fileno())
        os.chmod(temporary_path, 0o600)
        os.replace(temporary_path, path)
    finally:
        temporary_path.unlink(missing_ok=True)
