"""Privacy-safe run-manifest history and efficiency comparisons."""

from __future__ import annotations

import json
import os
import tempfile
from dataclasses import asdict, dataclass
from pathlib import Path
from statistics import median
from typing import Any

from zotero_arxiv_daily.core.errors import ApplicationError

MANIFEST_HISTORY_SCHEMA_VERSION = 1
DEFAULT_MANIFEST_HISTORY_LIMIT = 90
OUTPUT_TOKEN_REDUCTION_TARGET = 0.25


@dataclass(frozen=True, slots=True)
class EfficiencyAggregate:
    model: str
    run_count: int
    measured_run_count: int
    recommendation_count_total: int
    median_output_tokens_per_recommendation: float | None
    median_input_tokens_per_recommendation: float | None
    median_total_tokens_per_recommendation: float | None
    median_duration_seconds: float | None
    median_provider_latency_seconds: float | None
    median_cost_usd_per_recommendation: float | None
    median_model_requests: float
    median_cache_hits: float


@dataclass(frozen=True, slots=True)
class EfficiencyComparison:
    baseline: EfficiencyAggregate
    candidate: EfficiencyAggregate
    output_token_reduction: float | None
    input_token_change: float | None
    duration_change: float | None
    cost_change: float | None
    meets_output_token_target: bool
    comparable: bool
    reasons: tuple[str, ...]


def record_manifest(
    input_path: Path, history_path: Path, *, limit: int = DEFAULT_MANIFEST_HISTORY_LIMIT
) -> int:
    """Append one validated non-sensitive manifest idempotently and atomically."""

    if limit < 1:
        raise ValueError("manifest history limit must be positive")
    manifest = _read_object(input_path)
    _validate_manifest(manifest)
    history = _read_history(history_path)
    encoded = json.dumps(manifest, sort_keys=True, separators=(",", ":"))
    existing = {json.dumps(item, sort_keys=True, separators=(",", ":")) for item in history}
    if encoded not in existing:
        history.append(manifest)
    history = history[-limit:]
    _atomic_write(
        history_path,
        json.dumps(
            {"schema_version": MANIFEST_HISTORY_SCHEMA_VERSION, "manifests": history},
            sort_keys=True,
            separators=(",", ":"),
        ),
    )
    return len(history)


def compare_manifest_files(
    baseline_path: Path, candidate_path: Path, output_path: Path
) -> EfficiencyComparison:
    """Compare measured runs and persist only aggregate efficiency data."""

    baseline = aggregate_manifests(_read_manifest_series(baseline_path))
    candidate = aggregate_manifests(_read_manifest_series(candidate_path))
    comparison = compare_efficiency(baseline, candidate)
    _atomic_write(
        output_path, json.dumps(asdict(comparison), sort_keys=True, separators=(",", ":"))
    )
    return comparison


def aggregate_manifests(manifests: tuple[dict[str, Any], ...]) -> EfficiencyAggregate:
    """Aggregate per-run measurements without retaining candidate or profile content."""

    if not manifests:
        raise ApplicationError("efficiency comparison requires at least one run manifest")
    for manifest in manifests:
        _validate_manifest(manifest)
    models = {str(manifest["model"]) for manifest in manifests}
    if len(models) != 1:
        raise ApplicationError("efficiency comparison requires one model per side")
    measured = [
        manifest
        for manifest in manifests
        if manifest.get("actual_input_tokens") is not None
        and manifest.get("actual_output_tokens") is not None
        and int(manifest.get("recommendation_count", 0)) > 0
    ]
    output_per_recommendation = [
        int(item["actual_output_tokens"]) / int(item["recommendation_count"]) for item in measured
    ]
    input_per_recommendation = [
        int(item["actual_input_tokens"]) / int(item["recommendation_count"]) for item in measured
    ]
    total_per_recommendation = [
        (int(item["actual_input_tokens"]) + int(item["actual_output_tokens"]))
        / int(item["recommendation_count"])
        for item in measured
    ]
    duration = [float(item["duration_seconds"]) for item in measured]
    provider_latency = [
        float(item["provider_latency_seconds"])
        for item in measured
        if item.get("provider_latency_seconds") is not None
    ]
    cost = [
        float(item["actual_cost_usd"]) / int(item["recommendation_count"])
        for item in measured
        if item.get("actual_cost_usd") is not None
    ]
    return EfficiencyAggregate(
        next(iter(models)),
        len(manifests),
        len(measured),
        sum(int(item["recommendation_count"]) for item in manifests),
        _median(output_per_recommendation),
        _median(input_per_recommendation),
        _median(total_per_recommendation),
        _median(duration),
        _median(provider_latency),
        _median(cost),
        float(median(float(item["model_requests"]) for item in manifests)),
        float(median(float(item["cache_hits"]) for item in manifests)),
    )


def compare_efficiency(
    baseline: EfficiencyAggregate, candidate: EfficiencyAggregate
) -> EfficiencyComparison:
    """Apply the measured 25% output-token gate and expose missing-data reasons."""

    reasons: list[str] = []
    if baseline.model != candidate.model:
        reasons.append("baseline and candidate models differ")
    if baseline.measured_run_count == 0 or candidate.measured_run_count == 0:
        reasons.append("one side has no measured token usage")
    reduction = _relative_change(
        baseline.median_output_tokens_per_recommendation,
        candidate.median_output_tokens_per_recommendation,
    )
    input_change = _relative_change(
        baseline.median_input_tokens_per_recommendation,
        candidate.median_input_tokens_per_recommendation,
    )
    duration_change = _relative_change(
        baseline.median_duration_seconds, candidate.median_duration_seconds
    )
    cost_change = _relative_change(
        baseline.median_cost_usd_per_recommendation,
        candidate.median_cost_usd_per_recommendation,
    )
    meets_target = reduction is not None and reduction >= OUTPUT_TOKEN_REDUCTION_TARGET
    if reduction is None:
        reasons.append("output-token reduction is unavailable")
    elif not meets_target:
        reasons.append("median output-token reduction is below the 25% target")
    comparable = not reasons
    return EfficiencyComparison(
        baseline,
        candidate,
        reduction,
        input_change,
        duration_change,
        cost_change,
        meets_target,
        comparable,
        tuple(reasons),
    )


def _read_manifest_series(path: Path) -> tuple[dict[str, Any], ...]:
    value = _read_object(path)
    raw = value.get("manifests", [value])
    if not isinstance(raw, list) or not all(isinstance(item, dict) for item in raw):
        raise ApplicationError("manifest history is invalid")
    return tuple(raw)


def _read_history(path: Path) -> list[dict[str, Any]]:
    if not path.exists():
        return []
    value = _read_object(path)
    raw = value.get("manifests")
    if value.get("schema_version") != MANIFEST_HISTORY_SCHEMA_VERSION or not isinstance(raw, list):
        raise ApplicationError("manifest history schema is invalid")
    if not all(isinstance(item, dict) for item in raw):
        raise ApplicationError("manifest history contains invalid entries")
    return list(raw)


def _read_object(path: Path) -> dict[str, Any]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as error:
        raise ApplicationError(f"manifest file is unreadable: {path}") from error
    if not isinstance(value, dict):
        raise ApplicationError("manifest root must be an object")
    return value


def _validate_manifest(value: dict[str, Any]) -> None:
    required = {
        "schema_version",
        "model",
        "candidate_count",
        "recommendation_count",
        "model_requests",
        "cache_hits",
        "estimated_tokens",
        "estimated_cost_usd",
        "duration_seconds",
    }
    if (
        not required.issubset(value)
        or not isinstance(value["model"], str)
        or not value["model"].strip()
    ):
        raise ApplicationError("run manifest is invalid")
    for name in ("candidate_count", "recommendation_count", "model_requests", "cache_hits"):
        if not isinstance(value[name], int) or isinstance(value[name], bool) or value[name] < 0:
            raise ApplicationError("run manifest counts are invalid")
    for name in ("estimated_tokens", "estimated_cost_usd", "duration_seconds"):
        if (
            not isinstance(value[name], (int, float))
            or isinstance(value[name], bool)
            or value[name] < 0
        ):
            raise ApplicationError("run manifest measurements are invalid")
    for name in ("actual_input_tokens", "actual_output_tokens"):
        if value.get(name) is not None and (
            not isinstance(value[name], int) or isinstance(value[name], bool) or value[name] < 0
        ):
            raise ApplicationError("run manifest actual token measurements are invalid")
    for name in ("actual_cost_usd", "provider_latency_seconds"):
        if value.get(name) is not None and (
            not isinstance(value[name], (int, float))
            or isinstance(value[name], bool)
            or value[name] < 0
        ):
            raise ApplicationError("run manifest measured values are invalid")


def _median(values: list[float]) -> float | None:
    return float(median(values)) if values else None


def _relative_change(baseline: float | None, candidate: float | None) -> float | None:
    if baseline is None or candidate is None or baseline == 0:
        return None
    return (baseline - candidate) / baseline


def _atomic_write(path: Path, content: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary_name = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    temporary = Path(temporary_name)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as output:
            output.write(content)
            output.flush()
            os.fsync(output.fileno())
        os.replace(temporary, path)
        os.chmod(path, 0o600)
    finally:
        temporary.unlink(missing_ok=True)
