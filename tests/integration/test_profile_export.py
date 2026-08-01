from __future__ import annotations

import json
from pathlib import Path

import pytest

from zotero_arxiv_daily.core.errors import ConfigurationError
from zotero_arxiv_daily.profile.export import write_remote_profile
from zotero_arxiv_daily.profile.models import RemoteProfile
from zotero_arxiv_daily.profile.service import read_remote_profile


def test_remote_profile_export_is_compact_atomic_json_with_owner_permissions(
    tmp_path: Path,
) -> None:
    target = tmp_path / "remote.json"
    write_remote_profile(
        RemoteProfile(1, 3, ("learning",), ("cs.LG",), ("stat.ML",), ("learning",)), target
    )

    assert json.loads(target.read_text(encoding="utf-8"))["topics"] == ["learning"]
    assert target.stat().st_mode & 0o077 == 0


def test_remote_profile_read_rejects_unallowlisted_fields(tmp_path: Path) -> None:
    target = tmp_path / "remote.json"
    target.write_text('{"raw_note":"must not cross boundary"}', encoding="utf-8")

    with pytest.raises(ConfigurationError, match="unsupported fields"):
        read_remote_profile(target)
