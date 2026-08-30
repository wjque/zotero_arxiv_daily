"""Build synthetic static-site fixtures for browser execution tests."""

from __future__ import annotations

import argparse
import json
from datetime import UTC, datetime
from functools import partial
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

from zotero_arxiv_daily.arxiv.models import ArxivCandidate, ArxivId
from zotero_arxiv_daily.ranking.models import RecommendationRecord, RecommendationSet
from zotero_arxiv_daily.site.build import build_site
from zotero_arxiv_daily.site.models import make_published_set

PASSPHRASE = "synthetic browser fixture passphrase"


def build_fixtures(output: Path) -> None:
    """Create encrypted current-schema and public unsupported-schema fixtures."""

    observed_at = datetime(2026, 8, 4, 8, tzinfo=UTC)
    candidate = ArxivCandidate(
        ArxivId("2401.00001", 1),
        "Synthetic Browser Validation Paper",
        ("Ada Example",),
        ("cs.LG",),
        observed_at,
        observed_at,
        "https://arxiv.org/abs/2401.00001",
        "https://arxiv.org/pdf/2401.00001",
        "A synthetic public abstract used only for browser validation.",
    )
    record = RecommendationRecord(
        candidate,
        0.81,
        "core",
        0.74,
        "The paper validates the static recommendation rendering path.",
        "It matches the synthetic browser-testing preference.",
        uncertainty=0.22,
        limitation="The fixture does not make a scientific quality claim.",
        quality_evidence_fields=("method_evidence", "limitations_evidence"),
        reproducibility=0.8,
        reproducibility_evidence="implementation_and_evaluation",
        evidence_provenance=("arxiv-metadata", "ar5iv-sections-v1", "github-contents-v1"),
    )
    published = make_published_set(
        RecommendationSet(2, 1, observed_at, (record,), observed_at),
        profile_schema_version=4,
    )
    build_site(
        published,
        output / "current",
        public_output=False,
        passphrase=PASSPHRASE,
        feedback_repository="owner/repository",
    )
    build_site(published, output / "unsupported", public_output=True, passphrase=None)
    data_path = output / "unsupported" / "data" / "recommendations.json"
    data = json.loads(data_path.read_text(encoding="utf-8"))
    data["schema_version"] = 999
    data_path.write_text(json.dumps(data, separators=(",", ":")), encoding="utf-8")


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--serve", action="store_true")
    args = parser.parse_args()
    build_fixtures(args.output)
    if args.serve:
        handler = partial(SimpleHTTPRequestHandler, directory=str(args.output))
        ThreadingHTTPServer(("127.0.0.1", 4173), handler).serve_forever()


if __name__ == "__main__":
    main()
