from __future__ import annotations

import gzip
import json
from datetime import UTC, datetime
from pathlib import Path

import pytest

from zotero_arxiv_daily.arxiv.models import ArxivCandidate, ArxivId
from zotero_arxiv_daily.core.errors import ConfigurationError
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
    js = (output / "assets/app.js").read_text(encoding="utf-8")
    assert 'role="status"' in html
    assert "Skip to recommendations" in html
    assert '<select id="date-filter"></select>' in html
    assert 'type="date"' not in html
    assert 'allDates:"All dates"' in js
    assert "item=>item.published_on))].sort().reverse()" in js


def test_site_config_exposes_degraded_candidate_pool_freshness(tmp_path: Path) -> None:
    output = tmp_path / "site"
    source_checkpoint = datetime(2026, 8, 1, 12, 0, tzinfo=UTC)

    build_site(
        make_published_set(_recommendations()),
        output,
        public_output=True,
        passphrase=None,
        candidate_pool_status=(True, "arXiv timeout", source_checkpoint),
    )

    config = json.loads((output / "data/site-config.json").read_text(encoding="utf-8"))
    js = (output / "assets/app.js").read_text(encoding="utf-8")
    assert config["freshness"] == {
        "degraded": True,
        "reason": "arXiv timeout",
        "source_checkpoint": source_checkpoint.isoformat(),
    }
    assert "Candidate pool" in js
    assert "source_checkpoint" in js


def test_site_rejects_unbounded_candidate_pool_diagnostic(tmp_path: Path) -> None:
    with pytest.raises(ConfigurationError, match="160-character"):
        build_site(
            make_published_set(_recommendations()),
            tmp_path / "site",
            public_output=True,
            passphrase=None,
            candidate_pool_status=(True, "x" * 161, None),
        )


def test_publishable_input_round_trip_rejects_internal_record_fields(tmp_path: Path) -> None:
    path = tmp_path / "recommendations.json"
    original = make_published_set(_recommendations())
    write_published_set(original, path)

    loaded = read_published_set(path)

    assert loaded == original


def test_publishable_schema_v2_adapts_without_a_profile_snapshot(tmp_path: Path) -> None:
    path = tmp_path / "recommendations.json"
    current = make_published_set(_recommendations(), profile_schema_version=2)
    payload = current.to_dict()
    payload["schema_version"] = 2
    payload.pop("profile_snapshot_at")
    recommendations = payload["recommendations"]
    assert isinstance(recommendations, tuple)
    legacy_recommendations: list[dict[str, object]] = []
    for recommendation in recommendations:
        assert isinstance(recommendation, dict)
        legacy = dict(recommendation)
        for field in (
            "limitation",
            "quality_evidence_fields",
            "reproducibility",
            "reproducibility_evidence",
            "evidence_provenance",
        ):
            legacy.pop(field)
        legacy_recommendations.append(legacy)
    payload["recommendations"] = legacy_recommendations
    path.write_text(json.dumps(payload), encoding="utf-8")

    loaded = read_published_set(path)

    assert loaded.schema_version == 2
    assert loaded.profile_snapshot_at is None


def test_publishable_schema_v3_reader_defaults_the_new_limitation_field(tmp_path: Path) -> None:
    path = tmp_path / "recommendations.json"
    payload = make_published_set(_recommendations()).to_dict()
    payload["schema_version"] = 3
    recommendations = payload["recommendations"]
    assert isinstance(recommendations, tuple)
    legacy_recommendations: list[dict[str, object]] = []
    for recommendation in recommendations:
        assert isinstance(recommendation, dict)
        legacy = dict(recommendation)
        for field in (
            "limitation",
            "quality_evidence_fields",
            "reproducibility",
            "reproducibility_evidence",
            "evidence_provenance",
        ):
            legacy.pop(field)
        legacy_recommendations.append(legacy)
    payload["recommendations"] = legacy_recommendations
    path.write_text(json.dumps(payload), encoding="utf-8")

    loaded = read_published_set(path)

    assert loaded.schema_version == 3
    assert loaded.recommendations[0].limitation is None


def test_publishable_schema_v5_round_trips_separate_quality_and_implementation_evidence(
    tmp_path: Path,
) -> None:
    base = _recommendations()
    record = base.recommendations[0]
    refined = RecommendationRecord(
        record.candidate,
        record.score,
        record.source,
        record.quality,
        record.summary,
        record.reason,
        limitation="The abstract does not establish results beyond the described evaluation.",
        quality_evidence_fields=("method_evidence", "limitations_evidence"),
        reproducibility=0.8,
        reproducibility_evidence="implementation_and_evaluation",
        evidence_provenance=("arxiv-metadata", "ar5iv-sections-v1", "github-contents-v1"),
    )
    path = tmp_path / "recommendations.json"
    published = make_published_set(RecommendationSet(2, 9, base.generated_at, (refined,)))
    write_published_set(published, path)

    loaded = read_published_set(path)

    assert loaded.schema_version == 5
    assert loaded.recommendations[0].limitation == refined.limitation
    assert loaded.recommendations[0].quality_evidence_fields == refined.quality_evidence_fields
    assert loaded.recommendations[0].reproducibility == 0.8
    assert loaded.recommendations[0].reproducibility_evidence == ("implementation_and_evaluation")


def test_publishable_schema_v4_reader_defaults_new_evidence_fields(tmp_path: Path) -> None:
    path = tmp_path / "recommendations-v4.json"
    payload = make_published_set(_recommendations()).to_dict()
    payload["schema_version"] = 4
    recommendations = payload["recommendations"]
    assert isinstance(recommendations, tuple)
    for recommendation in recommendations:
        assert isinstance(recommendation, dict)
        for field in (
            "quality_evidence_fields",
            "reproducibility",
            "reproducibility_evidence",
            "evidence_provenance",
        ):
            recommendation.pop(field)
    path.write_text(json.dumps(payload), encoding="utf-8")

    loaded = read_published_set(path)

    assert loaded.schema_version == 4
    assert loaded.recommendations[0].quality_evidence_fields == ()
    assert loaded.recommendations[0].reproducibility is None


def test_publishable_schema_v4_keeps_quality_and_uncertainty_distinct(tmp_path: Path) -> None:
    record = RecommendationRecord(
        _recommendations().recommendations[0].candidate,
        3,
        "core",
        0.8,
        "Summary",
        "Reason",
        uncertainty=0.35,
    )
    path = tmp_path / "quality.json"
    result = RecommendationSet(
        2, 9, record.candidate.published, (record,), record.candidate.published
    )
    write_published_set(make_published_set(result), path)

    loaded = read_published_set(path)

    assert loaded.recommendations[0].quality == 0.8
    assert loaded.recommendations[0].uncertainty == 0.35


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
        2,
        9,
        result.generation_started_at,
        (record,),
        result.generation_started_at,
        "2026-08-01T12:00:00+00:00",
    )
    published = make_published_set(current, profile_schema_version=2, output_language="zh-CN")
    output = tmp_path / "site"

    build_site(published, output, public_output=True, passphrase=None)

    assert published.recommendations[0].published_on == "2026-08-02"
    assert published.schema_version == 5
    assert published.profile_snapshot_at == "2026-08-01T12:00:00+00:00"
    css = (output / "assets/site.css").read_text(encoding="utf-8")
    js = (output / "assets/app.js").read_text(encoding="utf-8")
    assert "prefers-color-scheme" in css
    assert "prefers-reduced-motion" in css
    assert "Asia/Shanghai" in js
    assert "Profile snapshot" in js
    assert "Limitations" in js
    assert "Zotero library version" not in js
    assert "status-panel h2" in css
    assert "font-size:.78rem" in css
    assert "Not interested" in js
    assert "Save for later" in js
    assert "normalizeData" in js
    assert "item.quality??item.confidence" in js
    assert "zh-CN" not in js
    assert not any("\u4e00" <= character <= "\u9fff" for character in js)
    assert len(css.encode()) <= 8_192
    assert len(js.encode()) <= 24_576
    assert len(gzip.compress(css.encode())) <= 3_072
    assert len(gzip.compress(js.encode())) <= 8_192
