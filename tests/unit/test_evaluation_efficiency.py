from __future__ import annotations

import json
from pathlib import Path

import pytest

from zotero_arxiv_daily.core.errors import ApplicationError
from zotero_arxiv_daily.evaluation.efficiency import (
    aggregate_manifests,
    compare_efficiency,
    record_manifest,
)


def _manifest(
    run: str,
    *,
    recommendation_count: int = 20,
    actual_input_tokens: int | None = 1_000,
    actual_output_tokens: int | None = 400,
) -> dict[str, object]:
    return {
        "schema_version": 1,
        "model": "deepseek-v4-flash",
        "candidate_count": 40,
        "recommendation_count": recommendation_count,
        "model_requests": 3,
        "cache_hits": 2,
        "estimated_tokens": 1_500,
        "estimated_cost_usd": 0.01,
        "duration_seconds": 10.0,
        "generation_started_at": f"2026-08-05T00:00:{run}Z",
        "actual_input_tokens": actual_input_tokens,
        "actual_output_tokens": actual_output_tokens,
        "actual_cost_usd": 0.008,
        "provider_latency_seconds": 8.0,
    }


def test_efficiency_comparison_applies_median_output_token_gate() -> None:
    baseline = aggregate_manifests((_manifest("01", actual_output_tokens=1_000),))
    candidate = aggregate_manifests((_manifest("02", actual_output_tokens=700),))

    report = compare_efficiency(baseline, candidate)

    assert report.output_token_reduction == pytest.approx(0.3)
    assert report.meets_output_token_target
    assert report.comparable


def test_efficiency_comparison_blocks_missing_measured_usage() -> None:
    baseline = aggregate_manifests((_manifest("01", actual_output_tokens=None),))
    candidate = aggregate_manifests((_manifest("02", actual_output_tokens=700),))

    report = compare_efficiency(baseline, candidate)

    assert not report.comparable
    assert any("no measured token usage" in reason for reason in report.reasons)


def test_manifest_history_is_idempotent_and_bounded(tmp_path: Path) -> None:
    input_path = tmp_path / "run-manifest.json"
    history_path = tmp_path / "history.json"
    input_path.write_text(json.dumps(_manifest("01")), encoding="utf-8")

    assert record_manifest(input_path, history_path, limit=2) == 1
    assert record_manifest(input_path, history_path, limit=2) == 1
    input_path.write_text(json.dumps(_manifest("02")), encoding="utf-8")
    assert record_manifest(input_path, history_path, limit=2) == 2
    input_path.write_text(json.dumps(_manifest("03")), encoding="utf-8")
    assert record_manifest(input_path, history_path, limit=2) == 2

    value = json.loads(history_path.read_text(encoding="utf-8"))
    assert [item["generation_started_at"] for item in value["manifests"]] == [
        "2026-08-05T00:00:02Z",
        "2026-08-05T00:00:03Z",
    ]
    assert history_path.stat().st_mode & 0o077 == 0


def test_manifest_history_rejects_invalid_input(tmp_path: Path) -> None:
    input_path = tmp_path / "run-manifest.json"
    input_path.write_text("{}", encoding="utf-8")

    with pytest.raises(ApplicationError, match="manifest is invalid"):
        record_manifest(input_path, tmp_path / "history.json")
