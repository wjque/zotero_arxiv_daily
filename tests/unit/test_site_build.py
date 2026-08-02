from __future__ import annotations

import gzip
from datetime import UTC, datetime
from pathlib import Path

from zotero_arxiv_daily.arxiv.models import ArxivCandidate, ArxivId
from zotero_arxiv_daily.ranking.models import RecommendationRecord, RecommendationSet
from zotero_arxiv_daily.site.build import build_site
from zotero_arxiv_daily.site.models import (
    make_published_set,
    read_published_set,
    write_published_set,
)


def _recommendations() -> RecommendationSet:
    now = datetime(2026, 8, 1, tzinfo=UTC)
    candidate = ArxivCandidate(
        ArxivId("2401.00001", 1),
        "<script>unsafe</script> learning",
        ("Ada",),
        ("cs.LG",),
        now,
        now,
        "https://arxiv.org/abs/2401.00001",
        "https://arxiv.org/pdf/2401.00001",
        "Public arXiv abstract",
    )
    record = RecommendationRecord(candidate, 3.0, "core", 0.8, "Summary", "Reason")
    return RecommendationSet(1, 9, now, (record,))


def test_encrypted_site_keeps_recommendation_plaintext_out_of_static_artifacts(
    tmp_path: Path,
) -> None:
    output = tmp_path / "site"
    result = build_site(
        make_published_set(_recommendations()),
        output,
        public_output=False,
        passphrase="a sufficiently long test passphrase",
        feedback_repository="owner/repository",
    )
    artifacts = "\n".join(
        path.read_text(encoding="utf-8") for path in output.rglob("*") if path.is_file()
    )

    assert result.encrypted is True
    assert (output / "data/recommendations.enc.json").is_file()
    assert "<script>unsafe</script> learning" not in artifacts
    assert "innerHTML" not in (output / "assets/app.js").read_text(encoding="utf-8")


def test_public_site_is_explicit_and_contains_accessible_feedback_controls(tmp_path: Path) -> None:
    output = tmp_path / "site"
    result = build_site(
        make_published_set(_recommendations()), output, public_output=True, passphrase=None
    )

    assert result.encrypted is False
    assert (output / "data/recommendations.json").is_file()
    html = (output / "index.html").read_text(encoding="utf-8")
    assert 'role="status"' in html
    assert "Skip to recommendations" in html


def test_publishable_input_round_trip_rejects_internal_record_fields(tmp_path: Path) -> None:
    path = tmp_path / "recommendations.json"
    original = make_published_set(_recommendations())
    write_published_set(original, path)

    loaded = read_published_set(path)

    assert loaded == original


def test_schema_v2_uses_shanghai_date_and_self_contained_accessible_assets(
    tmp_path: Path,
) -> None:
    result = _recommendations()
    candidate = result.recommendations[0].candidate
    local_midnight_candidate = ArxivCandidate(
        candidate.arxiv_id,
        candidate.title,
        candidate.authors,
        candidate.categories,
        datetime(2026, 8, 1, 16, 1, tzinfo=UTC),
        candidate.updated,
        candidate.abstract_url,
        candidate.pdf_url,
        candidate.summary,
    )
    record = RecommendationRecord(
        local_midnight_candidate, 3, "core", 0.8, "Summary", "Reason", ("watched_author",)
    )
    current = RecommendationSet(
        2, 9, result.generation_started_at, (record,), result.generation_started_at
    )
    published = make_published_set(current, profile_schema_version=2, output_language="zh-CN")
    output = tmp_path / "site"

    build_site(published, output, public_output=True, passphrase=None)

    assert published.recommendations[0].published_on == "2026-08-02"
    css = (output / "assets/site.css").read_text(encoding="utf-8")
    js = (output / "assets/app.js").read_text(encoding="utf-8")
    assert "prefers-color-scheme" in css
    assert "prefers-reduced-motion" in css
    assert "Asia/Shanghai" in js
    assert "Not interested" in js
    assert "Save for later" in js
    assert "zh-CN" not in js
    assert not any("\u4e00" <= character <= "\u9fff" for character in js)
    assert len(css.encode()) <= 8_192
    assert len(js.encode()) <= 24_576
    assert len(gzip.compress(css.encode())) <= 3_072
    assert len(gzip.compress(js.encode())) <= 8_192
