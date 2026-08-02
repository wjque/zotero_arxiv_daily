from __future__ import annotations

from datetime import UTC, datetime

import pytest

from zotero_arxiv_daily.core.time import generation_decision, generation_window_open, product_date


@pytest.mark.parametrize(
    ("hour", "minute", "expected"),
    [(10, 29, False), (10, 30, True), (15, 59, True), (16, 0, True), (0, 30, True), (0, 31, False)],
)
def test_generation_window_uses_asia_shanghai_boundaries(
    hour: int, minute: int, expected: bool
) -> None:
    assert generation_window_open(datetime(2026, 8, 1, hour, minute, tzinfo=UTC)) is expected


def test_schedule_skips_and_manual_run_fails_closed_outside_window() -> None:
    peak = datetime(2026, 8, 1, 2, 0, tzinfo=UTC)

    assert generation_decision(peak, event_name="schedule") == "scheduled-skip"
    assert generation_decision(peak, event_name="workflow_dispatch") == "manual-blocked"
    assert (
        generation_decision(peak, event_name="workflow_dispatch", allow_peak_generation=True)
        == "allowed"
    )


def test_product_date_converts_before_truncating_at_local_midnight() -> None:
    assert product_date(datetime(2026, 8, 1, 16, 1, tzinfo=UTC)) == "2026-08-02"
