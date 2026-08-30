from __future__ import annotations

import json
from pathlib import Path

import pytest

from zotero_arxiv_daily.core.errors import SecurityError
from zotero_arxiv_daily.security.state import (
    STATE_BUNDLE_FILENAME,
    decrypt_state_bundle,
    encrypt_state_directory,
)

_REQUIRED = {
    "arxiv-state.json": {"schema_version": 3, "candidates": []},
    "feedback-state.json": {"schema_version": 2, "events": []},
    "recommendation-history.json": {"schema_version": 1, "records": []},
}


def _write_required(directory: Path) -> None:
    directory.mkdir(parents=True)
    for name, value in _REQUIRED.items():
        (directory / name).write_text(json.dumps(value), encoding="utf-8")


def test_state_bundle_round_trip_writes_only_validated_files(tmp_path: Path) -> None:
    source = tmp_path / "runtime"
    output = tmp_path / STATE_BUNDLE_FILENAME
    restored = tmp_path / "restored"
    _write_required(source)
    (source / "proposal-cache.json").write_text('{"entries":[]}', encoding="utf-8")
    (source / "efficiency-report.json").write_text('{"comparable":false}', encoding="utf-8")
    (source / "private-unrelated.json").write_text("{}", encoding="utf-8")

    encrypt_state_directory(source, output, "state-passphrase-1234")
    written = decrypt_state_bundle(output, restored, "state-passphrase-1234")

    assert written == (
        "arxiv-state.json",
        "efficiency-report.json",
        "feedback-state.json",
        "proposal-cache.json",
        "recommendation-history.json",
    )
    assert not (restored / "private-unrelated.json").exists()
    assert json.loads((restored / "proposal-cache.json").read_text(encoding="utf-8")) == {
        "entries": []
    }
    assert (restored / "feedback-state.json").stat().st_mode & 0o077 == 0


def test_state_bundle_requires_all_core_files(tmp_path: Path) -> None:
    source = tmp_path / "runtime"
    source.mkdir()
    (source / "arxiv-state.json").write_text("{}", encoding="utf-8")

    with pytest.raises(SecurityError, match="missing required"):
        encrypt_state_directory(source, tmp_path / STATE_BUNDLE_FILENAME, "state-passphrase-1234")


def test_state_bundle_rejects_wrong_key_without_writing_output(tmp_path: Path) -> None:
    source = tmp_path / "runtime"
    output = tmp_path / STATE_BUNDLE_FILENAME
    restored = tmp_path / "restored"
    _write_required(source)
    encrypt_state_directory(source, output, "state-passphrase-1234")

    with pytest.raises(SecurityError, match="cannot be decrypted"):
        decrypt_state_bundle(output, restored, "different-passphrase-1234")
    assert not restored.exists()


def test_state_bundle_carries_worthwhile_predictions(tmp_path: Path) -> None:
    source = tmp_path / "runtime"
    output = tmp_path / STATE_BUNDLE_FILENAME
    restored = tmp_path / "restored"
    _write_required(source)
    (source / "worthwhile-predictions.json").write_text(
        '{"schema_version":1,"batches":[]}', encoding="utf-8"
    )

    encrypt_state_directory(source, output, "state-passphrase-1234")
    written = decrypt_state_bundle(output, restored, "state-passphrase-1234")

    assert "worthwhile-predictions.json" in written
    assert json.loads((restored / "worthwhile-predictions.json").read_text(encoding="utf-8")) == {
        "schema_version": 1,
        "batches": [],
    }


def test_state_bundle_without_worthwhile_predictions_still_restores(tmp_path: Path) -> None:
    source = tmp_path / "runtime"
    output = tmp_path / STATE_BUNDLE_FILENAME
    restored = tmp_path / "restored"
    _write_required(source)

    encrypt_state_directory(source, output, "state-passphrase-1234")
    written = decrypt_state_bundle(output, restored, "state-passphrase-1234")

    assert "worthwhile-predictions.json" not in written
    assert not (restored / "worthwhile-predictions.json").exists()
