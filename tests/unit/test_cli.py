from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path

import pytest
from pytest import CaptureFixture, MonkeyPatch

from zotero_arxiv_daily import cli
from zotero_arxiv_daily.arxiv.discovery import DiscoveryQuery
from zotero_arxiv_daily.arxiv.models import RetrievalCheckpoint, RetrievalResult
from zotero_arxiv_daily.arxiv.storage import ArxivStateStore
from zotero_arxiv_daily.core.config import AppConfig
from zotero_arxiv_daily.evidence.models import PublicPaperEvidence
from zotero_arxiv_daily.feedback.ledger import (
    FeedbackEvent,
    FeedbackEventType,
    FeedbackLedgerStore,
    FeedbackOutcome,
)
from zotero_arxiv_daily.pipeline.recommend import package_result
from zotero_arxiv_daily.profile.models import PreferenceFacet, RemoteServingProfile
from zotero_arxiv_daily.profile.quality import (
    ApprovedQualityExample,
    QualityProfileStore,
    build_quality_reference_profile,
)
from zotero_arxiv_daily.site.models import PublishedRecommendationSet, write_published_set
from zotero_arxiv_daily.zotero.models import SyncBatch, ZoteroCollection, ZoteroItem
from zotero_arxiv_daily.zotero.storage import ZoteroStore


def test_doctor_command_returns_configuration_exit_code_without_secret_output(
    monkeypatch: MonkeyPatch, capsys: CaptureFixture[str]
) -> None:
    monkeypatch.setattr(cli, "load_config", lambda **_: AppConfig())

    exit_code = cli.main(["doctor", "--skip-zotero-check", "--format", "json"])

    captured = capsys.readouterr()
    assert exit_code == 2
    assert "ZAD_DEEPSEEK_API_KEY" in captured.out
    assert "configured" not in captured.out


def test_arxiv_retrieval_defaults_to_released_discovery_policy() -> None:
    args = cli.build_parser().parse_args(["arxiv", "retrieve", "--profile", "profile.json"])

    assert args.discovery_mode == "v0.2.1"


def test_profile_build_writes_separate_local_and_serving_files(
    monkeypatch: MonkeyPatch, capsys: CaptureFixture[str], tmp_path: Path
) -> None:
    feature_key = "test-profile-feature-key-0000000000000001"
    database = tmp_path / "zotero.sqlite3"
    store = ZoteroStore(database)
    store.apply(
        SyncBatch(
            1,
            (
                ZoteroItem(
                    "PAPER001",
                    1,
                    "journalArticle",
                    None,
                    "Private learning methods",
                    (),
                    (),
                    (),
                    (),
                    "",
                    None,
                    False,
                ),
            ),
            (),
        )
    )
    local_output = tmp_path / "local-profile.json"
    serving_output = tmp_path / "serving-profile.json"
    monkeypatch.setattr(
        cli,
        "load_config",
        lambda **_: AppConfig(local_database_path=str(database), profile_feature_key=feature_key),
    )

    exit_code = cli.main(
        [
            "profile",
            "build",
            "--local-output",
            str(local_output),
            "--output",
            str(serving_output),
        ]
    )

    assert exit_code == 0
    assert "protected lexical features" in capsys.readouterr().out
    assert "private" in local_output.read_text(encoding="utf-8")
    serving_payload = json.loads(serving_output.read_text(encoding="utf-8"))
    assert serving_payload["schema_version"] == 5
    assert "topics" not in serving_payload
    assert "private" not in serving_output.read_text(encoding="utf-8")


def test_profile_build_requires_the_feature_key_before_accessing_local_state(
    monkeypatch: MonkeyPatch, capsys: CaptureFixture[str]
) -> None:
    monkeypatch.setattr(cli, "load_config", lambda **_: AppConfig())

    exit_code = cli.main(["profile", "build"])

    assert exit_code == 4
    assert "ZAD_PROFILE_FEATURE_KEY" in capsys.readouterr().out


def test_controlled_discovery_refuses_the_production_candidate_state(
    monkeypatch: MonkeyPatch, capsys: CaptureFixture[str]
) -> None:
    monkeypatch.setattr(cli, "load_config", lambda **_: AppConfig())

    exit_code = cli.main(
        [
            "arxiv",
            "retrieve",
            "--profile",
            "profile.json",
            "--discovery-mode",
            "controlled-shadow",
        ]
    )

    assert exit_code == 4
    assert "requires a separate --state path" in capsys.readouterr().out


def test_arxiv_retrieval_rejects_empty_profile_before_network(
    monkeypatch: MonkeyPatch, capsys: CaptureFixture[str]
) -> None:
    monkeypatch.setattr(cli, "load_config", lambda **_: AppConfig())
    monkeypatch.setattr(
        cli,
        "read_serving_profile",
        lambda _: RemoteServingProfile(1, 1, (), (), (), ()),
    )
    monkeypatch.setattr(
        cli,
        "retrieve",
        lambda *_args, **_kwargs: pytest.fail("retrieval must not run for an empty profile"),
    )

    exit_code = cli.main(["arxiv", "retrieve", "--profile", "profile.json"])

    assert exit_code == 4
    assert "requires at least one core category" in capsys.readouterr().out


def test_controlled_discovery_uses_profile_facets_and_separate_shadow_state(
    monkeypatch: MonkeyPatch, capsys: CaptureFixture[str], tmp_path: Path
) -> None:
    shadow_state = tmp_path / "controlled-shadow.json"
    captured_stores: list[ArxivStateStore] = []
    captured_queries: list[tuple[DiscoveryQuery, ...]] = []
    monkeypatch.setattr(cli, "load_config", lambda **_: AppConfig())
    monkeypatch.setattr(
        cli,
        "read_serving_profile",
        lambda _: RemoteServingProfile(
            4,
            1,
            (),
            ("cs.LG",),
            (),
            (),
            preference_facets=(PreferenceFacet("task", "retrieval", 1.0, 1.0, ("local-derived",)),),
        ),
    )

    def retrieve_stub(
        _client: object,
        store: ArxivStateStore,
        queries: tuple[DiscoveryQuery, ...],
        now: datetime,
    ) -> RetrievalResult:
        captured_stores.append(store)
        captured_queries.append(queries)
        return RetrievalResult((), RetrievalCheckpoint(now), 6, False, None, 6, 2, 1)

    monkeypatch.setattr(cli, "retrieve", retrieve_stub)

    exit_code = cli.main(
        [
            "arxiv",
            "retrieve",
            "--profile",
            "profile.json",
            "--state",
            str(shadow_state),
            "--discovery-mode",
            "controlled-shadow",
        ]
    )

    assert exit_code == 0
    assert captured_stores[0].path == shadow_state
    queries = captured_queries[0]
    assert [query.category for query in queries][-2:] == ["cs.IR", "cs.DB"]
    assert all(query.required_facets == ("retrieval",) for query in queries[-2:])
    assert "1 bridge candidates, 6 queries (2 bridge), 6 requests" in capsys.readouterr().out


def test_site_build_command_uses_protected_output_by_default(
    monkeypatch: MonkeyPatch, capsys: CaptureFixture[str], tmp_path: Path
) -> None:
    input_path = tmp_path / "recommendations.json"
    output_path = tmp_path / "site"
    write_published_set(PublishedRecommendationSet(1, "2026-08-01T00:00:00+00:00", ()), input_path)
    monkeypatch.setattr(
        cli,
        "load_config",
        lambda **_: AppConfig(pages_passphrase="a sufficiently long test passphrase"),
    )

    exit_code = cli.main(
        ["site", "build", "--input", str(input_path), "--output", str(output_path)]
    )

    assert exit_code == 0
    assert "encrypted site built" in capsys.readouterr().out
    assert (output_path / "data/recommendations.enc.json").is_file()


def test_feedback_report_emits_only_aggregate_explicit_outcomes(
    monkeypatch: MonkeyPatch, capsys: CaptureFixture[str], tmp_path: Path
) -> None:
    state = tmp_path / "feedback.json"
    store = FeedbackLedgerStore(state)
    shown_at = datetime(2026, 8, 1, tzinfo=UTC)
    store.record_impressions("published-one", ("paper-one",), shown_at)
    store.ingest(
        (
            FeedbackEvent(
                "read-one",
                FeedbackEventType.OUTCOME,
                "paper-one",
                datetime(2026, 8, 1, 1, tzinfo=UTC),
                FeedbackOutcome.READ,
            ),
            FeedbackEvent(
                "worthwhile-one",
                FeedbackEventType.OUTCOME,
                "paper-one",
                datetime(2026, 8, 1, 2, tzinfo=UTC),
                FeedbackOutcome.WORTHWHILE,
            ),
        )
    )
    monkeypatch.setattr(cli, "load_config", lambda **_: AppConfig())

    assert cli.main(["feedback", "report", "--state", str(state)]) == 0

    report = json.loads(capsys.readouterr().out)
    assert report == {
        "schema_version": 1,
        "batches": [
            {
                "batch_id": "published-one",
                "explicit_feedback_count": 1,
                "explicit_feedback_coverage": 1.0,
                "impression_count": 1,
                "not_worthwhile_read_count": 0,
                "post_reading_outcome_count": 1,
                "post_reading_outcome_coverage": 1.0,
                "reading_completion_count": 1,
                "worthwhile_given_explicit_outcome": 1.0,
                "worthwhile_read_count": 1,
            }
        ],
    }


def test_evaluate_worthwhile_writes_an_observation_only_objective_report(
    monkeypatch: MonkeyPatch, capsys: CaptureFixture[str], tmp_path: Path
) -> None:
    state = tmp_path / "feedback.json"
    store = FeedbackLedgerStore(state)
    shown_at = datetime(2026, 8, 1, tzinfo=UTC)
    store.record_impressions("published-one", ("paper-one", "paper-two"), shown_at)
    store.ingest(
        (
            FeedbackEvent(
                "worthwhile-one",
                FeedbackEventType.OUTCOME,
                "paper-one",
                datetime(2026, 8, 1, 2, tzinfo=UTC),
                FeedbackOutcome.WORTHWHILE,
            ),
        )
    )
    output = tmp_path / "worthwhile-report.json"
    monkeypatch.setattr(cli, "load_config", lambda **_: AppConfig())

    exit_code = cli.main(["evaluate", "worthwhile", "--state", str(state), "--output", str(output)])

    assert exit_code == 0
    assert "observation-only" in capsys.readouterr().out
    report = json.loads(output.read_text(encoding="utf-8"))
    assert report["worthwhile_read_count"] == 1
    assert report["not_worthwhile_read_count"] == 0
    assert report["unlabeled_impression_count"] == 1
    assert report["eligible_for_activation"] is False


def test_state_commands_round_trip_validated_private_workflow_state(
    monkeypatch: MonkeyPatch, capsys: CaptureFixture[str], tmp_path: Path
) -> None:
    source = tmp_path / "state-source"
    source.mkdir()
    for name, value in {
        "arxiv-state.json": {"schema_version": 3, "candidates": []},
        "feedback-state.json": {"schema_version": 2, "events": []},
        "recommendation-history.json": {"schema_version": 1, "records": []},
    }.items():
        (source / name).write_text(json.dumps(value), encoding="utf-8")
    bundle = tmp_path / "state.enc.json"
    restored = tmp_path / "restored"
    monkeypatch.setattr(
        cli, "load_config", lambda **_: AppConfig(state_encryption_key="state-test-key-1234")
    )

    assert cli.main(["state", "encrypt", "--input-dir", str(source), "--output", str(bundle)]) == 0
    assert (
        cli.main(["state", "decrypt", "--input", str(bundle), "--output-dir", str(restored)]) == 0
    )

    assert "encrypted workflow state" in capsys.readouterr().out
    assert json.loads((restored / "feedback-state.json").read_text(encoding="utf-8")) == {
        "schema_version": 2,
        "events": [],
    }


def test_quality_profile_commands_inspect_aggregates_and_clear_approval(
    capsys: CaptureFixture[str], tmp_path: Path
) -> None:
    state = tmp_path / "quality-profile.json"
    store = QualityProfileStore(state)
    profile = build_quality_reference_profile(
        (
            ApprovedQualityExample(
                "2401.00001",
                True,
                ("evaluation",),
                ("baselines", "ablations"),
                ("held_out_evaluation",),
                ("practical_utility",),
                ("limited_scale",),
            ),
        ),
        (),
    )
    store.register(profile)
    store.approve(profile.version)

    assert (
        cli.main(
            [
                "quality-profile",
                "inspect",
                "--version",
                profile.version,
                "--state",
                str(state),
            ]
        )
        == 0
    )
    inspected = json.loads(capsys.readouterr().out)
    assert inspected["version"] == profile.version
    assert inspected["approved"] is True
    assert inspected["approved_example_count"] == 1
    assert inspected["policy_version"] == profile.policy_version
    assert inspected["research_problems"]
    assert inspected["motivations"]
    assert inspected["criterion_count"] == profile.criterion_count
    assert "2401.00001" not in json.dumps(inspected)

    assert cli.main(["quality-profile", "clear-approval", "--state", str(state)]) == 0
    assert "approval cleared" in capsys.readouterr().out
    assert store.approved() is None


def test_recommend_command_records_candidate_pool_degradation_in_manifest(
    monkeypatch: MonkeyPatch, tmp_path: Path
) -> None:
    profile = RemoteServingProfile(1, 9, (), (), (), ())
    source_checkpoint = datetime(2026, 8, 1, 12, 0, tzinfo=UTC)

    class _CandidateStore:
        def __init__(self, path: Path) -> None:
            self.path = path

        def candidates(self) -> tuple[object, ...]:
            return ()

        def retrieval_status(self) -> tuple[bool, str, datetime]:
            return True, "arXiv timeout", source_checkpoint

    class _History:
        def __init__(self, path: Path) -> None:
            self.path = path

        def excluded_ids(self, now: object, suppression_days: int) -> frozenset[str]:
            return frozenset()

        def prepare_success(self, result: object, path: Path, completed_at: object) -> None:
            return None

    def _run(*args: object, **kwargs: object) -> tuple[object, object]:
        return package_result(
            (),
            profile,
            datetime(2026, 8, 2, tzinfo=UTC),
            model="test",
            candidate_count=0,
            model_requests=0,
            cache_hits=0,
            estimated_tokens=0,
        )

    monkeypatch.setattr(cli, "load_config", lambda **_: AppConfig(deepseek_api_key="key"))
    monkeypatch.setattr(cli, "read_serving_profile", lambda path: profile)
    monkeypatch.setattr(cli, "ArxivStateStore", _CandidateStore)
    monkeypatch.setattr(cli, "RecommendationHistoryStore", _History)
    monkeypatch.setattr(cli, "run_recommendation", _run)
    manifest_path = tmp_path / "run-manifest.json"

    assert (
        cli.main(
            [
                "recommend",
                "run",
                "--profile",
                str(tmp_path / "profile.json"),
                "--candidate-state",
                str(tmp_path / "arxiv-state.json"),
                "--output",
                str(tmp_path / "recommendations.json"),
                "--history",
                str(tmp_path / "history.json"),
                "--prepared-history",
                str(tmp_path / "history.next.json"),
                "--manifest",
                str(manifest_path),
                "--weight-state",
                str(tmp_path / "weights.json"),
            ]
        )
        == 0
    )

    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    assert manifest["candidate_pool_degraded"] is True
    assert manifest["candidate_pool_degraded_reason"] == "arXiv timeout"
    assert manifest["candidate_pool_source_checkpoint"] == str(source_checkpoint)


def test_recommend_parser_accepts_explicit_v012_rollback_mode() -> None:
    args = cli.build_parser().parse_args(
        [
            "recommend",
            "run",
            "--profile",
            "profile.json",
            "--candidate-state",
            "candidates.json",
            "--ranking-mode",
            "v0.1.2",
        ]
    )

    assert args.ranking_mode == "v0.1.2"


def test_corpus_list_collections_command_exposes_only_local_mapping_fields(
    monkeypatch: MonkeyPatch, capsys: CaptureFixture[str]
) -> None:
    class _Store:
        def __init__(self, path: Path) -> None:
            self.path = path

        def collections(self) -> tuple[ZoteroCollection, ...]:
            return (ZoteroCollection("POSITIVE", 1, "Curated positives", None),)

    monkeypatch.setattr(cli, "load_config", lambda **_: AppConfig())
    monkeypatch.setattr(cli, "ZoteroStore", _Store)

    exit_code = cli.main(["corpus", "list-collections"])

    assert exit_code == 0
    assert capsys.readouterr().out == "POSITIVE\tCurated positives\n"


def test_evidence_enrich_keeps_the_cli_projection_public_and_bounded(
    monkeypatch: MonkeyPatch, capsys: CaptureFixture[str], tmp_path: Path
) -> None:
    class _Enricher:
        def __init__(self, client: object, cache: object) -> None:
            self.client = client
            self.cache = cache

        def enrich(
            self, candidates: tuple[object, ...], now: object, *, limit: int
        ) -> tuple[PublicPaperEvidence, ...]:
            assert candidates == ()
            assert limit == 1
            return ()

    monkeypatch.setattr(cli, "load_config", lambda **_: AppConfig())
    monkeypatch.setattr(cli, "OpenAlexEvidenceEnricher", _Enricher)

    exit_code = cli.main(
        [
            "evidence",
            "enrich",
            "--candidate-state",
            str(tmp_path / "missing.json"),
            "--limit",
            "1",
        ]
    )

    assert exit_code == 0
    assert capsys.readouterr().out == "public evidence enriched: 0 candidates, 0 context records\n"


def test_weight_activation_is_an_explicit_operator_action_without_a_metric_gate(
    monkeypatch: MonkeyPatch, capsys: CaptureFixture[str], tmp_path: Path
) -> None:
    state = tmp_path / "weights.json"
    monkeypatch.setattr(cli, "load_config", lambda **_: AppConfig())
    register = [
        "ranking",
        "register-weights",
        "--state",
        str(state),
        "--version",
        "coarse-test",
        "--interest",
        "0.5",
        "--recency",
        "0.1",
        "--feedback",
        "0.1",
        "--identity",
        "0.1",
        "--scientific-quality",
        "0.05",
        "--reproducibility",
        "0.03",
        "--context",
        "0.02",
        "--negative-feedback-cap",
        "0.2",
    ]

    assert cli.main(register) == 0
    assert (
        cli.main(
            [
                "ranking",
                "activate-weights",
                "--state",
                str(state),
                "--version",
                "coarse-test",
            ]
        )
        == 0
    )
    assert "ranking weight set activated: coarse-test" in capsys.readouterr().out
