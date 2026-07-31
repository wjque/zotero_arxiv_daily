from __future__ import annotations

from datetime import UTC, datetime
from uuid import uuid4

import pytest

from zotero_arxiv_daily.core.models import FeedbackRecord, Recommendation


def test_recommendation_rejects_non_https_external_links() -> None:
    with pytest.raises(ValueError, match="HTTPS"):
        Recommendation(
            "1234.5678",
            "Synthetic title",
            "http://arxiv.org/abs/1234.5678",
            "https://arxiv.org/pdf/1234.5678",
        )


def test_feedback_requires_an_aware_timestamp() -> None:
    with pytest.raises(ValueError, match="timezone-aware"):
        FeedbackRecord(uuid4(), "1234.5678", "read", datetime(2026, 1, 1))


def test_feedback_accepts_version_one() -> None:
    record = FeedbackRecord(uuid4(), "1234.5678", "read", datetime.now(UTC))

    assert record.schema_version == 1
