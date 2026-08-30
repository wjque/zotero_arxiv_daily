"""Durable per-batch record of what the declared objective predicted at publication time.

The objective in :mod:`zotero_arxiv_daily.ranking.outcome` is computed for every published batch and
was, until this store existed, discarded immediately. Without it the offline report in
:mod:`zotero_arxiv_daily.evaluation.worthwhile` can only describe realized outcomes; it cannot say
whether the declared policy predicted them, which is the comparison V030-M6 requires.

This store only remembers. Nothing here feeds ranking, and the file is read exclusively by the
offline report - so a prediction can be compared against reality, but it can never quietly become a
ranking input. Deleting the file restores the prior behavior exactly.
"""

from __future__ import annotations

import json
import os
import tempfile
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

from zotero_arxiv_daily.core.errors import ExternalServiceError
from zotero_arxiv_daily.core.time import require_aware_utc
from zotero_arxiv_daily.ranking.outcome import WorthwhileEstimate

WORTHWHILE_PREDICTION_SCHEMA_VERSION = 1

# Batches kept before the oldest are dropped. The report refuses to propose a calibration below
# thirty labeled outcomes, so this retains far more history than any proposal can consume while
# keeping the encrypted state bundle bounded on a daily schedule.
_RETENTION = 60


@dataclass(frozen=True, slots=True)
class BatchPrediction:
    """One published batch's declared objective estimates, in displayed-rank order."""

    batch_id: str
    recorded_at: datetime
    policy_version: str
    weight_set_version: str
    estimates: tuple[WorthwhileEstimate, ...]

    def __post_init__(self) -> None:
        require_aware_utc(self.recorded_at, "recorded_at")
        if not self.batch_id.strip() or not self.policy_version or not self.weight_set_version:
            raise ValueError("batch prediction identity is invalid")
        identifiers = [estimate.arxiv_id for estimate in self.estimates]
        if len(set(identifiers)) != len(identifiers):
            raise ValueError("batch prediction repeats a paper")
        if any(estimate.policy_version != self.policy_version for estimate in self.estimates):
            raise ValueError("batch prediction mixes worthwhile policy versions")

    @property
    def predicted_worthwhile_reads(self) -> float:
        """Declared expected number of worthwhile reads for this batch."""

        return sum(estimate.expected_worthwhile for estimate in self.estimates)


class WorthwhilePredictionStore:
    """Own the local prediction record; write at publication, read only offline."""

    def __init__(self, path: Path) -> None:
        self.path = path

    def batches(self) -> tuple[BatchPrediction, ...]:
        return self._batches()

    def predictions(self) -> dict[str, tuple[WorthwhileEstimate, ...]]:
        """Return the mapping ``run_worthwhile_evaluation`` accepts; missing file means empty."""

        return {batch.batch_id: batch.estimates for batch in self._batches()}

    def record(self, batch: BatchPrediction) -> bool:
        """Persist one batch idempotently; return whether it was newly recorded.

        Re-recording an identical batch is a no-op so a retried publication is safe, but a
        conflicting record for the same batch is refused rather than overwritten: the prediction
        must stay the one that was actually served.
        """

        known = {item.batch_id: item for item in self._batches()}
        existing = known.get(batch.batch_id)
        if existing is not None:
            if existing != batch:
                raise ExternalServiceError("batch prediction conflicts with the stored prediction")
            return False
        known[batch.batch_id] = batch
        retained = sorted(known.values(), key=lambda item: (item.recorded_at, item.batch_id))
        self._write(tuple(retained[-_RETENTION:]))
        return True

    def _batches(self) -> tuple[BatchPrediction, ...]:
        if not self.path.exists():
            return ()
        try:
            value = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as error:
            raise ExternalServiceError("worthwhile prediction store is unreadable") from error
        if not isinstance(value, dict):
            raise ExternalServiceError("worthwhile prediction store root is invalid")
        if value.get("schema_version") != WORTHWHILE_PREDICTION_SCHEMA_VERSION:
            raise ExternalServiceError("unsupported worthwhile prediction schema")
        raw = value.get("batches", [])
        if not isinstance(raw, list):
            raise ExternalServiceError("worthwhile prediction batches are invalid")
        try:
            batches = tuple(_batch_from_value(item) for item in raw)
        except (KeyError, TypeError, ValueError) as error:
            raise ExternalServiceError("worthwhile prediction batch is invalid") from error
        if len({batch.batch_id for batch in batches}) != len(batches):
            raise ExternalServiceError("worthwhile prediction store repeats a batch")
        return batches

    def _write(self, batches: tuple[BatchPrediction, ...]) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        descriptor, temporary_name = tempfile.mkstemp(
            prefix=f".{self.path.name}.", dir=self.path.parent
        )
        temporary = Path(temporary_name)
        try:
            with os.fdopen(descriptor, "w", encoding="utf-8") as output:
                json.dump(_encode(batches), output, sort_keys=True, separators=(",", ":"))
                output.flush()
                os.fsync(output.fileno())
            os.replace(temporary, self.path)
            os.chmod(self.path, 0o600)
        finally:
            temporary.unlink(missing_ok=True)


def _encode(batches: tuple[BatchPrediction, ...]) -> dict[str, object]:
    return {
        "schema_version": WORTHWHILE_PREDICTION_SCHEMA_VERSION,
        "batches": [
            {
                "batch_id": batch.batch_id,
                "recorded_at": batch.recorded_at.isoformat(),
                "policy_version": batch.policy_version,
                "weight_set_version": batch.weight_set_version,
                "estimates": [
                    {
                        "arxiv_id": estimate.arxiv_id,
                        "reading_likelihood": estimate.reading_likelihood,
                        "reading_likelihood_confidence": estimate.reading_likelihood_confidence,
                        "post_reading_value": estimate.post_reading_value,
                        "post_reading_value_confidence": estimate.post_reading_value_confidence,
                        "expected_worthwhile": estimate.expected_worthwhile,
                        "value_evidence_available": estimate.value_evidence_available,
                        "policy_version": estimate.policy_version,
                        "provenance": estimate.provenance,
                    }
                    for estimate in batch.estimates
                ],
            }
            for batch in batches
        ],
    }


def _batch_from_value(value: object) -> BatchPrediction:
    if not isinstance(value, dict):
        raise ValueError
    estimates = value["estimates"]
    if not isinstance(estimates, list):
        raise ValueError
    return BatchPrediction(
        str(value["batch_id"]),
        datetime.fromisoformat(str(value["recorded_at"])),
        str(value["policy_version"]),
        str(value["weight_set_version"]),
        tuple(_estimate_from_value(item) for item in estimates),
    )


def _estimate_from_value(value: object) -> WorthwhileEstimate:
    if not isinstance(value, dict):
        raise ValueError
    return WorthwhileEstimate(
        str(value["arxiv_id"]),
        float(value["reading_likelihood"]),
        float(value["reading_likelihood_confidence"]),
        float(value["post_reading_value"]),
        float(value["post_reading_value_confidence"]),
        float(value["expected_worthwhile"]),
        bool(value["value_evidence_available"]),
        str(value["policy_version"]),
        str(value["provenance"]),
    )
